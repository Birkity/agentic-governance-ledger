from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from src.aggregates import AgentSessionAggregate, ComplianceRecordAggregate, LoanApplicationAggregate
from src.event_store import EventStore, InMemoryEventStore
from src.models.events import (
    AgentSessionStarted,
    AgentType,
    ApplicationApproved,
    ApplicationDeclined,
    ApplicationSubmitted,
    ComplianceCheckCompleted,
    ComplianceCheckInitiated,
    ComplianceRuleFailed,
    ComplianceRuleNoted,
    ComplianceRulePassed,
    ComplianceVerdict,
    CreditAnalysisCompleted,
    CreditDecision,
    DecisionGenerated,
    DecisionRequested,
    DocumentUploadRequested,
    DocumentType,
    FraudScreeningCompleted,
    FraudScreeningRequested,
    HumanReviewCompleted,
    HumanReviewRequested,
    LoanPurpose,
    DomainError,
)


StoreType = EventStore | InMemoryEventStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _metadata(correlation_id: str | None) -> dict | None:
    if not correlation_id:
        return None
    return {"correlation_id": correlation_id}


def _to_decimal(value: Decimal | str | int | float | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


async def _append_single_stream(
    store: StoreType,
    stream_id: str,
    events: list[dict],
    expected_version: int,
    *,
    causation_id: str | None = None,
    correlation_id: str | None = None,
) -> list[int]:
    return await store.append(
        stream_id=stream_id,
        events=events,
        expected_version=expected_version,
        causation_id=causation_id,
        metadata=_metadata(correlation_id),
    )


async def _maybe_append_followup(
    store: StoreType,
    application_id: str,
    event: dict,
    *,
    causation_id: str | None = None,
    correlation_id: str | None = None,
) -> list[int]:
    stream_id = f"loan-{application_id}"
    version = await store.stream_version(stream_id)
    return await _append_single_stream(
        store,
        stream_id,
        [event],
        version,
        causation_id=causation_id,
        correlation_id=correlation_id,
    )


async def _load_latest_credit_limit(store: StoreType, application_id: str) -> Decimal | None:
    credit_events = await store.load_stream(f"credit-{application_id}")
    for event in reversed(credit_events):
        if event.event_type == "CreditAnalysisCompleted":
            decision = event.payload.get("decision", {})
            return _to_decimal(decision.get("recommended_limit_usd"))
    return None


async def _ensure_package_ready(store: StoreType, application_id: str) -> None:
    package_events = await store.load_stream(f"docpkg-{application_id}")
    if not any(event.event_type == "PackageReadyForAnalysis" for event in package_events):
        raise DomainError("Document package is not ready for analysis")


def _parse_agent_stream_id(stream_id: str) -> tuple[str, str]:
    prefix = "agent-"
    if not stream_id.startswith(prefix):
        raise DomainError(f"Invalid agent session stream id: {stream_id}")
    remainder = stream_id[len(prefix) :]
    agent_type, _, session_id = remainder.rpartition("-")
    if not agent_type or not session_id:
        raise DomainError(f"Invalid agent session stream id: {stream_id}")
    return agent_type, session_id


async def _assert_contributing_sessions_valid(
    store: StoreType,
    application_id: str,
    contributing_sessions: Iterable[str],
) -> None:
    required_event_types = {
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "ComplianceCheckCompleted",
    }
    for stream_id in contributing_sessions:
        session = await AgentSessionAggregate.load_stream(store, stream_id)
        session.assert_references_application(application_id)
        if not any(
            session.has_written_domain_event(event_type, application_id)
            for event_type in required_event_types
        ):
            raise DomainError(
                f"Contributing session {stream_id} does not contain a recorded decision event for {application_id}"
            )


@dataclass(frozen=True)
class SubmitApplicationCommand:
    application_id: str
    applicant_id: str
    requested_amount_usd: Decimal
    loan_purpose: LoanPurpose
    loan_term_months: int
    submission_channel: str
    contact_email: str
    contact_name: str
    application_reference: str
    requested_document_types: list[DocumentType] = field(
        default_factory=lambda: [
            DocumentType.APPLICATION_PROPOSAL,
            DocumentType.INCOME_STATEMENT,
            DocumentType.BALANCE_SHEET,
        ]
    )
    submitted_at: datetime | None = None
    deadline: datetime | None = None
    requested_by: str = "system"
    causation_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class StartAgentSessionCommand:
    application_id: str
    agent_type: AgentType
    session_id: str
    agent_id: str
    model_version: str
    langgraph_graph_version: str
    context_source: str = "fresh"
    context_token_count: int = 0
    started_at: datetime | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class CreditAnalysisCompletedCommand:
    application_id: str
    session_id: str
    decision: CreditDecision
    model_version: str
    model_deployment_id: str
    input_data_hash: str
    analysis_duration_ms: int
    regulatory_basis: list[str] = field(default_factory=list)
    agent_type: AgentType = AgentType.CREDIT_ANALYSIS
    completed_at: datetime | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class FraudScreeningCompletedCommand:
    application_id: str
    session_id: str
    fraud_score: float
    risk_level: str
    anomalies_found: int
    recommendation: str
    screening_model_version: str
    input_data_hash: str
    agent_type: AgentType = AgentType.FRAUD_DETECTION
    completed_at: datetime | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class ComplianceRuleEvaluation:
    rule_id: str
    rule_name: str
    outcome: str
    rule_version: str
    evidence_hash: str
    evaluation_notes: str | None = None
    failure_reason: str | None = None
    is_hard_block: bool = False
    remediation_available: bool = False
    remediation_description: str | None = None
    note_type: str | None = None
    note_text: str | None = None


@dataclass(frozen=True)
class ComplianceCheckCommand:
    application_id: str
    session_id: str
    regulation_set_version: str
    rules_to_evaluate: list[str]
    rule_results: list[ComplianceRuleEvaluation]
    agent_type: AgentType = AgentType.COMPLIANCE
    model_version: str | None = None
    completed_at: datetime | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class GenerateDecisionCommand:
    application_id: str
    session_id: str
    recommendation: str
    confidence: float
    executive_summary: str
    key_risks: list[str]
    contributing_sessions: list[str]
    model_versions: dict[str, str]
    approved_amount_usd: Decimal | None = None
    conditions: list[str] = field(default_factory=list)
    agent_type: AgentType = AgentType.DECISION_ORCHESTRATOR
    generated_at: datetime | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class HumanReviewCompletedCommand:
    application_id: str
    reviewer_id: str
    override: bool
    original_recommendation: str
    final_decision: str
    override_reason: str | None = None
    approved_amount_usd: Decimal | None = None
    interest_rate_pct: float | None = None
    term_months: int | None = None
    conditions: list[str] = field(default_factory=list)
    decline_reasons: list[str] = field(default_factory=list)
    adverse_action_notice_required: bool = True
    adverse_action_codes: list[str] = field(default_factory=list)
    reviewed_at: datetime | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


async def handle_submit_application(cmd: SubmitApplicationCommand, store: StoreType) -> dict[str, list[int]]:
    loan = await LoanApplicationAggregate.load(store, cmd.application_id)
    loan.assert_can_submit()

    submitted_at = cmd.submitted_at or _now()
    deadline = cmd.deadline or (submitted_at + timedelta(days=7))
    stream_id = f"loan-{cmd.application_id}"
    events = [
        ApplicationSubmitted(
            application_id=cmd.application_id,
            applicant_id=cmd.applicant_id,
            requested_amount_usd=cmd.requested_amount_usd,
            loan_purpose=cmd.loan_purpose,
            loan_term_months=cmd.loan_term_months,
            submission_channel=cmd.submission_channel,
            contact_email=cmd.contact_email,
            contact_name=cmd.contact_name,
            submitted_at=submitted_at,
            application_reference=cmd.application_reference,
        ).to_store_dict(),
        DocumentUploadRequested(
            application_id=cmd.application_id,
            required_document_types=cmd.requested_document_types,
            deadline=deadline,
            requested_by=cmd.requested_by,
        ).to_store_dict(),
    ]
    positions = await _append_single_stream(
        store,
        stream_id,
        events,
        loan.version,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )
    return {"loan": positions}


async def handle_start_agent_session(cmd: StartAgentSessionCommand, store: StoreType) -> dict[str, list[int]]:
    session = await AgentSessionAggregate.load(store, cmd.agent_type.value, cmd.session_id)
    if session.started:
        raise DomainError(f"Agent session {session.stream_id} already exists")

    started_at = cmd.started_at or _now()
    stream_id = f"agent-{cmd.agent_type.value}-{cmd.session_id}"
    event = AgentSessionStarted(
        session_id=cmd.session_id,
        agent_type=cmd.agent_type,
        agent_id=cmd.agent_id,
        application_id=cmd.application_id,
        model_version=cmd.model_version,
        langgraph_graph_version=cmd.langgraph_graph_version,
        context_source=cmd.context_source,
        context_token_count=cmd.context_token_count,
        started_at=started_at,
    ).to_store_dict()
    positions = await _append_single_stream(
        store,
        stream_id,
        [event],
        session.version,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )
    return {stream_id: positions}


async def handle_credit_analysis_completed(
    cmd: CreditAnalysisCompletedCommand,
    store: StoreType,
) -> dict[str, list[int]]:
    loan = await LoanApplicationAggregate.load(store, cmd.application_id)
    session = await AgentSessionAggregate.load(store, cmd.agent_type.value, cmd.session_id)
    credit_stream_id = f"credit-{cmd.application_id}"
    existing_credit_events = await store.load_stream(credit_stream_id)

    loan.assert_awaiting_credit_analysis()
    session.assert_context_loaded(cmd.application_id)
    session.assert_model_version_current(cmd.model_version)
    await _ensure_package_ready(store, cmd.application_id)
    if any(event.event_type == "CreditAnalysisCompleted" for event in existing_credit_events) and not loan.human_override:
        raise DomainError("Credit analysis is model-version locked until a human override supersedes it")

    completed_at = cmd.completed_at or _now()
    credit_event = CreditAnalysisCompleted(
        application_id=cmd.application_id,
        session_id=cmd.session_id,
        decision=cmd.decision,
        model_version=cmd.model_version,
        model_deployment_id=cmd.model_deployment_id,
        input_data_hash=cmd.input_data_hash,
        analysis_duration_ms=cmd.analysis_duration_ms,
        regulatory_basis=cmd.regulatory_basis,
        completed_at=completed_at,
    ).to_store_dict()
    credit_version = await store.stream_version(credit_stream_id)
    credit_positions = await _append_single_stream(
        store,
        credit_stream_id,
        [credit_event],
        credit_version,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )

    fraud_requested = FraudScreeningRequested(
        application_id=cmd.application_id,
        requested_at=completed_at,
        triggered_by_event_id=f"{credit_stream_id}:{credit_positions[-1]}",
    ).to_store_dict()
    loan_positions = await _maybe_append_followup(
        store,
        cmd.application_id,
        fraud_requested,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )
    return {credit_stream_id: credit_positions, f"loan-{cmd.application_id}": loan_positions}


async def handle_fraud_screening_completed(
    cmd: FraudScreeningCompletedCommand,
    store: StoreType,
) -> dict[str, list[int]]:
    loan = await LoanApplicationAggregate.load(store, cmd.application_id)
    session = await AgentSessionAggregate.load(store, cmd.agent_type.value, cmd.session_id)

    loan.assert_awaiting_fraud_screening()
    session.assert_context_loaded(cmd.application_id)
    session.assert_model_version_current(cmd.screening_model_version)

    fraud_stream_id = f"fraud-{cmd.application_id}"
    completed_at = cmd.completed_at or _now()
    fraud_event = FraudScreeningCompleted(
        application_id=cmd.application_id,
        session_id=cmd.session_id,
        fraud_score=cmd.fraud_score,
        risk_level=cmd.risk_level,
        anomalies_found=cmd.anomalies_found,
        recommendation=cmd.recommendation,
        screening_model_version=cmd.screening_model_version,
        input_data_hash=cmd.input_data_hash,
        completed_at=completed_at,
    ).to_store_dict()
    fraud_version = await store.stream_version(fraud_stream_id)
    fraud_positions = await _append_single_stream(
        store,
        fraud_stream_id,
        [fraud_event],
        fraud_version,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )

    compliance_requested = {
        "event_type": "ComplianceCheckRequested",
        "event_version": 1,
        "payload": {
            "application_id": cmd.application_id,
            "requested_at": completed_at.isoformat(),
            "triggered_by_event_id": f"{fraud_stream_id}:{fraud_positions[-1]}",
            "regulation_set_version": "2026-Q1",
            "rules_to_evaluate": ["REG-001", "REG-002", "REG-003", "REG-004", "REG-005", "REG-006"],
        },
    }
    loan_positions = await _maybe_append_followup(
        store,
        cmd.application_id,
        compliance_requested,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )
    return {fraud_stream_id: fraud_positions, f"loan-{cmd.application_id}": loan_positions}


async def handle_compliance_check(cmd: ComplianceCheckCommand, store: StoreType) -> dict[str, list[int]]:
    loan = await LoanApplicationAggregate.load(store, cmd.application_id)
    session = await AgentSessionAggregate.load(store, cmd.agent_type.value, cmd.session_id)
    compliance = await ComplianceRecordAggregate.load(store, cmd.application_id)

    loan.assert_awaiting_compliance_review()
    session.assert_context_loaded(cmd.application_id)
    if cmd.model_version:
        session.assert_model_version_current(cmd.model_version)
    if compliance.completed:
        raise DomainError("Compliance check already completed for this application")

    completed_at = cmd.completed_at or _now()
    stream_id = f"compliance-{cmd.application_id}"
    events: list[dict] = []
    if compliance.version == -1:
        events.append(
            ComplianceCheckInitiated(
                application_id=cmd.application_id,
                session_id=cmd.session_id,
                regulation_set_version=cmd.regulation_set_version,
                rules_to_evaluate=cmd.rules_to_evaluate,
                initiated_at=completed_at,
            ).to_store_dict()
        )

    passed = 0
    failed = 0
    noted = 0
    has_hard_block = False

    for result in cmd.rule_results:
        outcome = result.outcome.upper()
        if outcome == "PASS":
            passed += 1
            events.append(
                ComplianceRulePassed(
                    application_id=cmd.application_id,
                    session_id=cmd.session_id,
                    rule_id=result.rule_id,
                    rule_name=result.rule_name,
                    rule_version=result.rule_version,
                    evidence_hash=result.evidence_hash,
                    evaluation_notes=result.evaluation_notes or "",
                    evaluated_at=completed_at,
                ).to_store_dict()
            )
        elif outcome == "FAIL":
            failed += 1
            has_hard_block = has_hard_block or result.is_hard_block
            events.append(
                ComplianceRuleFailed(
                    application_id=cmd.application_id,
                    session_id=cmd.session_id,
                    rule_id=result.rule_id,
                    rule_name=result.rule_name,
                    rule_version=result.rule_version,
                    failure_reason=result.failure_reason or "rule_failed",
                    is_hard_block=result.is_hard_block,
                    remediation_available=result.remediation_available,
                    remediation_description=result.remediation_description,
                    evidence_hash=result.evidence_hash,
                    evaluated_at=completed_at,
                ).to_store_dict()
            )
        elif outcome == "NOTE":
            noted += 1
            events.append(
                ComplianceRuleNoted(
                    application_id=cmd.application_id,
                    session_id=cmd.session_id,
                    rule_id=result.rule_id,
                    rule_name=result.rule_name,
                    note_type=result.note_type or "NOTE",
                    note_text=result.note_text or "",
                    evaluated_at=completed_at,
                ).to_store_dict()
            )
        else:
            raise DomainError(f"Unknown compliance rule outcome: {result.outcome}")

    overall_verdict = (
        ComplianceVerdict.BLOCKED
        if has_hard_block
        else ComplianceVerdict.CONDITIONAL
        if failed > 0
        else ComplianceVerdict.CLEAR
    )
    events.append(
        ComplianceCheckCompleted(
            application_id=cmd.application_id,
            session_id=cmd.session_id,
            rules_evaluated=len(cmd.rule_results),
            rules_passed=passed,
            rules_failed=failed,
            rules_noted=noted,
            has_hard_block=has_hard_block,
            overall_verdict=overall_verdict,
            completed_at=completed_at,
        ).to_store_dict()
    )

    positions = await _append_single_stream(
        store,
        stream_id,
        events,
        compliance.version,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )

    if has_hard_block:
        failed_rule_ids = [result.rule_id for result in cmd.rule_results if result.outcome.upper() == "FAIL"]
        followup = ApplicationDeclined(
            application_id=cmd.application_id,
            decline_reasons=[f"Compliance hard block: {rule_id}" for rule_id in failed_rule_ids],
            declined_by="compliance_system",
            adverse_action_notice_required=True,
            adverse_action_codes=failed_rule_ids,
            declined_at=completed_at,
        ).to_store_dict()
    else:
        followup = DecisionRequested(
            application_id=cmd.application_id,
            requested_at=completed_at,
            all_analyses_complete=True,
            triggered_by_event_id=f"{stream_id}:{positions[-1]}",
        ).to_store_dict()
    loan_positions = await _maybe_append_followup(
        store,
        cmd.application_id,
        followup,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )
    return {stream_id: positions, f"loan-{cmd.application_id}": loan_positions}


async def handle_generate_decision(cmd: GenerateDecisionCommand, store: StoreType) -> dict[str, list[int]]:
    loan = await LoanApplicationAggregate.load(store, cmd.application_id)
    compliance = await ComplianceRecordAggregate.load(store, cmd.application_id)
    session = await AgentSessionAggregate.load(store, cmd.agent_type.value, cmd.session_id)

    loan.assert_contributing_sessions(cmd.contributing_sessions)
    await _assert_contributing_sessions_valid(store, cmd.application_id, cmd.contributing_sessions)
    loan.assert_valid_orchestrator_decision(cmd.recommendation, cmd.confidence, compliance)
    session.assert_context_loaded(cmd.application_id)
    if "decision_orchestrator" in cmd.model_versions:
        session.assert_model_version_current(cmd.model_versions["decision_orchestrator"])

    generated_at = cmd.generated_at or _now()
    decision_model = DecisionGenerated(
        application_id=cmd.application_id,
        orchestrator_session_id=cmd.session_id,
        recommendation=cmd.recommendation,
        confidence=cmd.confidence,
        approved_amount_usd=cmd.approved_amount_usd,
        conditions=cmd.conditions,
        executive_summary=cmd.executive_summary,
        key_risks=cmd.key_risks,
        contributing_sessions=cmd.contributing_sessions,
        model_versions=cmd.model_versions,
        generated_at=generated_at,
    )
    decision_event = decision_model.to_store_dict()
    decision_event["event_id"] = str(decision_model.event_id)

    followups: list[dict] = [decision_event]
    if cmd.recommendation == "REFER":
        followups.append(
            HumanReviewRequested(
                application_id=cmd.application_id,
                reason="Low confidence or manual escalation required",
                decision_event_id=str(decision_model.event_id),
                assigned_to=None,
                requested_at=generated_at,
            ).to_store_dict()
        )

    positions = await _append_single_stream(
        store,
        f"loan-{cmd.application_id}",
        followups,
        loan.version,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )
    return {f"loan-{cmd.application_id}": positions}


async def handle_human_review_completed(cmd: HumanReviewCompletedCommand, store: StoreType) -> dict[str, list[int]]:
    loan = await LoanApplicationAggregate.load(store, cmd.application_id)
    compliance = await ComplianceRecordAggregate.load(store, cmd.application_id)

    loan.assert_pending_human_resolution()
    reviewed_at = cmd.reviewed_at or _now()
    if cmd.override and not cmd.override_reason:
        raise DomainError("override_reason is required when override=True")
    if (
        cmd.original_recommendation != "REFER"
        and cmd.final_decision != cmd.original_recommendation
        and not cmd.override
    ):
        raise DomainError("Changing the recommendation requires override=True")

    review_event = HumanReviewCompleted(
        application_id=cmd.application_id,
        reviewer_id=cmd.reviewer_id,
        override=cmd.override,
        original_recommendation=cmd.original_recommendation,
        final_decision=cmd.final_decision,
        override_reason=cmd.override_reason,
        reviewed_at=reviewed_at,
    ).to_store_dict()
    followups = [review_event]

    final_decision = cmd.final_decision.upper()
    if final_decision == "APPROVE":
        if cmd.approved_amount_usd is None or cmd.interest_rate_pct is None or cmd.term_months is None:
            raise DomainError("Approval requires approved_amount_usd, interest_rate_pct, and term_months")
        latest_limit = await _load_latest_credit_limit(store, cmd.application_id)
        loan.assert_can_finalize_approval(compliance, cmd.approved_amount_usd, latest_limit)
        followups.append(
            ApplicationApproved(
                application_id=cmd.application_id,
                approved_amount_usd=cmd.approved_amount_usd,
                interest_rate_pct=cmd.interest_rate_pct,
                term_months=cmd.term_months,
                conditions=cmd.conditions,
                approved_by=cmd.reviewer_id,
                effective_date=reviewed_at.date().isoformat(),
                approved_at=reviewed_at,
            ).to_store_dict()
        )
    elif final_decision == "DECLINE":
        followups.append(
            ApplicationDeclined(
                application_id=cmd.application_id,
                decline_reasons=cmd.decline_reasons or ["Human review decline"],
                declined_by=cmd.reviewer_id,
                adverse_action_notice_required=cmd.adverse_action_notice_required,
                adverse_action_codes=cmd.adverse_action_codes,
                declined_at=reviewed_at,
            ).to_store_dict()
        )
    else:
        raise DomainError(f"Unsupported final decision: {cmd.final_decision}")

    positions = await _append_single_stream(
        store,
        f"loan-{cmd.application_id}",
        followups,
        loan.version,
        causation_id=cmd.causation_id,
        correlation_id=cmd.correlation_id,
    )
    return {f"loan-{cmd.application_id}": positions}


__all__ = [
    "ComplianceCheckCommand",
    "ComplianceRuleEvaluation",
    "CreditAnalysisCompletedCommand",
    "FraudScreeningCompletedCommand",
    "GenerateDecisionCommand",
    "HumanReviewCompletedCommand",
    "StartAgentSessionCommand",
    "SubmitApplicationCommand",
    "handle_compliance_check",
    "handle_credit_analysis_completed",
    "handle_fraud_screening_completed",
    "handle_generate_decision",
    "handle_human_review_completed",
    "handle_start_agent_session",
    "handle_submit_application",
]
