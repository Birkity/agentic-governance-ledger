# The Ledger

The repository follows the Week 5 submission shape, with the assessed implementation under `src/` and the generated Apex Financial Services data in `documents/` and `data/`.

## Repo Layout

- `src/schema.sql`: PostgreSQL schema for events, streams, checkpoints, outbox, and snapshots
- `src/event_store.py`: async EventStore with OCC, ordered replay, checkpoints, archival, and metadata lookup
- `src/models/events.py`: canonical event catalogue plus `StoredEvent`, `StreamMetadata`, `DomainError`, and `OptimisticConcurrencyError`
- `src/aggregates/`: replay-driven domain aggregates for loans, agent sessions, and compliance records
- `src/commands/`: Phase 2 command handlers using the load -> validate -> determine -> append flow
- `src/document_processing/`: document parsing, optional Docling-first PDF extraction, event persistence helpers, and optional Ollama summaries
- `datagen/`: generator for the Applicant Registry seed data, document corpus, and seed event history
- `tests/`: Phase 1 in-memory tests, real PostgreSQL tests, concurrency tests, document-processing tests, Phase 2 domain tests, and schema/generator tests
- `scripts/analyze_documents.py`: CLI for analyzing a company package and optionally persisting it into the Event Store
- `reports/phase_1.md`: Phase 1 implementation report
- `reports/phase_2.md`: Phase 2 implementation report

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

## Run Tests

Fast local checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_document_processing.py tests\test_schema_and_generator.py -q
```

Phase 2 domain logic checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\phase2\test_domain_logic.py -q
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

## Current Status

- `src/event_store.py` is ready for the interim Phase 1 deliverable path.
- `src/document_processing/` gives us a working bridge from generated documents to structured facts, `docpkg-*` event streams, and optional package summaries.
- `src/aggregates/` and `src/commands/handlers.py` now provide the replay-driven Phase 2 domain layer with business rule enforcement before append.
- `reports/phase_1.md` and `reports/phase_2.md` capture the implementation details, test results, and current rubric fit.
