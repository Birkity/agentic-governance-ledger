from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.models.events import AGENT_SESSION_ANCHOR_EVENT_TYPES, StoredEvent
from src.projections.base import Projection


@dataclass(frozen=True)
class AgentSessionIndex:
    session_id: str
    application_id: str
    agent_type: str
    agent_id: str
    model_version: str
    stream_id: str
    started_at: datetime


@dataclass
class AgentPerformanceRecord:
    agent_type: str
    agent_id: str
    model_version: str
    total_sessions: int = 0
    analyses_completed: int = 0
    decisions_generated: int = 0
    avg_confidence_score: float | None = None
    avg_duration_ms: float | None = None
    approve_count: int = 0
    decline_count: int = 0
    refer_count: int = 0
    human_override_count: int = 0
    approve_rate: float = 0.0
    decline_rate: float = 0.0
    refer_rate: float = 0.0
    human_override_rate: float = 0.0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    confidence_samples: int = 0
    duration_samples: int = 0


class AgentPerformanceProjection(Projection):
    name = "agent_performance"

    def __init__(self, store) -> None:
        super().__init__(store)
        self._records: dict[tuple[str, str, str], AgentPerformanceRecord] = {}
        self._sessions: dict[str, AgentSessionIndex] = {}
        self._application_decision_keys: dict[str, tuple[str, str, str]] = {}

    def handles(self, event: StoredEvent) -> bool:
        return event.event_type in {
            *AGENT_SESSION_ANCHOR_EVENT_TYPES,
            "CreditAnalysisCompleted",
            "FraudScreeningCompleted",
            "ComplianceCheckCompleted",
            "DecisionGenerated",
            "HumanReviewCompleted",
        }

    async def reset(self) -> None:
        self._records.clear()
        self._sessions.clear()
        self._application_decision_keys.clear()
        await self._execute("TRUNCATE TABLE agent_performance_ledger")
        await self._execute("TRUNCATE TABLE agent_session_projection_index")
        await self._execute("TRUNCATE TABLE application_decision_projection_index")

    async def list_all(self) -> list[AgentPerformanceRecord]:
        if self._pool is None:
            return list(self._records.values())

        rows = await self._fetch(
            """
            SELECT agent_type, agent_id, model_version, total_sessions, analyses_completed,
                   decisions_generated, avg_confidence_score, avg_duration_ms, approve_count,
                   decline_count, refer_count, human_override_count, approve_rate,
                   decline_rate, refer_rate, human_override_rate, confidence_samples,
                   duration_samples, first_seen_at, last_seen_at
            FROM agent_performance_ledger
            ORDER BY agent_type, agent_id, model_version
            """
        )
        return [
            AgentPerformanceRecord(
                agent_type=row["agent_type"],
                agent_id=row["agent_id"],
                model_version=row["model_version"],
                total_sessions=row["total_sessions"],
                analyses_completed=row["analyses_completed"],
                decisions_generated=row["decisions_generated"],
                avg_confidence_score=row["avg_confidence_score"],
                avg_duration_ms=row["avg_duration_ms"],
                approve_count=row["approve_count"],
                decline_count=row["decline_count"],
                refer_count=row["refer_count"],
                human_override_count=row["human_override_count"],
                approve_rate=row["approve_rate"],
                decline_rate=row["decline_rate"],
                refer_rate=row["refer_rate"],
                human_override_rate=row["human_override_rate"],
                confidence_samples=row["confidence_samples"],
                duration_samples=row["duration_samples"],
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
            )
            for row in rows
        ]

    async def apply(self, event: StoredEvent) -> None:
        if event.event_type in AGENT_SESSION_ANCHOR_EVENT_TYPES:
            await self._handle_agent_session_started(event)
            return

        if event.event_type == "HumanReviewCompleted":
            await self._handle_human_review_completed(event)
            return

        session_id = str(
            event.payload.get("session_id")
            or event.payload.get("orchestrator_session_id")
            or ""
        )
        if not session_id:
            return

        session = await self._get_session(session_id)
        if session is None:
            return

        key = (session.agent_type, session.agent_id, session.model_version)
        record = await self._get_record(*key)
        if record is None:
            record = AgentPerformanceRecord(
                agent_type=session.agent_type,
                agent_id=session.agent_id,
                model_version=session.model_version,
            )
            self._records[key] = record

        record.last_seen_at = event.recorded_at
        if record.first_seen_at is None:
            record.first_seen_at = session.started_at

        if event.event_type == "CreditAnalysisCompleted":
            record.analyses_completed += 1
            self._apply_confidence_sample(record, float(event.payload["decision"]["confidence"]))
            self._apply_duration_sample(record, float(event.payload["analysis_duration_ms"]))
        elif event.event_type == "FraudScreeningCompleted":
            record.analyses_completed += 1
        elif event.event_type == "ComplianceCheckCompleted":
            record.analyses_completed += 1
        elif event.event_type == "DecisionGenerated":
            record.decisions_generated += 1
            self._apply_confidence_sample(record, float(event.payload["confidence"]))
            recommendation = str(event.payload["recommendation"]).upper()
            if recommendation == "APPROVE":
                record.approve_count += 1
            elif recommendation == "DECLINE":
                record.decline_count += 1
            elif recommendation == "REFER":
                record.refer_count += 1
            self._application_decision_keys[str(event.payload["application_id"])] = key
            await self._persist_decision_index(
                application_id=str(event.payload["application_id"]),
                session_id=session.session_id,
                agent_type=session.agent_type,
                agent_id=session.agent_id,
                model_version=session.model_version,
                updated_at=event.recorded_at,
            )

        self._recalculate_rates(record)
        await self._persist_record(record)

    async def _handle_agent_session_started(self, event: StoredEvent) -> None:
        payload = event.payload
        session = AgentSessionIndex(
            session_id=str(payload["session_id"]),
            application_id=str(payload["application_id"]),
            agent_type=str(payload["agent_type"]),
            agent_id=str(payload["agent_id"]),
            model_version=str(payload["model_version"]),
            stream_id=event.stream_id,
            started_at=event.recorded_at,
        )
        self._sessions[session.session_id] = session
        await self._persist_session(session)

        key = (session.agent_type, session.agent_id, session.model_version)
        record = await self._get_record(*key)
        if record is None:
            record = AgentPerformanceRecord(
                agent_type=session.agent_type,
                agent_id=session.agent_id,
                model_version=session.model_version,
            )
            self._records[key] = record

        record.total_sessions += 1
        record.first_seen_at = session.started_at if record.first_seen_at is None else min(record.first_seen_at, session.started_at)
        record.last_seen_at = session.started_at if record.last_seen_at is None else max(record.last_seen_at, session.started_at)
        self._recalculate_rates(record)
        await self._persist_record(record)

    async def _handle_human_review_completed(self, event: StoredEvent) -> None:
        if not bool(event.payload.get("override")):
            return

        application_id = str(event.payload["application_id"])
        key = self._application_decision_keys.get(application_id)
        if key is None:
            key = await self._get_decision_key(application_id)
        if key is None:
            return

        record = await self._get_record(*key)
        if record is None:
            return

        record.human_override_count += 1
        record.last_seen_at = event.recorded_at
        self._recalculate_rates(record)
        await self._persist_record(record)

    async def _get_record(self, agent_type: str, agent_id: str, model_version: str) -> AgentPerformanceRecord | None:
        key = (agent_type, agent_id, model_version)
        record = self._records.get(key)
        if record is not None:
            return record

        row = await self._fetchrow(
            """
            SELECT agent_type, agent_id, model_version, total_sessions, analyses_completed,
                   decisions_generated, avg_confidence_score, avg_duration_ms, approve_count,
                   decline_count, refer_count, human_override_count, approve_rate,
                   decline_rate, refer_rate, human_override_rate, confidence_samples,
                   duration_samples, first_seen_at, last_seen_at
            FROM agent_performance_ledger
            WHERE agent_type = $1 AND agent_id = $2 AND model_version = $3
            """,
            agent_type,
            agent_id,
            model_version,
        )
        if row is None:
            return None

        record = AgentPerformanceRecord(
            agent_type=row["agent_type"],
            agent_id=row["agent_id"],
            model_version=row["model_version"],
            total_sessions=row["total_sessions"],
            analyses_completed=row["analyses_completed"],
            decisions_generated=row["decisions_generated"],
            avg_confidence_score=row["avg_confidence_score"],
            avg_duration_ms=row["avg_duration_ms"],
            approve_count=row["approve_count"],
            decline_count=row["decline_count"],
            refer_count=row["refer_count"],
            human_override_count=row["human_override_count"],
            approve_rate=row["approve_rate"],
            decline_rate=row["decline_rate"],
            refer_rate=row["refer_rate"],
            human_override_rate=row["human_override_rate"],
            confidence_samples=row["confidence_samples"],
            duration_samples=row["duration_samples"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )
        self._records[key] = record
        return record

    async def _get_session(self, session_id: str) -> AgentSessionIndex | None:
        session = self._sessions.get(session_id)
        if session is not None:
            return session

        row = await self._fetchrow(
            """
            SELECT session_id, application_id, agent_type, agent_id, model_version, stream_id, started_at
            FROM agent_session_projection_index
            WHERE session_id = $1
            """,
            session_id,
        )
        if row is None:
            return None

        session = AgentSessionIndex(
            session_id=row["session_id"],
            application_id=row["application_id"],
            agent_type=row["agent_type"],
            agent_id=row["agent_id"],
            model_version=row["model_version"],
            stream_id=row["stream_id"],
            started_at=row["started_at"],
        )
        self._sessions[session_id] = session
        return session

    async def _get_decision_key(self, application_id: str) -> tuple[str, str, str] | None:
        row = await self._fetchrow(
            """
            SELECT agent_type, agent_id, model_version
            FROM application_decision_projection_index
            WHERE application_id = $1
            """,
            application_id,
        )
        if row is None:
            return None
        key = (row["agent_type"], row["agent_id"], row["model_version"])
        self._application_decision_keys[application_id] = key
        return key

    async def _persist_record(self, record: AgentPerformanceRecord) -> None:
        await self._execute(
            """
            INSERT INTO agent_performance_ledger(
                agent_type, agent_id, model_version, total_sessions, analyses_completed,
                decisions_generated, avg_confidence_score, avg_duration_ms, approve_count,
                decline_count, refer_count, human_override_count, approve_rate,
                decline_rate, refer_rate, human_override_rate, confidence_samples,
                duration_samples, first_seen_at, last_seen_at, last_updated_at
            )
            VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, NOW())
            ON CONFLICT (agent_type, agent_id, model_version)
            DO UPDATE SET
                total_sessions = EXCLUDED.total_sessions,
                analyses_completed = EXCLUDED.analyses_completed,
                decisions_generated = EXCLUDED.decisions_generated,
                avg_confidence_score = EXCLUDED.avg_confidence_score,
                avg_duration_ms = EXCLUDED.avg_duration_ms,
                approve_count = EXCLUDED.approve_count,
                decline_count = EXCLUDED.decline_count,
                refer_count = EXCLUDED.refer_count,
                human_override_count = EXCLUDED.human_override_count,
                approve_rate = EXCLUDED.approve_rate,
                decline_rate = EXCLUDED.decline_rate,
                refer_rate = EXCLUDED.refer_rate,
                human_override_rate = EXCLUDED.human_override_rate,
                confidence_samples = EXCLUDED.confidence_samples,
                duration_samples = EXCLUDED.duration_samples,
                first_seen_at = EXCLUDED.first_seen_at,
                last_seen_at = EXCLUDED.last_seen_at,
                last_updated_at = NOW()
            """,
            record.agent_type,
            record.agent_id,
            record.model_version,
            record.total_sessions,
            record.analyses_completed,
            record.decisions_generated,
            record.avg_confidence_score,
            record.avg_duration_ms,
            record.approve_count,
            record.decline_count,
            record.refer_count,
            record.human_override_count,
            record.approve_rate,
            record.decline_rate,
            record.refer_rate,
            record.human_override_rate,
            record.confidence_samples,
            record.duration_samples,
            record.first_seen_at,
            record.last_seen_at,
        )

    async def _persist_session(self, session: AgentSessionIndex) -> None:
        await self._execute(
            """
            INSERT INTO agent_session_projection_index(
                session_id, application_id, agent_type, agent_id, model_version, stream_id, started_at
            )
            VALUES($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (session_id)
            DO UPDATE SET
                application_id = EXCLUDED.application_id,
                agent_type = EXCLUDED.agent_type,
                agent_id = EXCLUDED.agent_id,
                model_version = EXCLUDED.model_version,
                stream_id = EXCLUDED.stream_id,
                started_at = EXCLUDED.started_at
            """,
            session.session_id,
            session.application_id,
            session.agent_type,
            session.agent_id,
            session.model_version,
            session.stream_id,
            session.started_at,
        )

    async def _persist_decision_index(
        self,
        *,
        application_id: str,
        session_id: str,
        agent_type: str,
        agent_id: str,
        model_version: str,
        updated_at: datetime,
    ) -> None:
        await self._execute(
            """
            INSERT INTO application_decision_projection_index(
                application_id, session_id, agent_type, agent_id, model_version, updated_at
            )
            VALUES($1, $2, $3, $4, $5, $6)
            ON CONFLICT (application_id)
            DO UPDATE SET
                session_id = EXCLUDED.session_id,
                agent_type = EXCLUDED.agent_type,
                agent_id = EXCLUDED.agent_id,
                model_version = EXCLUDED.model_version,
                updated_at = EXCLUDED.updated_at
            """,
            application_id,
            session_id,
            agent_type,
            agent_id,
            model_version,
            updated_at,
        )

    def _apply_confidence_sample(self, record: AgentPerformanceRecord, confidence: float) -> None:
        total = (record.avg_confidence_score or 0.0) * record.confidence_samples
        record.confidence_samples += 1
        record.avg_confidence_score = (total + confidence) / record.confidence_samples

    def _apply_duration_sample(self, record: AgentPerformanceRecord, duration_ms: float) -> None:
        total = (record.avg_duration_ms or 0.0) * record.duration_samples
        record.duration_samples += 1
        record.avg_duration_ms = (total + duration_ms) / record.duration_samples

    def _recalculate_rates(self, record: AgentPerformanceRecord) -> None:
        if record.decisions_generated <= 0:
            record.approve_rate = 0.0
            record.decline_rate = 0.0
            record.refer_rate = 0.0
            record.human_override_rate = 0.0
            return

        total = float(record.decisions_generated)
        record.approve_rate = record.approve_count / total
        record.decline_rate = record.decline_count / total
        record.refer_rate = record.refer_count / total
        record.human_override_rate = record.human_override_count / total
