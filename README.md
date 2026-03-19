# The Ledger - Week 5 Workspace

This repository contains the Week 5 challenge brief, the supporting architecture guide, the implementation under `src/`, and the phase reports under `reports/`.

## Workspace Layout

- `src/ledger/schema/events.py`: canonical event contract
- `src/ledger/event_store.py`: append-only event store and in-memory test store
- `src/ledger/domain/aggregates/`: replay-based aggregates and domain rules
- `src/ledger/commands/handlers.py`: command-to-event flow with OCC
- `src/ledger/projections/`: Phase 3 read models and projection daemon
- `src/tests/`: phase-by-phase verification
- `reports/phase1.md`, `reports/phase_2.md`, `reports/phase_3.md`: implementation notes per phase

## Setup

Create the virtual environment and install the dependencies needed for the implemented phases:

```powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe pytest pytest-asyncio asyncpg pydantic faker
```

If you want the broader scaffold dependencies as well, install from the requirements file inside `src/` instead:

```powershell
pip install -r src\requirements.txt
```

## Phase 1 - Event Store

Phase 1 implements the Ledger's single source of truth.

The event store now provides:

- append-only writes
- per-stream ordering through `stream_position`
- global ordering through `global_position`
- optimistic concurrency control with `expected_version`
- replay by stream for aggregate reconstruction
- replay across all streams for projections and audit
- projection checkpoint persistence
- helper lookups used by the projection daemon, including latest-event and checkpoint-existence queries

Design choices:

- PostgreSQL writes are transactional and take a per-stream advisory lock before validating OCC.
- Stored events are immutable; readers rebuild state rather than updating event rows.
- The production store keeps 1-based persisted stream positions, while the in-memory test store keeps the 0-based semantics expected by the provided Phase 1 tests.

Run the Phase 1 checks:

```powershell
.\.venv\Scripts\python.exe -m pytest src\tests\phase1\test_event_store.py -q
.\.venv\Scripts\python.exe -m pytest src\tests\test_schema_and_generator.py -q
```

## Phase 2 - Domain Logic

Phase 2 turns the event store into a business system by adding aggregates and command handlers.

Implemented pieces:

- `LoanApplicationAggregate` for lifecycle state and rules
- `AgentSessionAggregate` for Gas Town session bootstrap enforcement
- command handlers for submission, document uploads, analysis requests, orchestrator decisions, human review, and final approval or decline

Command flow:

1. replay the authoritative stream
2. rebuild aggregate state
3. validate business rules
4. create canonical event payloads
5. append with OCC

Key rules enforced:

- invalid state transitions are rejected
- missing required documents block credit analysis
- compliance hard blocks stop forward progress
- confidence below `0.60` forces `REFER`
- approval amounts cannot exceed the requested amount or the credit recommendation
- human overrides require a reason

Run the Phase 2 checks:

```powershell
.\.venv\Scripts\python.exe -m pytest src\tests\phase2\test_domain_logic.py -q
```

## Phase 3 - Projections And Daemon

Phase 3 implements the CQRS read side in `src/ledger/projections/`.

Included read models:

- `ApplicationSummaryProjection`: current dashboard view for each application
- `AgentPerformanceProjection`: session, confidence, duration, and decision metrics by agent/model
- `ComplianceAuditProjection`: current regulatory state plus temporal snapshots for `get_compliance_at(application_id, timestamp)`

The `ProjectionDaemon`:

- reads the global event log in order
- advances each projection with its own checkpoint
- resumes from the last checkpoint after restart
- tolerates per-projection handler failures with bounded retry
- exposes lag per projection using global position and event timestamps

Important CQRS rule:

- command validation stays on the write side
- projections are read-only views derived from events
- projections can be rebuilt from scratch without changing the source history

Run the Phase 3 checks:

```powershell
.\.venv\Scripts\python.exe -m pytest src\tests\phase3\test_projections.py -q
```

Run all implemented phase checks together:

```powershell
.\.venv\Scripts\python.exe -m pytest src\tests\phase1\test_event_store.py src\tests\phase2\test_domain_logic.py src\tests\phase3\test_projections.py src\tests\test_schema_and_generator.py -q
```

## Running The Projection Daemon

The daemon is used as a normal Python object around an EventStore instance and a list of projections. A minimal example looks like this:

```python
from ledger.event_store import EventStore
from ledger.projections import (
    AgentPerformanceProjection,
    ApplicationSummaryProjection,
    ComplianceAuditProjection,
    ProjectionDaemon,
)

store = EventStore("postgresql://postgres:newcode@localhost/apex_ledger")
await store.connect()

summary = ApplicationSummaryProjection(store)
performance = AgentPerformanceProjection(store)
compliance = ComplianceAuditProjection(store)
daemon = ProjectionDaemon(store, [summary, performance, compliance])

await daemon.process_once()
lags = await daemon.get_all_lags()
row = await summary.get("APEX-0024")
audit = await compliance.get_compliance_at("APEX-0028", "2026-02-22T07:55:20+00:00")
```

Typical query methods after the daemon has processed events:

- `await summary.get(application_id)`
- `await summary.count_by_state()`
- `await performance.list_all()`
- `await compliance.get(application_id)`
- `await compliance.get_compliance_at(application_id, timestamp)`
- `await daemon.get_all_lags()`

## Test Status

Verified in this workspace:

- `src/tests/phase1/test_event_store.py`: 10 passed, 1 skipped
- `src/tests/phase2/test_domain_logic.py`: 9 passed
- `src/tests/phase3/test_projections.py`: 5 passed
- `src/tests/test_schema_and_generator.py`: 10 passed

## Notes

- `src/pytest.ini` disables pytest's cache provider so new `pytest-cache-files-*` directories should not be created by future test runs.
- Some old `src/pytest-cache-files-*` directories already on disk are owned in a way that Windows would not let this session remove automatically, even after an elevated attempt. The prevention fix is in place, but those stale directories may need manual deletion from Explorer or an admin shell if you want them gone immediately.
