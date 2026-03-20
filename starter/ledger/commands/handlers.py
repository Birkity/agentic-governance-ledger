"""Phase 2 command handlers for aggregate-driven writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ledger.domain.aggregates import AgentSessionAggregate, LoanApplicationAggregate
from ledger.domain.errors import BusinessRuleViolation, DomainError
from ledger.schema.events import (
    AgentSessionStarted,
    AgentType,
    ApplicationApproved,
    ApplicationDeclined,
    ApplicationSubmitted,
    ComplianceCheckCompleted,
    ComplianceCheckRequested,
    ComplianceVerdict,
    CreditAnalysisCompleted,
    CreditAnalysisRequested,
    DecisionGenerated,
    DecisionRequested,
    DocumentFormat,
    DocumentType,
    DocumentUploadRequested,
    DocumentUploaded,
    FraudScreeningCompleted,
    FraudScreeningRequested,
    HumanReviewCompleted,
    HumanReviewRequested,
    LoanPurpose,
)


DEFAULT_DOCUMENT_TYPES = [
    DocumentType.APPLICATION_PROPOSAL,
    DocumentType.INCOME_STATEMENT,
    DocumentType.BALANCE_SHEET,
]

DEFAULT_COMPLIANCE_RULES = [
    "REG-001",
    "REG-002",
    "REG-003",
    "REG-004",
    "REG-005",
    "REG-006",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_decimal(value) -> Decimal:
    return Decimal(str(value))


def _as_document_type(value) -> DocumentType:
    return value if isinstance(value, DocumentType) else DocumentType(str(value))


def _as_document_format(value) -> DocumentFormat:
    return value if isinstance(value, DocumentFormat) else DocumentFormat(str(value))


def _as_loan_purpose(value) -> LoanPurpose:
    return value if isinstance(value, LoanPurpose) else LoanPurpose(str(value))


def _as_agent_type(value) -> AgentType:
    return value if isinstance(value, AgentType) else AgentType(str(value))


def _latest_event(events: list[dict], event_type: str) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


@dataclass(frozen=True)
class CommandResult:
    stream_id: str
    positions: list[int]
    events: list[dict]


@dataclass(frozen=True)
class SubmitApplicationCommand:
    application_id: str
    applicant_id: str
    requested_amount_usd: Decimal | str | float
    loan_purpose: LoanPurpose | str
    loan_term_months: int
    submission_channel: str
    contact_email: str
    contact_name: str
    application_reference: str | None = None
    required_document_types: list[DocumentType | str] = field(
        default_factory=lambda: list(DEFAULT_DOCUMENT_TYPES)
    )
    requested_by: str = "system"
    document_deadline_days: int = 7
    submitted_at: datetime | None = None


@dataclass(frozen=True)
class DocumentUploadedCommand:
    application_id: str
    document_id: str
    document_type: DocumentType | str
    document_format: DocumentFormat | str
    filename: str
    file_path: str
    file_size_bytes: int
    file_hash: str
    uploaded_by: str
    fiscal_year: int | None = None
    uploaded_at: datetime | None = None


@dataclass(frozen=True)
class RequestCreditAnalysisCommand:
    application_id: str
    requested_by: str
    priority: str = "NORMAL"
    requested_at: datetime | None = None


@dataclass(frozen=True)
class RequestFraudScreeningCommand:
    application_id: str
    triggered_by_event_id: str | None = None
    requested_at: datetime | None = None


@dataclass(frozen=True)
class RequestComplianceCheckCommand:
    application_id: str
    regulation_set_version: str
    rules_to_evaluate: list[str] = field(default_factory=lambda: list(DEFAULT_COMPLIANCE_RULES))
    triggered_by_event_id: str | None = None
    requested_at: datetime | None = None


@dataclass(frozen=True)
class RequestDecisionCommand:
    application_id: str
    triggered_by_event_id: str | None = None
    requested_at: datetime | None = None


@dataclass(frozen=True)
class GenerateDecisionCommand:
    application_id: str
    orchestrator_session_id: str
    recommendation: str
    confidence: float
    executive_summary: str
    key_risks: list[str] = field(default_factory=list)
    contributing_sessions: list[str] = field(default_factory=list)
    model_versions: dict[str, str] = field(default_factory=dict)
    approved_amount_usd: Decimal | str | float | None = None
    conditions: list[str] = field(default_factory=list)
    human_review_reason: str | None = None
    human_review_assignee: str | None = None
    generated_at: datetime | None = None


@dataclass(frozen=True)
class CompleteHumanReviewCommand:
    application_id: str
    reviewer_id: str
    override: bool
    original_recommendation: str
    final_decision: str
    override_reason: str | None = None
    reviewed_at: datetime | None = None
    approved_amount_usd: Decimal | str | float | None = None
    interest_rate_pct: float | None = None
    term_months: int | None = None
    conditions: list[str] = field(default_factory=list)
    effective_date: str | None = None
    decline_reasons: list[str] = field(default_factory=list)
    adverse_action_notice_required: bool = True
    adverse_action_codes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ApproveApplicationCommand:
    application_id: str
    approved_amount_usd: Decimal | str | float
    interest_rate_pct: float
    term_months: int
    approved_by: str
    conditions: list[str] = field(default_factory=list)
    effective_date: str | None = None
    approved_at: datetime | None = None


@dataclass(frozen=True)
class DeclineApplicationCommand:
    application_id: str
    decline_reasons: list[str]
    declined_by: str
    adverse_action_notice_required: bool
    adverse_action_codes: list[str] = field(default_factory=list)
    declined_at: datetime | None = None


@dataclass(frozen=True)
class StartAgentSessionCommand:
    agent_type: AgentType | str
    session_id: str
    agent_id: str
    application_id: str
    model_version: str
    langgraph_graph_version: str
    context_source: str = "fresh"
    context_token_count: int = 0
    started_at: datetime | None = None


@dataclass(frozen=True)
class CreditSummary:
    event_id: str
    session_id: str
    recommended_limit_usd: Decimal
    confidence: float


@dataclass(frozen=True)
class FraudSummary:
    event_id: str
    session_id: str
    fraud_score: float
    recommendation: str


@dataclass(frozen=True)
class ComplianceSummary:
    event_id: str
    session_id: str
    verdict: str
    has_hard_block: bool
    rules_evaluated: int


async def _load_credit_summary(store, application_id: str) -> CreditSummary | None:
    events = await store.load_stream(f"credit-{application_id}")
    latest = _latest_event(events, "CreditAnalysisCompleted")
    if latest is None:
        return None
    payload = latest["payload"]
    decision = payload.get("decision", {})
    return CreditSummary(
        event_id=str(latest["event_id"]),
        session_id=str(payload.get("session_id")),
        recommended_limit_usd=_as_decimal(decision.get("recommended_limit_usd", 0)),
        confidence=float(decision.get("confidence", 0.0)),
    )


async def _load_fraud_summary(store, application_id: str) -> FraudSummary | None:
    events = await store.load_stream(f"fraud-{application_id}")
    latest = _latest_event(events, "FraudScreeningCompleted")
    if latest is None:
        return None
    payload = latest["payload"]
    return FraudSummary(
        event_id=str(latest["event_id"]),
        session_id=str(payload.get("session_id")),
        fraud_score=float(payload.get("fraud_score", 0.0)),
        recommendation=str(payload.get("recommendation", "")),
    )


async def _load_compliance_summary(store, application_id: str) -> ComplianceSummary | None:
    events = await store.load_stream(f"compliance-{application_id}")
    latest = _latest_event(events, "ComplianceCheckCompleted")
    if latest is None:
        return None
    payload = latest["payload"]
    return ComplianceSummary(
        event_id=str(latest["event_id"]),
        session_id=str(payload.get("session_id")),
        verdict=str(payload.get("overall_verdict")),
        has_hard_block=bool(payload.get("has_hard_block")),
        rules_evaluated=int(payload.get("rules_evaluated", 0)),
    )


def _enforce_amount_against_credit_limit(
    aggregate: LoanApplicationAggregate,
    credit_summary: CreditSummary | None,
    approved_amount_usd,
) -> Decimal:
    approved_amount = _as_decimal(approved_amount_usd)
    if approved_amount <= 0:
        raise BusinessRuleViolation("Approved amount must be positive")
    if aggregate.requested_amount_usd is not None and approved_amount > aggregate.requested_amount_usd:
        raise BusinessRuleViolation(
            "Approved amount cannot exceed the originally requested amount"
        )
    if credit_summary is not None and approved_amount > credit_summary.recommended_limit_usd:
        raise BusinessRuleViolation(
            "Approved amount cannot exceed the credit analysis recommended limit"
        )
    return approved_amount


async def submit_application(store, cmd: SubmitApplicationCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_submit()

    submitted_at = cmd.submitted_at or _utcnow()
    required_document_types = [_as_document_type(item) for item in cmd.required_document_types]
    events = [
        ApplicationSubmitted(
            application_id=cmd.application_id,
            applicant_id=cmd.applicant_id,
            requested_amount_usd=_as_decimal(cmd.requested_amount_usd),
            loan_purpose=_as_loan_purpose(cmd.loan_purpose),
            loan_term_months=cmd.loan_term_months,
            submission_channel=cmd.submission_channel,
            contact_email=cmd.contact_email,
            contact_name=cmd.contact_name,
            submitted_at=submitted_at,
            application_reference=cmd.application_reference or cmd.application_id,
        ).to_store_dict(),
        DocumentUploadRequested(
            application_id=cmd.application_id,
            required_document_types=required_document_types,
            deadline=submitted_at + timedelta(days=cmd.document_deadline_days),
            requested_by=cmd.requested_by,
        ).to_store_dict(),
    ]
    positions = await store.append(
        f"loan-{cmd.application_id}",
        events,
        expected_version=aggregate.version,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, events)


async def record_document_uploaded(store, cmd: DocumentUploadedCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_upload_document()

    event = DocumentUploaded(
        application_id=cmd.application_id,
        document_id=cmd.document_id,
        document_type=_as_document_type(cmd.document_type),
        document_format=_as_document_format(cmd.document_format),
        filename=cmd.filename,
        file_path=cmd.file_path,
        file_size_bytes=cmd.file_size_bytes,
        file_hash=cmd.file_hash,
        fiscal_year=cmd.fiscal_year,
        uploaded_at=cmd.uploaded_at or _utcnow(),
        uploaded_by=cmd.uploaded_by,
    ).to_store_dict()
    positions = await store.append(
        f"loan-{cmd.application_id}",
        [event],
        expected_version=aggregate.version,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, [event])


async def request_credit_analysis(store, cmd: RequestCreditAnalysisCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_request_credit_analysis()

    event = CreditAnalysisRequested(
        application_id=cmd.application_id,
        requested_at=cmd.requested_at or _utcnow(),
        requested_by=cmd.requested_by,
        priority=cmd.priority,
    ).to_store_dict()
    positions = await store.append(
        f"loan-{cmd.application_id}",
        [event],
        expected_version=aggregate.version,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, [event])


async def request_fraud_screening(store, cmd: RequestFraudScreeningCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_request_fraud_screening()

    credit_summary = await _load_credit_summary(store, cmd.application_id)
    if credit_summary is None:
        raise BusinessRuleViolation(
            "Cannot request fraud screening before credit analysis is complete"
        )

    event = FraudScreeningRequested(
        application_id=cmd.application_id,
        requested_at=cmd.requested_at or _utcnow(),
        triggered_by_event_id=cmd.triggered_by_event_id or credit_summary.event_id,
    ).to_store_dict()
    positions = await store.append(
        f"loan-{cmd.application_id}",
        [event],
        expected_version=aggregate.version,
        causation_id=cmd.triggered_by_event_id or credit_summary.event_id,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, [event])


async def request_compliance_check(store, cmd: RequestComplianceCheckCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_request_compliance_check()

    fraud_summary = await _load_fraud_summary(store, cmd.application_id)
    if fraud_summary is None:
        raise BusinessRuleViolation(
            "Cannot request compliance until fraud screening is complete"
        )

    event = ComplianceCheckRequested(
        application_id=cmd.application_id,
        requested_at=cmd.requested_at or _utcnow(),
        triggered_by_event_id=cmd.triggered_by_event_id or fraud_summary.event_id,
        regulation_set_version=cmd.regulation_set_version,
        rules_to_evaluate=list(cmd.rules_to_evaluate),
    ).to_store_dict()
    positions = await store.append(
        f"loan-{cmd.application_id}",
        [event],
        expected_version=aggregate.version,
        causation_id=cmd.triggered_by_event_id or fraud_summary.event_id,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, [event])


async def request_decision(store, cmd: RequestDecisionCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_request_decision()

    credit_summary = await _load_credit_summary(store, cmd.application_id)
    fraud_summary = await _load_fraud_summary(store, cmd.application_id)
    compliance_summary = await _load_compliance_summary(store, cmd.application_id)
    if not all([credit_summary, fraud_summary, compliance_summary]):
        raise BusinessRuleViolation(
            "Decisioning requires completed credit, fraud, and compliance analyses"
        )
    if compliance_summary.has_hard_block or compliance_summary.verdict == ComplianceVerdict.BLOCKED.value:
        raise BusinessRuleViolation(
            "Compliance blocked applications must be declined instead of sent for decisioning"
        )

    event = DecisionRequested(
        application_id=cmd.application_id,
        requested_at=cmd.requested_at or _utcnow(),
        all_analyses_complete=True,
        triggered_by_event_id=cmd.triggered_by_event_id or compliance_summary.event_id,
    ).to_store_dict()
    positions = await store.append(
        f"loan-{cmd.application_id}",
        [event],
        expected_version=aggregate.version,
        causation_id=cmd.triggered_by_event_id or compliance_summary.event_id,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, [event])


async def generate_decision(store, cmd: GenerateDecisionCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    normalized_recommendation = aggregate.assert_valid_orchestrator_decision(
        cmd.recommendation,
        cmd.confidence,
    )

    credit_summary = await _load_credit_summary(store, cmd.application_id)
    compliance_summary = await _load_compliance_summary(store, cmd.application_id)
    if compliance_summary is None:
        raise BusinessRuleViolation("Compliance must be complete before a decision is generated")
    if compliance_summary.has_hard_block or compliance_summary.verdict == ComplianceVerdict.BLOCKED.value:
        raise BusinessRuleViolation(
            "Compliance blocked applications cannot generate a non-decline decision"
        )

    approved_amount_usd = None
    if normalized_recommendation == "APPROVE":
        if cmd.approved_amount_usd is None:
            raise BusinessRuleViolation(
                "APPROVE decisions must include an approved amount"
            )
        approved_amount_usd = _enforce_amount_against_credit_limit(
            aggregate,
            credit_summary,
            cmd.approved_amount_usd,
        )

    generated_at = cmd.generated_at or _utcnow()
    events = [
        DecisionGenerated(
            application_id=cmd.application_id,
            orchestrator_session_id=cmd.orchestrator_session_id,
            recommendation=normalized_recommendation,
            confidence=cmd.confidence,
            approved_amount_usd=approved_amount_usd,
            conditions=list(cmd.conditions),
            executive_summary=cmd.executive_summary,
            key_risks=list(cmd.key_risks),
            contributing_sessions=list(cmd.contributing_sessions),
            model_versions=dict(cmd.model_versions),
            generated_at=generated_at,
        ).to_store_dict()
    ]

    if normalized_recommendation == "REFER":
        aggregate.assert_can_request_human_review()
        events.append(
            HumanReviewRequested(
                application_id=cmd.application_id,
                reason=cmd.human_review_reason
                or "Confidence below threshold or policy constraints require human review",
                decision_event_id=cmd.orchestrator_session_id,
                assigned_to=cmd.human_review_assignee,
                requested_at=generated_at,
            ).to_store_dict()
        )

    positions = await store.append(
        f"loan-{cmd.application_id}",
        events,
        expected_version=aggregate.version,
        causation_id=cmd.orchestrator_session_id,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, events)


async def complete_human_review(store, cmd: CompleteHumanReviewCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_complete_human_review()

    original_recommendation = cmd.original_recommendation.upper()
    final_decision = cmd.final_decision.upper()
    if final_decision not in {"APPROVE", "DECLINE"}:
        raise BusinessRuleViolation(
            "Human review must end in APPROVE or DECLINE"
        )
    if cmd.override and not cmd.override_reason:
        raise BusinessRuleViolation(
            "Override reason is required when a human review overrides the system recommendation"
        )
    if cmd.override != (final_decision != original_recommendation):
        raise BusinessRuleViolation(
            "override flag must match whether the final decision differs from the original recommendation"
        )

    review_event = HumanReviewCompleted(
        application_id=cmd.application_id,
        reviewer_id=cmd.reviewer_id,
        override=cmd.override,
        original_recommendation=original_recommendation,
        final_decision=final_decision,
        override_reason=cmd.override_reason,
        reviewed_at=cmd.reviewed_at or _utcnow(),
    ).to_store_dict()

    events = [review_event]
    if final_decision == "APPROVE":
        credit_summary = await _load_credit_summary(store, cmd.application_id)
        compliance_summary = await _load_compliance_summary(store, cmd.application_id)
        if compliance_summary is None:
            raise BusinessRuleViolation("Cannot approve without completed compliance results")
        if compliance_summary.has_hard_block or compliance_summary.verdict == ComplianceVerdict.BLOCKED.value:
            raise BusinessRuleViolation("Compliance blocked applications cannot be approved")
        if cmd.approved_amount_usd is None or cmd.interest_rate_pct is None or cmd.term_months is None:
            raise BusinessRuleViolation(
                "Human approval requires approved amount, interest rate, and term"
            )
        approved_amount = _enforce_amount_against_credit_limit(
            aggregate,
            credit_summary,
            cmd.approved_amount_usd,
        )
        events.append(
            ApplicationApproved(
                application_id=cmd.application_id,
                approved_amount_usd=approved_amount,
                interest_rate_pct=cmd.interest_rate_pct,
                term_months=cmd.term_months,
                conditions=list(cmd.conditions),
                approved_by=cmd.reviewer_id,
                effective_date=cmd.effective_date
                or (cmd.reviewed_at or _utcnow()).date().isoformat(),
                approved_at=cmd.reviewed_at or _utcnow(),
            ).to_store_dict()
        )
    else:
        if not cmd.decline_reasons:
            raise BusinessRuleViolation("Decline reasons are required for a final decline")
        events.append(
            ApplicationDeclined(
                application_id=cmd.application_id,
                decline_reasons=list(cmd.decline_reasons),
                declined_by=cmd.reviewer_id,
                adverse_action_notice_required=cmd.adverse_action_notice_required,
                adverse_action_codes=list(cmd.adverse_action_codes),
                declined_at=cmd.reviewed_at or _utcnow(),
            ).to_store_dict()
        )

    positions = await store.append(
        f"loan-{cmd.application_id}",
        events,
        expected_version=aggregate.version,
        causation_id=cmd.reviewer_id,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, events)


async def approve_application(store, cmd: ApproveApplicationCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_approve()

    compliance_summary = await _load_compliance_summary(store, cmd.application_id)
    if compliance_summary is None:
        raise BusinessRuleViolation("Compliance must be complete before approval")
    if compliance_summary.has_hard_block or compliance_summary.verdict == ComplianceVerdict.BLOCKED.value:
        raise BusinessRuleViolation("Compliance blocked applications cannot be approved")

    credit_summary = await _load_credit_summary(store, cmd.application_id)
    approved_amount = _enforce_amount_against_credit_limit(
        aggregate,
        credit_summary,
        cmd.approved_amount_usd,
    )
    event = ApplicationApproved(
        application_id=cmd.application_id,
        approved_amount_usd=approved_amount,
        interest_rate_pct=cmd.interest_rate_pct,
        term_months=cmd.term_months,
        conditions=list(cmd.conditions),
        approved_by=cmd.approved_by,
        effective_date=cmd.effective_date or (cmd.approved_at or _utcnow()).date().isoformat(),
        approved_at=cmd.approved_at or _utcnow(),
    ).to_store_dict()
    positions = await store.append(
        f"loan-{cmd.application_id}",
        [event],
        expected_version=aggregate.version,
        causation_id=cmd.approved_by,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, [event])


async def decline_application(store, cmd: DeclineApplicationCommand) -> CommandResult:
    aggregate = await LoanApplicationAggregate.load(store, cmd.application_id)
    aggregate.assert_can_decline()

    event = ApplicationDeclined(
        application_id=cmd.application_id,
        decline_reasons=list(cmd.decline_reasons),
        declined_by=cmd.declined_by,
        adverse_action_notice_required=cmd.adverse_action_notice_required,
        adverse_action_codes=list(cmd.adverse_action_codes),
        declined_at=cmd.declined_at or _utcnow(),
    ).to_store_dict()
    positions = await store.append(
        f"loan-{cmd.application_id}",
        [event],
        expected_version=aggregate.version,
        causation_id=cmd.declined_by,
    )
    return CommandResult(f"loan-{cmd.application_id}", positions, [event])


async def start_agent_session(store, cmd: StartAgentSessionCommand) -> CommandResult:
    agent_type = _as_agent_type(cmd.agent_type)
    aggregate = await AgentSessionAggregate.load(store, agent_type.value, cmd.session_id)
    aggregate.assert_can_start()

    event = AgentSessionStarted(
        session_id=cmd.session_id,
        agent_type=agent_type,
        agent_id=cmd.agent_id,
        application_id=cmd.application_id,
        model_version=cmd.model_version,
        langgraph_graph_version=cmd.langgraph_graph_version,
        context_source=cmd.context_source,
        context_token_count=cmd.context_token_count,
        started_at=cmd.started_at or _utcnow(),
    ).to_store_dict()
    stream_id = f"agent-{agent_type.value}-{cmd.session_id}"
    positions = await store.append(stream_id, [event], expected_version=aggregate.version)
    return CommandResult(stream_id, positions, [event])
