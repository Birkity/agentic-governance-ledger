from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.event_store import InMemoryEventStore
from src.models.events import BaseEvent, StoredEvent
from src.projections import (
    AgentPerformanceProjection,
    ApplicationSummaryProjection,
    ComplianceAuditProjection,
    ProjectionDaemon,
)
from src.models.events import AGENT_SESSION_ANCHOR_EVENT_TYPES


STAGE_ORDER: dict[str, int] = {
    "ApplicationSubmitted": 0,
    "DocumentUploadRequested": 1,
    "DocumentUploaded": 1,
    "PackageReadyForAnalysis": 2,
    "CreditAnalysisRequested": 3,
    "CreditAnalysisCompleted": 4,
    "FraudScreeningRequested": 5,
    "FraudScreeningCompleted": 6,
    "ComplianceCheckRequested": 7,
    "ComplianceCheckInitiated": 7,
    "ComplianceRulePassed": 8,
    "ComplianceRuleFailed": 8,
    "ComplianceRuleNoted": 8,
    "ComplianceCheckCompleted": 9,
    "DecisionRequested": 10,
    "DecisionGenerated": 11,
    "HumanReviewRequested": 12,
    "HumanReviewCompleted": 13,
    "ApplicationApproved": 14,
    "ApplicationDeclined": 14,
}


@dataclass(frozen=True)
class WhatIfOutcome:
    application_summary: dict[str, Any] | None
    compliance_audit: dict[str, Any] | None
    agent_performance: list[dict[str, Any]]
    decision_recommendation: str | None
    projected_recommendation: str | None
    projected_approved_amount_usd: str | None
    reasoning: str | None


@dataclass(frozen=True)
class WhatIfResult:
    application_id: str
    branch_event_type: str
    branch_stream_id: str
    branch_event_id: str
    total_real_events: int
    total_counterfactual_events: int
    injected_events: list[dict[str, Any]]
    divergence_events: list[dict[str, Any]]
    real_outcome: WhatIfOutcome
    counterfactual_outcome: WhatIfOutcome


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _normalize_counterfactual_event(
    counterfactual_event: BaseEvent | dict[str, Any],
    *,
    original_stream_id: str,
    original_metadata: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(counterfactual_event, BaseEvent):
        normalized = counterfactual_event.to_store_dict()
        normalized["recorded_at"] = counterfactual_event.recorded_at
    else:
        normalized = dict(counterfactual_event)
    normalized.setdefault("metadata", dict(original_metadata))
    normalized["stream_id"] = original_stream_id
    return normalized


def _reference_tokens(event: StoredEvent) -> set[str]:
    refs: set[str] = set()
    causation_id = event.metadata.get("causation_id")
    if causation_id:
        refs.add(str(causation_id))

    for key in ("triggered_by_event_id", "decision_event_id", "recovered_from_session_id"):
        value = event.payload.get(key)
        if value:
            refs.add(str(value))

    for item in event.payload.get("events_written", []):
        stream_id = item.get("stream_id")
        stream_position = item.get("stream_position")
        if stream_id is not None and stream_position is not None:
            refs.add(f"{stream_id}:{stream_position}")

    return refs


def _event_identifier_tokens(event: StoredEvent) -> set[str]:
    return {
        str(event.event_id),
        f"{event.stream_id}:{event.stream_position}",
    }


def _is_related_event(event: StoredEvent, application_id: str, *, include_audit: bool = False) -> bool:
    payload = event.payload
    if str(payload.get("application_id", "")) == application_id:
        return True
    if event.stream_id in {
        f"loan-{application_id}",
        f"docpkg-{application_id}",
        f"credit-{application_id}",
        f"fraud-{application_id}",
        f"compliance-{application_id}",
    }:
        return True
    if include_audit and event.stream_id in {
        f"audit-loan-{application_id}",
        f"audit-application-{application_id}",
    }:
        return True
    return False


async def collect_related_events(
    store,
    application_id: str,
    *,
    include_audit: bool = False,
    upcasted: bool = True,
) -> list[StoredEvent]:
    events: list[StoredEvent] = []
    async for event in store.load_all(from_position=0, apply_upcasters=upcasted):
        if _is_related_event(event, application_id, include_audit=include_audit):
            events.append(event)
    return events


async def replay_projection_snapshot(events: list[StoredEvent], application_id: str) -> dict[str, Any]:
    temp_store = InMemoryEventStore()
    for event in events:
        expected_version = await temp_store.stream_version(event.stream_id)
        await temp_store.append(
            event.stream_id,
            [
                {
                    "event_id": str(event.event_id),
                    "event_type": event.event_type,
                    "event_version": event.event_version,
                    "payload": event.payload,
                    "metadata": event.metadata,
                    "recorded_at": event.recorded_at,
                }
            ],
            expected_version=expected_version,
        )

    summary = ApplicationSummaryProjection(temp_store)
    performance = AgentPerformanceProjection(temp_store)
    compliance = ComplianceAuditProjection(temp_store)
    daemon = ProjectionDaemon(temp_store, [summary, performance, compliance], batch_size=500)
    await daemon.run_until_caught_up(max_cycles=50)

    application_summary = await summary.get(application_id)
    compliance_state = await compliance.get_current_compliance(application_id)
    related_agent_ids = {
        str(event.payload["agent_id"])
        for event in events
        if event.event_type in AGENT_SESSION_ANCHOR_EVENT_TYPES and event.payload.get("application_id") == application_id
    }
    performance_rows = [
        asdict(record)
        for record in await performance.list_all()
        if record.agent_id in related_agent_ids
    ]
    return {
        "application_summary": asdict(application_summary) if application_summary else None,
        "compliance_audit": asdict(compliance_state) if compliance_state else None,
        "agent_performance": performance_rows,
    }


def _latest_event(events: list[StoredEvent], event_type: str) -> StoredEvent | None:
    for event in reversed(events):
        if event.event_type == event_type:
            return event
    return None


def _derive_counterfactual_recommendation(
    snapshot: dict[str, Any],
    events: list[StoredEvent],
) -> tuple[str | None, str | None, str | None]:
    summary = snapshot.get("application_summary") or {}
    compliance = snapshot.get("compliance_audit") or {}
    latest_credit = _latest_event(events, "CreditAnalysisCompleted")
    latest_fraud = _latest_event(events, "FraudScreeningCompleted")

    if not latest_credit:
        return None, None, "No credit analysis exists in the counterfactual branch."

    decision = latest_credit.payload.get("decision", {})
    risk_tier = str(decision.get("risk_tier", "")).upper()
    confidence = float(decision.get("confidence", 0.0))
    recommended_limit = _to_decimal(decision.get("recommended_limit_usd"))
    requested_amount = _to_decimal(summary.get("requested_amount_usd"))
    fraud_score = float(latest_fraud.payload.get("fraud_score", 0.0)) if latest_fraud else 0.0
    verdict = str(compliance.get("overall_verdict") or "")

    if verdict == "BLOCKED":
        return "DECLINE", None, "Compliance remains blocked in the counterfactual branch."
    if risk_tier == "HIGH":
        return "DECLINE", None, "High risk credit outcome still violates the approval policy."
    if confidence < 0.60:
        return "REFER", None, "Confidence is below the floor, so the case must be referred."
    if fraud_score >= 0.65:
        return "DECLINE", None, "Fraud score remains too high for approval."
    if requested_amount is None or recommended_limit is None:
        return "REFER", None, "Requested amount or recommended limit is unavailable."
    if recommended_limit < requested_amount:
        approved_amount = str(recommended_limit.quantize(Decimal("1.00")))
        return "REFER", approved_amount, "The counterfactual credit limit does not fully cover the request."

    approved_amount = str(requested_amount.quantize(Decimal("1.00")))
    return "APPROVE", approved_amount, "Risk tier, confidence, compliance, and fraud signals support approval."


async def run_what_if(
    store,
    application_id: str,
    branch_at_event_type: str,
    counterfactual_events: list[BaseEvent | dict[str, Any]],
) -> WhatIfResult:
    real_events = await collect_related_events(store, application_id, include_audit=False, upcasted=True)
    branch_index = next(
        (index for index, event in enumerate(real_events) if event.event_type == branch_at_event_type),
        None,
    )
    if branch_index is None:
        raise ValueError(f"No event {branch_at_event_type!r} found for application {application_id}")

    branch_event = real_events[branch_index]
    branch_stage = STAGE_ORDER.get(branch_event.event_type, 0)

    injected = [
        _normalize_counterfactual_event(
            item,
            original_stream_id=branch_event.stream_id,
            original_metadata=branch_event.metadata,
        )
        for item in counterfactual_events
    ]

    filtered_events: list[StoredEvent] = list(real_events[:branch_index])
    divergence_events: list[dict[str, Any]] = [
        {
            "kind": "replaced",
            "event_type": branch_event.event_type,
            "stream_id": branch_event.stream_id,
            "stream_position": branch_event.stream_position,
            "event_id": str(branch_event.event_id),
        }
    ]
    dependency_tokens = _event_identifier_tokens(branch_event)

    for event in real_events[branch_index + 1 :]:
        references = _reference_tokens(event)
        is_dependent = bool(references & dependency_tokens)
        if event.stream_id.startswith(f"loan-{application_id}") and STAGE_ORDER.get(event.event_type, branch_stage) > branch_stage:
            is_dependent = True
        if event.event_type in {"AgentOutputWritten", "AgentSessionCompleted"} and event.payload.get("application_id") == application_id:
            is_dependent = True

        if is_dependent:
            divergence_events.append(
                {
                    "kind": "skipped_dependent",
                    "event_type": event.event_type,
                    "stream_id": event.stream_id,
                    "stream_position": event.stream_position,
                    "event_id": str(event.event_id),
                }
            )
            dependency_tokens.update(_event_identifier_tokens(event))
            continue

        filtered_events.append(event)

    temp_store = InMemoryEventStore()
    for event in filtered_events:
        expected_version = await temp_store.stream_version(event.stream_id)
        await temp_store.append(
            event.stream_id,
            [
                {
                    "event_id": str(event.event_id),
                    "event_type": event.event_type,
                    "event_version": event.event_version,
                    "payload": event.payload,
                    "metadata": event.metadata,
                    "recorded_at": event.recorded_at,
                }
            ],
            expected_version=expected_version,
        )

    for injected_event in injected:
        stream_id = str(injected_event["stream_id"])
        expected_version = await temp_store.stream_version(stream_id)
        await temp_store.append(
            stream_id,
            [
                {
                    "event_type": injected_event["event_type"],
                    "event_version": injected_event.get("event_version", 1),
                    "payload": injected_event.get("payload", {}),
                    "metadata": injected_event.get("metadata", {}),
                    "recorded_at": injected_event.get("recorded_at"),
                }
            ],
            expected_version=expected_version,
        )

    counterfactual_events_replayed = await collect_related_events(
        temp_store,
        application_id,
        include_audit=False,
        upcasted=True,
    )
    real_snapshot = await replay_projection_snapshot(real_events, application_id)
    counterfactual_snapshot = await replay_projection_snapshot(counterfactual_events_replayed, application_id)
    projected_recommendation, projected_amount, reasoning = _derive_counterfactual_recommendation(
        counterfactual_snapshot,
        counterfactual_events_replayed,
    )

    real_decision = _latest_event(real_events, "DecisionGenerated")
    real_outcome = WhatIfOutcome(
        application_summary=real_snapshot["application_summary"],
        compliance_audit=real_snapshot["compliance_audit"],
        agent_performance=real_snapshot["agent_performance"],
        decision_recommendation=real_decision.payload.get("recommendation") if real_decision else None,
        projected_recommendation=real_decision.payload.get("recommendation") if real_decision else None,
        projected_approved_amount_usd=str(real_decision.payload.get("approved_amount_usd"))
        if real_decision and real_decision.payload.get("approved_amount_usd") is not None
        else None,
        reasoning="Observed recommendation from the real timeline.",
    )
    counterfactual_outcome = WhatIfOutcome(
        application_summary=counterfactual_snapshot["application_summary"],
        compliance_audit=counterfactual_snapshot["compliance_audit"],
        agent_performance=counterfactual_snapshot["agent_performance"],
        decision_recommendation=None,
        projected_recommendation=projected_recommendation,
        projected_approved_amount_usd=projected_amount,
        reasoning=reasoning,
    )

    return WhatIfResult(
        application_id=application_id,
        branch_event_type=branch_at_event_type,
        branch_stream_id=branch_event.stream_id,
        branch_event_id=str(branch_event.event_id),
        total_real_events=len(real_events),
        total_counterfactual_events=len(counterfactual_events_replayed),
        injected_events=injected,
        divergence_events=divergence_events,
        real_outcome=real_outcome,
        counterfactual_outcome=counterfactual_outcome,
    )
