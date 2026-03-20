"""
PostgreSQL-backed EventStore for The Ledger.

Phase 1 scope:
- append-only writes
- per-stream ordering
- global ordering
- optimistic concurrency control
- replay by stream
- replay across all streams

The production store persists stream positions as 1-based values to match the
seeded database written by datagen/generate_all.py. The in-memory test store
uses 0-based positions because the provided Phase 1 unit tests expect that
behavior.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from uuid import UUID, uuid4

import asyncpg


StoredEvent = dict[str, Any]


EVENT_STORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id         UUID PRIMARY KEY,
    stream_id        TEXT NOT NULL,
    stream_position  BIGINT NOT NULL,
    global_position  BIGINT GENERATED ALWAYS AS IDENTITY,
    event_type       TEXT NOT NULL,
    event_version    SMALLINT NOT NULL DEFAULT 1,
    payload          JSONB NOT NULL,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);
CREATE INDEX IF NOT EXISTS idx_events_stream   ON events (stream_id, stream_position);
CREATE INDEX IF NOT EXISTS idx_events_global   ON events (global_position);
CREATE INDEX IF NOT EXISTS idx_events_type     ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_recorded ON events (recorded_at);

CREATE TABLE IF NOT EXISTS event_streams (
    stream_id        TEXT PRIMARY KEY,
    aggregate_type   TEXT NOT NULL,
    current_version  BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at      TIMESTAMPTZ,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_streams_type ON event_streams (aggregate_type);

CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name  TEXT PRIMARY KEY,
    last_position    BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS outbox (
    id               UUID PRIMARY KEY,
    event_id         UUID NOT NULL REFERENCES events(event_id),
    destination      TEXT NOT NULL,
    payload          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at     TIMESTAMPTZ,
    attempts         SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON outbox (created_at)
    WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id      UUID PRIMARY KEY,
    stream_id        TEXT NOT NULL REFERENCES event_streams(stream_id),
    stream_position  BIGINT NOT NULL,
    aggregate_type   TEXT NOT NULL,
    snapshot_version INT NOT NULL,
    state            JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class OptimisticConcurrencyError(Exception):
    """Raised when expected_version does not match the current stream version."""

    def __init__(self, stream_id: str, expected: int, actual: int):
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"OCC on '{stream_id}': expected version {expected}, actual {actual}"
        )


class EventStore:
    """Append-only PostgreSQL event store used by all agents and projections."""

    def __init__(self, db_url: str, upcaster_registry=None):
        self.db_url = db_url
        self.upcasters = upcaster_registry
        self._pool: asyncpg.Pool | None = None

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "json",
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
        )
        await conn.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
            format="text",
        )

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self.db_url,
            min_size=1,
            max_size=10,
            init=self._init_connection,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(EVENT_STORE_SCHEMA_SQL)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before use")
        return self._pool

    @staticmethod
    def _aggregate_type_for_stream(stream_id: str) -> str:
        return stream_id.partition("-")[0]

    @staticmethod
    def _stream_lock_key(stream_id: str) -> int:
        digest = hashlib.sha256(stream_id.encode("utf-8")).digest()[:8]
        return int.from_bytes(digest, byteorder="big", signed=True)

    def _row_to_event(self, row: asyncpg.Record) -> StoredEvent:
        event = dict(row)
        event["payload"] = dict(event.get("payload") or {})
        event["metadata"] = dict(event.get("metadata") or {})
        if self.upcasters is not None:
            event = self.upcasters.upcast(event)
        return event

    @staticmethod
    def _coerce_recorded_at(value: Any, fallback: datetime | None = None) -> datetime:
        if value is None:
            return fallback or datetime.now(timezone.utc)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        raise TypeError(f"Unsupported recorded_at value: {value!r}")

    async def stream_version(self, stream_id: str) -> int:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
        return row["current_version"] if row else -1

    async def append(
        self,
        stream_id: str,
        events: list[dict],
        expected_version: int,
        causation_id: str | None = None,
        metadata: dict | None = None,
    ) -> list[int]:
        if not events:
            return []

        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    self._stream_lock_key(stream_id),
                )
                row = await conn.fetchrow(
                    "SELECT current_version FROM event_streams WHERE stream_id = $1",
                    stream_id,
                )
                current_version = row["current_version"] if row else -1
                if current_version != expected_version:
                    raise OptimisticConcurrencyError(
                        stream_id, expected_version, current_version
                    )

                next_position = 1 if current_version == -1 else current_version + 1
                positions: list[int] = []
                aggregate_type = self._aggregate_type_for_stream(stream_id)
                now = datetime.now(timezone.utc)

                for offset, event in enumerate(events):
                    stream_position = next_position + offset
                    event_metadata = dict(metadata or {})
                    event_metadata.update(dict(event.get("metadata") or {}))
                    if causation_id is not None:
                        event_metadata.setdefault("causation_id", causation_id)

                    await conn.fetchrow(
                        """
                        INSERT INTO events (
                            event_id,
                            stream_id,
                            stream_position,
                            event_type,
                            event_version,
                            payload,
                            metadata,
                            recorded_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        RETURNING global_position
                        """,
                        uuid4(),
                        stream_id,
                        stream_position,
                        event["event_type"],
                        event.get("event_version", 1),
                        dict(event.get("payload") or {}),
                        event_metadata,
                        self._coerce_recorded_at(event.get("recorded_at"), now),
                    )
                    positions.append(stream_position)

                new_version = positions[-1]
                if row is None:
                    await conn.execute(
                        """
                        INSERT INTO event_streams (
                            stream_id,
                            aggregate_type,
                            current_version
                        )
                        VALUES ($1, $2, $3)
                        """,
                        stream_id,
                        aggregate_type,
                        new_version,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE event_streams
                        SET current_version = $1
                        WHERE stream_id = $2
                        """,
                        new_version,
                        stream_id,
                    )

        return positions

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        pool = self._require_pool()
        query = [
            "SELECT event_id, stream_id, stream_position, global_position,",
            "event_type, event_version, payload, metadata, recorded_at",
            "FROM events",
            "WHERE stream_id = $1 AND stream_position >= $2",
        ]
        params: list[Any] = [stream_id, from_position]
        if to_position is not None:
            query.append("AND stream_position <= $3")
            params.append(to_position)
        query.append("ORDER BY stream_position ASC")

        async with pool.acquire() as conn:
            rows = await conn.fetch(" ".join(query), *params)
        return [self._row_to_event(row) for row in rows]

    async def load_all(
        self,
        from_position: int = 0,
        batch_size: int = 500,
    ) -> AsyncGenerator[StoredEvent, None]:
        pool = self._require_pool()
        cursor_position = from_position
        async with pool.acquire() as conn:
            while True:
                rows = await conn.fetch(
                    """
                    SELECT event_id, stream_id, stream_position, global_position,
                           event_type, event_version, payload, metadata, recorded_at
                    FROM events
                    WHERE global_position > $1
                    ORDER BY global_position ASC
                    LIMIT $2
                    """,
                    cursor_position,
                    batch_size,
                )
                if not rows:
                    break

                for row in rows:
                    yield self._row_to_event(row)

                cursor_position = rows[-1]["global_position"]
                if len(rows) < batch_size:
                    break

    async def get_event(self, event_id: UUID) -> StoredEvent | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT event_id, stream_id, stream_position, global_position,
                       event_type, event_version, payload, metadata, recorded_at
                FROM events
                WHERE event_id = $1
                """,
                event_id,
            )
        return self._row_to_event(row) if row else None

    async def get_event_by_global_position(
        self,
        global_position: int,
    ) -> StoredEvent | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT event_id, stream_id, stream_position, global_position,
                       event_type, event_version, payload, metadata, recorded_at
                FROM events
                WHERE global_position = $1
                """,
                global_position,
            )
        return self._row_to_event(row) if row else None

    async def latest_event(self) -> StoredEvent | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT event_id, stream_id, stream_position, global_position,
                       event_type, event_version, payload, metadata, recorded_at
                FROM events
                ORDER BY global_position DESC
                LIMIT 1
                """
            )
        return self._row_to_event(row) if row else None

    async def latest_global_position(self) -> int:
        latest = await self.latest_event()
        return int(latest["global_position"]) if latest else -1

    async def load_all_after(
        self,
        after_position: int,
        batch_size: int = 500,
    ) -> AsyncGenerator[StoredEvent, None]:
        async for event in self.load_all(from_position=after_position, batch_size=batch_size):
            yield event

    async def save_checkpoint(self, projection_name: str, position: int) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO projection_checkpoints (projection_name, last_position)
                VALUES ($1, $2)
                ON CONFLICT (projection_name) DO UPDATE
                SET last_position = EXCLUDED.last_position,
                    updated_at = NOW()
                """,
                projection_name,
                position,
            )

    async def load_checkpoint(self, projection_name: str) -> int:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT last_position
                FROM projection_checkpoints
                WHERE projection_name = $1
                """,
                projection_name,
            )
        return row["last_position"] if row else 0

    async def has_checkpoint(self, projection_name: str) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM projection_checkpoints
                WHERE projection_name = $1
                """,
                projection_name,
            )
        return row is not None

    async def clear_checkpoint(self, projection_name: str) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM projection_checkpoints
                WHERE projection_name = $1
                """,
                projection_name,
            )


class UpcasterRegistry:
    """
    Applies pure read-time migrations to historical events.

    This registry never mutates the stored database row. It only transforms the
    loaded event structure returned to callers.
    """

    def __init__(self):
        self._upcasters: dict[str, dict[int, Any]] = {}

    def upcaster(self, event_type: str, from_version: int, to_version: int):
        if to_version != from_version + 1:
            raise ValueError("Upcasters must advance exactly one version step")

        def decorator(fn):
            self._upcasters.setdefault(event_type, {})[from_version] = fn
            return fn

        return decorator

    def upcast(self, event: StoredEvent) -> StoredEvent:
        current = dict(event)
        current["payload"] = dict(event.get("payload") or {})
        current["metadata"] = dict(event.get("metadata") or {})
        event_type = current["event_type"]
        version = current.get("event_version", 1)
        chain = self._upcasters.get(event_type, {})

        while version in chain:
            current["payload"] = chain[version](dict(current["payload"]))
            version += 1
            current["event_version"] = version

        return current


import asyncio as _asyncio
from collections import defaultdict as _defaultdict


class InMemoryEventStore:
    """
    Async-safe in-memory event store used by the provided Phase 1 tests.

    This store intentionally uses 0-based stream and global positions because
    the test suite in starter/tests/phase1/test_event_store.py is written
    against those semantics.
    """

    def __init__(self, upcaster_registry=None):
        self.upcasters = upcaster_registry
        self._streams: dict[str, list[StoredEvent]] = _defaultdict(list)
        self._versions: dict[str, int] = {}
        self._global: list[StoredEvent] = []
        self._checkpoints: dict[str, int] = {}
        self._locks: dict[str, _asyncio.Lock] = _defaultdict(_asyncio.Lock)
        self._projection_state: dict[str, dict[str, Any]] = {}

    async def stream_version(self, stream_id: str) -> int:
        return self._versions.get(stream_id, -1)

    async def append(
        self,
        stream_id: str,
        events: list[dict],
        expected_version: int,
        causation_id: str | None = None,
        metadata: dict | None = None,
    ) -> list[int]:
        if not events:
            return []

        async with self._locks[stream_id]:
            current_version = self._versions.get(stream_id, -1)
            if current_version != expected_version:
                raise OptimisticConcurrencyError(
                    stream_id, expected_version, current_version
                )

            positions: list[int] = []
            base_metadata = dict(metadata or {})
            if causation_id is not None:
                base_metadata.setdefault("causation_id", causation_id)

            for offset, event in enumerate(events):
                stream_position = current_version + 1 + offset
                stored: StoredEvent = {
                    "event_id": str(uuid4()),
                    "stream_id": stream_id,
                    "stream_position": stream_position,
                    "global_position": len(self._global),
                    "event_type": event["event_type"],
                    "event_version": event.get("event_version", 1),
                    "payload": dict(event.get("payload") or {}),
                    "metadata": {
                        **base_metadata,
                        **dict(event.get("metadata") or {}),
                    },
                    "recorded_at": EventStore._coerce_recorded_at(
                        event.get("recorded_at"),
                        datetime.now(timezone.utc),
                    ),
                }
                self._streams[stream_id].append(stored)
                self._global.append(stored)
                positions.append(stream_position)

            self._versions[stream_id] = positions[-1]
            return positions

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        events = [
            dict(event)
            for event in self._streams.get(stream_id, [])
            if event["stream_position"] >= from_position
            and (to_position is None or event["stream_position"] <= to_position)
        ]
        if self.upcasters is not None:
            return [self.upcasters.upcast(event) for event in events]
        return events

    async def load_all(
        self,
        from_position: int = 0,
        batch_size: int = 500,
    ) -> AsyncGenerator[StoredEvent, None]:
        del batch_size
        for event in self._global:
            if event["global_position"] >= from_position:
                loaded = dict(event)
                if self.upcasters is not None:
                    loaded = self.upcasters.upcast(loaded)
                yield loaded

    async def get_event(self, event_id: str | UUID) -> StoredEvent | None:
        event_id_str = str(event_id)
        for event in self._global:
            if event["event_id"] == event_id_str:
                loaded = dict(event)
                if self.upcasters is not None:
                    loaded = self.upcasters.upcast(loaded)
                return loaded
        return None

    async def get_event_by_global_position(
        self,
        global_position: int,
    ) -> StoredEvent | None:
        for event in self._global:
            if event["global_position"] == global_position:
                loaded = dict(event)
                if self.upcasters is not None:
                    loaded = self.upcasters.upcast(loaded)
                return loaded
        return None

    async def latest_event(self) -> StoredEvent | None:
        if not self._global:
            return None
        latest = dict(self._global[-1])
        if self.upcasters is not None:
            latest = self.upcasters.upcast(latest)
        return latest

    async def latest_global_position(self) -> int:
        latest = await self.latest_event()
        return int(latest["global_position"]) if latest else -1

    async def load_all_after(
        self,
        after_position: int,
        batch_size: int = 500,
    ) -> AsyncGenerator[StoredEvent, None]:
        yielded = 0
        for event in self._global:
            if event["global_position"] <= after_position:
                continue
            loaded = dict(event)
            if self.upcasters is not None:
                loaded = self.upcasters.upcast(loaded)
            yield loaded
            yielded += 1
            if yielded >= batch_size:
                break

    async def save_checkpoint(self, projection_name: str, position: int) -> None:
        self._checkpoints[projection_name] = position

    async def load_checkpoint(self, projection_name: str) -> int:
        return self._checkpoints.get(projection_name, 0)

    async def has_checkpoint(self, projection_name: str) -> bool:
        return projection_name in self._checkpoints

    async def clear_checkpoint(self, projection_name: str) -> None:
        self._checkpoints.pop(projection_name, None)
