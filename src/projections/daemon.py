from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any
from uuid import UUID

from src.models.events import StoredEvent
from src.projections.base import Projection, ProjectionLag


logger = logging.getLogger(__name__)


class ProjectionDaemon:
    def __init__(
        self,
        store,
        projections: list[Projection],
        *,
        batch_size: int = 250,
        max_retries: int = 2,
    ) -> None:
        self._store = store
        self._projections = {projection.name: projection for projection in projections}
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._running = False
        self._initialized = False
        self._retry_counts: dict[tuple[str, UUID], int] = {}
        self._skipped_events: dict[str, list[dict[str, Any]]] = {}

    async def initialize(self) -> None:
        if self._initialized:
            return
        for projection in self._projections.values():
            await projection.initialize()
        self._initialized = True

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        await self.initialize()
        self._running = True
        while self._running:
            await self.process_batch()
            await asyncio.sleep(poll_interval_ms / 1000)

    def stop(self) -> None:
        self._running = False

    async def process_batch(self) -> int:
        await self.initialize()
        if not self._projections:
            return 0

        start_position = min(projection.next_position for projection in self._projections.values())
        events = await self._collect_batch(start_position)
        if not events:
            return 0

        blocked_projections: set[str] = set()
        for event in events:
            for projection in self._projections.values():
                if projection.name in blocked_projections:
                    continue
                if projection.next_position > (event.global_position if event.global_position is not None else -1):
                    continue

                if not projection.handles(event):
                    projection.mark_processed(event)
                    await self._store.save_checkpoint(projection.name, projection.next_position)
                    continue

                try:
                    await projection.apply(event)
                except Exception as exc:  # noqa: BLE001
                    if not await self._handle_projection_error(projection, event, exc):
                        blocked_projections.add(projection.name)
                        continue

                projection.mark_processed(event)
                await self._store.save_checkpoint(projection.name, projection.next_position)

        return len(events)

    async def run_until_caught_up(self, *, max_cycles: int = 100, poll_interval_ms: int = 10) -> int:
        total_processed = 0
        for _ in range(max_cycles):
            processed = await self.process_batch()
            total_processed += processed
            latest_position = await self._store.latest_global_position()
            if latest_position is None:
                return total_processed
            if all(projection.next_position > latest_position for projection in self._projections.values()):
                return total_processed
            await asyncio.sleep(poll_interval_ms / 1000)
        return total_processed

    async def get_lag(self, projection_name: str) -> ProjectionLag:
        projection = self._projections[projection_name]
        latest_position = await self._store.latest_global_position()
        latest_recorded_at = await self._store.latest_recorded_at()
        last_processed_position = projection.next_position - 1
        lag_positions = 0 if latest_position is None else max(0, latest_position - last_processed_position)
        if latest_position is None or latest_recorded_at is None or projection.last_processed_at is None:
            lag_ms = 0
        else:
            lag_ms = max(0, int((latest_recorded_at - projection.last_processed_at).total_seconds() * 1000))
        return ProjectionLag(
            projection_name=projection_name,
            lag_positions=lag_positions,
            lag_ms=lag_ms,
            next_position=projection.next_position,
            latest_store_position=latest_position,
        )

    async def get_all_lags(self) -> dict[str, ProjectionLag]:
        return {name: await self.get_lag(name) for name in self._projections}

    def skipped_events(self, projection_name: str) -> list[dict[str, Any]]:
        return list(self._skipped_events.get(projection_name, []))

    async def _collect_batch(self, start_position: int) -> list[StoredEvent]:
        events: list[StoredEvent] = []
        async for event in self._store.load_all(from_position=start_position, batch_size=self._batch_size):
            events.append(event)
            if len(events) >= self._batch_size:
                break
        return events

    async def _handle_projection_error(
        self,
        projection: Projection,
        event: StoredEvent,
        exc: Exception,
    ) -> bool:
        key = (projection.name, event.event_id)
        attempts = self._retry_counts.get(key, 0) + 1
        self._retry_counts[key] = attempts

        if attempts <= self._max_retries:
            logger.warning(
                "Projection %s failed on %s at global_position=%s (attempt %s/%s): %s",
                projection.name,
                event.event_type,
                event.global_position,
                attempts,
                self._max_retries,
                exc,
            )
            return False

        logger.error(
            "Projection %s skipped %s at global_position=%s after %s attempts: %s",
            projection.name,
            event.event_type,
            event.global_position,
            attempts - 1,
            exc,
        )
        self._skipped_events.setdefault(projection.name, []).append(
            {
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "global_position": event.global_position,
                "error": str(exc),
                "attempts": attempts - 1,
            }
        )
        return True
