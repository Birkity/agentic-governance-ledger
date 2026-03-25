from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.models.events import AGENT_SESSION_ANCHOR_EVENT_TYPES
from src.upcasting.registry import UpcasterRegistry


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime(2025, 1, 1, tzinfo=timezone.utc)


def infer_credit_model_version(recorded_at: Any) -> str:
    """Map a stored timestamp to the deployed credit model active at that time."""
    timestamp = _as_utc(recorded_at)
    if timestamp < datetime(2026, 1, 1, tzinfo=timezone.utc):
        return "credit-v0-legacy"
    if timestamp < datetime(2026, 3, 1, tzinfo=timezone.utc):
        return "credit-v1"
    return "credit-v2"


def infer_regulatory_basis(recorded_at: Any) -> list[str]:
    """Infer the active rule-set family from the event's timestamp window."""
    timestamp = _as_utc(recorded_at)
    if timestamp < datetime(2026, 1, 1, tzinfo=timezone.utc):
        return ["APEX-CREDIT-POLICY-2025.4", "US-GAAP-BASELINE-2025"]
    if timestamp < datetime(2026, 3, 1, tzinfo=timezone.utc):
        return ["APEX-CREDIT-POLICY-2026.1", "US-GAAP-BASELINE-2026.1"]
    return ["APEX-CREDIT-POLICY-2026.2", "US-GAAP-BASELINE-2026.2"]


def _normalize_session_stream_id(session_ref: str) -> str:
    return session_ref if session_ref.startswith("agent-") else f"agent-{session_ref}"


async def infer_model_versions_from_sessions(
    registry: UpcasterRegistry,
    contributing_sessions: list[str],
) -> dict[str, str]:
    if registry.store is None:
        return {}

    model_versions: dict[str, str] = {}
    for session_ref in contributing_sessions:
        session_stream_id = _normalize_session_stream_id(session_ref)
        session_events = await registry.store.load_stream(session_stream_id)
        anchor = next((event for event in session_events if event.event_type in AGENT_SESSION_ANCHOR_EVENT_TYPES), None)
        if anchor is None:
            continue
        agent_type = str(anchor.payload.get("agent_type", "unknown"))
        model_version = anchor.payload.get("model_version")
        if model_version:
            model_versions[agent_type] = str(model_version)
    return model_versions


def build_default_registry() -> UpcasterRegistry:
    registry = UpcasterRegistry()
    registry.set_current_version("CreditAnalysisCompleted", 2)
    registry.set_current_version("DecisionGenerated", 2)

    @registry.register("CreditAnalysisCompleted", 1, to_version=2)
    def upcast_credit_analysis_completed_v1_to_v2(
        event: dict[str, Any],
        _: UpcasterRegistry,
    ) -> dict[str, Any]:
        payload = dict(event.get("payload", {}))
        if not payload.get("model_version"):
            payload["model_version"] = infer_credit_model_version(event.get("recorded_at"))
        if not payload.get("regulatory_basis"):
            payload["regulatory_basis"] = infer_regulatory_basis(event.get("recorded_at"))
        migrated = dict(event)
        migrated["payload"] = payload
        return migrated

    @registry.register("DecisionGenerated", 1, to_version=2)
    async def upcast_decision_generated_v1_to_v2(
        event: dict[str, Any],
        active_registry: UpcasterRegistry,
    ) -> dict[str, Any]:
        payload = dict(event.get("payload", {}))
        contributing_sessions = list(payload.get("contributing_sessions", []))
        if not payload.get("model_versions"):
            payload["model_versions"] = await infer_model_versions_from_sessions(
                active_registry,
                contributing_sessions,
            )
        migrated = dict(event)
        migrated["payload"] = payload
        return migrated

    return registry
