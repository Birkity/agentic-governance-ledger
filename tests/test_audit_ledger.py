from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.aggregates import AuditLedgerAggregate
from src.event_store import InMemoryEventStore
from src.integrity import run_integrity_check
from src.models.events import DomainError


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _append_current(store: InMemoryEventStore, stream_id: str, events: list[dict]) -> list[int]:
    version = await store.stream_version(stream_id)
    return await store.append(stream_id, events, expected_version=version)


@pytest.mark.asyncio
async def test_audit_ledger_preserves_append_only_integrity_chain():
    store = InMemoryEventStore()
    application_id = f"APEX-AUDIT-{uuid4().hex[:8].upper()}"
    loan_stream = f"loan-{application_id}"

    await _append_current(
        store,
        loan_stream,
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": application_id, "submitted_at": _now().isoformat()},
            },
            {
                "event_type": "DecisionRequested",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "requested_at": _now().isoformat(),
                    "all_analyses_complete": True,
                    "triggered_by_event_id": "seeded",
                },
            },
        ],
    )

    first = await run_integrity_check(store, "application", application_id)
    await _append_current(
        store,
        loan_stream,
        [
            {
                "event_type": "DecisionGenerated",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "orchestrator_session_id": "sess-audit",
                    "recommendation": "REFER",
                    "confidence": 0.61,
                    "conditions": [],
                    "executive_summary": "needs review",
                    "key_risks": ["manual review"],
                    "contributing_sessions": [],
                    "model_versions": {},
                    "generated_at": _now().isoformat(),
                },
            }
        ],
    )
    second = await run_integrity_check(store, "application", application_id)

    aggregate = await AuditLedgerAggregate.load(store, "application", application_id, include_related=False)

    assert len(aggregate.audit_events) == 2
    assert aggregate.is_append_only() is True
    assert aggregate.latest_integrity_hash == second.integrity_hash
    assert aggregate.previous_hash == first.integrity_hash


@pytest.mark.asyncio
async def test_audit_ledger_accepts_valid_cross_stream_causal_chain():
    store = InMemoryEventStore()
    application_id = f"APEX-AUDIT-{uuid4().hex[:8].upper()}"
    correlation_id = f"corr-{uuid4().hex[:8]}"
    submit_event_id = str(uuid4())
    credit_event_id = str(uuid4())
    decision_event_id = str(uuid4())

    await _append_current(
        store,
        f"loan-{application_id}",
        [
            {
                "event_id": submit_event_id,
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": application_id, "submitted_at": _now().isoformat()},
                "metadata": {"correlation_id": correlation_id},
            }
        ],
    )
    await _append_current(
        store,
        f"credit-{application_id}",
        [
            {
                "event_id": credit_event_id,
                "event_type": "CreditAnalysisCompleted",
                "event_version": 1,
                "payload": {"application_id": application_id, "completed_at": _now().isoformat()},
                "metadata": {
                    "correlation_id": correlation_id,
                    "causation_id": submit_event_id,
                },
            }
        ],
    )
    await _append_current(
        store,
        f"loan-{application_id}",
        [
            {
                "event_id": decision_event_id,
                "event_type": "DecisionGenerated",
                "event_version": 1,
                "payload": {"application_id": application_id, "generated_at": _now().isoformat()},
                "metadata": {
                    "correlation_id": correlation_id,
                    "causation_id": credit_event_id,
                },
            }
        ],
    )

    aggregate = await AuditLedgerAggregate.load(store, "application", application_id)

    assert aggregate.is_causally_ordered() is True
    assert correlation_id in aggregate.correlation_chains
    assert [event.event_type for event in aggregate.correlation_chains[correlation_id]] == [
        "ApplicationSubmitted",
        "CreditAnalysisCompleted",
        "DecisionGenerated",
    ]
    aggregate.assert_cross_stream_causal_ordering()


@pytest.mark.asyncio
async def test_audit_ledger_detects_out_of_order_cross_stream_causation():
    store = InMemoryEventStore()
    application_id = f"APEX-AUDIT-{uuid4().hex[:8].upper()}"
    correlation_id = f"corr-{uuid4().hex[:8]}"
    submit_event_id = str(uuid4())
    credit_event_id = str(uuid4())

    await _append_current(
        store,
        f"credit-{application_id}",
        [
            {
                "event_id": credit_event_id,
                "event_type": "CreditAnalysisCompleted",
                "event_version": 1,
                "payload": {"application_id": application_id, "completed_at": _now().isoformat()},
                "metadata": {
                    "correlation_id": correlation_id,
                    "causation_id": submit_event_id,
                },
            }
        ],
    )
    await _append_current(
        store,
        f"loan-{application_id}",
        [
            {
                "event_id": submit_event_id,
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": application_id, "submitted_at": _now().isoformat()},
                "metadata": {"correlation_id": correlation_id},
            }
        ],
    )

    aggregate = await AuditLedgerAggregate.load(store, "application", application_id)

    assert aggregate.is_causally_ordered() is False
    assert len(aggregate.causal_violations) == 1
    with pytest.raises(DomainError):
        aggregate.assert_cross_stream_causal_ordering()
