# The Ledger - Week 5 Workspace

This repository contains the Week 5 challenge materials, the supporting architecture notes, and the implementation scaffold under `starter/`.

## Where The Build Lives

The working application scaffold is in `starter/`. That is where the canonical event schema, the data generator, the EventStore implementation, the agent scaffolding, and the provided tests live.

For Phase 1, the main implementation file is:

- `starter/ledger/event_store.py`

The Phase 1 report is here:

- `reports/phase1.md`

## Phase 1 - Event Store

Phase 1 implements the Ledger's core event store, which acts as the single source of truth for the entire platform.

It is responsible for:

- append-only event writes
- per-stream ordering via `stream_position`
- global ordering via `global_position`
- optimistic concurrency control through `expected_version`
- replay by stream for aggregate rehydration
- replay across all streams for projections and audit
- checkpoint persistence for projection progress

This layer is the foundation for later phases because:

- agents record all decisions and execution steps here
- aggregates reconstruct current state from event history here
- projections rebuild read models from the global event log here
- recovery and audit both depend on this immutable history

## Design Choices

The Phase 1 implementation uses these core design decisions:

- Append-only persistence: rows in `events` are never updated or deleted.
- Transactional writes: every append validates OCC, writes all events, and updates stream version in one transaction.
- Per-stream concurrency safety: PostgreSQL advisory transaction locks serialize competing writers to the same stream.
- Strict ordering in PostgreSQL: persisted `stream_position` values are 1-based to match the seeded database.
- Strict ordering in tests: the in-memory Phase 1 test store stays 0-based because the provided test suite expects that convention.
- Global replay: `load_all()` reads by ascending `global_position`, which is what later projections and audit processes depend on.

## Running Phase 1

Create a virtual environment and install the minimum test dependencies:

```powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe pytest pytest-asyncio asyncpg pydantic faker
```

Run the provided Phase 1 and Phase 0 verification from inside `starter/`:

```powershell
cd starter
..\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py -q
..\.venv\Scripts\python.exe -m pytest tests\test_schema_and_generator.py -q
```

If you already have a full environment, you can instead install from:

```powershell
pip install -r starter\requirements.txt
```

## Current Phase 1 Status

Phase 1 implementation is complete in the starter scaffold and was verified with:

- `starter/tests/phase1/test_event_store.py`
- `starter/tests/test_schema_and_generator.py`
- a real PostgreSQL smoke test covering append, OCC collision handling, replay ordering, and checkpoints

## Next Dependencies

Once Phase 1 is stable, the next files that build directly on this layer are:

- `starter/ledger/registry/client.py`
- `starter/ledger/domain/aggregates/loan_application.py`
- `starter/ledger/agents/base_agent.py`
- `starter/ledger/agents/stub_agents.py`
- `starter/ledger/projections/`

Those pieces all assume the EventStore guarantees implemented in Phase 1 are correct.
