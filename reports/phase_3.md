# Phase 3 Report - Projections And Projection Daemon

## Goal

Phase 3 implements the CQRS read side for The Ledger.

The write side remains the source of truth. The new read side is derived entirely from the global event log and exists to support:

- fast dashboard reads
- agent monitoring
- compliance and audit queries
- checkpointed replay and rebuild
- lag visibility for eventual consistency

## What Was Implemented

### 1. ApplicationSummaryProjection

File:

- `src/ledger/projections/application_summary.py`

Purpose:

- one current row per application
- tracks lifecycle state, requested and approved amounts, risk tier, fraud score, compliance status, decision recommendation, final decision, and completed agent-session count

Event mapping highlights:

- loan-stream events move the visible application state
- credit-stream events enrich the row with `risk_tier` and credit confidence
- fraud-stream events enrich the row with `fraud_score`
- compliance-stream events enrich the row with current compliance status
- agent-session completion events increment the operational completion count

### 2. AgentPerformanceProjection

File:

- `src/ledger/projections/agent_performance.py`

Purpose:

- tracks agent behavior by `agent_type`, `agent_id`, and `model_version`
- records session totals, completion and failure counts, average confidence, average duration, decision distribution, and human-override counts

Design:

- an `agent_session_index` maps `session_id` to the owning agent identity
- aggregate metrics are updated from session lifecycle events and domain outcome events such as `CreditAnalysisCompleted` and `DecisionGenerated`

### 3. ComplianceAuditProjection

File:

- `src/ledger/projections/compliance_audit.py`

Purpose:

- current compliance state per application
- full pass/fail/note history
- regulation-set tracking
- temporal lookup through `get_compliance_at(application_id, timestamp)`

Design:

- every compliance-related event produces a new snapshot in `compliance_audit_history`
- current state is stored separately in `compliance_audit_current`
- `ApplicationDeclined` with compliance decline codes updates the audit view so the regulatory story remains complete

### 4. ProjectionDaemon

File:

- `src/ledger/projections/daemon.py`

Purpose:

- polls the global event log in order
- routes each event to the registered projections
- persists per-projection checkpoints
- retries failing projection handlers and skips forward after the retry budget is exhausted
- exposes lag metrics through `get_lag()` and `get_all_lags()`

## Event To Projection Flow

The daemon processes events in ascending `global_position`.

For each event:

1. load the current checkpoint for each projection
2. skip projections that have already advanced past the event
3. apply the event transactionally to the projection state
4. mark the event as applied for that projection so reprocessing is idempotent
5. save the projection checkpoint after handling the event

This keeps the read side rebuildable and restart-safe while preserving event ordering.

## Checkpoints And Restart Behavior

The implementation uses the existing Phase 1 checkpoint store and adds two supporting helpers to the EventStore:

- `has_checkpoint(projection_name)`
- `clear_checkpoint(projection_name)`

Additional replay helpers were also added:

- `load_all_after(after_position)`
- `latest_event()`
- `latest_global_position()`
- `get_event_by_global_position(position)`

Why they matter:

- restart resumes from the last processed `global_position`
- rebuild can clear a projection and replay from the beginning
- lag reporting can compare the latest store event with the latest processed event

## Lag Handling

Lag is measured per projection with two views:

- `position_lag`: latest `global_position` in the store minus the projection checkpoint
- `time_lag_ms`: time difference between the latest event in the store and the event at the projection checkpoint

This reflects the challenge requirement that projections are eventually consistent and must surface freshness rather than pretending to be the source of truth.

## Correctness And Idempotency

Correctness guarantees in this phase:

- projections never participate in command validation
- projections are derived only from events
- each projection keeps an applied-event ledger so duplicate processing is safe
- checkpoints advance independently per projection
- rebuild-from-scratch was tested for `ApplicationSummaryProjection`

Failure handling:

- one projection failure does not stop the whole daemon loop permanently
- each event is retried up to the configured retry budget
- if the retries are exhausted, the daemon records the skip by advancing the checkpoint and continues

## Tests Added

File:

- `src/tests/phase3/test_projections.py`

Coverage:

- application-summary build from a realistic command and event flow
- agent-performance metrics from credit and orchestrator sessions
- daemon resume from checkpoint after restart
- compliance temporal query behavior
- lag reporting before and after catch-up
- seed-history replay and rebuild without drift

## Verification Results

Executed successfully in this workspace:

- `.\.venv\Scripts\python.exe -m pytest src\tests\phase3\test_projections.py -q`
  - `5 passed`
- `.\.venv\Scripts\python.exe -m pytest src\tests\phase1\test_event_store.py -q`
  - `10 passed, 1 skipped`
- `.\.venv\Scripts\python.exe -m pytest src\tests\phase2\test_domain_logic.py -q`
  - `9 passed`
- `.\.venv\Scripts\python.exe -m pytest src\tests\test_schema_and_generator.py -q`
  - `10 passed`

## Seed Data Notes

The current `src/data/seed_events.jsonl` bundle in this workspace rebuilds cleanly through the Phase 3 projections and currently produces these terminal-style `ApplicationSummary` counts:

- `APPROVED = 4`
- `DECLINED = 2`
- `DECLINED_COMPLIANCE = 2`
- `REFERRED = 1`

That is the count verified against the actual seed file present in this repo during implementation.

## Challenges And Decisions

### Challenge: One code path for real PostgreSQL and in-memory tests

Decision:

- the projection layer supports both database-backed storage and in-memory storage tied to the `InMemoryEventStore`

Why:

- the provided tests rely on the in-memory store
- later phases need the same projection behavior to work against PostgreSQL

### Challenge: Reprocessing after crashes

Decision:

- use a shared per-projection applied-event ledger plus checkpoints

Why:

- a crash after mutating the projection but before saving the checkpoint must not double-apply the same event

### Challenge: Temporal compliance queries

Decision:

- snapshot compliance state on every compliance event instead of only at coarse intervals

Why:

- it is simple, deterministic, and directly supports `get_compliance_at(...)`
- the event volume for the compliance stream is small enough that snapshot-per-event is an acceptable tradeoff here

## Alignment With The Challenge

Phase 3 now satisfies the core rubric expectations for the read side:

- all 3 required projections are implemented
- a projection daemon exists
- per-projection checkpointing is implemented
- lag metrics are exposed
- the compliance audit view supports temporal queries
- rebuild-from-scratch behavior is tested

Remaining future-phase work still outside this report:

- sustained lag SLO testing under heavier concurrent load
- MCP resources that query these projections
- narrative test integration with the full agent pipeline
