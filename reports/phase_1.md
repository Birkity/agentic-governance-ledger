# Phase 1

## What Was Implemented

Phase 1 now covers the EventStore core plus the document package intake layer that later aggregates and agents will build on.

### Event Store

- `src/schema.sql`
  - `events`
  - `event_streams`
  - `projection_checkpoints`
  - `outbox`
  - `snapshots`
- `src/event_store.py`
  - `connect()` and schema bootstrap
  - `append()`
  - `load_stream()`
  - `load_all()`
  - `stream_version()`
  - `get_event()`
  - `archive_stream()`
  - `get_stream_metadata()`
  - `save_checkpoint()` and `load_checkpoint()`
- `src/models/events.py`
  - added `StoredEvent`
  - added `StreamMetadata`
  - added `DomainError`
  - added `OptimisticConcurrencyError`
  - extended `DocumentType` for generated workbook and summary files
  - extended `FinancialFacts` with `ebitda_margin`

### Document Processing

- `src/document_processing/pipeline.py`
  - optional `Docling`-first PDF extraction
  - `pdfplumber` fallback
  - `pypdf` fallback
  - XLSX parsing via `openpyxl`
  - CSV parsing and normalization
  - merged facts model with source backfill
  - consistency checks, including the support-doc balance-sheet discrepancy case
- `src/document_processing/event_writer.py`
  - turns an analyzed package into `docpkg-*` events
  - persists `PackageCreated`, `DocumentAdded`, `DocumentFormatValidated`, `ExtractionStarted`, `ExtractionCompleted`, `QualityAssessmentCompleted`, and `PackageReadyForAnalysis`
- `src/document_processing/summarizer.py`
  - local Ollama summarizer for per-document and package-level summaries
  - environment-based model selection
  - prompt shrinking / retry path for flaky cloud-backed responses
- `scripts/analyze_documents.py`
  - CLI to analyze a generated company folder
  - optional `--persist-events` path to write the package into PostgreSQL

## Design Decisions

### OCC and Transactions

- Every append runs inside a single PostgreSQL transaction.
- A stream-scoped advisory transaction lock is taken before the OCC check.
- If `expected_version` does not match the current stream version, `OptimisticConcurrencyError` is raised immediately.
- Event rows, stream version updates, and optional outbox rows all happen in the same transaction.

### Ordering

- PostgreSQL-backed streams use 1-based `stream_position` and 1-based `global_position`.
- The in-memory store keeps the starter test contract of 0-based stream positions so the provided Phase 1 suite still passes unchanged.
- `load_stream()` replays by `stream_position`.
- `load_all()` replays by `global_position` and supports projection-style checkpointing.

### Document Intake

- PDFs are treated as the primary business documents, matching the support doc’s DocumentProcessingAgent path.
- Docling is optional and used first when installed because it is better suited to harder layouts.
- Workbook and CSV files are supporting sources used for structured cross-checking and missing-field backfill.
- Missing values are preserved honestly. For example, a missing EBITDA line in the PDF remains `None` in the PDF extraction result and is only backfilled in the merged package view from structured sources.
- Raw documents remain on disk; extracted facts are persisted as immutable event payloads in the `docpkg-{application_id}` stream.

## Challenge Alignment

This implementation follows the challenge and support docs in the important Phase 1 ways:

- the event store is the single source of truth
- writes are immutable and append-only
- per-stream ordering and global ordering are both preserved
- OCC protects against conflicting agent decisions
- checkpoints exist for projection replay
- document inputs stay on disk while extracted facts become event-ready structured data
- PDFs, XLSX, and CSV files all map into the same normalized financial-facts shape for later agent use
- document processing can now write its results into the Event Store instead of only returning in-memory analysis objects

## Tests Run

### Final bundled Phase 1 run

```text
38 passed, 1 skipped
```

Command used:

```powershell
$env:TEST_DB_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe -m pytest tests\phase1\test_event_store.py tests\test_event_store.py tests\test_concurrency.py tests\test_document_processing.py tests\test_schema_and_generator.py -q
```

### Real PostgreSQL document persistence smoke

```text
docpkg-APEX-DOC-024 persisted with 27 ordered events
```

Command used:

```powershell
$env:DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger'
.\.venv\Scripts\python.exe scripts\analyze_documents.py --company COMP-024 --application-id APEX-DOC-024 --persist-events
```

### Live Ollama smoke

```text
1 passed, 6 deselected
```

Command used:

```powershell
$env:RUN_OLLAMA_TESTS='1'
$env:OLLAMA_PART_MODEL='qwen3-coder:480b-cloud'
$env:OLLAMA_PACKAGE_MODEL='qwen3-coder:480b-cloud'
.\.venv\Scripts\python.exe -m pytest tests\test_document_processing.py -q -k ollama
```

### Key coverage points

- in-memory OCC contract from the provided Phase 1 suite
- PostgreSQL append/load/checkpoint/archive behavior
- explicit double-decision concurrency test at `expected_version=3`
- schema/generator compatibility with the seeded challenge data
- missing-EBITDA fallback behavior
- balance-sheet discrepancy detection
- Docling-first priority path when available
- document package persistence into a real `docpkg-*` stream
- local-LLM document and package summary smoke test

## Sample Output

### Income statement summary excerpt (`COMP-024`, `qwen3-coder:480b-cloud`)

```text
This is an income statement for the fiscal year ending December 31, 2024.
It shows the company's financial performance over that period, including
revenues, costs, expenses, and profits.
```

### Package summary excerpt (`COMP-024`, `qwen3-coder:480b-cloud`)

```text
This applicant package includes a complete set of financial documents for
Stanley Tucker and Lee, an S-Corporation manufacturing business founded in
2005 with 381 employees.
```

## Challenges Faced

- `asyncpg` returned JSONB as strings on this local setup, so event loader paths now decode JSON explicitly before building `StoredEvent`.
- Parallel schema bootstraps could deadlock under concurrent test startup, so schema initialization now runs behind a PostgreSQL advisory lock.
- The cloud-backed Ollama models occasionally returned transient 502 responses on long prompts, so the summarizer now retries with a shortened prompt.
- The event schema does not store raw files, so document persistence was designed around immutable extraction and quality events rather than binary file blobs.

## Output Summary

The repo now has a working Phase 1 foundation:

- EventStore is implemented and tested against both the in-memory gate and real PostgreSQL.
- The explicit concurrency test required by the interim deliverable passes.
- Generated PDFs, XLSX files, and CSV summaries can now be read, normalized, optionally summarized with local Ollama models, and persisted as `docpkg-*` events.
- `README.md` has been updated with setup, testing, analysis, and persistence instructions.
