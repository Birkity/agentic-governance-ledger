from __future__ import annotations

import copy
import inspect
from collections.abc import Awaitable, Callable
from typing import Any


UpcasterFn = Callable[[dict[str, Any], "UpcasterRegistry"], dict[str, Any] | Awaitable[dict[str, Any]]]


class UpcasterRegistry:
    """Central registry for read-time event migrations."""

    def __init__(self) -> None:
        self._current_versions: dict[str, int] = {}
        self._upcasters: dict[tuple[str, int], UpcasterFn] = {}
        self._store: Any | None = None

    @property
    def store(self) -> Any | None:
        return self._store

    def bind_store(self, store: Any) -> "UpcasterRegistry":
        self._store = store
        return self

    def set_current_version(self, event_type: str, version: int) -> "UpcasterRegistry":
        self._current_versions[event_type] = version
        return self

    def register(
        self,
        event_type: str,
        from_version: int,
        *,
        to_version: int | None = None,
    ) -> Callable[[UpcasterFn], UpcasterFn]:
        target_version = to_version if to_version is not None else from_version + 1

        def decorator(func: UpcasterFn) -> UpcasterFn:
            async def wrapped(event: dict[str, Any], registry: UpcasterRegistry) -> dict[str, Any]:
                result = func(event, registry)
                if inspect.isawaitable(result):
                    result = await result
                migrated = copy.deepcopy(result)
                migrated["event_version"] = target_version
                return migrated

            self._upcasters[(event_type, from_version)] = wrapped
            self._current_versions[event_type] = max(self._current_versions.get(event_type, 1), target_version)
            return func

        return decorator

    async def upcast(self, event: dict[str, Any]) -> dict[str, Any]:
        migrated = copy.deepcopy(event)
        event_type = migrated["event_type"]
        version = int(migrated.get("event_version", 1))
        target_version = self._current_versions.get(event_type, version)

        while version < target_version:
            upcaster = self._upcasters.get((event_type, version))
            if upcaster is None:
                raise ValueError(
                    f"No upcaster registered for {event_type} v{version} -> v{version + 1}"
                )
            migrated = await upcaster(migrated, self)
            version = int(migrated.get("event_version", version + 1))

        return migrated
