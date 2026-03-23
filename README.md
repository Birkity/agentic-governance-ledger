# The Ledger

The Ledger is an event-sourced lending platform for document-to-decision workflows. It treats the event store as the system of record, rebuilds business state by replay, derives query views through projections, exposes safe command and query surfaces through MCP, and supports audit-grade integrity checks, counterfactual replay, and regulator-facing package generation.

## What The System Does

- stores every meaningful business change as an immutable event
- enforces lending rules before new events are written
- rebuilds application state by replaying aggregate streams
- generates read models for dashboards, audit views, and MCP resources
- preserves schema compatibility through read-time upcasting
- verifies stream integrity through an audit hash chain
- reconstructs agent context after failure using Gas Town session history
- analyzes generated PDF, XLSX, and CSV inputs and persists their extracted facts
- exposes command tools and read resources through FastMCP
- supports counterfactual replay and self-contained regulatory package generation

## Repository Layout

- `src/schema.sql`
  - PostgreSQL schema for events, stream metadata, checkpoints, outbox, snapshots, and projection tables
- `src/event_store.py`
  - append-only EventStore and in-memory store used by the tests
- `src/models/events.py`
  - canonical event catalogue, stored-event wrapper, stream metadata model, and domain exceptions
- `src/aggregates/`
  - replay-driven aggregate boundaries for loan applications, agent sessions, compliance records, and the audit ledger
- `src/commands/`
  - command handlers that follow load -> replay -> validate -> determine -> append
- `src/document_processing/`
  - PDF/XLSX/CSV parsing, normalization, event writing, and optional local-model summarization
- `src/registry/`
  - read-only adapter for the seeded Applicant Registry schema
- `src/projections/`
  - application summary, agent performance, compliance audit, daemon, and seed validation helpers
- `src/upcasting/`
  - version-chain registry and concrete upcasters for evolved events
- `src/integrity/`
  - audit-chain verification and Gas Town recovery helpers
- `src/mcp/`
  - FastMCP server, command tools, query resources, and in-process runtime gateway
- `src/what_if/`
  - counterfactual replay helpers
- `src/regulatory/`
  - regulatory package generation and verification
- `DATA_GENERATION.md`
  - data-generation rules, document variants, seed scenarios, and database outputs
- `datagen/`
  - company, document, and seed event generation
- `tests/`
  - contract, integration, lifecycle, projection, integrity, MCP, and counterfactual tests
- `scripts/`
  - CLI helpers for document analysis, lag-report generation, counterfactual replay, and regulatory packages
- `artifacts/`
  - generated benchmark or submission artifacts such as the projection lag report
- `ui/`
  - Next.js command-center interface for application selection, event timelines, evidence previews, audit status, and operational guardrails

## Architecture

### Event Store

The write side is anchored in PostgreSQL.

- `events` stores immutable event rows
- `event_streams` tracks current per-stream version and archival state
- `projection_checkpoints` stores per-projection replay progress
- `outbox` reserves the transactional publication boundary
- `snapshots` is available for aggregate snapshotting when needed

Each append:

- checks `expected_version`
- acquires a stream-scoped advisory lock
- writes ordered events
- updates stream version
- optionally writes outbox rows in the same transaction

This gives the system:

- append-only history
- per-stream ordering
- global ordering
- optimistic concurrency control
- replay safety for aggregates and projections

### Aggregate Boundaries

The main business consistency boundaries are:

- `LoanApplication`
  - lifecycle, recommendation state, human review, final approval or decline
- `AgentSession`
  - Gas Town anchor, model version identity, and durable session context
- `ComplianceRecord`
  - rule-by-rule compliance evidence and approval gating

Specialized streams support the lifecycle without overloading the application stream:

- document package
- credit record
- fraud screening
- audit ledger

### Command Side

Command handlers never validate against projections. They always:

1. load streams
2. replay aggregate state
3. enforce domain rules
4. determine new event payloads
5. append with optimistic concurrency

This keeps the system auditable and replayable even under concurrent activity.

### Read Side

The read side is derived from the event stream through projections:

- `ApplicationSummaryProjection`
  - current application state and decision-facing summary
- `AgentPerformanceProjection`
  - metrics per agent, model version, and override behavior
- `ComplianceAuditProjection`
  - current and temporal compliance views

The projection daemon processes global event order, saves checkpoints, resumes safely after restart, and reports lag.

### Schema Evolution And Integrity

Stored history is never rewritten.

- upcasters run on read and migrate older events to the current logical shape
- integrity checks hash raw stored events and append `AuditIntegrityCheckRun` to audit streams
- Gas Town recovery rebuilds agent context from durable session history rather than trusting in-memory state

### External Interfaces

The MCP layer separates writes from reads:

- tools call command handlers and integrity services
- resources read projections by default
- direct stream reads are reserved for justified audit and session-replay cases

### Counterfactual And Regulatory Analysis

The What-If projector branches an application timeline in memory, substitutes events, skips downstream dependent events, and compares the counterfactual outcome to the real one without touching the live store.

The regulatory package generator creates a self-contained JSON package with:

- related event history
- audit stream
- projection state at an examination date
- integrity metadata
- narrative summary
- agent model metadata
- package hash and verification support

## Setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt` or `uv sync`.
3. Copy `.env.example` to `.env`.
4. Make sure PostgreSQL is running and that `apex_ledger` exists.

Example environment values:

```powershell
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger
TEST_DB_URL=postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger
DOCUMENTS_DIR=./documents
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_PART_MODEL=qwen3-coder:480b-cloud
OLLAMA_PACKAGE_MODEL=deepseek-v3.1:671b-cloud
```

If you want Docling support for richer PDF extraction:

```powershell
uv sync --extra docling
```

## Generate The Seed World

To regenerate the Applicant Registry data, document corpus, and seed event history:

```powershell
.\.venv\Scripts\python.exe datagen\generate_all.py --db-url postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger --output-dir data --docs-dir documents
```

Generated directories such as `data/` and `documents/` are ignored by git and can be recreated.

## Working With Documents

The document-processing layer reads:

- PDFs through `Docling -> pdfplumber -> pypdf`
- Excel workbooks through `openpyxl`
- CSV summaries through the standard `csv` module

It merges extracted facts into one structured package view and can persist the results as document-package events.

Analyze a company package:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_documents.py --company COMP-024
```

Analyze with local-model summaries:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_documents.py --company COMP-024 --with-llm
```

Persist the extracted package into the ledger:

```powershell
$env:DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe scripts\analyze_documents.py --company COMP-024 --application-id APEX-DOC-024 --persist-events
```

## Running MCP

Start the MCP server over HTTP:

```powershell
$env:DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m src.mcp.server --transport http --host 127.0.0.1 --port 8765
```

The exposed interface includes:

- command tools for application submission, session start, credit/fraud/compliance recording, decision generation, human review, and integrity checks
- resources for applications, compliance, audit trails, agent performance, agent sessions, and ledger health

## Counterfactual Replay And Regulatory Packages

Run a counterfactual replay:

```powershell
$env:DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe scripts\run_what_if.py --application-id YOUR_APP_ID --recommended-limit-usd 750000 --confidence 0.91 --risk-tier LOW
```

Generate a regulatory package:

```powershell
$env:DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe scripts\generate_regulatory_package.py --application-id YOUR_APP_ID
```

Generate the projection lag artifact:

```powershell
.\.venv\Scripts\python.exe scripts\generate_projection_lag_report.py
```

## UI Command Center

The repository also includes a Next.js interface in `ui/` for demos and operator workflows. It is designed to show:

- application selection and lifecycle status
- the full immutable event timeline for one application
- source documents and evidence previews
- human review state and override details
- audit integrity status
- projection lag and optimistic concurrency guardrail reports

The UI reads from the live PostgreSQL ledger when `DATABASE_URL` is available. If not, it falls back to the seeded JSON event world in `data/`.

Install and run the UI:

```powershell
cd ui
npm.cmd install
$env:DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
npm.cmd run dev
```

Then open `http://localhost:3000`.

If you ever see a stale Next.js chunk error such as `Cannot find module './948.js'`, clear the local build cache and restart the UI:

```powershell
cd ui
npm.cmd run clean
npm.cmd run dev
```

The default `npm.cmd run dev` and `npm.cmd run build` commands now clear stale `.next` output automatically before starting.

## Tests

Event store contract checks:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_event_store.py tests\test_concurrency.py -q
```

Document-processing and seed-generation checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_document_processing.py tests\test_schema_and_generator.py tests\test_registry_client.py -q
```

Domain logic checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\phase2\test_domain_logic.py -q
```

Projection and rebuild checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_projections.py tests\test_projection_seed_rebuild.py -q
```

Upcasting, integrity, and recovery checks:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\test_upcasting.py tests\test_integrity.py tests\test_gas_town.py -q
```

MCP lifecycle checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_mcp_lifecycle.py -q
```

Counterfactual and regulatory package checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase6.py -q
```

Full regression:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_event_store.py tests\test_concurrency.py tests\test_document_processing.py tests\test_schema_and_generator.py tests\phase2\test_domain_logic.py tests\test_projections.py tests\test_projection_seed_rebuild.py tests\test_upcasting.py tests\test_integrity.py tests\test_gas_town.py tests\test_mcp_lifecycle.py tests\test_phase6.py tests\test_registry_client.py -q
```

Optional live Ollama smoke test:

```powershell
$env:RUN_OLLAMA_TESTS='1'
$env:OLLAMA_PART_MODEL='qwen3-coder:480b-cloud'
$env:OLLAMA_PACKAGE_MODEL='qwen3-coder:480b-cloud'
.\.venv\Scripts\python.exe -m pytest tests\test_document_processing.py -q -k ollama
```

## Current State

The current codebase includes:

- a transactional append-only event store
- replay-based domain logic
- projection-driven read models
- read-time upcasting
- audit-chain verification
- Gas Town recovery
- MCP tools and resources
- What-If replay
- regulatory package generation and verification
- generated seed data and document fixtures

`DOMAIN_NOTES.md` remains the main domain reasoning artifact at the repo root.
