from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.event_store import EventStore, InMemoryEventStore, OutboxRecord


StoreType = EventStore | InMemoryEventStore
PublishCallback = Callable[[OutboxRecord], Awaitable[None]]


def _json_default(value: Any) -> Any:
  if isinstance(value, datetime):
      return value.isoformat()
  return value


class JsonlOutboxSink:
    def __init__(self, output_path: str | Path):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    async def publish(self, record: OutboxRecord) -> None:
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), default=_json_default) + "\n")


class OutboxPublisher:
    def __init__(self, store: StoreType):
        self.store = store

    async def publish_pending(
        self,
        callback: PublishCallback,
        *,
        destination: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        records = await self.store.list_outbox_pending(destination=destination, limit=limit)
        published = 0
        for record in records:
            await callback(record)
            await self.store.mark_outbox_published(record.id)
            published += 1
        return {
            "published": published,
            "destination": destination,
            "remaining": len(await self.store.list_outbox_pending(destination=destination, limit=limit)),
        }


__all__ = ["JsonlOutboxSink", "OutboxPublisher"]
