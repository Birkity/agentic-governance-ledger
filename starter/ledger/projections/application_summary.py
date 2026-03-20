from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any

from ledger.projections.base import BaseProjection, coerce_datetime


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _compliance_decline(payload: dict[str, Any]) -> bool:
    reasons = [str(reason) for reason in payload.get("decline_reasons", [])]
    codes = [str(code) for code in payload.get("adverse_action_codes", [])]
    return "COMPLIANCE_BLOCK" in codes or any(
        "compliance" in reason.lower() or "reg-" in reason.lower() for reason in reasons
    )


class ApplicationSummaryProjection(BaseProjection):
    projection_name = "application_summary"
    handled_event_types = {
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

    async def _setup_db(self, conn) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS application_summary (
                application_id            TEXT PRIMARY KEY,
                applicant_id              TEXT,
                state                     TEXT NOT NULL,
                requested_amount_usd      NUMERIC,
                approved_amount_usd       NUMERIC,
                risk_tier                 TEXT,
                credit_confidence         DOUBLE PRECISION,
                fraud_score               DOUBLE PRECISION,
                compliance_status         TEXT,
                decision_recommendation   TEXT,
                decision_confidence       DOUBLE PRECISION,
                final_decision            TEXT,
                required_documents        INTEGER NOT NULL DEFAULT 0,
                uploaded_documents        INTEGER NOT NULL DEFAULT 0,
                agent_sessions_completed  INTEGER NOT NULL DEFAULT 0,
                quality_flag_count        INTEGER NOT NULL DEFAULT 0,
                human_reviewer_id         TEXT,
                last_event_type           TEXT,
                created_at                TIMESTAMPTZ,
                updated_at                TIMESTAMPTZ,
                last_event_at             TIMESTAMPTZ,
                final_decision_at         TIMESTAMPTZ
            );
            """
        )

    async def _clear_db(self, conn) -> None:
        await conn.execute("TRUNCATE TABLE application_summary")

    def _blank_row(self, application_id: str) -> dict[str, Any]:
        return {
            "application_id": application_id,
            "applicant_id": None,
            "state": "NEW",
            "requested_amount_usd": None,
            "approved_amount_usd": None,
            "risk_tier": None,
            "credit_confidence": None,
            "fraud_score": None,
            "compliance_status": None,
            "decision_recommendation": None,
            "decision_confidence": None,
            "final_decision": None,
            "required_documents": 0,
            "uploaded_documents": 0,
            "agent_sessions_completed": 0,
            "quality_flag_count": 0,
            "human_reviewer_id": None,
            "last_event_type": None,
            "created_at": None,
            "updated_at": None,
            "last_event_at": None,
            "final_decision_at": None,
        }

    async def _load_db_row(self, conn, application_id: str) -> dict[str, Any]:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM application_summary
            WHERE application_id = $1
            """,
            application_id,
        )
        return dict(row) if row else self._blank_row(application_id)

    async def _save_db_row(self, conn, row: dict[str, Any]) -> None:
        await conn.execute(
            """
            INSERT INTO application_summary (
                application_id,
                applicant_id,
                state,
                requested_amount_usd,
                approved_amount_usd,
                risk_tier,
                credit_confidence,
                fraud_score,
                compliance_status,
                decision_recommendation,
                decision_confidence,
                final_decision,
                required_documents,
                uploaded_documents,
                agent_sessions_completed,
                quality_flag_count,
                human_reviewer_id,
                last_event_type,
                created_at,
                updated_at,
                last_event_at,
                final_decision_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17, $18, $19, $20, $21, $22
            )
            ON CONFLICT (application_id) DO UPDATE SET
                applicant_id = EXCLUDED.applicant_id,
                state = EXCLUDED.state,
                requested_amount_usd = EXCLUDED.requested_amount_usd,
                approved_amount_usd = EXCLUDED.approved_amount_usd,
                risk_tier = EXCLUDED.risk_tier,
                credit_confidence = EXCLUDED.credit_confidence,
                fraud_score = EXCLUDED.fraud_score,
                compliance_status = EXCLUDED.compliance_status,
                decision_recommendation = EXCLUDED.decision_recommendation,
                decision_confidence = EXCLUDED.decision_confidence,
                final_decision = EXCLUDED.final_decision,
                required_documents = EXCLUDED.required_documents,
                uploaded_documents = EXCLUDED.uploaded_documents,
                agent_sessions_completed = EXCLUDED.agent_sessions_completed,
                quality_flag_count = EXCLUDED.quality_flag_count,
                human_reviewer_id = EXCLUDED.human_reviewer_id,
                last_event_type = EXCLUDED.last_event_type,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                last_event_at = EXCLUDED.last_event_at,
                final_decision_at = EXCLUDED.final_decision_at
            """,
            row["application_id"],
            row["applicant_id"],
            row["state"],
            row["requested_amount_usd"],
            row["approved_amount_usd"],
            row["risk_tier"],
            row["credit_confidence"],
            row["fraud_score"],
            row["compliance_status"],
            row["decision_recommendation"],
            row["decision_confidence"],
            row["final_decision"],
            row["required_documents"],
            row["uploaded_documents"],
            row["agent_sessions_completed"],
            row["quality_flag_count"],
            row["human_reviewer_id"],
            row["last_event_type"],
            row["created_at"],
            row["updated_at"],
            row["last_event_at"],
            row["final_decision_at"],
        )

    def _application_id_for_event(self, event: dict[str, Any]) -> str | None:
        payload = event["payload"]
        if "application_id" in payload:
            return str(payload["application_id"])
        if event["stream_id"].startswith("loan-"):
            return event["stream_id"][5:]
        return None

    def _mutate_row(self, row: dict[str, Any], event: dict[str, Any]) -> None:
        payload = event["payload"]
        event_type = event["event_type"]
        recorded_at: datetime | None = event.get("recorded_at")

        row["last_event_type"] = event_type
        row["updated_at"] = recorded_at
        row["last_event_at"] = recorded_at

        if event_type == "ApplicationSubmitted":
            row["applicant_id"] = payload.get("applicant_id")
            row["requested_amount_usd"] = _as_decimal(payload.get("requested_amount_usd"))
            row["state"] = "SUBMITTED"
            row["created_at"] = coerce_datetime(payload.get("submitted_at")) or recorded_at
        elif event_type == "DocumentUploadRequested":
            row["required_documents"] = len(payload.get("required_document_types", []))
            row["state"] = "DOCUMENTS_PENDING"
        elif event_type == "DocumentUploaded":
            row["uploaded_documents"] = int(row["uploaded_documents"]) + 1
            row["state"] = "DOCUMENTS_UPLOADED"
        elif event_type == "PackageReadyForAnalysis":
            row["state"] = "DOCUMENTS_PROCESSED"
            row["quality_flag_count"] = int(payload.get("quality_flag_count", 0))
        elif event_type == "CreditAnalysisRequested":
            row["state"] = "CREDIT_ANALYSIS_REQUESTED"
        elif event_type == "CreditAnalysisCompleted":
            decision = payload.get("decision", {})
            row["risk_tier"] = decision.get("risk_tier")
            confidence = decision.get("confidence")
            row["credit_confidence"] = float(confidence) if confidence is not None else None
            row["state"] = "CREDIT_ANALYSIS_COMPLETE"
        elif event_type == "FraudScreeningRequested":
            row["state"] = "FRAUD_SCREENING_REQUESTED"
        elif event_type == "FraudScreeningCompleted":
            fraud_score = payload.get("fraud_score")
            row["fraud_score"] = float(fraud_score) if fraud_score is not None else None
            row["state"] = "FRAUD_SCREENING_COMPLETE"
        elif event_type == "ComplianceCheckRequested":
            row["state"] = "COMPLIANCE_CHECK_REQUESTED"
        elif event_type == "ComplianceCheckCompleted":
            row["compliance_status"] = payload.get("overall_verdict")
            row["state"] = "COMPLIANCE_CHECK_COMPLETE"
        elif event_type == "DecisionRequested":
            row["state"] = "PENDING_DECISION"
        elif event_type == "DecisionGenerated":
            row["decision_recommendation"] = payload.get("recommendation")
            confidence = payload.get("confidence")
            row["decision_confidence"] = float(confidence) if confidence is not None else None
            row["approved_amount_usd"] = _as_decimal(payload.get("approved_amount_usd"))
            if payload.get("recommendation") == "REFER":
                row["state"] = "REFERRED"
        elif event_type == "HumanReviewRequested":
            row["state"] = "PENDING_HUMAN_REVIEW"
        elif event_type == "HumanReviewCompleted":
            row["human_reviewer_id"] = payload.get("reviewer_id")
        elif event_type == "ApplicationApproved":
            row["approved_amount_usd"] = _as_decimal(payload.get("approved_amount_usd"))
            row["final_decision"] = "APPROVE"
            row["state"] = "APPROVED"
            row["final_decision_at"] = coerce_datetime(payload.get("approved_at")) or recorded_at
        elif event_type == "ApplicationDeclined":
            row["final_decision"] = "DECLINE"
            row["state"] = (
                "DECLINED_COMPLIANCE" if _compliance_decline(payload) else "DECLINED"
            )
            row["final_decision_at"] = coerce_datetime(payload.get("declined_at")) or recorded_at
        elif event_type == "AgentSessionCompleted":
            row["agent_sessions_completed"] = int(row["agent_sessions_completed"]) + 1

    async def _apply_db(self, conn, event: dict[str, Any]) -> None:
        application_id = self._application_id_for_event(event)
        if application_id is None:
            return
        row = await self._load_db_row(conn, application_id)
        self._mutate_row(row, event)
        await self._save_db_row(conn, row)

    async def _apply_memory(self, bucket: dict[str, Any], event: dict[str, Any]) -> None:
        rows = bucket.setdefault("rows", {})
        application_id = self._application_id_for_event(event)
        if application_id is None:
            return
        row = dict(rows.get(application_id, self._blank_row(application_id)))
        self._mutate_row(row, event)
        rows[application_id] = row

    async def get(self, application_id: str) -> dict[str, Any] | None:
        await self.setup()
        if self.uses_database:
            pool = self.store._require_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM application_summary
                    WHERE application_id = $1
                    """,
                    application_id,
                )
                return dict(row) if row else None

        return self._memory_bucket().setdefault("rows", {}).get(application_id)

    async def list_all(self) -> list[dict[str, Any]]:
        await self.setup()
        if self.uses_database:
            pool = self.store._require_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM application_summary
                    ORDER BY application_id
                    """
                )
                return [dict(row) for row in rows]

        rows = self._memory_bucket().setdefault("rows", {})
        return [dict(rows[key]) for key in sorted(rows)]

    async def count_by_state(self) -> dict[str, int]:
        rows = await self.list_all()
        return dict(Counter(str(row["state"]) for row in rows))
