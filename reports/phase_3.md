## Phase 3 Report

Phase 3 implements the CQRS read side for The Ledger. The Event Store remains the source of truth, while projections provide queryable read models that are rebuilt from ordered events. The implementation follows the challenge and support documents directly: three projections, an async projection daemon, per-projection checkpoints, lag measurement, temporal compliance queries, and rebuild-from-scratch support.

### Implemented Components

Files added:

- `src/projections/base.py`
- `src/projections/application_summary.py`
- `src/projections/agent_performance.py`
- `src/projections/compliance_audit.py`
- `src/projections/daemon.py`
- `tests/test_projections.py`

Files updated:

- `src/schema.sql`
- `src/event_store.py`
- `src/projections/__init__.py`

### Projection Tables

The PostgreSQL schema now includes dedicated read-side tables:

- `application_summary`
- `agent_performance_ledger`
- `compliance_audit_current`
- `compliance_audit_history`

Two helper tables were also added to support restart-safe projection continuity:

- `agent_session_projection_index`
- `application_decision_projection_index`

These helper tables let the daemon resume after a restart without needing to rescan the full event store just to recover session-to-model mappings for performance metrics.

### Projection 1: ApplicationSummaryProjection

File: `src/projections/application_summary.py`

Purpose:

- one current row per application
- fast dashboard-style reads
- no stream replay required for ordinary UI queries

Tracked fields include:

- `application_id`
- `state`
- `applicant_id`
- `requested_amount_usd`
- `approved_amount_usd`
- `risk_tier`
- `fraud_score`
- `compliance_status`
- `decision`
- `agent_sessions_completed`
- `last_event_type`
- `last_event_at`
- `human_reviewer_id`
- `final_decision_at`

Event mapping:

- loan-stream events drive lifecycle state
- credit events enrich risk tier
- fraud events enrich fraud score
- compliance events enrich compliance status
- agent-session completion events track completed sessions

The projection intentionally mirrors the loan lifecycle closely enough for dashboard reads while staying fully derived from events.

### Projection 2: AgentPerformanceProjection

File: `src/projections/agent_performance.py`

Purpose:

- measure behavior by agent model version
- support operational questions such as whether one model version is systematically producing different decisions

Tracked metrics include:

- `total_sessions`
- `analyses_completed`
- `decisions_generated`
- `avg_confidence_score`
- `avg_duration_ms`
- `approve_rate`
- `decline_rate`
- `refer_rate`
- `human_override_rate`
- `first_seen_at`
- `last_seen_at`

Design notes:

- `AgentSessionStarted` initializes the session-to-model mapping
- later domain outputs resolve back to that session index
- `HumanReviewCompleted(override=True)` is attributed back to the latest orchestrator decision for that application
- average calculations persist sample counts so the metrics stay correct across daemon restarts

### Projection 3: ComplianceAuditProjection

File: `src/projections/compliance_audit.py`

Purpose:

- regulatory and audit-grade compliance reads
- complete rule history
- temporal queries

Supported interfaces:

- `get_current_compliance(application_id)`
- `get_compliance_at(application_id, timestamp)`
- `rebuild_from_scratch()`

Snapshot strategy:

- snapshot after every compliance event

Reason:

- it gives exact temporal reconstruction for the compliance stream
- the compliance event volume is low enough that per-event snapshots are a simple and reliable tradeoff
- it avoids ambiguous partial reconstruction logic during regulatory time-travel queries

Current state is stored in `compliance_audit_current`, and every point-in-time snapshot is stored in `compliance_audit_history`.

### ProjectionDaemon

File: `src/projections/daemon.py`

Responsibilities:

- load events from the global stream in order
- route each event to subscribed projections
- maintain per-projection checkpoints
- expose lag metrics
- tolerate projection failures without crashing the whole daemon

Checkpoint semantics:

- the stored checkpoint is the next global position to process, not the last processed event

Why:

- this works cleanly for both the real PostgreSQL store and the in-memory store used by the test suite
- it avoids off-by-one drift between their different global-position conventions

Failure handling:

- each projection gets its own retry budget per event
- if a handler fails and retries remain, that projection is blocked for the rest of the current batch and retried on the next batch
- if retries are exhausted, the event is recorded as skipped for that projection and processing continues

This matches the challenge requirement that a bad event must not bring down the full projection subsystem.

### Lag Handling

The daemon exposes lag through:

- `get_lag(projection_name)`
- `get_all_lags()`

Lag includes:

- `lag_positions`
- `lag_ms`
- `next_position`
- `latest_store_position`

This satisfies the Phase 3 requirement that projection freshness be measurable and observable.

### Rebuildability

All three projections are rebuildable from the event stream:

- clear the projection state
- replay ordered events
- repopulate read models
- update checkpoints

`ComplianceAuditProjection.rebuild_from_scratch()` is implemented explicitly because it is required by the challenge and is the most important temporal read model.

### Test Coverage

File: `tests/test_projections.py`

The new Phase 3 test suite covers:

1. terminal-state projection building across `ApplicationSummary`, `AgentPerformance`, and `ComplianceAudit`
2. temporal compliance query behavior
3. rebuild-from-scratch correctness
4. daemon checkpoint resume behavior
5. daemon retry-then-skip fault tolerance
6. lag behavior under 50 concurrent submissions

### Verification Results

Latest results:

- `tests/test_projections.py`: `5 passed`
- Phase 2 + Phase 3 combined: `16 passed`
- Phase 1 + Phase 2 + Phase 3 bundle: `54 passed, 1 skipped`

### Rubric Fit

Against the Phase 3 rubric, the implementation now covers the Score 4-level technical behaviors within current scope:

- all 3 projections exist
- daemon with checkpointing exists
- lag metric is exposed
- temporal query exists on `ComplianceAuditProjection`
- rebuild-from-zero is tested
- failure tolerance is tested
- concurrent-load lag behavior is tested

What is still outside this exact Phase 3 slice:

- a committed `projection_lag_report.txt` artifact for final submission
- final narrative-scenario integration through agents and MCP
- DESIGN.md justification of the chosen SLOs and snapshot strategy

### Design Decisions

- The Event Store remains the only source of truth; projections never participate in command validation.
- Projection checkpoints are persisted per projection, not globally, so one faulty projection does not pin the whole read side.
- Compliance snapshots are stored per event rather than by periodic interval because audit correctness matters more here than storage minimization.
- Agent performance keeps helper indices so restart-safe incremental updates remain possible without replaying the full event log.

### Challenges

- The in-memory and PostgreSQL stores use different global-position conventions, so the daemon needed explicit checkpoint semantics to avoid off-by-one replay errors.
- Agent performance metrics required restart-safe session resolution, which meant persisting auxiliary projection-side indices rather than relying only on in-memory state.
