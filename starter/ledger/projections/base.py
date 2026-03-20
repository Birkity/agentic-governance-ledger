from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class ProjectionLag:
    projection_name: str
    checkpoint: int
    latest_position: int
    position_lag: int
    time_lag_ms: int | None
    latest_event_at: datetime | None
    processed_event_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    return value


def coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class BaseProjection(ABC):
    projection_name = "base_projection"
    handled_event_types: set[str] = set()

    def __init__(self, store):
        self.store = store

    @property
    def uses_database(self) -> bool:
        return hasattr(self.store, "_require_pool")

    def handles(self, event_type: str) -> bool:
        return not self.handled_event_types or event_type in self.handled_event_types

    async def setup(self) -> None:
        if self.uses_database:
            pool = self.store._require_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS projection_applied_events (
                            projection_name TEXT NOT NULL,
                            event_id        TEXT NOT NULL,
                            global_position BIGINT NOT NULL,
                            applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (projection_name, event_id)
                        );
                        CREATE INDEX IF NOT EXISTS idx_projection_applied_position
                            ON projection_applied_events (projection_name, global_position);
                        """
                    )
                    await self._setup_db(conn)
            return

        bucket = self._memory_bucket()
        bucket.setdefault("applied_event_ids", set())
        await self._setup_memory(bucket)

    async def apply_event(self, event: dict[str, Any]) -> bool:
        if not self.handles(event["event_type"]):
            return False

        event_id = str(event["event_id"])
        if self.uses_database:
            pool = self.store._require_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if await self._db_event_already_applied(conn, event_id):
                        return False
                    await self._apply_db(conn, event)
                    await conn.execute(
                        """
                        INSERT INTO projection_applied_events (
                            projection_name,
                            event_id,
                            global_position
                        )
                        VALUES ($1, $2, $3)
                        """,
                        self.projection_name,
                        event_id,
                        int(event["global_position"]),
                    )
            return True

        bucket = self._memory_bucket()
        applied = bucket.setdefault("applied_event_ids", set())
        if event_id in applied:
            return False
        await self._apply_memory(bucket, event)
        applied.add(event_id)
        return True

    async def current_checkpoint(self) -> int:
        if not await self.store.has_checkpoint(self.projection_name):
            return -1
        return int(await self.store.load_checkpoint(self.projection_name))

    async def get_lag(self) -> ProjectionLag:
        latest = await self.store.latest_event()
        checkpoint = await self.current_checkpoint()
        if latest is None:
            return ProjectionLag(
                projection_name=self.projection_name,
                checkpoint=checkpoint,
                latest_position=-1,
                position_lag=0,
                time_lag_ms=0,
                latest_event_at=None,
                processed_event_at=None,
            )

        latest_position = int(latest["global_position"])
        latest_recorded_at = latest.get("recorded_at")
        processed_event = (
            await self.store.get_event_by_global_position(checkpoint)
            if checkpoint >= 0
            else None
        )
        processed_at = (
            processed_event.get("recorded_at") if processed_event is not None else None
        )
        time_lag_ms = None
        if latest_recorded_at is not None and processed_at is not None:
            time_lag_ms = max(
                int((latest_recorded_at - processed_at).total_seconds() * 1000),
                0,
            )

        if checkpoint < 0:
            position_lag = latest_position + 1
        else:
            position_lag = max(latest_position - checkpoint, 0)

        return ProjectionLag(
            projection_name=self.projection_name,
            checkpoint=checkpoint,
            latest_position=latest_position,
            position_lag=position_lag,
            time_lag_ms=time_lag_ms,
            latest_event_at=latest_recorded_at,
            processed_event_at=processed_at,
        )

    async def rebuild_from_scratch(self) -> None:
        await self.setup()
        if self.uses_database:
            pool = self.store._require_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self._clear_db(conn)
                    await conn.execute(
                        """
                        DELETE FROM projection_applied_events
                        WHERE projection_name = $1
                        """,
                        self.projection_name,
                    )
        else:
            bucket = self._memory_bucket()
            bucket.clear()
            bucket["applied_event_ids"] = set()
            await self._setup_memory(bucket)

        await self.store.clear_checkpoint(self.projection_name)

    def _memory_bucket(self) -> dict[str, Any]:
        return self.store._projection_state.setdefault(self.projection_name, {})

    async def _db_event_already_applied(self, conn, event_id: str) -> bool:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM projection_applied_events
            WHERE projection_name = $1
              AND event_id = $2
            """,
            self.projection_name,
            event_id,
        )
        return row is not None

    @abstractmethod
    async def _setup_db(self, conn) -> None:
        raise NotImplementedError

    async def _setup_memory(self, bucket: dict[str, Any]) -> None:
        del bucket

    @abstractmethod
    async def _apply_db(self, conn, event: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def _apply_memory(self, bucket: dict[str, Any], event: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def _clear_db(self, conn) -> None:
        raise NotImplementedError
