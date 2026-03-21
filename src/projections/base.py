from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.models.events import StoredEvent


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


def _json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


@dataclass(frozen=True)
class ProjectionLag:
    projection_name: str
    lag_positions: int
    lag_ms: int
    next_position: int
    latest_store_position: int | None


class Projection(ABC):
    name: str

    def __init__(self, store) -> None:
        self.store = store
        self._next_position = 0
        self._last_processed_at: datetime | None = None

    @property
    def next_position(self) -> int:
        return self._next_position

    @property
    def last_processed_at(self) -> datetime | None:
        return self._last_processed_at

    def set_runtime_state(self, next_position: int, last_processed_at: datetime | None) -> None:
        self._next_position = next_position
        self._last_processed_at = last_processed_at

    def mark_processed(self, event: StoredEvent) -> None:
        global_position = event.global_position if event.global_position is not None else -1
        self._next_position = global_position + 1
        self._last_processed_at = event.recorded_at

    @property
    def _pool(self):
        return getattr(self.store, "_pool", None)

    async def _execute(self, query: str, *params: Any) -> None:
        if self._pool is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def _fetch(self, query: str, *params: Any):
        if self._pool is None:
            return []
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *params)

    async def _fetchrow(self, query: str, *params: Any):
        if self._pool is None:
            return None
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *params)

    async def initialize(self) -> None:
        checkpoint = await self.store.load_checkpoint(self.name)
        self.set_runtime_state(checkpoint, None)

    @abstractmethod
    def handles(self, event: StoredEvent) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def apply(self, event: StoredEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    async def reset(self) -> None:
        raise NotImplementedError

    async def rebuild_from_scratch(self) -> None:
        await self.reset()
        self.set_runtime_state(0, None)
        async for event in self.store.load_all(from_position=0):
            if self.handles(event):
                await self.apply(event)
            self.mark_processed(event)
        await self.store.save_checkpoint(self.name, self.next_position)

