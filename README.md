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

## Phase 2 - Domain Logic

Phase 2 adds the domain layer on top of the event store.

The main pieces are:

- `starter/ledger/domain/aggregates/loan_application.py`
- `starter/ledger/domain/aggregates/agent_session.py`
- `starter/ledger/commands/handlers.py`
- `reports/phase_2.md`

This layer is responsible for:

- rebuilding aggregate state by replaying event streams
- enforcing lifecycle rules before events are written
- translating commands into canonical events
- using `expected_version` from replayed aggregate state
- keeping command validation on the write side, not in projections

The command flow is:

1. load aggregate state from the stream
2. validate business rules
3. load related authoritative streams when cross-stream checks are needed
4. create canonical event objects
5. append with OCC

Implemented Phase 2 handlers cover:

- application submission
- document uploads
- credit / fraud / compliance / decision requests
- decision generation
- human review completion
- approval / decline finalization
- agent session bootstrap

Key rules now enforced in the domain layer include:

- invalid state transitions are rejected
- required documents must be uploaded before credit analysis
- compliance hard blocks prevent forward progress
- confidence below `0.60` forces `REFER`
- approval amounts cannot exceed the requested amount or the credit recommendation
- human overrides require an override reason
- agent sessions must start with the Gas Town bootstrap event

Phase 2 verification:

- `starter/tests/phase2/test_domain_logic.py`
- replay and aggregate reconstruction checks
- command rejection checks
- concurrency rejection check for competing decision writes

## Next Dependencies

Once Phase 1 is stable, the next files that build directly on this layer are:

- `starter/ledger/registry/client.py`
- `starter/ledger/agents/base_agent.py`
- `starter/ledger/agents/stub_agents.py`
- `starter/ledger/projections/`

Those pieces now build on both Phase 1 and Phase 2 guarantees: durable history, replayable aggregate state, and domain-safe command handling.
