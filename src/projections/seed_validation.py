from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.event_store import InMemoryEventStore


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


@dataclass
class SeedProjectionValidationResult:
    total_seed_events: int
    total_applications: int
    application_summary_records: int
    compliance_current_records: int
    agent_performance_rows: int
    state_counts: dict[str, int]
    compliance_verdict_counts: dict[str, int]
    agent_type_counts: dict[str, int]
    mismatches: list[str]

    @property
    def ok(self) -> bool:
        return not self.mismatches


async def load_seed_events_into_store(
    store: InMemoryEventStore,
    seed_path: str | Path = "data/seed_events.jsonl",
) -> list[dict[str, Any]]:
    path = Path(seed_path)
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            expected_version = await store.stream_version(event["stream_id"])
            await store.append(event["stream_id"], [event], expected_version=expected_version)
            events.append(event)
    return events


def _build_application_summary_expectations(seed_events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}

    for raw_event in seed_events:
        payload = raw_event.get("payload", {})
        application_id = payload.get("application_id")
        if not application_id:
            continue

        record = expected.setdefault(
            application_id,
            {
                "state": "NEW",
                "applicant_id": None,
                "requested_amount_usd": None,
                "approved_amount_usd": None,
                "risk_tier": None,
                "fraud_score": None,
                "compliance_status": None,
                "decision": None,
                "agent_sessions_completed": set(),
                "human_reviewer_id": None,
            },
        )

        event_type = raw_event["event_type"]
        if event_type == "ApplicationSubmitted":
            record["state"] = "SUBMITTED"
            record["applicant_id"] = payload.get("applicant_id")
            record["requested_amount_usd"] = _to_decimal(payload.get("requested_amount_usd"))
        elif event_type == "DocumentUploadRequested":
            record["state"] = "DOCUMENTS_PENDING"
        elif event_type == "DocumentUploaded":
            record["state"] = "DOCUMENTS_UPLOADED"
        elif event_type == "PackageReadyForAnalysis":
            record["state"] = "DOCUMENTS_PROCESSED"
        elif event_type == "CreditAnalysisRequested":
            record["state"] = "AWAITING_ANALYSIS"
        elif event_type == "CreditAnalysisCompleted":
            decision = payload.get("decision", {})
            record["risk_tier"] = decision.get("risk_tier")
        elif event_type == "FraudScreeningRequested":
            record["state"] = "ANALYSIS_COMPLETE"
        elif event_type == "FraudScreeningCompleted":
            record["fraud_score"] = _to_float(payload.get("fraud_score"))
        elif event_type == "ComplianceCheckRequested":
            record["state"] = "COMPLIANCE_REVIEW"
        elif event_type == "ComplianceCheckCompleted":
            record["compliance_status"] = payload.get("overall_verdict")
        elif event_type == "DecisionRequested":
            record["state"] = "PENDING_DECISION"
        elif event_type == "DecisionGenerated":
            recommendation = payload.get("recommendation")
            record["decision"] = recommendation
            approved_amount = payload.get("approved_amount_usd")
            if approved_amount is not None:
                record["approved_amount_usd"] = _to_decimal(approved_amount)
            if recommendation == "APPROVE":
                record["state"] = "APPROVED_PENDING_HUMAN"
            elif recommendation == "DECLINE":
                record["state"] = "DECLINED_PENDING_HUMAN"
            else:
                record["state"] = "PENDING_HUMAN_REVIEW"
        elif event_type == "HumanReviewRequested":
            record["state"] = "PENDING_HUMAN_REVIEW"
        elif event_type == "HumanReviewCompleted":
            record["human_reviewer_id"] = payload.get("reviewer_id")
            record["decision"] = payload.get("final_decision")
        elif event_type == "ApplicationApproved":
            record["state"] = "FINAL_APPROVED"
            record["decision"] = "APPROVE"
            approved_amount = payload.get("approved_amount_usd")
            if approved_amount is not None:
                record["approved_amount_usd"] = _to_decimal(approved_amount)
        elif event_type == "ApplicationDeclined":
            decline_reasons = payload.get("decline_reasons", [])
            is_compliance_decline = (
                record.get("compliance_status") == "BLOCKED"
                or any("Compliance hard block" in str(reason) for reason in decline_reasons)
                or "COMPLIANCE_BLOCK" in payload.get("adverse_action_codes", [])
            )
            record["state"] = "DECLINED_COMPLIANCE" if is_compliance_decline else "FINAL_DECLINED"
            record["decision"] = "DECLINE"
        elif event_type == "AgentSessionCompleted":
            session_id = payload.get("session_id")
            if session_id:
                record["agent_sessions_completed"].add(str(session_id))

    return expected


def _build_compliance_expectations(seed_events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    handled_event_types = {
        "ComplianceCheckInitiated",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "ComplianceRuleNoted",
        "ComplianceCheckCompleted",
    }

    for raw_event in seed_events:
        payload = raw_event.get("payload", {})
        application_id = payload.get("application_id")
        if not application_id or raw_event["event_type"] not in handled_event_types:
            continue

        record = expected.setdefault(
            application_id,
            {
                "session_id": None,
                "regulation_set_version": None,
                "required_rules": set(),
                "passed_rule_ids": [],
                "failed_rule_ids": [],
                "noted_rule_ids": [],
                "hard_block_rules": set(),
                "completed": False,
                "overall_verdict": None,
            },
        )

        event_type = raw_event["event_type"]
        if event_type == "ComplianceCheckInitiated":
            record["session_id"] = payload.get("session_id")
            record["regulation_set_version"] = payload.get("regulation_set_version")
            record["required_rules"] = set(payload.get("rules_to_evaluate", []))
        elif event_type == "ComplianceRulePassed":
            record["passed_rule_ids"].append(str(payload["rule_id"]))
        elif event_type == "ComplianceRuleFailed":
            rule_id = str(payload["rule_id"])
            record["failed_rule_ids"].append(rule_id)
            if bool(payload.get("is_hard_block")):
                record["hard_block_rules"].add(rule_id)
        elif event_type == "ComplianceRuleNoted":
            record["noted_rule_ids"].append(str(payload["rule_id"]))
        elif event_type == "ComplianceCheckCompleted":
            record["completed"] = True
            record["overall_verdict"] = payload.get("overall_verdict")
            if bool(payload.get("has_hard_block")) and not record["hard_block_rules"]:
                record["hard_block_rules"].update(record["failed_rule_ids"])

    return expected


def _build_agent_performance_expectations(seed_events: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    sessions: dict[str, tuple[str, str, str]] = {}
    application_decision_keys: dict[str, tuple[str, str, str]] = {}
    metrics: dict[tuple[str, str, str], dict[str, Any]] = {}

    def ensure(key: tuple[str, str, str]) -> dict[str, Any]:
        record = metrics.get(key)
        if record is None:
            agent_type, agent_id, model_version = key
            record = {
                "agent_type": agent_type,
                "agent_id": agent_id,
                "model_version": model_version,
                "total_sessions": 0,
                "analyses_completed": 0,
                "decisions_generated": 0,
                "approve_count": 0,
                "decline_count": 0,
                "refer_count": 0,
                "human_override_count": 0,
                "confidence_sum": 0.0,
                "confidence_samples": 0,
                "duration_sum": 0.0,
                "duration_samples": 0,
            }
            metrics[key] = record
        return record

    for raw_event in seed_events:
        event_type = raw_event["event_type"]
        payload = raw_event.get("payload", {})

        if event_type == "AgentSessionStarted":
            key = (
                str(payload["agent_type"]),
                str(payload["agent_id"]),
                str(payload["model_version"]),
            )
            sessions[str(payload["session_id"])] = key
            ensure(key)["total_sessions"] += 1
            continue

        if event_type == "CreditAnalysisCompleted":
            key = sessions.get(str(payload.get("session_id")))
            if key is None:
                continue
            record = ensure(key)
            record["analyses_completed"] += 1
            confidence = payload.get("decision", {}).get("confidence")
            if confidence is not None:
                record["confidence_sum"] += float(confidence)
                record["confidence_samples"] += 1
            duration = payload.get("analysis_duration_ms")
            if duration is not None:
                record["duration_sum"] += float(duration)
                record["duration_samples"] += 1
            continue

        if event_type in {"FraudScreeningCompleted", "ComplianceCheckCompleted"}:
            key = sessions.get(str(payload.get("session_id")))
            if key is None:
                continue
            ensure(key)["analyses_completed"] += 1
            continue

        if event_type == "DecisionGenerated":
            session_id = str(payload.get("session_id") or payload.get("orchestrator_session_id") or "")
            key = sessions.get(session_id)
            if key is None:
                continue
            record = ensure(key)
            record["decisions_generated"] += 1
            confidence = payload.get("confidence")
            if confidence is not None:
                record["confidence_sum"] += float(confidence)
                record["confidence_samples"] += 1
            recommendation = str(payload.get("recommendation", "")).upper()
            if recommendation == "APPROVE":
                record["approve_count"] += 1
            elif recommendation == "DECLINE":
                record["decline_count"] += 1
            elif recommendation == "REFER":
                record["refer_count"] += 1
            application_id = payload.get("application_id")
            if application_id:
                application_decision_keys[str(application_id)] = key
            continue

        if event_type == "HumanReviewCompleted" and bool(payload.get("override")):
            key = application_decision_keys.get(str(payload.get("application_id")))
            if key is not None:
                ensure(key)["human_override_count"] += 1

    for record in metrics.values():
        confidence_samples = record["confidence_samples"]
        duration_samples = record["duration_samples"]
        decisions = record["decisions_generated"]
        record["avg_confidence_score"] = (
            record["confidence_sum"] / confidence_samples if confidence_samples else None
        )
        record["avg_duration_ms"] = record["duration_sum"] / duration_samples if duration_samples else None
        record["approve_rate"] = record["approve_count"] / decisions if decisions else 0.0
        record["decline_rate"] = record["decline_count"] / decisions if decisions else 0.0
        record["refer_rate"] = record["refer_count"] / decisions if decisions else 0.0
        record["human_override_rate"] = record["human_override_count"] / decisions if decisions else 0.0

    return metrics


async def validate_seed_projection_rebuild(
    summary_projection,
    agent_performance_projection,
    compliance_projection,
    seed_events: list[dict[str, Any]],
) -> SeedProjectionValidationResult:
    mismatches: list[str] = []

    expected_summary = _build_application_summary_expectations(seed_events)
    expected_compliance = _build_compliance_expectations(seed_events)
    expected_agent_performance = _build_agent_performance_expectations(seed_events)

    actual_summary_records = await summary_projection.list_all()
    actual_summary = {record.application_id: record for record in actual_summary_records}

    if set(actual_summary) != set(expected_summary):
        missing = sorted(set(expected_summary) - set(actual_summary))
        extra = sorted(set(actual_summary) - set(expected_summary))
        if missing:
            mismatches.append(f"ApplicationSummary missing applications: {missing}")
        if extra:
            mismatches.append(f"ApplicationSummary unexpected applications: {extra}")

    for application_id, expected in expected_summary.items():
        actual = actual_summary.get(application_id)
        if actual is None:
            continue

        comparisons = {
            "state": actual.state,
            "applicant_id": actual.applicant_id,
            "requested_amount_usd": _to_decimal(actual.requested_amount_usd),
            "approved_amount_usd": _to_decimal(actual.approved_amount_usd),
            "risk_tier": actual.risk_tier,
            "fraud_score": _round(actual.fraud_score),
            "compliance_status": actual.compliance_status,
            "decision": actual.decision,
            "human_reviewer_id": actual.human_reviewer_id,
        }
        expected_values = {
            "state": expected["state"],
            "applicant_id": expected["applicant_id"],
            "requested_amount_usd": expected["requested_amount_usd"],
            "approved_amount_usd": expected["approved_amount_usd"],
            "risk_tier": expected["risk_tier"],
            "fraud_score": _round(expected["fraud_score"]),
            "compliance_status": expected["compliance_status"],
            "decision": expected["decision"],
            "human_reviewer_id": expected["human_reviewer_id"],
        }

        for field_name, actual_value in comparisons.items():
            expected_value = expected_values[field_name]
            if actual_value != expected_value:
                mismatches.append(
                    f"ApplicationSummary[{application_id}] field {field_name}: "
                    f"expected {expected_value!r}, got {actual_value!r}"
                )

        if set(actual.agent_sessions_completed) != expected["agent_sessions_completed"]:
            mismatches.append(
                f"ApplicationSummary[{application_id}] agent_sessions_completed mismatch: "
                f"expected {sorted(expected['agent_sessions_completed'])}, "
                f"got {sorted(actual.agent_sessions_completed)}"
            )

    actual_compliance_records = await compliance_projection.list_current()
    actual_compliance = {record.application_id: record for record in actual_compliance_records}

    if set(actual_compliance) != set(expected_compliance):
        missing = sorted(set(expected_compliance) - set(actual_compliance))
        extra = sorted(set(actual_compliance) - set(expected_compliance))
        if missing:
            mismatches.append(f"ComplianceAudit missing applications: {missing}")
        if extra:
            mismatches.append(f"ComplianceAudit unexpected applications: {extra}")

    for application_id, expected in expected_compliance.items():
        actual = actual_compliance.get(application_id)
        if actual is None:
            continue

        if actual.session_id != expected["session_id"]:
            mismatches.append(
                f"ComplianceAudit[{application_id}] session_id: "
                f"expected {expected['session_id']!r}, got {actual.session_id!r}"
            )
        if actual.regulation_set_version != expected["regulation_set_version"]:
            mismatches.append(
                f"ComplianceAudit[{application_id}] regulation_set_version: "
                f"expected {expected['regulation_set_version']!r}, got {actual.regulation_set_version!r}"
            )
        if set(actual.required_rules) != expected["required_rules"]:
            mismatches.append(
                f"ComplianceAudit[{application_id}] required_rules mismatch"
            )
        if len(actual.passed_rules) != len(expected["passed_rule_ids"]):
            mismatches.append(
                f"ComplianceAudit[{application_id}] passed_rules count: "
                f"expected {len(expected['passed_rule_ids'])}, got {len(actual.passed_rules)}"
            )
        if len(actual.failed_rules) != len(expected["failed_rule_ids"]):
            mismatches.append(
                f"ComplianceAudit[{application_id}] failed_rules count: "
                f"expected {len(expected['failed_rule_ids'])}, got {len(actual.failed_rules)}"
            )
        if len(actual.noted_rules) != len(expected["noted_rule_ids"]):
            mismatches.append(
                f"ComplianceAudit[{application_id}] noted_rules count: "
                f"expected {len(expected['noted_rule_ids'])}, got {len(actual.noted_rules)}"
            )
        if set(actual.hard_block_rules) != expected["hard_block_rules"]:
            mismatches.append(
                f"ComplianceAudit[{application_id}] hard_block_rules mismatch: "
                f"expected {sorted(expected['hard_block_rules'])}, got {sorted(actual.hard_block_rules)}"
            )
        if actual.completed != expected["completed"]:
            mismatches.append(
                f"ComplianceAudit[{application_id}] completed mismatch: "
                f"expected {expected['completed']!r}, got {actual.completed!r}"
            )
        if actual.overall_verdict != expected["overall_verdict"]:
            mismatches.append(
                f"ComplianceAudit[{application_id}] overall_verdict mismatch: "
                f"expected {expected['overall_verdict']!r}, got {actual.overall_verdict!r}"
            )

    actual_agent_rows = await agent_performance_projection.list_all()
    actual_agent_performance = {
        (row.agent_type, row.agent_id, row.model_version): row
        for row in actual_agent_rows
    }

    if set(actual_agent_performance) != set(expected_agent_performance):
        missing = sorted(set(expected_agent_performance) - set(actual_agent_performance))
        extra = sorted(set(actual_agent_performance) - set(expected_agent_performance))
        if missing:
            mismatches.append(f"AgentPerformance missing rows: {missing}")
        if extra:
            mismatches.append(f"AgentPerformance unexpected rows: {extra}")

    for key, expected in expected_agent_performance.items():
        actual = actual_agent_performance.get(key)
        if actual is None:
            continue

        comparisons = {
            "total_sessions": actual.total_sessions,
            "analyses_completed": actual.analyses_completed,
            "decisions_generated": actual.decisions_generated,
            "approve_count": actual.approve_count,
            "decline_count": actual.decline_count,
            "refer_count": actual.refer_count,
            "human_override_count": actual.human_override_count,
            "avg_confidence_score": _round(actual.avg_confidence_score),
            "avg_duration_ms": _round(actual.avg_duration_ms),
            "approve_rate": _round(actual.approve_rate),
            "decline_rate": _round(actual.decline_rate),
            "refer_rate": _round(actual.refer_rate),
            "human_override_rate": _round(actual.human_override_rate),
        }
        expected_values = {
            "total_sessions": expected["total_sessions"],
            "analyses_completed": expected["analyses_completed"],
            "decisions_generated": expected["decisions_generated"],
            "approve_count": expected["approve_count"],
            "decline_count": expected["decline_count"],
            "refer_count": expected["refer_count"],
            "human_override_count": expected["human_override_count"],
            "avg_confidence_score": _round(expected["avg_confidence_score"]),
            "avg_duration_ms": _round(expected["avg_duration_ms"]),
            "approve_rate": _round(expected["approve_rate"]),
            "decline_rate": _round(expected["decline_rate"]),
            "refer_rate": _round(expected["refer_rate"]),
            "human_override_rate": _round(expected["human_override_rate"]),
        }
        for field_name, actual_value in comparisons.items():
            expected_value = expected_values[field_name]
            if actual_value != expected_value:
                mismatches.append(
                    f"AgentPerformance{key} field {field_name}: "
                    f"expected {expected_value!r}, got {actual_value!r}"
                )

    total_applications = len(expected_summary)
    state_counts = dict(sorted(Counter(record.state for record in actual_summary_records).items()))
    verdict_counts = dict(
        sorted(Counter((record.overall_verdict or "INCOMPLETE") for record in actual_compliance_records).items())
    )
    agent_type_counts = dict(sorted(Counter(row.agent_type for row in actual_agent_rows).items()))

    return SeedProjectionValidationResult(
        total_seed_events=len(seed_events),
        total_applications=total_applications,
        application_summary_records=len(actual_summary_records),
        compliance_current_records=len(actual_compliance_records),
        agent_performance_rows=len(actual_agent_rows),
        state_counts=state_counts,
        compliance_verdict_counts=verdict_counts,
        agent_type_counts=agent_type_counts,
        mismatches=mismatches,
    )
