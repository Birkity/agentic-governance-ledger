"""
LoanApplication aggregate for Phase 2 domain logic.

The aggregate is rebuilt exclusively by replaying the loan stream.
Command handlers use the reconstructed state to validate commands before
appending new events with optimistic concurrency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from ledger.domain.errors import BusinessRuleViolation, InvalidStateTransition


class ApplicationState(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    DOCUMENTS_PENDING = "DOCUMENTS_PENDING"
    DOCUMENTS_UPLOADED = "DOCUMENTS_UPLOADED"
    CREDIT_ANALYSIS_REQUESTED = "CREDIT_ANALYSIS_REQUESTED"
    FRAUD_SCREENING_REQUESTED = "FRAUD_SCREENING_REQUESTED"
    COMPLIANCE_CHECK_REQUESTED = "COMPLIANCE_CHECK_REQUESTED"
    PENDING_DECISION = "PENDING_DECISION"
    REFERRED = "REFERRED"
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    DECLINED_COMPLIANCE = "DECLINED_COMPLIANCE"


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _to_text(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


@dataclass
class LoanApplicationAggregate:
    application_id: str
    state: ApplicationState = ApplicationState.NEW
    applicant_id: str | None = None
    requested_amount_usd: Decimal | None = None
    loan_purpose: str | None = None
    loan_term_months: int | None = None
    required_document_types: set[str] = field(default_factory=set)
    uploaded_document_types: set[str] = field(default_factory=set)
    uploaded_document_ids: list[str] = field(default_factory=list)
    required_compliance_rules: list[str] = field(default_factory=list)
    decision_requested: bool = False
    decision_generated: bool = False
    human_review_requested: bool = False
    human_review_completed: bool = False
    compliance_blocked: bool = False
    last_recommendation: str | None = None
    last_confidence: float | None = None
    final_decision: str | None = None
    version: int = -1
    events: list[dict] = field(default_factory=list)

    @classmethod
    async def load(cls, store, application_id: str) -> "LoanApplicationAggregate":
        aggregate = cls(application_id=application_id)
        stream_events = await store.load_stream(f"loan-{application_id}")
        for event in stream_events:
            aggregate.apply(event)
        return aggregate

    def apply(self, event: dict) -> None:
        event_type = event.get("event_type")
        handler = getattr(self, f"_apply_{event_type}", None)
        if handler is not None:
            handler(event)

        self.version = event.get("stream_position", self.version + 1)
        self.events.append(dict(event))

    def has_required_documents(self) -> bool:
        return bool(self.required_document_types) and self.required_document_types.issubset(
            self.uploaded_document_types
        )

    def assert_can_submit(self) -> None:
        if self.state != ApplicationState.NEW:
            raise InvalidStateTransition(
                f"Application {self.application_id} already exists in state {self.state}"
            )

    def assert_can_upload_document(self) -> None:
        allowed = {
            ApplicationState.SUBMITTED,
            ApplicationState.DOCUMENTS_PENDING,
            ApplicationState.DOCUMENTS_UPLOADED,
        }
        if self.state not in allowed:
            raise InvalidStateTransition(
                f"Cannot upload documents while application is in state {self.state}"
            )

    def assert_can_request_credit_analysis(self) -> None:
        if self.state not in {
            ApplicationState.DOCUMENTS_PENDING,
            ApplicationState.DOCUMENTS_UPLOADED,
        }:
            raise InvalidStateTransition(
                f"Cannot request credit analysis from state {self.state}"
            )
        if not self.has_required_documents():
            raise BusinessRuleViolation(
                "Cannot request credit analysis until all required documents are uploaded"
            )

    def assert_can_request_fraud_screening(self) -> None:
        if self.state != ApplicationState.CREDIT_ANALYSIS_REQUESTED:
            raise InvalidStateTransition(
                f"Cannot request fraud screening from state {self.state}"
            )

    def assert_can_request_compliance_check(self) -> None:
        if self.state != ApplicationState.FRAUD_SCREENING_REQUESTED:
            raise InvalidStateTransition(
                f"Cannot request compliance check from state {self.state}"
            )

    def assert_can_request_decision(self) -> None:
        if self.state != ApplicationState.COMPLIANCE_CHECK_REQUESTED:
            raise InvalidStateTransition(
                f"Cannot request a decision from state {self.state}"
            )
        if self.compliance_blocked:
            raise BusinessRuleViolation(
                "Compliance is blocked; the application must be declined instead of sent for decisioning"
            )

    def assert_valid_orchestrator_decision(
        self,
        recommendation: str,
        confidence: float,
    ) -> str:
        normalized = recommendation.upper()
        if normalized not in {"APPROVE", "DECLINE", "REFER"}:
            raise BusinessRuleViolation(f"Unsupported recommendation '{recommendation}'")
        if self.state != ApplicationState.PENDING_DECISION:
            raise InvalidStateTransition(
                f"Cannot generate a decision while application is in state {self.state}"
            )
        if self.decision_generated:
            raise BusinessRuleViolation(
                "A decision has already been generated for this application state"
            )
        if self.compliance_blocked and normalized != "DECLINE":
            raise BusinessRuleViolation(
                "Compliance hard block allows only a DECLINE recommendation"
            )
        if confidence < 0.60 and normalized != "REFER":
            raise BusinessRuleViolation(
                "Confidence below 0.60 must result in a REFER recommendation"
            )
        return normalized

    def assert_can_request_human_review(self) -> None:
        if self.state not in {ApplicationState.REFERRED, ApplicationState.PENDING_DECISION}:
            raise InvalidStateTransition(
                f"Cannot request human review from state {self.state}"
            )

    def assert_can_complete_human_review(self) -> None:
        if self.state != ApplicationState.PENDING_HUMAN_REVIEW:
            raise InvalidStateTransition(
                f"Cannot complete human review from state {self.state}"
            )

    def assert_can_approve(self) -> None:
        if self.state not in {
            ApplicationState.PENDING_DECISION,
            ApplicationState.PENDING_HUMAN_REVIEW,
        }:
            raise InvalidStateTransition(
                f"Cannot approve application from state {self.state}"
            )
        if self.compliance_blocked:
            raise BusinessRuleViolation(
                "Cannot approve an application that is compliance blocked"
            )

    def assert_can_decline(self) -> None:
        if self.state not in {
            ApplicationState.COMPLIANCE_CHECK_REQUESTED,
            ApplicationState.PENDING_DECISION,
            ApplicationState.REFERRED,
            ApplicationState.PENDING_HUMAN_REVIEW,
        }:
            raise InvalidStateTransition(
                f"Cannot decline application from state {self.state}"
            )

    def _apply_ApplicationSubmitted(self, event: dict) -> None:
        payload = event["payload"]
        self.state = ApplicationState.SUBMITTED
        self.applicant_id = payload.get("applicant_id")
        self.requested_amount_usd = _to_decimal(payload.get("requested_amount_usd"))
        self.loan_purpose = payload.get("loan_purpose")
        self.loan_term_months = payload.get("loan_term_months")

    def _apply_DocumentUploadRequested(self, event: dict) -> None:
        payload = event["payload"]
        self.required_document_types = {
            _to_text(document_type)
            for document_type in payload.get("required_document_types", [])
        }
        self.state = ApplicationState.DOCUMENTS_PENDING

    def _apply_DocumentUploaded(self, event: dict) -> None:
        payload = event["payload"]
        document_type = payload.get("document_type")
        if document_type is not None:
            self.uploaded_document_types.add(_to_text(document_type))
        document_id = payload.get("document_id")
        if document_id and document_id not in self.uploaded_document_ids:
            self.uploaded_document_ids.append(document_id)
        self.state = ApplicationState.DOCUMENTS_UPLOADED

    def _apply_CreditAnalysisRequested(self, event: dict) -> None:
        self.state = ApplicationState.CREDIT_ANALYSIS_REQUESTED

    def _apply_FraudScreeningRequested(self, event: dict) -> None:
        self.state = ApplicationState.FRAUD_SCREENING_REQUESTED

    def _apply_ComplianceCheckRequested(self, event: dict) -> None:
        payload = event["payload"]
        self.required_compliance_rules = list(payload.get("rules_to_evaluate", []))
        self.state = ApplicationState.COMPLIANCE_CHECK_REQUESTED

    def _apply_DecisionRequested(self, event: dict) -> None:
        self.decision_requested = True
        self.state = ApplicationState.PENDING_DECISION

    def _apply_DecisionGenerated(self, event: dict) -> None:
        payload = event["payload"]
        self.decision_generated = True
        self.last_recommendation = str(payload.get("recommendation", "")).upper()
        confidence = payload.get("confidence")
        self.last_confidence = float(confidence) if confidence is not None else None
        if self.last_recommendation == "REFER":
            self.state = ApplicationState.REFERRED

    def _apply_HumanReviewRequested(self, event: dict) -> None:
        self.human_review_requested = True
        self.state = ApplicationState.PENDING_HUMAN_REVIEW

    def _apply_HumanReviewCompleted(self, event: dict) -> None:
        payload = event["payload"]
        self.human_review_completed = True
        self.final_decision = str(payload.get("final_decision", "")).upper() or None

    def _apply_ApplicationApproved(self, event: dict) -> None:
        self.final_decision = "APPROVE"
        self.state = ApplicationState.APPROVED

    def _apply_ApplicationDeclined(self, event: dict) -> None:
        payload = event["payload"]
        reasons = [str(reason) for reason in payload.get("decline_reasons", [])]
        codes = [str(code) for code in payload.get("adverse_action_codes", [])]
        compliance_decline = "COMPLIANCE_BLOCK" in codes or any(
            "compliance" in reason.lower() or "reg-" in reason.lower() for reason in reasons
        )
        self.compliance_blocked = self.compliance_blocked or compliance_decline
        self.final_decision = "DECLINE"
        self.state = (
            ApplicationState.DECLINED_COMPLIANCE
            if compliance_decline
            else ApplicationState.DECLINED
        )
