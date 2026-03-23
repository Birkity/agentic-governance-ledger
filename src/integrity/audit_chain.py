from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.models.events import AuditIntegrityCheckRun, StoredEvent


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
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    source_stream_id: str
    audit_stream_id: str
    events_verified: int
    integrity_hash: str
    previous_hash: str | None
    chain_valid: bool
    tamper_detected: bool
    check_stream_position: int


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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_event(event: StoredEvent) -> str:
    return _hash_text(
        _canonical_json(
            {
                "event_id": str(event.event_id),
                "stream_id": event.stream_id,
                "stream_position": event.stream_position,
                "global_position": event.global_position,
                "event_type": event.event_type,
                "event_version": event.event_version,
                "payload": event.payload,
                "metadata": event.metadata,
                "recorded_at": event.recorded_at,
            }
        )
    )


def _aggregate_events_hash(events: list[StoredEvent]) -> str:
    material = "".join(_hash_event(event) for event in events)
    return _hash_text(material)


def _resolve_source_stream_id(entity_type: str, entity_id: str) -> str:
    normalized = entity_type.replace("-", "").replace("_", "").lower()
    prefix = ENTITY_STREAM_PREFIXES.get(normalized, entity_type)
    if prefix == "agent" and entity_id.startswith("agent-"):
        return entity_id
    return f"{prefix}-{entity_id}"


def _audit_stream_id(entity_type: str, entity_id: str) -> str:
    return f"audit-{entity_type}-{entity_id}"


def _expected_chain_hash(previous_hash: str | None, events: list[StoredEvent]) -> str:
    return _hash_text(f"{previous_hash or ''}:{_aggregate_events_hash(events)}")


async def run_integrity_check(
    store,
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    source_stream_id = _resolve_source_stream_id(entity_type, entity_id)
    audit_stream_id = _audit_stream_id(entity_type, entity_id)

    source_events = await store.load_stream(source_stream_id, apply_upcasters=False)
    audit_events = await store.load_stream(audit_stream_id, apply_upcasters=False)
    prior_checks = [event for event in audit_events if event.event_type == "AuditIntegrityCheckRun"]
    previous_check = prior_checks[-1] if prior_checks else None

    previous_hash = previous_check.payload.get("integrity_hash") if previous_check else None
    chain_valid = True
    tamper_detected = False
    if previous_check is not None:
        previous_count = int(previous_check.payload.get("events_verified_count", 0))
        expected_prior_hash = _expected_chain_hash(
            previous_check.payload.get("previous_hash"),
            source_events[:previous_count],
        )
        chain_valid = expected_prior_hash == previous_check.payload.get("integrity_hash") and not bool(
            previous_check.payload.get("tamper_detected")
        )
        tamper_detected = not chain_valid

    current_hash = _expected_chain_hash(previous_hash, source_events)
    check_timestamp = datetime.now(timezone.utc)
    event = AuditIntegrityCheckRun(
        entity_type=entity_type,
        entity_id=entity_id,
        check_timestamp=check_timestamp,
        events_verified_count=len(source_events),
        integrity_hash=current_hash,
        previous_hash=previous_hash,
        chain_valid=chain_valid,
        tamper_detected=tamper_detected,
    ).to_store_dict()
    expected_version = await store.stream_version(audit_stream_id)
    positions = await store.append(audit_stream_id, [event], expected_version=expected_version)

    return IntegrityCheckResult(
        entity_type=entity_type,
        entity_id=entity_id,
        source_stream_id=source_stream_id,
        audit_stream_id=audit_stream_id,
        events_verified=len(source_events),
        integrity_hash=current_hash,
        previous_hash=previous_hash,
        chain_valid=chain_valid,
        tamper_detected=tamper_detected,
        check_stream_position=positions[-1],
    )
