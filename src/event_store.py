from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncGenerator
from uuid import UUID, uuid4

import asyncpg
from pydantic import BaseModel

from src.models.events import DomainError, OptimisticConcurrencyError, StoredEvent, StreamMetadata


def _build_default_upcaster_registry() -> Any | None:
    try:
        from src.upcasting import build_default_registry
    except Exception:  # noqa: BLE001
        return None
    return build_default_registry()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


def _to_json_compatible(value: Any) -> Any:
    return json.loads(_json_dumps(value))


def _aggregate_type_from_stream(stream_id: str) -> str:
    prefix = stream_id.split("-", 1)[0]
    mapping = {
        "loan": "LoanApplication",
        "docpkg": "DocumentPackage",
        "agent": "AgentSession",
        "credit": "CreditRecord",
        "fraud": "FraudScreening",
        "compliance": "ComplianceRecord",
        "audit": "AuditLedger",
    }
    return mapping.get(prefix, prefix.title())


def _normalize_recorded_at(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise TypeError(f"Unsupported recorded_at value: {value!r}")


def _row_to_stored_event(row: asyncpg.Record) -> StoredEvent:
    payload = row["payload"]
    metadata = row["metadata"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return StoredEvent(
        event_id=row["event_id"],
        stream_id=row["stream_id"],
        stream_position=row["stream_position"],
        global_position=row["global_position"],
        event_type=row["event_type"],
        event_version=row["event_version"],
        payload=dict(payload),
        metadata=dict(metadata),
        recorded_at=row["recorded_at"],
    )


def _row_to_stream_metadata(row: asyncpg.Record) -> StreamMetadata:
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return StreamMetadata(
        stream_id=row["stream_id"],
        aggregate_type=row["aggregate_type"],
        current_version=row["current_version"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
        metadata=dict(metadata),
    )


class EventStore:
    """PostgreSQL-backed append-only event store."""

    def __init__(self, db_url: str, upcaster_registry: Any | None = None, max_pool_size: int = 10):
        self.db_url = db_url
        self.upcasters = upcaster_registry if upcaster_registry is not None else _build_default_upcaster_registry()
        self.max_pool_size = max_pool_size
        self._pool: asyncpg.Pool | None = None
        self._schema_path = Path(__file__).with_name("schema.sql")
        if self.upcasters is not None and hasattr(self.upcasters, "bind_store"):
            self.upcasters.bind_store(self)

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=self.max_pool_size)
        await self.ensure_schema()

    async def ensure_schema(self) -> None:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before ensure_schema()")

        schema_sql = self._schema_path.read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(551004220180764295)")
                statements = [statement.strip() for statement in schema_sql.split(";") if statement.strip()]
                for statement in statements:
                    await conn.execute(statement)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def stream_version(self, stream_id: str) -> int:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before stream_version()")

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
        return int(row["current_version"]) if row else -1

    async def append(
        self,
        stream_id: str,
        events: list[dict],
        expected_version: int,
        causation_id: str | None = None,
        metadata: dict | None = None,
    ) -> list[int]:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before append()")
        if not events:
            return []

        shared_metadata = dict(metadata or {})
        stream_metadata = _to_json_compatible(shared_metadata.pop("stream_metadata", {}))
        outbox_destinations = list(shared_metadata.pop("outbox_destinations", []))
        normalized_shared_metadata = _to_json_compatible(shared_metadata)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    stream_id,
                )
                row = await conn.fetchrow(
                    """
                    SELECT stream_id, aggregate_type, current_version, created_at, archived_at, metadata
                    FROM event_streams
                    WHERE stream_id = $1
                    """,
                    stream_id,
                )

                current_version = int(row["current_version"]) if row else -1
                if current_version != expected_version:
                    raise OptimisticConcurrencyError(stream_id, expected_version, current_version)
                if row and row["archived_at"] is not None:
                    raise DomainError(f"Cannot append to archived stream '{stream_id}'")

                if row is None:
                    await conn.execute(
                        """
                        INSERT INTO event_streams(stream_id, aggregate_type, current_version, metadata)
                        VALUES ($1, $2, 0, $3::jsonb)
                        """,
                        stream_id,
                        _aggregate_type_from_stream(stream_id),
                        _json_dumps(stream_metadata),
                    )

                base_position = 0 if current_version == -1 else current_version
                positions: list[int] = []

                for offset, event in enumerate(events, start=1):
                    event_type = event.get("event_type")
                    if not event_type:
                        raise ValueError("Each event must include 'event_type'")

                    stored_event_id = UUID(str(event.get("event_id"))) if event.get("event_id") else uuid4()
                    stored_event_version = int(event.get("event_version", 1))
                    stored_payload = _to_json_compatible(event.get("payload", {}))
                    event_metadata = _to_json_compatible(event.get("metadata", {}))
                    merged_metadata = {
                        **normalized_shared_metadata,
                        **event_metadata,
                    }
                    if causation_id and "causation_id" not in merged_metadata:
                        merged_metadata["causation_id"] = causation_id

                    stream_position = base_position + offset
                    recorded_at = _normalize_recorded_at(event.get("recorded_at"))

                    inserted = await conn.fetchrow(
                        """
                        INSERT INTO events(
                            event_id,
                            stream_id,
                            stream_position,
                            event_type,
                            event_version,
                            payload,
                            metadata,
                            recorded_at
                        )
                        VALUES($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)
                        RETURNING event_id, global_position, recorded_at
                        """,
                        stored_event_id,
                        stream_id,
                        stream_position,
                        event_type,
                        stored_event_version,
                        _json_dumps(stored_payload),
                        _json_dumps(merged_metadata),
                        recorded_at,
                    )

                    for destination in outbox_destinations:
                        await conn.execute(
                            """
                            INSERT INTO outbox(event_id, destination, payload)
                            VALUES ($1, $2, $3::jsonb)
                            """,
                            inserted["event_id"],
                            destination,
                            _json_dumps(
                                {
                                    "stream_id": stream_id,
                                    "stream_position": stream_position,
                                    "event_type": event_type,
                                    "event_version": stored_event_version,
                                    "payload": stored_payload,
                                    "metadata": merged_metadata,
                                }
                            ),
                        )

                    positions.append(stream_position)

                await conn.execute(
                    """
                    UPDATE event_streams
                    SET current_version = $1
                    WHERE stream_id = $2
                    """,
                    positions[-1],
                    stream_id,
                )

        return positions

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
        *,
        apply_upcasters: bool = True,
    ) -> list[StoredEvent]:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before load_stream()")

        params: list[Any] = [stream_id, from_position]
        query = """
            SELECT event_id, stream_id, stream_position, global_position, event_type,
                   event_version, payload, metadata, recorded_at
            FROM events
            WHERE stream_id = $1
              AND stream_position >= $2
        """
        if to_position is not None:
            query += " AND stream_position <= $3"
            params.append(to_position)
        query += " ORDER BY stream_position ASC"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        events = [_row_to_stored_event(row) for row in rows]
        if not apply_upcasters:
            return events
        return [await self._apply_upcasters(event) for event in events]

    async def load_all(
        self,
        from_position: int = 0,
        batch_size: int = 500,
        after_position: int | None = None,
        *,
        apply_upcasters: bool = True,
    ) -> AsyncGenerator[StoredEvent, None]:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before load_all()")

        cursor = after_position if after_position is not None else max(from_position - 1, 0)

        async with self._pool.acquire() as conn:
            while True:
                rows = await conn.fetch(
                    """
                    SELECT event_id, stream_id, stream_position, global_position, event_type,
                           event_version, payload, metadata, recorded_at
                    FROM events
                    WHERE global_position > $1
                    ORDER BY global_position ASC
                    LIMIT $2
                    """,
                    cursor,
                    batch_size,
                )
                if not rows:
                    break

                for row in rows:
                    stored_event = _row_to_stored_event(row)
                    if apply_upcasters:
                        stored_event = await self._apply_upcasters(stored_event)
                    yield stored_event

                cursor = rows[-1]["global_position"]
                if len(rows) < batch_size:
                    break

    async def get_event(self, event_id: UUID | str, *, apply_upcasters: bool = True) -> StoredEvent | None:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before get_event()")

        normalized_event_id = UUID(str(event_id))
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT event_id, stream_id, stream_position, global_position, event_type,
                       event_version, payload, metadata, recorded_at
                FROM events
                WHERE event_id = $1
                """,
                normalized_event_id,
            )
        if row is None:
            return None
        event = _row_to_stored_event(row)
        if not apply_upcasters:
            return event
        return await self._apply_upcasters(event)

    async def archive_stream(self, stream_id: str) -> StreamMetadata | None:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before archive_stream()")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE event_streams
                    SET archived_at = COALESCE(archived_at, NOW())
                    WHERE stream_id = $1
                    RETURNING stream_id, aggregate_type, current_version, created_at, archived_at, metadata
                    """,
                    stream_id,
                )
        return _row_to_stream_metadata(row) if row else None

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata | None:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before get_stream_metadata()")

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT stream_id, aggregate_type, current_version, created_at, archived_at, metadata
                FROM event_streams
                WHERE stream_id = $1
                """,
                stream_id,
            )
        return _row_to_stream_metadata(row) if row else None

    async def save_checkpoint(self, projection_name: str, position: int) -> None:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before save_checkpoint()")

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO projection_checkpoints(projection_name, last_position, updated_at)
                VALUES($1, $2, NOW())
                ON CONFLICT (projection_name)
                DO UPDATE SET last_position = EXCLUDED.last_position, updated_at = NOW()
                """,
                projection_name,
                position,
            )

    async def load_checkpoint(self, projection_name: str) -> int:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before load_checkpoint()")

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
                projection_name,
            )
        return int(row["last_position"]) if row else 0

    async def latest_global_position(self) -> int | None:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before latest_global_position()")

        async with self._pool.acquire() as conn:
            value = await conn.fetchval("SELECT MAX(global_position) FROM events")
        return int(value) if value is not None else None

    async def latest_recorded_at(self) -> datetime | None:
        if self._pool is None:
            raise RuntimeError("EventStore.connect() must be called before latest_recorded_at()")

        async with self._pool.acquire() as conn:
            value = await conn.fetchval("SELECT MAX(recorded_at) FROM events")
        return value

    async def _apply_upcasters(self, event: StoredEvent) -> StoredEvent:
        if self.upcasters is None:
            return event

        upcasted = await self.upcasters.upcast(event.model_dump(mode="python"))
        if isinstance(upcasted, StoredEvent):
            return upcasted
        if isinstance(upcasted, dict):
            return StoredEvent(**upcasted)
        raise TypeError("UpcasterRegistry.upcast() must return StoredEvent or dict")


class InMemoryEventStore:
    """Async-safe in-memory store used by the Phase 1 unit tests."""

    def __init__(self, upcaster_registry: Any | None = None):
        self.upcasters = upcaster_registry if upcaster_registry is not None else _build_default_upcaster_registry()
        self._streams: dict[str, list[StoredEvent]] = defaultdict(list)
        self._versions: dict[str, int] = {}
        self._stream_metadata: dict[str, StreamMetadata] = {}
        self._global: list[StoredEvent] = []
        self._checkpoints: dict[str, int] = {}
        self._locks: dict[str, Any] = defaultdict(__import__("asyncio").Lock)
        if self.upcasters is not None and hasattr(self.upcasters, "bind_store"):
            self.upcasters.bind_store(self)

    async def connect(self) -> None:
        return None

    async def ensure_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None

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

        stream_metadata = dict((metadata or {}).get("stream_metadata", {}))
        shared_metadata = {
            key: value
            for key, value in (metadata or {}).items()
            if key not in {"stream_metadata", "outbox_destinations"}
        }

        async with self._locks[stream_id]:
            current_version = self._versions.get(stream_id, -1)
            if current_version != expected_version:
                raise OptimisticConcurrencyError(stream_id, expected_version, current_version)

            meta_row = self._stream_metadata.get(stream_id)
            if meta_row and meta_row.archived_at is not None:
                raise DomainError(f"Cannot append to archived stream '{stream_id}'")

            if meta_row is None:
                self._stream_metadata[stream_id] = StreamMetadata(
                    stream_id=stream_id,
                    aggregate_type=_aggregate_type_from_stream(stream_id),
                    current_version=-1,
                    created_at=datetime.now(timezone.utc),
                    archived_at=None,
                    metadata=stream_metadata,
                )

            positions: list[int] = []
            for offset, event in enumerate(events, start=1):
                event_type = event.get("event_type")
                if not event_type:
                    raise ValueError("Each event must include 'event_type'")

                stored_metadata = {
                    **_to_json_compatible(shared_metadata),
                    **_to_json_compatible(event.get("metadata", {})),
                }
                if causation_id and "causation_id" not in stored_metadata:
                    stored_metadata["causation_id"] = causation_id

                stream_position = current_version + offset
                stored_event = StoredEvent(
                    event_id=UUID(str(event.get("event_id"))) if event.get("event_id") else uuid4(),
                    stream_id=stream_id,
                    stream_position=stream_position,
                    global_position=len(self._global),
                    event_type=event_type,
                    event_version=int(event.get("event_version", 1)),
                    payload=_to_json_compatible(event.get("payload", {})),
                    metadata=stored_metadata,
                    recorded_at=_normalize_recorded_at(event.get("recorded_at")),
                )
                self._streams[stream_id].append(stored_event)
                self._global.append(stored_event)
                positions.append(stream_position)

            self._versions[stream_id] = positions[-1]
            existing = self._stream_metadata[stream_id]
            self._stream_metadata[stream_id] = existing.model_copy(
                update={"current_version": positions[-1]}
            )
            return positions

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
        *,
        apply_upcasters: bool = True,
    ) -> list[StoredEvent]:
        events = [
            event
            for event in self._streams.get(stream_id, [])
            if event.stream_position >= from_position
            and (to_position is None or event.stream_position <= to_position)
        ]
        if not apply_upcasters:
            return list(events)
        return [await self._apply_upcasters(event) for event in events]

    async def load_all(
        self,
        from_position: int = 0,
        batch_size: int = 500,
        after_position: int | None = None,
        *,
        apply_upcasters: bool = True,
    ) -> AsyncGenerator[StoredEvent, None]:
        start = after_position + 1 if after_position is not None else from_position
        for event in self._global:
            if event.global_position is not None and event.global_position >= start:
                yield await self._apply_upcasters(event) if apply_upcasters else event

    async def get_event(self, event_id: UUID | str, *, apply_upcasters: bool = True) -> StoredEvent | None:
        normalized = UUID(str(event_id))
        for event in self._global:
            if event.event_id == normalized:
                if not apply_upcasters:
                    return event
                return await self._apply_upcasters(event)
        return None

    async def archive_stream(self, stream_id: str) -> StreamMetadata | None:
        metadata = self._stream_metadata.get(stream_id)
        if metadata is None:
            return None
        archived = metadata.model_copy(update={"archived_at": datetime.now(timezone.utc)})
        self._stream_metadata[stream_id] = archived
        return archived

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata | None:
        return self._stream_metadata.get(stream_id)

    async def save_checkpoint(self, projection_name: str, position: int) -> None:
        self._checkpoints[projection_name] = position

    async def load_checkpoint(self, projection_name: str) -> int:
        return self._checkpoints.get(projection_name, 0)

    async def latest_global_position(self) -> int | None:
        if not self._global:
            return None
        latest = self._global[-1].global_position
        return int(latest) if latest is not None else None

    async def latest_recorded_at(self) -> datetime | None:
        if not self._global:
            return None
        return self._global[-1].recorded_at

    async def _apply_upcasters(self, event: StoredEvent) -> StoredEvent:
        if self.upcasters is None:
            return event
        upcasted = await self.upcasters.upcast(event.model_dump(mode="python"))
        if isinstance(upcasted, StoredEvent):
            return upcasted
        if isinstance(upcasted, dict):
            return StoredEvent(**upcasted)
        raise TypeError("UpcasterRegistry.upcast() must return StoredEvent or dict")


__all__ = [
    "DomainError",
    "EventStore",
    "InMemoryEventStore",
    "OptimisticConcurrencyError",
    "StoredEvent",
    "StreamMetadata",
]
