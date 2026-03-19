from __future__ import annotations

from datetime import datetime
from typing import Any

from ledger.projections.base import BaseProjection, to_jsonable


def _coerce_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _is_compliance_decline(payload: dict[str, Any]) -> bool:
    reasons = [str(reason) for reason in payload.get("decline_reasons", [])]
    codes = [str(code) for code in payload.get("adverse_action_codes", [])]
    return "COMPLIANCE_BLOCK" in codes or any(
        "compliance" in reason.lower() or "reg-" in reason.lower() for reason in reasons
    )


class ComplianceAuditProjection(BaseProjection):
    projection_name = "compliance_audit"
    handled_event_types = {
        "ComplianceCheckInitiated",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "ComplianceRuleNoted",
        "ComplianceCheckCompleted",
        "ApplicationDeclined",
    }

    async def _setup_db(self, conn) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS compliance_audit_current (
                application_id             TEXT PRIMARY KEY,
                session_id                 TEXT,
                regulation_set_version     TEXT,
                rules_to_evaluate          JSONB NOT NULL DEFAULT '[]'::jsonb,
                rules_evaluated            INTEGER NOT NULL DEFAULT 0,
                rules_passed               INTEGER NOT NULL DEFAULT 0,
                rules_failed               INTEGER NOT NULL DEFAULT 0,
                rules_noted                INTEGER NOT NULL DEFAULT 0,
                passed_rules               JSONB NOT NULL DEFAULT '[]'::jsonb,
                failed_rules               JSONB NOT NULL DEFAULT '[]'::jsonb,
                noted_rules                JSONB NOT NULL DEFAULT '[]'::jsonb,
                has_hard_block             BOOLEAN NOT NULL DEFAULT FALSE,
                overall_verdict            TEXT,
                declined_due_to_compliance BOOLEAN NOT NULL DEFAULT FALSE,
                last_event_type            TEXT,
                last_event_at              TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS compliance_audit_history (
                application_id   TEXT NOT NULL,
                global_position  BIGINT NOT NULL,
                recorded_at      TIMESTAMPTZ NOT NULL,
                state            JSONB NOT NULL,
                PRIMARY KEY (application_id, global_position)
            );
            CREATE INDEX IF NOT EXISTS idx_compliance_audit_history_lookup
                ON compliance_audit_history (application_id, recorded_at DESC, global_position DESC);
            """
        )

    async def _clear_db(self, conn) -> None:
        await conn.execute("TRUNCATE TABLE compliance_audit_current")
        await conn.execute("TRUNCATE TABLE compliance_audit_history")

    def _blank_state(self, application_id: str) -> dict[str, Any]:
        return {
            "application_id": application_id,
            "session_id": None,
            "regulation_set_version": None,
            "rules_to_evaluate": [],
            "rules_evaluated": 0,
            "rules_passed": 0,
            "rules_failed": 0,
            "rules_noted": 0,
            "passed_rules": [],
            "failed_rules": [],
            "noted_rules": [],
            "has_hard_block": False,
            "overall_verdict": None,
            "declined_due_to_compliance": False,
            "last_event_type": None,
            "last_event_at": None,
        }

    async def _load_db_state(self, conn, application_id: str) -> dict[str, Any]:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM compliance_audit_current
            WHERE application_id = $1
            """,
            application_id,
        )
        return dict(row) if row else self._blank_state(application_id)

    async def _save_db_state(self, conn, state: dict[str, Any]) -> None:
        await conn.execute(
            """
            INSERT INTO compliance_audit_current (
                application_id,
                session_id,
                regulation_set_version,
                rules_to_evaluate,
                rules_evaluated,
                rules_passed,
                rules_failed,
                rules_noted,
                passed_rules,
                failed_rules,
                noted_rules,
                has_hard_block,
                overall_verdict,
                declined_due_to_compliance,
                last_event_type,
                last_event_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10, $11, $12, $13, $14, $15, $16
            )
            ON CONFLICT (application_id) DO UPDATE SET
                session_id = EXCLUDED.session_id,
                regulation_set_version = EXCLUDED.regulation_set_version,
                rules_to_evaluate = EXCLUDED.rules_to_evaluate,
                rules_evaluated = EXCLUDED.rules_evaluated,
                rules_passed = EXCLUDED.rules_passed,
                rules_failed = EXCLUDED.rules_failed,
                rules_noted = EXCLUDED.rules_noted,
                passed_rules = EXCLUDED.passed_rules,
                failed_rules = EXCLUDED.failed_rules,
                noted_rules = EXCLUDED.noted_rules,
                has_hard_block = EXCLUDED.has_hard_block,
                overall_verdict = EXCLUDED.overall_verdict,
                declined_due_to_compliance = EXCLUDED.declined_due_to_compliance,
                last_event_type = EXCLUDED.last_event_type,
                last_event_at = EXCLUDED.last_event_at
            """,
            state["application_id"],
            state["session_id"],
            state["regulation_set_version"],
            to_jsonable(state["rules_to_evaluate"]),
            state["rules_evaluated"],
            state["rules_passed"],
            state["rules_failed"],
            state["rules_noted"],
            to_jsonable(state["passed_rules"]),
            to_jsonable(state["failed_rules"]),
            to_jsonable(state["noted_rules"]),
            state["has_hard_block"],
            state["overall_verdict"],
            state["declined_due_to_compliance"],
            state["last_event_type"],
            state["last_event_at"],
        )

    def _mutate_state(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        payload = event["payload"]
        event_type = event["event_type"]
        recorded_at = event.get("recorded_at")
        state["last_event_type"] = event_type
        state["last_event_at"] = recorded_at

        if event_type == "ComplianceCheckInitiated":
            state["session_id"] = payload.get("session_id")
            state["regulation_set_version"] = payload.get("regulation_set_version")
            state["rules_to_evaluate"] = list(payload.get("rules_to_evaluate", []))
            state["rules_evaluated"] = 0
            state["rules_passed"] = 0
            state["rules_failed"] = 0
            state["rules_noted"] = 0
            state["passed_rules"] = []
            state["failed_rules"] = []
            state["noted_rules"] = []
            state["has_hard_block"] = False
            state["overall_verdict"] = None
        elif event_type == "ComplianceRulePassed":
            state["passed_rules"] = [
                *state["passed_rules"],
                {
                    "rule_id": payload.get("rule_id"),
                    "rule_name": payload.get("rule_name"),
                    "rule_version": payload.get("rule_version"),
                    "evaluation_notes": payload.get("evaluation_notes"),
                    "evidence_hash": payload.get("evidence_hash"),
                    "evaluated_at": payload.get("evaluated_at") or recorded_at,
                },
            ]
            state["rules_passed"] = int(state["rules_passed"]) + 1
            state["rules_evaluated"] = int(state["rules_evaluated"]) + 1
        elif event_type == "ComplianceRuleFailed":
            is_hard_block = bool(payload.get("is_hard_block"))
            state["failed_rules"] = [
                *state["failed_rules"],
                {
                    "rule_id": payload.get("rule_id"),
                    "rule_name": payload.get("rule_name"),
                    "rule_version": payload.get("rule_version"),
                    "failure_reason": payload.get("failure_reason"),
                    "is_hard_block": is_hard_block,
                    "remediation_available": bool(payload.get("remediation_available")),
                    "remediation_description": payload.get("remediation_description"),
                    "evidence_hash": payload.get("evidence_hash"),
                    "evaluated_at": payload.get("evaluated_at") or recorded_at,
                },
            ]
            state["rules_failed"] = int(state["rules_failed"]) + 1
            state["rules_evaluated"] = int(state["rules_evaluated"]) + 1
            state["has_hard_block"] = bool(state["has_hard_block"]) or is_hard_block
        elif event_type == "ComplianceRuleNoted":
            state["noted_rules"] = [
                *state["noted_rules"],
                {
                    "rule_id": payload.get("rule_id"),
                    "rule_name": payload.get("rule_name"),
                    "note_type": payload.get("note_type"),
                    "note_text": payload.get("note_text"),
                    "evaluated_at": payload.get("evaluated_at") or recorded_at,
                },
            ]
            state["rules_noted"] = int(state["rules_noted"]) + 1
            state["rules_evaluated"] = int(state["rules_evaluated"]) + 1
        elif event_type == "ComplianceCheckCompleted":
            state["session_id"] = payload.get("session_id") or state["session_id"]
            state["rules_evaluated"] = int(payload.get("rules_evaluated", state["rules_evaluated"]))
            state["rules_passed"] = int(payload.get("rules_passed", state["rules_passed"]))
            state["rules_failed"] = int(payload.get("rules_failed", state["rules_failed"]))
            state["rules_noted"] = int(payload.get("rules_noted", state["rules_noted"]))
            state["has_hard_block"] = bool(payload.get("has_hard_block"))
            state["overall_verdict"] = payload.get("overall_verdict")
        elif event_type == "ApplicationDeclined" and _is_compliance_decline(payload):
            state["declined_due_to_compliance"] = True
            if state["overall_verdict"] is None:
                state["overall_verdict"] = "BLOCKED"
            state["has_hard_block"] = True

    async def _write_db_snapshot(self, conn, state: dict[str, Any], event: dict[str, Any]) -> None:
        await conn.execute(
            """
            INSERT INTO compliance_audit_history (
                application_id,
                global_position,
                recorded_at,
                state
            )
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (application_id, global_position) DO UPDATE SET
                recorded_at = EXCLUDED.recorded_at,
                state = EXCLUDED.state
            """,
            state["application_id"],
            int(event["global_position"]),
            event.get("recorded_at"),
            to_jsonable(state),
        )

    async def _apply_db(self, conn, event: dict[str, Any]) -> None:
        payload = event["payload"]
        application_id = str(payload.get("application_id") or "")
        if not application_id:
            return
        state = await self._load_db_state(conn, application_id)
        self._mutate_state(state, event)
        await self._save_db_state(conn, state)
        await self._write_db_snapshot(conn, state, event)

    async def _apply_memory(self, bucket: dict[str, Any], event: dict[str, Any]) -> None:
        payload = event["payload"]
        application_id = str(payload.get("application_id") or "")
        if not application_id:
            return
        current = bucket.setdefault("current", {})
        history = bucket.setdefault("history", {})
        state = dict(current.get(application_id, self._blank_state(application_id)))
        self._mutate_state(state, event)
        current[application_id] = state
        history.setdefault(application_id, []).append(
            {
                "global_position": int(event["global_position"]),
                "recorded_at": event.get("recorded_at"),
                "state": to_jsonable(state),
            }
        )

    async def get(self, application_id: str) -> dict[str, Any] | None:
        await self.setup()
        if self.uses_database:
            pool = self.store._require_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM compliance_audit_current
                    WHERE application_id = $1
                    """,
                    application_id,
                )
                return dict(row) if row else None

        return self._memory_bucket().setdefault("current", {}).get(application_id)

    async def get_compliance_at(
        self,
        application_id: str,
        timestamp: datetime | str,
    ) -> dict[str, Any] | None:
        as_of = _coerce_timestamp(timestamp)
        await self.setup()
        if self.uses_database:
            pool = self.store._require_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT state
                    FROM compliance_audit_history
                    WHERE application_id = $1
                      AND recorded_at <= $2
                    ORDER BY recorded_at DESC, global_position DESC
                    LIMIT 1
                    """,
                    application_id,
                    as_of,
                )
                return dict(row["state"]) if row else None

        history = self._memory_bucket().setdefault("history", {}).get(application_id, [])
        candidate = None
        for item in history:
            recorded_at = item["recorded_at"]
            if recorded_at is not None and recorded_at <= as_of:
                candidate = item
        return dict(candidate["state"]) if candidate else None
