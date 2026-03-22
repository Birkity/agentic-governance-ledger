from __future__ import annotations

import pytest

from src.event_store import InMemoryEventStore
from src.projections import (
    AgentPerformanceProjection,
    ApplicationSummaryProjection,
    ComplianceAuditProjection,
    ProjectionDaemon,
)
from src.projections.seed_validation import load_seed_events_into_store, validate_seed_projection_rebuild


@pytest.mark.asyncio
async def test_seed_event_rebuild_matches_projection_expectations():
    store = InMemoryEventStore()
    seed_events = await load_seed_events_into_store(store, "data/seed_events.jsonl")

    summary = ApplicationSummaryProjection(store)
    performance = AgentPerformanceProjection(store)
    compliance = ComplianceAuditProjection(store)
    daemon = ProjectionDaemon(store, [summary, performance, compliance], batch_size=1000)

    await daemon.run_until_caught_up(max_cycles=100)
    validation = await validate_seed_projection_rebuild(summary, performance, compliance, seed_events)

    assert validation.application_summary_records == validation.total_applications
    assert validation.ok, "\n".join(validation.mismatches[:20])

