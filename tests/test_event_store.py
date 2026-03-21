import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio

from src.event_store import EventStore, OptimisticConcurrencyError


DB_URL = os.environ.get("TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="Set TEST_DB_URL to run PostgreSQL event store tests",
)


def _stream(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _events(event_type: str, count: int = 1) -> list[dict]:
    return [
        {
            "event_type": event_type,
            "event_version": 1,
            "payload": {"sequence": index, "test": True},
        }
        for index in range(count)
    ]


@pytest_asyncio.fixture
async def store() -> EventStore:
    event_store = EventStore(DB_URL)
    await event_store.connect()
    yield event_store
    await event_store.close()


@pytest.mark.asyncio
async def test_append_new_stream(store: EventStore):
    stream_id = _stream("test-new")
    positions = await store.append(stream_id, _events("TestEvent"), expected_version=-1)
    assert positions == [1]


@pytest.mark.asyncio
async def test_append_existing_stream(store: EventStore):
    stream_id = _stream("test-existing")
    await store.append(stream_id, _events("TestEvent"), expected_version=-1)
    positions = await store.append(stream_id, _events("TestEvent2"), expected_version=1)
    assert positions == [2]


@pytest.mark.asyncio
async def test_occ_wrong_version_raises(store: EventStore):
    stream_id = _stream("test-occ")
    await store.append(stream_id, _events("TestEvent"), expected_version=-1)
    with pytest.raises(OptimisticConcurrencyError) as exc_info:
        await store.append(stream_id, _events("TestEvent"), expected_version=99)
    assert exc_info.value.expected == 99
    assert exc_info.value.actual == 1


@pytest.mark.asyncio
async def test_concurrent_double_append_exactly_one_succeeds(store: EventStore):
    stream_id = _stream("test-concurrent")
    await store.append(stream_id, _events("Init"), expected_version=-1)

    results = await asyncio.gather(
        store.append(stream_id, _events("A"), expected_version=1),
        store.append(stream_id, _events("B"), expected_version=1),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, list)]
    failures = [result for result in results if isinstance(result, OptimisticConcurrencyError)]
    assert len(successes) == 1
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_load_stream_returns_ordered_events(store: EventStore):
    stream_id = _stream("test-load-stream")
    await store.append(stream_id, _events("TestEvent", 3), expected_version=-1)
    events = await store.load_stream(stream_id)
    assert [event["stream_position"] for event in events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_stream_version_tracks_last_position(store: EventStore):
    stream_id = _stream("test-version")
    await store.append(stream_id, _events("TestEvent", 4), expected_version=-1)
    assert await store.stream_version(stream_id) == 4


@pytest.mark.asyncio
async def test_stream_version_nonexistent_is_minus_one(store: EventStore):
    assert await store.stream_version(_stream("missing")) == -1


@pytest.mark.asyncio
async def test_load_all_yields_events_in_global_order(store: EventStore):
    stream_a = _stream("test-global-a")
    stream_b = _stream("test-global-b")
    await store.append(stream_a, _events("Alpha", 2), expected_version=-1)
    await store.append(stream_b, _events("Beta", 2), expected_version=-1)

    relevant = [
        event
        async for event in store.load_all(from_position=0)
        if event["stream_id"] in {stream_a, stream_b}
    ]
    positions = [event["global_position"] for event in relevant]
    assert positions == sorted(positions)


@pytest.mark.asyncio
async def test_checkpoints_round_trip(store: EventStore):
    await store.save_checkpoint("projection-test", 42)
    assert await store.load_checkpoint("projection-test") == 42


@pytest.mark.asyncio
async def test_archive_stream_returns_metadata(store: EventStore):
    stream_id = _stream("test-archive")
    await store.append(stream_id, _events("ArchiveCandidate"), expected_version=-1)
    metadata = await store.archive_stream(stream_id)
    assert metadata is not None
    assert metadata.stream_id == stream_id
    assert metadata.archived_at is not None
