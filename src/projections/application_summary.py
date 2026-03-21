from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.models.events import StoredEvent
from src.projections.base import Projection, _json_dumps, _json_loads


@dataclass
class ApplicationSummaryRecord:
    application_id: str
    state: str = "NEW"
    applicant_id: str | None = None
    requested_amount_usd: Decimal | None = None
    approved_amount_usd: Decimal | None = None
    risk_tier: str | None = None
    fraud_score: float | None = None
    compliance_status: str | None = None
    decision: str | None = None
    agent_sessions_completed: list[str] = field(default_factory=list)
    last_event_type: str | None = None
    last_event_at: datetime | None = None
    human_reviewer_id: str | None = None
    final_decision_at: datetime | None = None


class ApplicationSummaryProjection(Projection):
    name = "application_summary"

    def __init__(self, store) -> None:
        super().__init__(store)
        self._records: dict[str, ApplicationSummaryRecord] = {}

    def handles(self, event: StoredEvent) -> bool:
        return event.event_type in {
            "ApplicationSubmitted",
            "DocumentUploadRequested",
            "DocumentUploaded",
            "PackageReadyForAnalysis",
            "CreditAnalysisRequested",
            "CreditAnalysisCompleted",
            "FraudScreeningRequested",
            "FraudScreeningCompleted",
            "ComplianceCheckRequested",
            "ComplianceCheckCompleted",
            "DecisionRequested",
            "DecisionGenerated",
            "HumanReviewRequested",
            "HumanReviewCompleted",
            "ApplicationApproved",
            "ApplicationDeclined",
            "AgentSessionCompleted",
        }

    async def reset(self) -> None:
        self._records.clear()
        await self._execute("TRUNCATE TABLE application_summary")

    async def get(self, application_id: str) -> ApplicationSummaryRecord | None:
        record = self._records.get(application_id)
        if record is not None:
            return record

        row = await self._fetchrow(
            """
            SELECT application_id, state, applicant_id, requested_amount_usd, approved_amount_usd,
                   risk_tier, fraud_score, compliance_status, decision, agent_sessions_completed,
                   last_event_type, last_event_at, human_reviewer_id, final_decision_at
            FROM application_summary
            WHERE application_id = $1
            """,
            application_id,
        )
        if row is None:
            return None

        record = ApplicationSummaryRecord(
            application_id=row["application_id"],
            state=row["state"],
            applicant_id=row["applicant_id"],
            requested_amount_usd=row["requested_amount_usd"],
            approved_amount_usd=row["approved_amount_usd"],
            risk_tier=row["risk_tier"],
            fraud_score=row["fraud_score"],
            compliance_status=row["compliance_status"],
            decision=row["decision"],
            agent_sessions_completed=list(_json_loads(row["agent_sessions_completed"], [])),
            last_event_type=row["last_event_type"],
            last_event_at=row["last_event_at"],
            human_reviewer_id=row["human_reviewer_id"],
            final_decision_at=row["final_decision_at"],
        )
        self._records[application_id] = record
        return record

    async def list_all(self) -> list[ApplicationSummaryRecord]:
        if self._pool is None:
            return list(self._records.values())

        rows = await self._fetch(
            """
            SELECT application_id, state, applicant_id, requested_amount_usd, approved_amount_usd,
                   risk_tier, fraud_score, compliance_status, decision, agent_sessions_completed,
                   last_event_type, last_event_at, human_reviewer_id, final_decision_at
            FROM application_summary
            ORDER BY application_id
            """
        )
        return [
            ApplicationSummaryRecord(
                application_id=row["application_id"],
                state=row["state"],
                applicant_id=row["applicant_id"],
                requested_amount_usd=row["requested_amount_usd"],
                approved_amount_usd=row["approved_amount_usd"],
                risk_tier=row["risk_tier"],
                fraud_score=row["fraud_score"],
                compliance_status=row["compliance_status"],
                decision=row["decision"],
                agent_sessions_completed=list(_json_loads(row["agent_sessions_completed"], [])),
                last_event_type=row["last_event_type"],
                last_event_at=row["last_event_at"],
                human_reviewer_id=row["human_reviewer_id"],
                final_decision_at=row["final_decision_at"],
            )
            for row in rows
        ]

    async def apply(self, event: StoredEvent) -> None:
        application_id = self._extract_application_id(event)
        if application_id is None:
            return

        record = await self.get(application_id)
        if record is None:
            record = ApplicationSummaryRecord(application_id=application_id)
            self._records[application_id] = record

        payload = event.payload
        if event.event_type == "ApplicationSubmitted":
            record.state = "SUBMITTED"
            record.applicant_id = payload.get("applicant_id", record.applicant_id)
            amount = payload.get("requested_amount_usd")
            record.requested_amount_usd = Decimal(str(amount)) if amount is not None else None
        elif event.event_type == "DocumentUploadRequested":
            record.state = "DOCUMENTS_PENDING"
        elif event.event_type == "DocumentUploaded":
            record.state = "DOCUMENTS_UPLOADED"
        elif event.event_type == "PackageReadyForAnalysis":
            record.state = "DOCUMENTS_PROCESSED"
        elif event.event_type == "CreditAnalysisRequested":
            record.state = "AWAITING_ANALYSIS"
        elif event.event_type == "CreditAnalysisCompleted":
            decision = payload.get("decision", {})
            record.risk_tier = decision.get("risk_tier")
        elif event.event_type == "FraudScreeningRequested":
            record.state = "ANALYSIS_COMPLETE"
        elif event.event_type == "FraudScreeningCompleted":
            score = payload.get("fraud_score")
            record.fraud_score = float(score) if score is not None else None
        elif event.event_type == "ComplianceCheckRequested":
            record.state = "COMPLIANCE_REVIEW"
        elif event.event_type == "ComplianceCheckCompleted":
            verdict = payload.get("overall_verdict")
            record.compliance_status = verdict
        elif event.event_type == "DecisionRequested":
            record.state = "PENDING_DECISION"
        elif event.event_type == "DecisionGenerated":
            recommendation = payload.get("recommendation")
            record.decision = recommendation
            amount = payload.get("approved_amount_usd")
            record.approved_amount_usd = Decimal(str(amount)) if amount is not None else record.approved_amount_usd
            if recommendation == "APPROVE":
                record.state = "APPROVED_PENDING_HUMAN"
            elif recommendation == "DECLINE":
                record.state = "DECLINED_PENDING_HUMAN"
            else:
                record.state = "PENDING_HUMAN_REVIEW"
        elif event.event_type == "HumanReviewRequested":
            record.state = "PENDING_HUMAN_REVIEW"
        elif event.event_type == "HumanReviewCompleted":
            record.human_reviewer_id = payload.get("reviewer_id")
            record.decision = payload.get("final_decision")
        elif event.event_type == "ApplicationApproved":
            record.state = "FINAL_APPROVED"
            amount = payload.get("approved_amount_usd")
            record.approved_amount_usd = Decimal(str(amount)) if amount is not None else record.approved_amount_usd
            record.decision = "APPROVE"
            record.final_decision_at = self._event_timestamp(event, "approved_at")
        elif event.event_type == "ApplicationDeclined":
            decline_reasons = payload.get("decline_reasons", [])
            if record.compliance_status == "BLOCKED" or any("Compliance hard block" in reason for reason in decline_reasons):
                record.state = "DECLINED_COMPLIANCE"
            else:
                record.state = "FINAL_DECLINED"
            record.decision = "DECLINE"
            record.final_decision_at = self._event_timestamp(event, "declined_at")
        elif event.event_type == "AgentSessionCompleted":
            session_id = payload.get("session_id")
            if session_id and session_id not in record.agent_sessions_completed:
                record.agent_sessions_completed.append(session_id)

        record.last_event_type = event.event_type
        record.last_event_at = event.recorded_at
        await self._persist(record)

    async def _persist(self, record: ApplicationSummaryRecord) -> None:
        await self._execute(
            """
            INSERT INTO application_summary(
                application_id, state, applicant_id, requested_amount_usd, approved_amount_usd,
                risk_tier, fraud_score, compliance_status, decision, agent_sessions_completed,
                last_event_type, last_event_at, human_reviewer_id, final_decision_at, updated_at
            )
            VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14, NOW())
            ON CONFLICT (application_id)
            DO UPDATE SET
                state = EXCLUDED.state,
                applicant_id = EXCLUDED.applicant_id,
                requested_amount_usd = EXCLUDED.requested_amount_usd,
                approved_amount_usd = EXCLUDED.approved_amount_usd,
                risk_tier = EXCLUDED.risk_tier,
                fraud_score = EXCLUDED.fraud_score,
                compliance_status = EXCLUDED.compliance_status,
                decision = EXCLUDED.decision,
                agent_sessions_completed = EXCLUDED.agent_sessions_completed,
                last_event_type = EXCLUDED.last_event_type,
                last_event_at = EXCLUDED.last_event_at,
                human_reviewer_id = EXCLUDED.human_reviewer_id,
                final_decision_at = EXCLUDED.final_decision_at,
                updated_at = NOW()
            """,
            record.application_id,
            record.state,
            record.applicant_id,
            record.requested_amount_usd,
            record.approved_amount_usd,
            record.risk_tier,
            record.fraud_score,
            record.compliance_status,
            record.decision,
            _json_dumps(record.agent_sessions_completed),
            record.last_event_type,
            record.last_event_at,
            record.human_reviewer_id,
            record.final_decision_at,
        )

    def _extract_application_id(self, event: StoredEvent) -> str | None:
        payload = event.payload
        if "application_id" in payload:
            return str(payload["application_id"])
        if event.stream_id.startswith("loan-"):
            return event.stream_id[len("loan-") :]
        if event.stream_id.startswith("credit-"):
            return event.stream_id[len("credit-") :]
        if event.stream_id.startswith("fraud-"):
            return event.stream_id[len("fraud-") :]
        if event.stream_id.startswith("compliance-"):
            return event.stream_id[len("compliance-") :]
        if event.stream_id.startswith("docpkg-"):
            return event.stream_id[len("docpkg-") :]
        return None

    def _event_timestamp(self, event: StoredEvent, payload_key: str) -> datetime:
        value: Any = event.payload.get(payload_key)
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return event.recorded_at
