from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.integrity import run_integrity_check
from src.integrity.audit_chain import _expected_chain_hash
from src.models.events import StoredEvent
from src.what_if.projector import collect_related_events, replay_projection_snapshot


@dataclass(frozen=True)
class RegulatoryPackageVerification:
    package_hash_valid: bool
    audit_chain_valid: bool
    latest_integrity_hash_matches: bool
    tamper_detected: bool
    ok: bool


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass_instance(value):
        return asdict(value)
    return value


def is_dataclass_instance(value: Any) -> bool:
    return hasattr(value, "__dataclass_fields__")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stored_event_to_dict(event: StoredEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


def _expected_chain_hash_for_dicts(previous_hash: str | None, events: list[dict[str, Any]]) -> str:
    return _expected_chain_hash(
        previous_hash,
        [StoredEvent(**event) for event in events],
    )


def _package_hash(package_without_hash: dict[str, Any]) -> str:
    return _hash_text(_canonical_json(package_without_hash))


def _narrative_sentence(event: StoredEvent) -> str:
    payload = event.payload
    event_type = event.event_type
    timestamp = event.recorded_at.isoformat()
    if event_type == "ApplicationSubmitted":
        return f"{timestamp}: Application {payload['application_id']} was submitted for ${payload['requested_amount_usd']}."
    if event_type == "CreditAnalysisCompleted":
        decision = payload.get("decision", {})
        return (
            f"{timestamp}: Credit analysis completed with risk tier {decision.get('risk_tier')} "
            f"and confidence {decision.get('confidence')}."
        )
    if event_type == "FraudScreeningCompleted":
        return f"{timestamp}: Fraud screening completed with score {payload.get('fraud_score')}."
    if event_type == "ComplianceCheckCompleted":
        return f"{timestamp}: Compliance review finished with verdict {payload.get('overall_verdict')}."
    if event_type == "DecisionGenerated":
        return (
            f"{timestamp}: The orchestrator recommended {payload.get('recommendation')} "
            f"with confidence {payload.get('confidence')}."
        )
    if event_type == "HumanReviewCompleted":
        decision = payload.get("final_decision")
        override = "with override" if payload.get("override") else "without override"
        return f"{timestamp}: Human review ended {override}; final decision was {decision}."
    if event_type == "ApplicationApproved":
        return f"{timestamp}: The application was approved for ${payload.get('approved_amount_usd')}."
    if event_type == "ApplicationDeclined":
        return f"{timestamp}: The application was declined."
    if event_type == "AgentSessionStarted":
        return f"{timestamp}: Agent session {payload.get('session_id')} started for {payload.get('agent_type')}."
    return f"{timestamp}: {event_type} was recorded."


def _extract_agent_model_metadata(events: list[StoredEvent], application_id: str) -> list[dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    outputs: list[dict[str, Any]] = []

    for event in events:
        if event.event_type == "AgentSessionStarted" and event.payload.get("application_id") == application_id:
            sessions[str(event.payload["session_id"])] = {
                "session_id": str(event.payload["session_id"]),
                "agent_type": str(event.payload["agent_type"]),
                "agent_id": str(event.payload["agent_id"]),
                "model_version": str(event.payload["model_version"]),
                "context_source": event.payload.get("context_source"),
            }
        elif event.event_type in {
            "CreditAnalysisCompleted",
            "FraudScreeningCompleted",
            "ComplianceCheckCompleted",
            "DecisionGenerated",
        }:
            outputs.append(event.model_dump(mode="json"))

    metadata: list[dict[str, Any]] = []
    for output in outputs:
        session_id = output["payload"].get("session_id") or output["payload"].get("orchestrator_session_id")
        session_meta = sessions.get(str(session_id), {"session_id": session_id})
        row = {
            **session_meta,
            "event_type": output["event_type"],
            "input_data_hash": output["payload"].get("input_data_hash"),
            "confidence": output["payload"].get("confidence")
            or output["payload"].get("decision", {}).get("confidence"),
            "recorded_at": output["recorded_at"],
        }
        if output["event_type"] == "DecisionGenerated":
            row["model_versions"] = output["payload"].get("model_versions", {})
        metadata.append(row)
    return metadata


def _sort_event_dicts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda item: (
            int(item.get("global_position", -1) or -1),
            int(item.get("stream_position", -1) or -1),
            str(item.get("event_id", "")),
        ),
    )


async def generate_regulatory_package(
    store,
    application_id: str,
    examination_date: datetime,
) -> dict[str, Any]:
    integrity_result = await run_integrity_check(store, "loan", application_id)

    source_events = await collect_related_events(store, application_id, include_audit=False, upcasted=False)
    audit_events = await collect_related_events(store, application_id, include_audit=True, upcasted=False)
    audit_only = [
        event
        for event in audit_events
        if event.stream_id in {f"audit-loan-{application_id}", f"audit-application-{application_id}"}
    ]

    projection_events = [event for event in source_events if event.recorded_at <= examination_date]
    projection_states = await replay_projection_snapshot(projection_events, application_id)

    package = {
        "package_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "application_id": application_id,
        "examination_date": examination_date.isoformat(),
        "event_stream": [_stored_event_to_dict(event) for event in source_events],
        "audit_stream": [_stored_event_to_dict(event) for event in audit_only],
        "projection_states_as_of": projection_states,
        "integrity_verification": {
            "source_stream_id": integrity_result.source_stream_id,
            "audit_stream_id": integrity_result.audit_stream_id,
            "events_verified": integrity_result.events_verified,
            "chain_valid": integrity_result.chain_valid,
            "tamper_detected": integrity_result.tamper_detected,
            "latest_integrity_hash": integrity_result.integrity_hash,
            "previous_hash": integrity_result.previous_hash,
        },
        "narrative": [_narrative_sentence(event) for event in source_events],
        "agent_model_metadata": _extract_agent_model_metadata(source_events, application_id),
    }
    package["package_hash"] = _package_hash(package)
    return package


def verify_regulatory_package(package: dict[str, Any]) -> RegulatoryPackageVerification:
    candidate = dict(package)
    package_hash = candidate.pop("package_hash", "")
    package_hash_valid = _package_hash(candidate) == package_hash

    source_events = _sort_event_dicts(list(candidate.get("event_stream", [])))
    audit_events = _sort_event_dicts(list(candidate.get("audit_stream", [])))
    audit_chain_valid = True
    latest_integrity_hash_matches = False
    tamper_detected = False
    embedded_integrity = candidate.get("integrity_verification", {})
    scoped_stream_id = embedded_integrity.get("source_stream_id")
    scoped_source_events = [
        event for event in source_events if not scoped_stream_id or event.get("stream_id") == scoped_stream_id
    ]

    previous_hash: str | None = None
    for audit_event in audit_events:
        payload = audit_event.get("payload", {})
        count = int(payload.get("events_verified_count", 0))
        expected = _expected_chain_hash_for_dicts(previous_hash, scoped_source_events[:count])
        if expected != payload.get("integrity_hash"):
            audit_chain_valid = False
        if payload.get("tamper_detected"):
            tamper_detected = True
        previous_hash = payload.get("integrity_hash")

    latest_integrity_hash_matches = (
        previous_hash == embedded_integrity.get("latest_integrity_hash")
        if audit_events
        else embedded_integrity.get("latest_integrity_hash") is None
    )
    ok = package_hash_valid and audit_chain_valid and latest_integrity_hash_matches and not tamper_detected
    return RegulatoryPackageVerification(
        package_hash_valid=package_hash_valid,
        audit_chain_valid=audit_chain_valid,
        latest_integrity_hash_matches=latest_integrity_hash_matches,
        tamper_detected=tamper_detected,
        ok=ok,
    )
