# The Ledger

The repository follows the Week 5 submission shape, with the assessed implementation under `src/` and the generated Apex Financial Services data in `documents/` and `data/`.

## Repo Layout

- `src/schema.sql`: PostgreSQL schema for events, streams, checkpoints, outbox, and snapshots
- `src/event_store.py`: async EventStore with OCC, ordered replay, checkpoints, archival, and metadata lookup
- `src/models/events.py`: canonical event catalogue plus `StoredEvent`, `StreamMetadata`, `DomainError`, and `OptimisticConcurrencyError`
- `src/aggregates/`: replay-driven domain aggregates for loans, agent sessions, and compliance records
- `src/commands/`: Phase 2 command handlers using the load -> validate -> determine -> append flow
- `src/document_processing/`: document parsing, optional Docling-first PDF extraction, event persistence helpers, and optional Ollama summaries
- `src/projections/`: Phase 3 read models and the async projection daemon
- `src/upcasting/`: Phase 4 read-time schema migration registry and concrete upcasters
- `src/integrity/`: Phase 4 audit-chain verification and Gas Town recovery helpers
- `src/mcp/`: Phase 5 FastMCP server, command tools, query resources, and in-process gateway helpers
- `datagen/`: generator for the Applicant Registry seed data, document corpus, and seed event history
- `tests/`: Phase 1 in-memory tests, real PostgreSQL tests, concurrency tests, document-processing tests, Phase 2 domain tests, Phase 3 projection tests, and schema/generator tests
- `scripts/analyze_documents.py`: CLI for analyzing a company package and optionally persisting it into the Event Store
- `reports/phase_1.md`: Phase 1 implementation report
- `reports/phase_2.md`: Phase 2 implementation report
- `reports/phase_3.md`: Phase 3 implementation report
- `phase_4.md`: Phase 4 implementation report
- `phase_5.md`: Phase 5 implementation report

## Setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt` or `uv sync`.
3. Copy `.env.example` to `.env` and fill in your PostgreSQL password.
4. Make sure PostgreSQL is running and that the `apex_ledger` database exists.

Example environment values:

```powershell
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger
TEST_DB_URL=postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger
DOCUMENTS_DIR=./documents
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_PART_MODEL=qwen3-coder:480b-cloud
OLLAMA_PACKAGE_MODEL=deepseek-v3.1:671b-cloud
```

If you want richer PDF extraction, install the optional Docling extra:

```powershell
uv sync --extra docling
```

## Data Generation

To regenerate the Apex ledger inputs:

```powershell
.\.venv\Scripts\python.exe datagen\generate_all.py --db-url postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger --output-dir data --docs-dir documents
```

## Phase 1

Phase 1 now covers two concrete layers:

- the append-only EventStore foundation that every aggregate, agent, and projection will use
- the document package processor that reads generated PDF, XLSX, and CSV files, normalizes them into structured financial facts, can persist them as `docpkg-*` events, and can optionally summarize them with local Ollama models

### Event Store Design

- PostgreSQL is the system of record.
- Writes are append-only and ordered by both `stream_position` and `global_position`.
- OCC is enforced with `expected_version` inside one transaction.
- The store uses a stream-scoped PostgreSQL advisory lock during append to prevent split-brain writes.
- `event_streams`, `events`, optional outbox rows, and checkpoints live in the same schema.

### Document Processing Design

- PDFs are parsed with `Docling` first when available, then `pdfplumber`, then `pypdf`.
- Excel workbooks are parsed with `openpyxl`.
- CSV summaries are parsed directly and used as structured cross-checks.
- The merged facts model keeps PDFs as the primary source and backfills missing fields from workbook or CSV data.
- `src/document_processing/event_writer.py` turns extracted packages into `PackageCreated`, `DocumentAdded`, `DocumentFormatValidated`, `ExtractionStarted`, `ExtractionCompleted`, `QualityAssessmentCompleted`, and `PackageReadyForAnalysis` events.
- If Ollama is available, the processor can summarize each document part and the full package using your local configured models.

## Phase 2

Phase 2 adds the write-side domain logic on top of the Event Store. Commands do not consult projections or cached views. They rebuild aggregates from streams, enforce business rules, and only then append the next valid event sequence.

### Aggregates

- `src/aggregates/loan_application.py` reconstructs the loan lifecycle and enforces the state machine from submission through final approval or decline.
- `src/aggregates/agent_session.py` enforces Gas Town session anchoring and model version consistency for agent-driven work.
- `src/aggregates/compliance_record.py` tracks mandatory rule evaluation, hard blocks, and approval readiness.

### Command Flow

All handlers in `src/commands/handlers.py` follow the same pattern:

1. load the relevant stream or streams
2. rebuild aggregate state by replay
3. validate rules before any write
4. determine the event or events to append
5. append with `expected_version` so OCC protects consistency

Implemented handlers:

- `handle_submit_application`
- `handle_start_agent_session`
- `handle_credit_analysis_completed`
- `handle_fraud_screening_completed`
- `handle_compliance_check`
- `handle_generate_decision`
- `handle_human_review_completed`

### Phase 2 Rules Enforced

- invalid loan lifecycle transitions are rejected
- agent work requires a valid Gas Town session anchor
- repeated credit analysis is locked to the recorded model version
- decisions below the `0.60` confidence floor must be `REFER`
- compliance must complete before approval, and hard blocks force decline
- decision causal chains must reference valid contributing agent sessions
- approvals above the latest recommended credit limit are rejected

These checks run on the command side before events are appended, which keeps the ledger replayable and auditable.

## Phase 3

Phase 3 adds the CQRS read side. Projections subscribe to the ordered event stream, build queryable read models, and stay current through an async projection daemon with per-projection checkpoints and lag measurement.

### Implemented Read Models

- `src/projections/application_summary.py`
  - current state per application
  - optimized for dashboards and MCP application resources later
- `src/projections/agent_performance.py`
  - metrics per agent model version
  - tracks sessions, analyses, decisions, averages, decision distribution, and override rate
- `src/projections/compliance_audit.py`
  - audit-grade compliance record
  - supports current reads, temporal reads, and rebuild-from-scratch

### Projection Daemon

`src/projections/daemon.py` provides:

- ordered global event consumption
- per-projection checkpointing
- retry-then-skip fault tolerance
- lag reporting through `get_lag()` and `get_all_lags()`
- restart-safe resume from stored checkpoints

Checkpoint values store the next global position to process, which keeps replay semantics correct for both the PostgreSQL-backed store and the in-memory store used in tests.

### Phase 3 Higher-Band Evidence

The repository now includes two extra Phase 3 proof paths beyond the basic projection tests:

- `tests/test_projection_seed_rebuild.py`
  - replays the generated `data/seed_events.jsonl` history into the projections
  - confirms the rebuilt `ApplicationSummary`, `AgentPerformance`, and `ComplianceAudit` views match expectations derived independently from the seed stream
- `scripts/generate_projection_lag_report.py`
  - runs the `<800ms` lag check under 50 concurrent submissions
  - generates `artifacts/projection_lag_report.txt`
  - includes seed-rebuild confirmation details in the same artifact

### Projection Tables

`src/schema.sql` now includes:

- `application_summary`
- `agent_performance_ledger`
- `compliance_audit_current`
- `compliance_audit_history`

Helper tables for restart-safe incremental processing:

- `agent_session_projection_index`
- `application_decision_projection_index`

## Phase 4

Phase 4 adds schema evolution and tamper-evidence without breaking event-sourcing rules. Stored history remains immutable in PostgreSQL. Compatibility is handled on read, and integrity is checked against the raw stored stream.

### Upcasting

- `src/upcasting/registry.py` provides an async `UpcasterRegistry` that applies version chains automatically inside `EventStore.load_stream()`, `load_all()`, and `get_event()`
- `src/upcasting/upcasters.py` registers the required migrations:
  - `CreditAnalysisCompleted` v1 -> v2 infers `model_version` and `regulatory_basis` from the event timestamp window
  - `DecisionGenerated` v1 -> v2 reconstructs `model_versions` by replaying the contributing agent-session streams
- the Event Store also exposes raw-read paths with `apply_upcasters=False`, which lets integrity checks hash the original stored bytes instead of the migrated read model

### Integrity and Gas Town Recovery

- `src/integrity/audit_chain.py` implements `run_integrity_check()`
- each run hashes the raw source stream with SHA-256, compares against the previous stored audit hash, detects tampering, and appends a new `AuditIntegrityCheckRun` event to `audit-{entity_type}-{entity_id}`
- `src/integrity/gas_town.py` implements `reconstruct_agent_context()`, which rebuilds session context from the event stream, enforces the `AgentSessionStarted` anchor used by this codebase, tracks resume position, and flags `NEEDS_RECONCILIATION` when the replayed context is unsafe or incomplete

## Phase 5

Phase 5 exposes the ledger through FastMCP while keeping the CQRS boundary explicit.

### Command Tools

`src/mcp/tools.py` registers 8 tools:

- `submit_application`
- `start_agent_session`
- `record_credit_analysis`
- `record_fraud_screening`
- `record_compliance_check`
- `generate_decision`
- `record_human_review`
- `run_integrity_check`

These tools call the existing command handlers and integrity service, then synchronize the projection daemon so query resources can observe the committed state. Agent-facing tools also write session provenance events such as `AgentOutputWritten` and `AgentSessionCompleted`, which keeps Gas Town recovery and orchestrator causal validation intact.

### Query Resources

`src/mcp/resources.py` exposes 6 resources:

- `ledger://applications/{id}`
- `ledger://applications/{id}/compliance`
- `ledger://applications/{id}/audit-trail`
- `ledger://agents/{id}/performance`
- `ledger://agents/{id}/sessions/{session_id}`
- `ledger://ledger/health`

Projection-backed resources read from:

- `ApplicationSummaryProjection`
- `ComplianceAuditProjection`
- `AgentPerformanceProjection`

The two justified direct-stream exceptions are:

- `ledger://applications/{id}/audit-trail`
- `ledger://agents/{id}/sessions/{session_id}`

### MCP Runtime

`src/mcp/server.py` builds a FastMCP app and can run it over stdio or HTTP. The default HTTP command is:

```powershell
$env:DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m src.mcp.server --transport http --host 127.0.0.1 --port 8765
```

For in-process testing, `src/mcp/runtime.py` provides a thin gateway that uses `app.call_tool()` and resource URIs directly, including query-string aware reads for temporal compliance and audit-trail range filtering.

## Run Tests

Fast local checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_document_processing.py tests\test_schema_and_generator.py -q
```

Phase 2 domain logic checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\phase2\test_domain_logic.py -q
```

Phase 3 projection checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_projections.py -q
```

Phase 4 upcasting, integrity, and Gas Town checks:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\test_upcasting.py tests\test_integrity.py tests\test_gas_town.py -q
```

Phase 5 MCP lifecycle and resource checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_mcp_lifecycle.py -q
```

Seed-backed Phase 3 rebuild verification:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_projection_seed_rebuild.py -q
```

Real PostgreSQL EventStore tests:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\test_event_store.py tests\test_concurrency.py -q
```

Full Phase 1 bundle:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_event_store.py tests\test_concurrency.py tests\test_document_processing.py tests\test_schema_and_generator.py -q
```

Full Phase 1 + Phase 2 bundle:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_event_store.py tests\test_concurrency.py tests\test_document_processing.py tests\test_schema_and_generator.py tests\phase2\test_domain_logic.py -q
```

Full Phase 1 + Phase 2 + Phase 3 bundle:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_event_store.py tests\test_concurrency.py tests\test_document_processing.py tests\test_schema_and_generator.py tests\phase2\test_domain_logic.py tests\test_projections.py tests\test_projection_seed_rebuild.py -q
```

Full Phase 1 through Phase 4 bundle:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_event_store.py tests\test_concurrency.py tests\test_document_processing.py tests\test_schema_and_generator.py tests\phase2\test_domain_logic.py tests\test_projections.py tests\test_projection_seed_rebuild.py tests\test_upcasting.py tests\test_integrity.py tests\test_gas_town.py -q
```

Full Phase 1 through Phase 5 bundle:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_event_store.py tests\test_concurrency.py tests\test_document_processing.py tests\test_schema_and_generator.py tests\phase2\test_domain_logic.py tests\test_projections.py tests\test_projection_seed_rebuild.py tests\test_upcasting.py tests\test_integrity.py tests\test_gas_town.py tests\test_mcp_lifecycle.py -q
```

Optional live Ollama smoke test:

```powershell
$env:RUN_OLLAMA_TESTS='1'
$env:OLLAMA_PART_MODEL='qwen3-coder:480b-cloud'
$env:OLLAMA_PACKAGE_MODEL='qwen3-coder:480b-cloud'
.\.venv\Scripts\python.exe -m pytest tests\test_document_processing.py -q -k ollama
```

## Analyze One Company Package

Without LLM summaries:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_documents.py --company COMP-024
```

With Ollama summaries:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_documents.py --company COMP-024 --with-llm
```

Persist the extracted package into the Event Store:

```powershell
$env:DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe scripts\analyze_documents.py --company COMP-024 --application-id APEX-DOC-024 --persist-events
```

Generate the Phase 3 projection lag artifact:

```powershell
.\.venv\Scripts\python.exe scripts\generate_projection_lag_report.py
```

## Current Status

- `src/event_store.py` is ready for the interim Phase 1 deliverable path.
- `src/document_processing/` gives us a working bridge from generated documents to structured facts, `docpkg-*` event streams, and optional package summaries.
- `src/aggregates/` and `src/commands/handlers.py` now provide the replay-driven Phase 2 domain layer with business rule enforcement before append.
- `src/projections/` now provides all 3 required read models, the async daemon, checkpointing, lag metrics, temporal compliance queries, rebuild-from-scratch support, and seed-backed rebuild verification.
- `src/upcasting/` and `src/integrity/` now provide Phase 4 schema evolution, audit-chain verification, and Gas Town recovery.
- `src/mcp/` now provides the Phase 5 FastMCP layer with 8 tools, 6 resources, a runnable server entry point, and an MCP-only lifecycle test.
- `reports/phase_1.md`, `reports/phase_2.md`, and `reports/phase_3.md` plus `phase_4.md` and `phase_5.md` capture the implementation details, test results, and current rubric fit.
