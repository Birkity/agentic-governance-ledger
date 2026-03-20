import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio

from src.event_store import EventStore, OptimisticConcurrencyError


DB_URL = os.environ.get("TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="Set TEST_DB_URL to run PostgreSQL concurrency tests",
)


def _stream(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _event(event_type: str, marker: str) -> list[dict]:
    return [
        {
            "event_type": event_type,
            "event_version": 1,
            "payload": {"marker": marker},
        }
    ]


@pytest_asyncio.fixture
async def store() -> EventStore:
    event_store = EventStore(DB_URL)
    await event_store.connect()
    yield event_store
    await event_store.close()


@pytest.mark.asyncio
async def test_double_decision_concurrency_expected_version_three(store: EventStore):
    stream_id = _stream("loan-double-decision")
    await store.append(
        stream_id,
        [
            {"event_type": "ApplicationSubmitted", "event_version": 1, "payload": {"sequence": 1}},
            {"event_type": "CreditAnalysisRequested", "event_version": 1, "payload": {"sequence": 2}},
            {"event_type": "FraudScreeningRequested", "event_version": 1, "payload": {"sequence": 3}},
        ],
        expected_version=-1,
    )

    results = await asyncio.gather(
        store.append(stream_id, _event("DecisionGenerated", "A"), expected_version=3),
        store.append(stream_id, _event("DecisionGenerated", "B"), expected_version=3),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, list)]
    failures = [result for result in results if isinstance(result, OptimisticConcurrencyError)]
    stream_events = await store.load_stream(stream_id)

    assert len(successes) == 1
    assert len(failures) == 1
    assert len(stream_events) == 4
    assert [event["stream_position"] for event in stream_events] == [1, 2, 3, 4]
