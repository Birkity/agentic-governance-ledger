from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from src.models.events import StoredEvent
from src.projections.base import Projection, _json_dumps, _json_loads


@dataclass
class ComplianceAuditState:
    application_id: str
    session_id: str | None = None
    regulation_set_version: str | None = None
    required_rules: list[str] = field(default_factory=list)
    passed_rules: list[dict[str, Any]] = field(default_factory=list)
    failed_rules: list[dict[str, Any]] = field(default_factory=list)
    noted_rules: list[dict[str, Any]] = field(default_factory=list)
    hard_block_rules: list[str] = field(default_factory=list)
    completed: bool = False
    overall_verdict: str | None = None
    last_event_type: str | None = None
    as_of: datetime | None = None
    checkpoint_position: int = 0
    snapshot_version: int = 0


class ComplianceAuditProjection(Projection):
    name = "compliance_audit"

    def __init__(self, store) -> None:
        super().__init__(store)
        self._current: dict[str, ComplianceAuditState] = {}
        self._history: dict[str, list[ComplianceAuditState]] = {}

    def handles(self, event: StoredEvent) -> bool:
        return event.event_type in {
            "ComplianceCheckInitiated",
            "ComplianceRulePassed",
            "ComplianceRuleFailed",
            "ComplianceRuleNoted",
            "ComplianceCheckCompleted",
        }

    async def reset(self) -> None:
        self._current.clear()
        self._history.clear()
        await self._execute("TRUNCATE TABLE compliance_audit_history")
        await self._execute("TRUNCATE TABLE compliance_audit_current")

    async def get_current_compliance(self, application_id: str) -> ComplianceAuditState | None:
        state = self._current.get(application_id)
        if state is not None:
            return state

        row = await self._fetchrow(
            """
            SELECT application_id, session_id, regulation_set_version, required_rules, passed_rules,
                   failed_rules, noted_rules, hard_block_rules, completed, overall_verdict,
                   last_event_type, as_of, checkpoint_position, snapshot_version
            FROM compliance_audit_current
            WHERE application_id = $1
            """,
            application_id,
        )
        if row is None:
            return None
        state = self._row_to_state(row)
        self._current[application_id] = state
        return state

    async def get_compliance_at(self, application_id: str, timestamp: datetime) -> ComplianceAuditState | None:
        history = self._history.get(application_id)
        if history:
            eligible = [snapshot for snapshot in history if snapshot.as_of and snapshot.as_of <= timestamp]
            return eligible[-1] if eligible else None

        row = await self._fetchrow(
            """
            SELECT state
            FROM compliance_audit_history
            WHERE application_id = $1 AND as_of <= $2
            ORDER BY as_of DESC, snapshot_version DESC
            LIMIT 1
            """,
            application_id,
            timestamp,
        )
        if row is None:
            return None
        state = self._state_from_payload(_json_loads(row["state"], {}))
        return state

    async def list_current(self) -> list[ComplianceAuditState]:
        if self._pool is None:
            return list(self._current.values())

        rows = await self._fetch(
            """
            SELECT application_id, session_id, regulation_set_version, required_rules, passed_rules,
                   failed_rules, noted_rules, hard_block_rules, completed, overall_verdict,
                   last_event_type, as_of, checkpoint_position, snapshot_version
            FROM compliance_audit_current
            ORDER BY application_id
            """
        )
        return [self._row_to_state(row) for row in rows]

    async def apply(self, event: StoredEvent) -> None:
        application_id = str(event.payload["application_id"])
        state = await self.get_current_compliance(application_id)
        if state is None:
            state = ComplianceAuditState(application_id=application_id)
            self._current[application_id] = state

        payload = event.payload
        if event.event_type == "ComplianceCheckInitiated":
            state.session_id = payload["session_id"]
            state.regulation_set_version = payload["regulation_set_version"]
            state.required_rules = list(payload.get("rules_to_evaluate", []))
            state.passed_rules = []
            state.failed_rules = []
            state.noted_rules = []
            state.hard_block_rules = []
            state.completed = False
            state.overall_verdict = None
        elif event.event_type == "ComplianceRulePassed":
            state.passed_rules.append(
                {
                    "rule_id": payload["rule_id"],
                    "rule_name": payload["rule_name"],
                    "rule_version": payload["rule_version"],
                    "evidence_hash": payload["evidence_hash"],
                    "evaluation_notes": payload["evaluation_notes"],
                    "evaluated_at": payload["evaluated_at"],
                }
            )
        elif event.event_type == "ComplianceRuleFailed":
            state.failed_rules.append(
                {
                    "rule_id": payload["rule_id"],
                    "rule_name": payload["rule_name"],
                    "rule_version": payload["rule_version"],
                    "failure_reason": payload["failure_reason"],
                    "is_hard_block": bool(payload["is_hard_block"]),
                    "remediation_available": bool(payload["remediation_available"]),
                    "remediation_description": payload.get("remediation_description"),
                    "evidence_hash": payload["evidence_hash"],
                    "evaluated_at": payload["evaluated_at"],
                }
            )
            if bool(payload["is_hard_block"]) and payload["rule_id"] not in state.hard_block_rules:
                state.hard_block_rules.append(payload["rule_id"])
        elif event.event_type == "ComplianceRuleNoted":
            state.noted_rules.append(
                {
                    "rule_id": payload["rule_id"],
                    "rule_name": payload["rule_name"],
                    "note_type": payload["note_type"],
                    "note_text": payload["note_text"],
                    "evaluated_at": payload["evaluated_at"],
                }
            )
        elif event.event_type == "ComplianceCheckCompleted":
            state.completed = True
            state.overall_verdict = payload["overall_verdict"]
            if bool(payload["has_hard_block"]) and not state.hard_block_rules:
                state.hard_block_rules = [rule["rule_id"] for rule in state.failed_rules]

        state.last_event_type = event.event_type
        state.as_of = event.recorded_at
        global_position = event.global_position if event.global_position is not None else -1
        state.checkpoint_position = global_position + 1
        state.snapshot_version += 1

        snapshot = self._clone_state(state)
        self._current[application_id] = snapshot
        self._history.setdefault(application_id, []).append(self._clone_state(snapshot))
        await self._persist(snapshot, event)

    def get_projection_lag(self, latest_store_position: int | None, latest_store_recorded_at: datetime | None) -> int:
        if latest_store_position is None or latest_store_recorded_at is None:
            return 0
        if self.last_processed_at is None:
            return 0
        return max(0, int((latest_store_recorded_at - self.last_processed_at).total_seconds() * 1000))

    async def _persist(self, state: ComplianceAuditState, event: StoredEvent) -> None:
        payload = asdict(state)
        await self._execute(
            """
            INSERT INTO compliance_audit_current(
                application_id, session_id, regulation_set_version, required_rules, passed_rules,
                failed_rules, noted_rules, hard_block_rules, completed, overall_verdict,
                last_event_type, as_of, checkpoint_position, snapshot_version, updated_at
            )
            VALUES($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, $11, $12, $13, $14, NOW())
            ON CONFLICT (application_id)
            DO UPDATE SET
                session_id = EXCLUDED.session_id,
                regulation_set_version = EXCLUDED.regulation_set_version,
                required_rules = EXCLUDED.required_rules,
                passed_rules = EXCLUDED.passed_rules,
                failed_rules = EXCLUDED.failed_rules,
                noted_rules = EXCLUDED.noted_rules,
                hard_block_rules = EXCLUDED.hard_block_rules,
                completed = EXCLUDED.completed,
                overall_verdict = EXCLUDED.overall_verdict,
                last_event_type = EXCLUDED.last_event_type,
                as_of = EXCLUDED.as_of,
                checkpoint_position = EXCLUDED.checkpoint_position,
                snapshot_version = EXCLUDED.snapshot_version,
                updated_at = NOW()
            """,
            state.application_id,
            state.session_id,
            state.regulation_set_version,
            _json_dumps(state.required_rules),
            _json_dumps(state.passed_rules),
            _json_dumps(state.failed_rules),
            _json_dumps(state.noted_rules),
            _json_dumps(state.hard_block_rules),
            state.completed,
            state.overall_verdict,
            state.last_event_type,
            state.as_of,
            state.checkpoint_position,
            state.snapshot_version,
        )
        await self._execute(
            """
            INSERT INTO compliance_audit_history(
                application_id, snapshot_version, as_of, global_position, event_type, state
            )
            VALUES($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (application_id, snapshot_version)
            DO UPDATE SET
                as_of = EXCLUDED.as_of,
                global_position = EXCLUDED.global_position,
                event_type = EXCLUDED.event_type,
                state = EXCLUDED.state
            """,
            state.application_id,
            state.snapshot_version,
            state.as_of,
            event.global_position if event.global_position is not None else -1,
            event.event_type,
            _json_dumps(payload),
        )

    def _clone_state(self, state: ComplianceAuditState) -> ComplianceAuditState:
        return ComplianceAuditState(
            application_id=state.application_id,
            session_id=state.session_id,
            regulation_set_version=state.regulation_set_version,
            required_rules=list(state.required_rules),
            passed_rules=[dict(item) for item in state.passed_rules],
            failed_rules=[dict(item) for item in state.failed_rules],
            noted_rules=[dict(item) for item in state.noted_rules],
            hard_block_rules=list(state.hard_block_rules),
            completed=state.completed,
            overall_verdict=state.overall_verdict,
            last_event_type=state.last_event_type,
            as_of=state.as_of,
            checkpoint_position=state.checkpoint_position,
            snapshot_version=state.snapshot_version,
        )

    def _row_to_state(self, row) -> ComplianceAuditState:
        return ComplianceAuditState(
            application_id=row["application_id"],
            session_id=row["session_id"],
            regulation_set_version=row["regulation_set_version"],
            required_rules=list(_json_loads(row["required_rules"], [])),
            passed_rules=list(_json_loads(row["passed_rules"], [])),
            failed_rules=list(_json_loads(row["failed_rules"], [])),
            noted_rules=list(_json_loads(row["noted_rules"], [])),
            hard_block_rules=list(_json_loads(row["hard_block_rules"], [])),
            completed=bool(row["completed"]),
            overall_verdict=row["overall_verdict"],
            last_event_type=row["last_event_type"],
            as_of=row["as_of"],
            checkpoint_position=int(row["checkpoint_position"]),
            snapshot_version=int(row["snapshot_version"]),
        )

    def _state_from_payload(self, payload: dict[str, Any]) -> ComplianceAuditState:
        return ComplianceAuditState(
            application_id=payload["application_id"],
            session_id=payload.get("session_id"),
            regulation_set_version=payload.get("regulation_set_version"),
            required_rules=list(payload.get("required_rules", [])),
            passed_rules=list(payload.get("passed_rules", [])),
            failed_rules=list(payload.get("failed_rules", [])),
            noted_rules=list(payload.get("noted_rules", [])),
            hard_block_rules=list(payload.get("hard_block_rules", [])),
            completed=bool(payload.get("completed", False)),
            overall_verdict=payload.get("overall_verdict"),
            last_event_type=payload.get("last_event_type"),
            as_of=self._parse_datetime(payload.get("as_of")),
            checkpoint_position=int(payload.get("checkpoint_position", 0)),
            snapshot_version=int(payload.get("snapshot_version", 0)),
        )

    def _parse_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return None

