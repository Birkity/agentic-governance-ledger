# Phase 1 Report - Event Store Implementation

## What Was Implemented

Phase 1 implemented the core event store in `starter/ledger/event_store.py`:

- `EventStore.connect()` now creates a PostgreSQL pool, registers JSON/JSONB codecs, and bootstraps the core schema.
- `append(stream_id, events, expected_version)` now performs transactional append-only writes with optimistic concurrency control.
- `load_stream(stream_id, from_position, to_position)` now replays one stream in stream order.
- `load_all(from_position, batch_size)` now yields the global event log in global order.
- `stream_version(stream_id)` now returns the current persisted version or `-1` when the stream does not exist.
- `get_event(event_id)` now loads a single event by identifier.
- `save_checkpoint()` and `load_checkpoint()` were added so the store is ready for projection work in later phases.
- The in-memory store was cleaned up and preserved as the Phase 1 test double.

## Design Decisions

### Optimistic Concurrency Control

The store uses two protections together:

- `expected_version` is compared against the persisted `event_streams.current_version`
- a PostgreSQL transaction-scoped advisory lock is taken per `stream_id`

This combination prevents split-brain writes on the same stream, including the "two agents write at the same version" scenario described in the challenge.

### Ordering

The PostgreSQL store keeps two ordering dimensions:

- `stream_position`: 1-based ordering within a single stream
- `global_position`: identity-based ordering across all streams

This matches the seeded database conventions in the data generator and gives projections a stable global replay order.

### Transactions

Each append runs inside one database transaction:

1. acquire per-stream advisory lock
2. read current stream version
3. validate OCC
4. insert all events
5. update or create the `event_streams` row

If any step fails, the whole append rolls back.

### Immutability

The implementation never updates or deletes rows in `events`. New facts are appended only. Mutable state such as stream version and projection checkpoints lives outside the immutable event log.

## Challenges Faced

- The starter file contained duplicate in-memory store definitions, so the first step was cleaning the module into one coherent implementation.
- The scaffold uses two position conventions:
  - the in-memory Phase 1 test store is 0-based
  - the real PostgreSQL store and seeded data are 1-based

This was handled deliberately instead of forcing one model onto both environments.

- The active Python environment did not include the test dependencies, so a repo-local virtual environment had to be created to verify the work.

## How Concurrency Is Handled

Concurrency is handled per stream, not globally.

- Two writers targeting different streams can proceed independently.
- Two writers targeting the same stream are serialized by the advisory lock.
- After the lock is acquired, the store checks `expected_version`.
- If the stream advanced since the caller last read it, the store raises `OptimisticConcurrencyError`.

That is exactly the retry contract agents will need in later phases.

## How Correctness Is Guaranteed

Correctness was verified in two layers:

- Provided Phase 1 tests:
  - `starter/tests/phase1/test_event_store.py` -> `11 passed`
- Phase 0 regression check:
  - `starter/tests/test_schema_and_generator.py` -> `10 passed`
- Real PostgreSQL smoke test:
  - verified append
  - verified OCC collision behavior
  - verified `load_stream()` ordering
  - verified `load_all()` global ordering
  - verified checkpoint persistence

## Why This Matters For The Ledger

This layer is the system boundary all later phases depend on:

- agents append their decisions here
- aggregates rebuild state from here
- projections replay from here
- audit and recovery both rely on immutable history here

If this layer loses ordering or OCC guarantees, the rest of the Ledger becomes untrustworthy.
