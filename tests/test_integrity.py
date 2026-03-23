from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.event_store import InMemoryEventStore
from src.integrity import run_integrity_check


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_integrity_check_appends_audit_event_and_links_hash_chain():
    store = InMemoryEventStore()
    application_id = f"APEX-INTEGRITY-{uuid4().hex[:8].upper()}"
    stream_id = f"loan-{application_id}"

    await store.append(
        stream_id,
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": application_id, "submitted_at": _now().isoformat()},
            },
            {
                "event_type": "DecisionRequested",
                "event_version": 1,
                "payload": {"application_id": application_id, "requested_at": _now().isoformat()},
            },
        ],
        expected_version=-1,
    )

    first = await run_integrity_check(store, "loan", application_id)
    await store.append(
        stream_id,
        [
            {
                "event_type": "DecisionGenerated",
                "event_version": 1,
                "payload": {"application_id": application_id, "generated_at": _now().isoformat()},
            }
        ],
        expected_version=1,
    )
    second = await run_integrity_check(store, "loan", application_id)
    audit_events = await store.load_stream(first.audit_stream_id, apply_upcasters=False)

    assert first.events_verified == 2
    assert first.chain_valid is True
    assert first.tamper_detected is False
    assert second.events_verified == 3
    assert second.previous_hash == first.integrity_hash
    assert second.chain_valid is True
    assert second.tamper_detected is False
    assert len(audit_events) == 2
    assert audit_events[-1].event_type == "AuditIntegrityCheckRun"


@pytest.mark.asyncio
async def test_integrity_check_detects_tampering_against_previous_hash():
    store = InMemoryEventStore()
    application_id = f"APEX-INTEGRITY-{uuid4().hex[:8].upper()}"
    stream_id = f"loan-{application_id}"

    await store.append(
        stream_id,
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": application_id, "submitted_at": _now().isoformat()},
            }
        ],
        expected_version=-1,
    )

    first = await run_integrity_check(store, "loan", application_id)
    store._streams[stream_id][0] = store._streams[stream_id][0].with_payload(  # type: ignore[index]
        {"application_id": application_id, "submitted_at": _now().isoformat(), "tampered": True}
    )
    store._global[0] = store._streams[stream_id][0]

    second = await run_integrity_check(store, "loan", application_id)

    assert first.chain_valid is True
    assert second.chain_valid is False
    assert second.tamper_detected is True
