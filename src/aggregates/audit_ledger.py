from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.models.events import DomainError, StoredEvent


APPLICATION_ENTITY_TYPES = {"application", "loan", "loanapplication"}
ENTITY_STREAM_PREFIXES: dict[str, str] = {
    "application": "loan",
    "loan": "loan",
    "loanapplication": "loan",
    "documentpackage": "docpkg",
    "docpkg": "docpkg",
    "credit": "credit",
    "creditrecord": "credit",
    "fraud": "fraud",
    "fraudscreening": "fraud",
    "compliance": "compliance",
    "compliancerecord": "compliance",
    "agent": "agent",
    "agentsession": "agent",
}


@dataclass(frozen=True)
class CausalOrderingViolation:
    event_id: str
    stream_id: str
    event_type: str
    correlation_id: str | None
    causation_id: str | None
    reason: str
    global_position: int | None = None


def _normalize_entity_type(entity_type: str) -> str:
    return entity_type.replace("-", "").replace("_", "").lower()


def _audit_stream_id(entity_type: str, entity_id: str) -> str:
    return f"audit-{entity_type}-{entity_id}"


def _related_stream_id(entity_type: str, entity_id: str) -> str:
    normalized = _normalize_entity_type(entity_type)
    prefix = ENTITY_STREAM_PREFIXES.get(normalized, entity_type)
    if prefix == "agent" and entity_id.startswith("agent-"):
        return entity_id
    return f"{prefix}-{entity_id}"


def _candidate_package_ids(entity_id: str) -> set[str]:
    return {entity_id, f"docpkg-{entity_id}"}


def _event_application_id(event: StoredEvent) -> str | None:
    payload = event.payload
    application_id = payload.get("application_id")
    if application_id:
        return str(application_id)

    package_id = payload.get("package_id")
    if isinstance(package_id, str):
        if package_id.startswith("docpkg-"):
            return package_id.removeprefix("docpkg-")
        return package_id
    return None


def _event_belongs_to_entity(event: StoredEvent, entity_type: str, entity_id: str, audit_stream_id: str) -> bool:
    if event.stream_id == audit_stream_id:
        return False

    normalized = _normalize_entity_type(entity_type)
    if normalized in APPLICATION_ENTITY_TYPES:
        if _event_application_id(event) == entity_id:
            return True
        package_id = event.payload.get("package_id")
        if isinstance(package_id, str) and package_id in _candidate_package_ids(entity_id):
            return True
        return False

    return event.stream_id == _related_stream_id(entity_type, entity_id)


@dataclass
class AuditLedgerAggregate:
    entity_type: str
    entity_id: str
    stream_id: str = field(init=False)
    version: int = -1
    audit_events: list[StoredEvent] = field(default_factory=list)
    related_events: list[StoredEvent] = field(default_factory=list)
    correlation_chains: dict[str, list[StoredEvent]] = field(default_factory=dict)
    causal_violations: list[CausalOrderingViolation] = field(default_factory=list)
    latest_integrity_hash: str | None = None
    previous_hash: str | None = None
    chain_valid: bool | None = None
    tamper_detected: bool | None = None

    def __post_init__(self) -> None:
        self.stream_id = _audit_stream_id(self.entity_type, self.entity_id)

    @classmethod
    async def load(
        cls,
        store,
        entity_type: str,
        entity_id: str,
        *,
        include_related: bool = True,
    ) -> "AuditLedgerAggregate":
        aggregate = cls(entity_type=entity_type, entity_id=entity_id)
        audit_events = await store.load_stream(aggregate.stream_id, apply_upcasters=False)
        for event in audit_events:
            aggregate._apply(event)

        if include_related:
            all_events = [event async for event in store.load_all(from_position=0, apply_upcasters=False)]
            aggregate.related_events = [
                event
                for event in all_events
                if _event_belongs_to_entity(event, entity_type, entity_id, aggregate.stream_id)
            ]
            aggregate._rebuild_correlation_chains()
            aggregate._validate_cross_stream_causal_ordering()
        return aggregate

    def _apply(self, event: StoredEvent) -> None:
        if event.stream_id != self.stream_id:
            raise DomainError(f"Audit event {event.event_id} does not belong to stream {self.stream_id}")
        if event.event_type != "AuditIntegrityCheckRun":
            raise DomainError(f"Unsupported audit event type in AuditLedger: {event.event_type}")
        if self.audit_events:
            previous = self.audit_events[-1]
            if event.stream_position <= previous.stream_position:
                raise DomainError("Audit ledger stream positions must be strictly increasing")
            if (
                event.global_position is not None
                and previous.global_position is not None
                and event.global_position <= previous.global_position
            ):
                raise DomainError("Audit ledger global positions must be strictly increasing")
            if event.payload.get("previous_hash") != previous.payload.get("integrity_hash"):
                raise DomainError("Audit integrity chain is broken: previous_hash does not match the prior integrity hash")

        self.version = event.stream_position
        self.audit_events.append(event)
        self.latest_integrity_hash = event.payload.get("integrity_hash")
        self.previous_hash = event.payload.get("previous_hash")
        self.chain_valid = bool(event.payload.get("chain_valid"))
        self.tamper_detected = bool(event.payload.get("tamper_detected"))

    def _rebuild_correlation_chains(self) -> None:
        chains: dict[str, list[StoredEvent]] = {}
        for event in self.related_events:
            correlation_id = event.metadata.get("correlation_id")
            if not correlation_id:
                continue
            chains.setdefault(str(correlation_id), []).append(event)

        for correlation_id, events in chains.items():
            chains[correlation_id] = sorted(
                events,
                key=lambda item: (
                    item.global_position if item.global_position is not None else 10**18,
                    item.stream_id,
                    item.stream_position,
                ),
            )
        self.correlation_chains = chains

    def _validate_cross_stream_causal_ordering(self) -> None:
        indexed_by_event_id = {str(event.event_id): event for event in self.related_events}
        violations: list[CausalOrderingViolation] = []

        for event in self.related_events:
            causation_id = event.metadata.get("causation_id")
            if not causation_id:
                continue

            referenced = indexed_by_event_id.get(str(causation_id))
            if referenced is None:
                violations.append(
                    CausalOrderingViolation(
                        event_id=str(event.event_id),
                        stream_id=event.stream_id,
                        event_type=event.event_type,
                        correlation_id=event.metadata.get("correlation_id"),
                        causation_id=str(causation_id),
                        reason="causation target is missing from the related event set",
                        global_position=event.global_position,
                    )
                )
                continue

            if (
                referenced.global_position is not None
                and event.global_position is not None
                and referenced.global_position >= event.global_position
            ):
                violations.append(
                    CausalOrderingViolation(
                        event_id=str(event.event_id),
                        stream_id=event.stream_id,
                        event_type=event.event_type,
                        correlation_id=event.metadata.get("correlation_id"),
                        causation_id=str(causation_id),
                        reason="causation target appears at or after the dependent event in global order",
                        global_position=event.global_position,
                    )
                )

            event_correlation = event.metadata.get("correlation_id")
            referenced_correlation = referenced.metadata.get("correlation_id")
            if event_correlation and referenced_correlation and str(event_correlation) != str(referenced_correlation):
                violations.append(
                    CausalOrderingViolation(
                        event_id=str(event.event_id),
                        stream_id=event.stream_id,
                        event_type=event.event_type,
                        correlation_id=str(event_correlation),
                        causation_id=str(causation_id),
                        reason="causation target belongs to a different correlation chain",
                        global_position=event.global_position,
                    )
                )

        self.causal_violations = violations

    def is_append_only(self) -> bool:
        try:
            previous_stream_position: int | None = None
            previous_global_position: int | None = None
            previous_hash: str | None = None
            for event in self.audit_events:
                if previous_stream_position is not None and event.stream_position <= previous_stream_position:
                    return False
                if (
                    previous_global_position is not None
                    and event.global_position is not None
                    and event.global_position <= previous_global_position
                ):
                    return False
                if previous_hash is not None and event.payload.get("previous_hash") != previous_hash:
                    return False
                previous_stream_position = event.stream_position
                previous_global_position = event.global_position
                previous_hash = event.payload.get("integrity_hash")
            return True
        except Exception:  # noqa: BLE001
            return False

    def is_causally_ordered(self) -> bool:
        return not self.causal_violations

    def assert_cross_stream_causal_ordering(self) -> None:
        if not self.causal_violations:
            return
        summary = "; ".join(
            f"{violation.event_type}@{violation.stream_id}: {violation.reason}"
            for violation in self.causal_violations
        )
        raise DomainError(f"Audit ledger detected cross-stream causal ordering violations: {summary}")

    def related_event_types(self) -> set[str]:
        return {event.event_type for event in self.related_events}

    def latest_check_timestamp(self) -> datetime | None:
        if not self.audit_events:
            return None
        payload = self.audit_events[-1].payload
        raw_timestamp: Any = payload.get("check_timestamp")
        if isinstance(raw_timestamp, datetime):
            return raw_timestamp
        if isinstance(raw_timestamp, str):
            return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        return None
