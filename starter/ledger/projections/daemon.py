from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from ledger.projections.base import BaseProjection


class ProjectionDaemon:
    def __init__(
        self,
        store,
        projections: list[BaseProjection],
        *,
        batch_size: int = 250,
        retry_limit: int = 3,
        poll_interval_seconds: float = 0.25,
    ):
        self.store = store
        self.projections = projections
        self.batch_size = batch_size
        self.retry_limit = retry_limit
        self.poll_interval_seconds = poll_interval_seconds
        self.failure_counts: dict[str, int] = defaultdict(int)

    async def setup(self) -> None:
        for projection in self.projections:
            await projection.setup()

    async def process_once(self) -> dict[str, Any]:
        await self.setup()
        checkpoints = {
            projection.projection_name: await projection.current_checkpoint()
            for projection in self.projections
        }
        start_after = min(checkpoints.values(), default=-1)
        events = []
        async for event in self.store.load_all_after(
            start_after,
            batch_size=self.batch_size,
        ):
            events.append(event)

        processed = 0
        skipped = 0
        for event in events:
            global_position = int(event["global_position"])
            for projection in self.projections:
                name = projection.projection_name
                if global_position <= checkpoints[name]:
                    continue

                try:
                    await self._apply_with_retry(projection, event)
                except Exception:
                    skipped += 1
                finally:
                    await self.store.save_checkpoint(name, global_position)
                    checkpoints[name] = global_position
            processed += 1

        return {
            "events_seen": len(events),
            "events_processed": processed,
            "events_skipped": skipped,
            "checkpoints": checkpoints,
        }

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        await self.setup()
        while stop_event is None or not stop_event.is_set():
            result = await self.process_once()
            if result["events_seen"] == 0:
                await asyncio.sleep(self.poll_interval_seconds)

    async def get_lag(self, projection_name: str) -> dict[str, Any]:
        projection = self._projection_by_name(projection_name)
        return (await projection.get_lag()).to_dict()

    async def get_all_lags(self) -> dict[str, dict[str, Any]]:
        return {
            projection.projection_name: (await projection.get_lag()).to_dict()
            for projection in self.projections
        }

    async def _apply_with_retry(self, projection: BaseProjection, event: dict[str, Any]) -> None:
        attempts = 0
        while True:
            try:
                await projection.apply_event(event)
                self.failure_counts[projection.projection_name] = 0
                return
            except Exception:
                attempts += 1
                self.failure_counts[projection.projection_name] = attempts
                if attempts >= self.retry_limit:
                    raise
                await asyncio.sleep(min(0.05 * attempts, 0.25))

    def _projection_by_name(self, projection_name: str) -> BaseProjection:
        for projection in self.projections:
            if projection.projection_name == projection_name:
                return projection
        raise KeyError(f"Unknown projection: {projection_name}")
