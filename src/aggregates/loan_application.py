from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Iterable

from src.models.events import DomainError, StoredEvent


class LoanLifecycleState(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"
    DECLINED_COMPLIANCE = "DECLINED_COMPLIANCE"


VALID_TRANSITIONS: dict[LoanLifecycleState, set[LoanLifecycleState]] = {
    LoanLifecycleState.NEW: {LoanLifecycleState.SUBMITTED},
    LoanLifecycleState.SUBMITTED: {LoanLifecycleState.AWAITING_ANALYSIS},
    LoanLifecycleState.AWAITING_ANALYSIS: {LoanLifecycleState.ANALYSIS_COMPLETE},
    LoanLifecycleState.ANALYSIS_COMPLETE: {LoanLifecycleState.COMPLIANCE_REVIEW},
    LoanLifecycleState.COMPLIANCE_REVIEW: {
        LoanLifecycleState.PENDING_DECISION,
        LoanLifecycleState.DECLINED_COMPLIANCE,
    },
    LoanLifecycleState.PENDING_DECISION: {
        LoanLifecycleState.APPROVED_PENDING_HUMAN,
        LoanLifecycleState.DECLINED_PENDING_HUMAN,
        LoanLifecycleState.PENDING_HUMAN_REVIEW,
    },
    LoanLifecycleState.APPROVED_PENDING_HUMAN: {LoanLifecycleState.FINAL_APPROVED, LoanLifecycleState.FINAL_DECLINED},
    LoanLifecycleState.DECLINED_PENDING_HUMAN: {LoanLifecycleState.FINAL_APPROVED, LoanLifecycleState.FINAL_DECLINED},
    LoanLifecycleState.PENDING_HUMAN_REVIEW: {LoanLifecycleState.FINAL_APPROVED, LoanLifecycleState.FINAL_DECLINED},
}


def _to_decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


_MISSING = object()


@dataclass
class LoanApplicationAggregate:
    application_id: str
    version: int = -1
    state: LoanLifecycleState = LoanLifecycleState.NEW
    applicant_id: str | None = None
    requested_amount_usd: Decimal | None = None
    loan_term_months: int | None = None
    loan_purpose: str | None = None
    submission_channel: str | None = None
    application_reference: str | None = None
    documents_requested: bool = False
    documents_uploaded: set[str] = field(default_factory=set)
    credit_requested: bool = False
    fraud_requested: bool = False
    compliance_requested: bool = False
    decision_requested: bool = False
    recommendation: str | None = None
    decision_confidence: float | None = None
    contributing_sessions: list[str] = field(default_factory=list)
    approved_amount_usd: Decimal | None = None
    final_decision: str | None = None
    human_review_completed: bool = False
    human_override: bool = False
    compliance_declined: bool = False
    decline_reasons: list[str] = field(default_factory=list)
    events: list[StoredEvent] = field(default_factory=list)

    @classmethod
    async def load(cls, store, application_id: str) -> "LoanApplicationAggregate":
        stream_id = f"loan-{application_id}"
        snapshot = await store.load_latest_snapshot(stream_id) if hasattr(store, "load_latest_snapshot") else None
        if snapshot is not None:
            aggregate = cls.from_snapshot(application_id=application_id, snapshot=snapshot.state)
            from_position = snapshot.stream_position + 1
        else:
            aggregate = cls(application_id=application_id)
            from_position = 0
        events = await store.load_stream(stream_id, from_position=from_position)
        for event in events:
            aggregate._apply(event)
        return aggregate

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event)
        self.version = event.stream_position
        self.events.append(event)

    def _transition(self, target: LoanLifecycleState) -> None:
        if self.state == target:
            return
        allowed = VALID_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            raise DomainError(f"Invalid transition {self.state.value} -> {target.value}")
        self.state = target

    def _required_payload_value(self, event: StoredEvent, field: str) -> object:
        value = event.payload.get(field, _MISSING)
        if value is _MISSING:
            raise DomainError(
                f"Malformed event {event.event_type} in loan-{self.application_id}: "
                f"missing required payload field '{field}'"
            )
        return value

    def _on_ApplicationSubmitted(self, event: StoredEvent) -> None:
        self._transition(LoanLifecycleState.SUBMITTED)
        self.applicant_id = str(self._required_payload_value(event, "applicant_id"))
        self.requested_amount_usd = _to_decimal(self._required_payload_value(event, "requested_amount_usd"))
        self.loan_term_months = int(self._required_payload_value(event, "loan_term_months"))
        self.loan_purpose = str(self._required_payload_value(event, "loan_purpose"))
        self.submission_channel = str(self._required_payload_value(event, "submission_channel"))
        self.application_reference = str(self._required_payload_value(event, "application_reference"))

    def _on_DocumentUploadRequested(self, event: StoredEvent) -> None:
        self.documents_requested = True

    def _on_DocumentUploaded(self, event: StoredEvent) -> None:
        self.documents_uploaded.add(str(self._required_payload_value(event, "document_id")))

    def _on_CreditAnalysisRequested(self, event: StoredEvent) -> None:
        self.credit_requested = True
        self._transition(LoanLifecycleState.AWAITING_ANALYSIS)

    def _on_FraudScreeningRequested(self, event: StoredEvent) -> None:
        self.fraud_requested = True
        self._transition(LoanLifecycleState.ANALYSIS_COMPLETE)

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self.compliance_requested = True
        self._transition(LoanLifecycleState.COMPLIANCE_REVIEW)

    def _on_DecisionRequested(self, event: StoredEvent) -> None:
        self.decision_requested = True
        self._transition(LoanLifecycleState.PENDING_DECISION)

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        recommendation = str(self._required_payload_value(event, "recommendation"))
        self.recommendation = recommendation
        self.decision_confidence = float(self._required_payload_value(event, "confidence"))
        self.contributing_sessions = list(event.payload.get("contributing_sessions", []))
        if recommendation == "APPROVE":
            self._transition(LoanLifecycleState.APPROVED_PENDING_HUMAN)
        elif recommendation == "DECLINE":
            self._transition(LoanLifecycleState.DECLINED_PENDING_HUMAN)
        else:
            self._transition(LoanLifecycleState.PENDING_HUMAN_REVIEW)

    def _on_HumanReviewRequested(self, event: StoredEvent) -> None:
        if self.state in {
            LoanLifecycleState.APPROVED_PENDING_HUMAN,
            LoanLifecycleState.DECLINED_PENDING_HUMAN,
        }:
            self.state = LoanLifecycleState.PENDING_HUMAN_REVIEW

    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None:
        self.human_review_completed = True
        self.human_override = bool(self._required_payload_value(event, "override"))
        self.final_decision = str(self._required_payload_value(event, "final_decision"))

    def _on_ApplicationApproved(self, event: StoredEvent) -> None:
        self._transition(LoanLifecycleState.FINAL_APPROVED)
        self.approved_amount_usd = _to_decimal(self._required_payload_value(event, "approved_amount_usd"))
        self.final_decision = "APPROVE"

    def _on_ApplicationDeclined(self, event: StoredEvent) -> None:
        if self.state == LoanLifecycleState.COMPLIANCE_REVIEW:
            self._transition(LoanLifecycleState.DECLINED_COMPLIANCE)
            self.compliance_declined = True
        else:
            self._transition(LoanLifecycleState.FINAL_DECLINED)
        self.final_decision = "DECLINE"
        self.decline_reasons = list(event.payload.get("decline_reasons", []))

    def assert_can_submit(self) -> None:
        if self.state != LoanLifecycleState.NEW:
            raise DomainError(f"Application {self.application_id} has already been submitted")

    def assert_awaiting_credit_analysis(self) -> None:
        if self.state != LoanLifecycleState.AWAITING_ANALYSIS or not self.credit_requested:
            raise DomainError("Loan application is not awaiting credit analysis")

    def assert_awaiting_fraud_screening(self) -> None:
        if self.state != LoanLifecycleState.ANALYSIS_COMPLETE or not self.fraud_requested:
            raise DomainError("Loan application is not awaiting fraud screening completion")

    def assert_awaiting_compliance_review(self) -> None:
        if self.state != LoanLifecycleState.COMPLIANCE_REVIEW or not self.compliance_requested:
            raise DomainError("Loan application is not in compliance review")

    def assert_pending_decision(self) -> None:
        if self.state != LoanLifecycleState.PENDING_DECISION or not self.decision_requested:
            raise DomainError("Loan application is not pending a decision")

    def assert_pending_human_resolution(self) -> None:
        if self.state not in {
            LoanLifecycleState.APPROVED_PENDING_HUMAN,
            LoanLifecycleState.DECLINED_PENDING_HUMAN,
            LoanLifecycleState.PENDING_HUMAN_REVIEW,
        }:
            raise DomainError("Loan application is not awaiting human resolution")

    def assert_valid_orchestrator_decision(
        self,
        recommendation: str,
        confidence: float,
        compliance_record,
    ) -> None:
        self.assert_pending_decision()
        if confidence < 0.60 and recommendation != "REFER":
            raise DomainError("Decisions below the confidence floor must be REFER")
        if compliance_record is None or not compliance_record.completed:
            raise DomainError("Compliance must be complete before generating a decision")
        if compliance_record.has_hard_block and recommendation != "DECLINE":
            raise DomainError("A compliance hard block allows only DECLINE")

    def assert_can_finalize_approval(
        self,
        compliance_record,
        approved_amount_usd: Decimal,
        max_recommended_limit: Decimal | None,
    ) -> None:
        self.assert_pending_human_resolution()
        if compliance_record is None or not compliance_record.is_cleared_for_approval():
            raise DomainError("Application cannot be approved before compliance is fully clear")
        if max_recommended_limit is not None and approved_amount_usd > max_recommended_limit:
            raise DomainError("Approved amount exceeds the latest agent-assessed maximum limit")

    def assert_contributing_sessions(self, session_ids: Iterable[str]) -> None:
        session_ids = list(session_ids)
        if not session_ids:
            raise DomainError("DecisionGenerated must reference contributing agent sessions")
        if len(session_ids) != len(set(session_ids)):
            raise DomainError("DecisionGenerated contains duplicate contributing sessions")

    def to_snapshot(self) -> dict[str, object]:
        return {
            "version": self.version,
            "state": self.state.value,
            "applicant_id": self.applicant_id,
            "requested_amount_usd": str(self.requested_amount_usd) if self.requested_amount_usd is not None else None,
            "loan_term_months": self.loan_term_months,
            "loan_purpose": self.loan_purpose,
            "submission_channel": self.submission_channel,
            "application_reference": self.application_reference,
            "documents_requested": self.documents_requested,
            "documents_uploaded": sorted(self.documents_uploaded),
            "credit_requested": self.credit_requested,
            "fraud_requested": self.fraud_requested,
            "compliance_requested": self.compliance_requested,
            "decision_requested": self.decision_requested,
            "recommendation": self.recommendation,
            "decision_confidence": self.decision_confidence,
            "contributing_sessions": list(self.contributing_sessions),
            "approved_amount_usd": str(self.approved_amount_usd) if self.approved_amount_usd is not None else None,
            "final_decision": self.final_decision,
            "human_review_completed": self.human_review_completed,
            "human_override": self.human_override,
            "compliance_declined": self.compliance_declined,
            "decline_reasons": list(self.decline_reasons),
        }

    @classmethod
    def from_snapshot(cls, *, application_id: str, snapshot: dict[str, object]) -> "LoanApplicationAggregate":
        return cls(
            application_id=application_id,
            version=int(snapshot.get("version", -1)),
            state=LoanLifecycleState(str(snapshot.get("state", LoanLifecycleState.NEW.value))),
            applicant_id=str(snapshot["applicant_id"]) if snapshot.get("applicant_id") is not None else None,
            requested_amount_usd=_to_decimal(snapshot.get("requested_amount_usd")),
            loan_term_months=int(snapshot["loan_term_months"]) if snapshot.get("loan_term_months") is not None else None,
            loan_purpose=str(snapshot["loan_purpose"]) if snapshot.get("loan_purpose") is not None else None,
            submission_channel=str(snapshot["submission_channel"]) if snapshot.get("submission_channel") is not None else None,
            application_reference=str(snapshot["application_reference"]) if snapshot.get("application_reference") is not None else None,
            documents_requested=bool(snapshot.get("documents_requested", False)),
            documents_uploaded=set(snapshot.get("documents_uploaded", [])),
            credit_requested=bool(snapshot.get("credit_requested", False)),
            fraud_requested=bool(snapshot.get("fraud_requested", False)),
            compliance_requested=bool(snapshot.get("compliance_requested", False)),
            decision_requested=bool(snapshot.get("decision_requested", False)),
            recommendation=str(snapshot["recommendation"]) if snapshot.get("recommendation") is not None else None,
            decision_confidence=float(snapshot["decision_confidence"]) if snapshot.get("decision_confidence") is not None else None,
            contributing_sessions=list(snapshot.get("contributing_sessions", [])),
            approved_amount_usd=_to_decimal(snapshot.get("approved_amount_usd")),
            final_decision=str(snapshot["final_decision"]) if snapshot.get("final_decision") is not None else None,
            human_review_completed=bool(snapshot.get("human_review_completed", False)),
            human_override=bool(snapshot.get("human_override", False)),
            compliance_declined=bool(snapshot.get("compliance_declined", False)),
            decline_reasons=list(snapshot.get("decline_reasons", [])),
        )
