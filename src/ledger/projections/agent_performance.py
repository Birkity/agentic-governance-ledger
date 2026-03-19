from __future__ import annotations

from typing import Any

from ledger.projections.base import BaseProjection, coerce_datetime


class AgentPerformanceProjection(BaseProjection):
    projection_name = "agent_performance"
    handled_event_types = {
        "AgentSessionStarted",
        "AgentSessionCompleted",
        "AgentSessionFailed",
        "AgentSessionRecovered",
        "CreditAnalysisCompleted",
        "DecisionGenerated",
        "HumanReviewCompleted",
    }

    async def _setup_db(self, conn) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_session_index (
                session_id         TEXT PRIMARY KEY,
                agent_type         TEXT NOT NULL,
                agent_id           TEXT NOT NULL,
                application_id     TEXT NOT NULL,
                model_version      TEXT NOT NULL,
                started_at         TIMESTAMPTZ,
                completed_at       TIMESTAMPTZ,
                failed_at          TIMESTAMPTZ,
                status             TEXT NOT NULL DEFAULT 'STARTED',
                total_duration_ms  INTEGER NOT NULL DEFAULT 0,
                last_confidence    DOUBLE PRECISION,
                decision_outcome   TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_performance_ledger (
                agent_type             TEXT NOT NULL,
                agent_id               TEXT NOT NULL,
                model_version          TEXT NOT NULL,
                total_sessions         INTEGER NOT NULL DEFAULT 0,
                completed_sessions     INTEGER NOT NULL DEFAULT 0,
                failed_sessions        INTEGER NOT NULL DEFAULT 0,
                recovered_sessions     INTEGER NOT NULL DEFAULT 0,
                human_override_count   INTEGER NOT NULL DEFAULT 0,
                decision_approve_count INTEGER NOT NULL DEFAULT 0,
                decision_decline_count INTEGER NOT NULL DEFAULT 0,
                decision_refer_count   INTEGER NOT NULL DEFAULT 0,
                total_confidence       DOUBLE PRECISION NOT NULL DEFAULT 0,
                confidence_samples     INTEGER NOT NULL DEFAULT 0,
                average_confidence     DOUBLE PRECISION,
                total_duration_ms      BIGINT NOT NULL DEFAULT 0,
                average_duration_ms    DOUBLE PRECISION,
                error_rate             DOUBLE PRECISION NOT NULL DEFAULT 0,
                last_session_id        TEXT,
                last_application_id    TEXT,
                last_completed_at      TIMESTAMPTZ,
                PRIMARY KEY (agent_type, agent_id, model_version)
            );
            """
        )

    async def _clear_db(self, conn) -> None:
        await conn.execute("TRUNCATE TABLE agent_performance_ledger")
        await conn.execute("TRUNCATE TABLE agent_session_index")

    def _blank_metric_row(
        self,
        *,
        agent_type: str,
        agent_id: str,
        model_version: str,
    ) -> dict[str, Any]:
        return {
            "agent_type": agent_type,
            "agent_id": agent_id,
            "model_version": model_version,
            "total_sessions": 0,
            "completed_sessions": 0,
            "failed_sessions": 0,
            "recovered_sessions": 0,
            "human_override_count": 0,
            "decision_approve_count": 0,
            "decision_decline_count": 0,
            "decision_refer_count": 0,
            "total_confidence": 0.0,
            "confidence_samples": 0,
            "average_confidence": None,
            "total_duration_ms": 0,
            "average_duration_ms": None,
            "error_rate": 0.0,
            "last_session_id": None,
            "last_application_id": None,
            "last_completed_at": None,
        }

    def _refresh_derived_metrics(self, row: dict[str, Any]) -> None:
        confidence_samples = int(row["confidence_samples"])
        row["average_confidence"] = (
            float(row["total_confidence"]) / confidence_samples
            if confidence_samples
            else None
        )

        completed_sessions = int(row["completed_sessions"])
        row["average_duration_ms"] = (
            float(row["total_duration_ms"]) / completed_sessions
            if completed_sessions
            else None
        )

        total_sessions = int(row["total_sessions"])
        row["error_rate"] = (
            float(row["failed_sessions"]) / total_sessions if total_sessions else 0.0
        )

    async def _load_db_session(self, conn, session_id: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM agent_session_index
            WHERE session_id = $1
            """,
            session_id,
        )
        return dict(row) if row else None

    async def _save_db_session(self, conn, session: dict[str, Any]) -> None:
        await conn.execute(
            """
            INSERT INTO agent_session_index (
                session_id,
                agent_type,
                agent_id,
                application_id,
                model_version,
                started_at,
                completed_at,
                failed_at,
                status,
                total_duration_ms,
                last_confidence,
                decision_outcome
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (session_id) DO UPDATE SET
                agent_type = EXCLUDED.agent_type,
                agent_id = EXCLUDED.agent_id,
                application_id = EXCLUDED.application_id,
                model_version = EXCLUDED.model_version,
                started_at = EXCLUDED.started_at,
                completed_at = EXCLUDED.completed_at,
                failed_at = EXCLUDED.failed_at,
                status = EXCLUDED.status,
                total_duration_ms = EXCLUDED.total_duration_ms,
                last_confidence = EXCLUDED.last_confidence,
                decision_outcome = EXCLUDED.decision_outcome
            """,
            session["session_id"],
            session["agent_type"],
            session["agent_id"],
            session["application_id"],
            session["model_version"],
            session.get("started_at"),
            session.get("completed_at"),
            session.get("failed_at"),
            session.get("status", "STARTED"),
            int(session.get("total_duration_ms", 0)),
            session.get("last_confidence"),
            session.get("decision_outcome"),
        )

    async def _load_db_metric_row(
        self,
        conn,
        *,
        agent_type: str,
        agent_id: str,
        model_version: str,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM agent_performance_ledger
            WHERE agent_type = $1
              AND agent_id = $2
              AND model_version = $3
            """,
            agent_type,
            agent_id,
            model_version,
        )
        if row:
            return dict(row)
        return self._blank_metric_row(
            agent_type=agent_type,
            agent_id=agent_id,
            model_version=model_version,
        )

    async def _save_db_metric_row(self, conn, row: dict[str, Any]) -> None:
        self._refresh_derived_metrics(row)
        await conn.execute(
            """
            INSERT INTO agent_performance_ledger (
                agent_type,
                agent_id,
                model_version,
                total_sessions,
                completed_sessions,
                failed_sessions,
                recovered_sessions,
                human_override_count,
                decision_approve_count,
                decision_decline_count,
                decision_refer_count,
                total_confidence,
                confidence_samples,
                average_confidence,
                total_duration_ms,
                average_duration_ms,
                error_rate,
                last_session_id,
                last_application_id,
                last_completed_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, $18, $19, $20
            )
            ON CONFLICT (agent_type, agent_id, model_version) DO UPDATE SET
                total_sessions = EXCLUDED.total_sessions,
                completed_sessions = EXCLUDED.completed_sessions,
                failed_sessions = EXCLUDED.failed_sessions,
                recovered_sessions = EXCLUDED.recovered_sessions,
                human_override_count = EXCLUDED.human_override_count,
                decision_approve_count = EXCLUDED.decision_approve_count,
                decision_decline_count = EXCLUDED.decision_decline_count,
                decision_refer_count = EXCLUDED.decision_refer_count,
                total_confidence = EXCLUDED.total_confidence,
                confidence_samples = EXCLUDED.confidence_samples,
                average_confidence = EXCLUDED.average_confidence,
                total_duration_ms = EXCLUDED.total_duration_ms,
                average_duration_ms = EXCLUDED.average_duration_ms,
                error_rate = EXCLUDED.error_rate,
                last_session_id = EXCLUDED.last_session_id,
                last_application_id = EXCLUDED.last_application_id,
                last_completed_at = EXCLUDED.last_completed_at
            """,
            row["agent_type"],
            row["agent_id"],
            row["model_version"],
            row["total_sessions"],
            row["completed_sessions"],
            row["failed_sessions"],
            row["recovered_sessions"],
            row["human_override_count"],
            row["decision_approve_count"],
            row["decision_decline_count"],
            row["decision_refer_count"],
            row["total_confidence"],
            row["confidence_samples"],
            row["average_confidence"],
            row["total_duration_ms"],
            row["average_duration_ms"],
            row["error_rate"],
            row["last_session_id"],
            row["last_application_id"],
            row["last_completed_at"],
        )

    async def _apply_db(self, conn, event: dict[str, Any]) -> None:
        payload = event["payload"]
        event_type = event["event_type"]

        if event_type == "AgentSessionStarted":
            session = {
                "session_id": str(payload["session_id"]),
                "agent_type": str(payload["agent_type"]),
                "agent_id": str(payload["agent_id"]),
                "application_id": str(payload["application_id"]),
                "model_version": str(payload["model_version"]),
                "started_at": coerce_datetime(payload.get("started_at")) or event.get("recorded_at"),
                "completed_at": None,
                "failed_at": None,
                "status": "STARTED",
                "total_duration_ms": 0,
                "last_confidence": None,
                "decision_outcome": None,
            }
            await self._save_db_session(conn, session)
            row = await self._load_db_metric_row(
                conn,
                agent_type=session["agent_type"],
                agent_id=session["agent_id"],
                model_version=session["model_version"],
            )
            row["total_sessions"] = int(row["total_sessions"]) + 1
            row["last_session_id"] = session["session_id"]
            row["last_application_id"] = session["application_id"]
            await self._save_db_metric_row(conn, row)
            return

        session_id = str(
            payload.get("session_id") or payload.get("orchestrator_session_id") or ""
        )
        if not session_id:
            return
        session = await self._load_db_session(conn, session_id)
        if session is None:
            return
        row = await self._load_db_metric_row(
            conn,
            agent_type=session["agent_type"],
            agent_id=session["agent_id"],
            model_version=session["model_version"],
        )

        if event_type == "AgentSessionCompleted":
            session["status"] = "COMPLETED"
            session["completed_at"] = coerce_datetime(payload.get("completed_at")) or event.get("recorded_at")
            session["total_duration_ms"] = int(payload.get("total_duration_ms", 0))
            row["completed_sessions"] = int(row["completed_sessions"]) + 1
            row["total_duration_ms"] = int(row["total_duration_ms"]) + session["total_duration_ms"]
            row["last_completed_at"] = session["completed_at"]
        elif event_type == "AgentSessionFailed":
            session["status"] = "FAILED"
            session["failed_at"] = coerce_datetime(payload.get("failed_at")) or event.get("recorded_at")
            row["failed_sessions"] = int(row["failed_sessions"]) + 1
        elif event_type == "AgentSessionRecovered":
            session["status"] = "RECOVERED"
            row["recovered_sessions"] = int(row["recovered_sessions"]) + 1
        elif event_type == "CreditAnalysisCompleted":
            confidence = payload.get("decision", {}).get("confidence")
            if confidence is not None:
                session["last_confidence"] = float(confidence)
                row["total_confidence"] = float(row["total_confidence"]) + float(confidence)
                row["confidence_samples"] = int(row["confidence_samples"]) + 1
        elif event_type == "DecisionGenerated":
            recommendation = str(payload.get("recommendation", "")).upper()
            confidence = payload.get("confidence")
            session["last_confidence"] = float(confidence) if confidence is not None else None
            session["decision_outcome"] = recommendation
            if session["last_confidence"] is not None:
                row["total_confidence"] = float(row["total_confidence"]) + session["last_confidence"]
                row["confidence_samples"] = int(row["confidence_samples"]) + 1
            if recommendation == "APPROVE":
                row["decision_approve_count"] = int(row["decision_approve_count"]) + 1
            elif recommendation == "DECLINE":
                row["decision_decline_count"] = int(row["decision_decline_count"]) + 1
            elif recommendation == "REFER":
                row["decision_refer_count"] = int(row["decision_refer_count"]) + 1
        elif event_type == "HumanReviewCompleted":
            if bool(payload.get("override")):
                row["human_override_count"] = int(row["human_override_count"]) + 1

        row["last_session_id"] = session["session_id"]
        row["last_application_id"] = session["application_id"]
        await self._save_db_session(conn, session)
        await self._save_db_metric_row(conn, row)

    async def _apply_memory(self, bucket: dict[str, Any], event: dict[str, Any]) -> None:
        sessions = bucket.setdefault("sessions", {})
        metrics = bucket.setdefault("metrics", {})
        payload = event["payload"]
        event_type = event["event_type"]

        if event_type == "AgentSessionStarted":
            session = {
                "session_id": str(payload["session_id"]),
                "agent_type": str(payload["agent_type"]),
                "agent_id": str(payload["agent_id"]),
                "application_id": str(payload["application_id"]),
                "model_version": str(payload["model_version"]),
                "started_at": coerce_datetime(payload.get("started_at")) or event.get("recorded_at"),
                "completed_at": None,
                "failed_at": None,
                "status": "STARTED",
                "total_duration_ms": 0,
                "last_confidence": None,
                "decision_outcome": None,
            }
            sessions[session["session_id"]] = session
            key = (
                session["agent_type"],
                session["agent_id"],
                session["model_version"],
            )
            row = dict(metrics.get(key, self._blank_metric_row(
                agent_type=key[0],
                agent_id=key[1],
                model_version=key[2],
            )))
            row["total_sessions"] = int(row["total_sessions"]) + 1
            row["last_session_id"] = session["session_id"]
            row["last_application_id"] = session["application_id"]
            self._refresh_derived_metrics(row)
            metrics[key] = row
            return

        session_id = str(
            payload.get("session_id") or payload.get("orchestrator_session_id") or ""
        )
        if not session_id or session_id not in sessions:
            return

        session = dict(sessions[session_id])
        key = (
            session["agent_type"],
            session["agent_id"],
            session["model_version"],
        )
        row = dict(metrics.get(key, self._blank_metric_row(
            agent_type=key[0],
            agent_id=key[1],
            model_version=key[2],
        )))

        if event_type == "AgentSessionCompleted":
            session["status"] = "COMPLETED"
            session["completed_at"] = coerce_datetime(payload.get("completed_at")) or event.get("recorded_at")
            session["total_duration_ms"] = int(payload.get("total_duration_ms", 0))
            row["completed_sessions"] = int(row["completed_sessions"]) + 1
            row["total_duration_ms"] = int(row["total_duration_ms"]) + session["total_duration_ms"]
            row["last_completed_at"] = session["completed_at"]
        elif event_type == "AgentSessionFailed":
            session["status"] = "FAILED"
            session["failed_at"] = coerce_datetime(payload.get("failed_at")) or event.get("recorded_at")
            row["failed_sessions"] = int(row["failed_sessions"]) + 1
        elif event_type == "AgentSessionRecovered":
            session["status"] = "RECOVERED"
            row["recovered_sessions"] = int(row["recovered_sessions"]) + 1
        elif event_type == "CreditAnalysisCompleted":
            confidence = payload.get("decision", {}).get("confidence")
            if confidence is not None:
                session["last_confidence"] = float(confidence)
                row["total_confidence"] = float(row["total_confidence"]) + float(confidence)
                row["confidence_samples"] = int(row["confidence_samples"]) + 1
        elif event_type == "DecisionGenerated":
            recommendation = str(payload.get("recommendation", "")).upper()
            confidence = payload.get("confidence")
            session["last_confidence"] = float(confidence) if confidence is not None else None
            session["decision_outcome"] = recommendation
            if session["last_confidence"] is not None:
                row["total_confidence"] = float(row["total_confidence"]) + session["last_confidence"]
                row["confidence_samples"] = int(row["confidence_samples"]) + 1
            if recommendation == "APPROVE":
                row["decision_approve_count"] = int(row["decision_approve_count"]) + 1
            elif recommendation == "DECLINE":
                row["decision_decline_count"] = int(row["decision_decline_count"]) + 1
            elif recommendation == "REFER":
                row["decision_refer_count"] = int(row["decision_refer_count"]) + 1
        elif event_type == "HumanReviewCompleted":
            if bool(payload.get("override")):
                row["human_override_count"] = int(row["human_override_count"]) + 1

        row["last_session_id"] = session["session_id"]
        row["last_application_id"] = session["application_id"]
        self._refresh_derived_metrics(row)
        sessions[session_id] = session
        metrics[key] = row

    async def list_all(self) -> list[dict[str, Any]]:
        await self.setup()
        if self.uses_database:
            pool = self.store._require_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM agent_performance_ledger
                    ORDER BY agent_type, agent_id, model_version
                    """
                )
                return [dict(row) for row in rows]

        metrics = self._memory_bucket().setdefault("metrics", {})
        return [dict(metrics[key]) for key in sorted(metrics)]
