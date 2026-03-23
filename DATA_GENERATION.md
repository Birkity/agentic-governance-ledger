# DATA_GENERATION.md

This document explains how the repository generates its seed world: company master data, financial history, document packages, and event-store history. It is intentionally based on the current codebase rather than the original starter wording.

## Purpose

The generator exists to produce a reproducible lending sandbox with:

- realistic applicant registry data
- three years of GAAP-style financial history per company
- heterogeneous document packages for extraction and audit testing
- seeded multi-stream event history that exercises the end-to-end ledger design

The generated data is used for:

- local development
- schema and generator validation
- document-extraction testing
- projection rebuild tests
- audit and regulatory package scenarios

## Main Entry Point

The generator entry point is [generate_all.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/generate_all.py).

It orchestrates five steps:

1. generate company profiles
2. generate financial documents
3. simulate event history
4. validate all generated events against the canonical schema
5. write the outputs to PostgreSQL and local artifacts

Typical command:

```powershell
.\.venv\Scripts\python.exe datagen\generate_all.py --db-url postgresql://postgres:YOUR_PASSWORD@localhost/apex_ledger --output-dir data --docs-dir documents
```

## Generated Outputs

The generator writes to three places:

- [data](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/data)
  - `applicant_profiles.json`
  - `seed_events.jsonl`
- [documents](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/documents)
  - one directory per company with PDFs, workbook, and CSV summary
- PostgreSQL
  - Applicant Registry tables
  - Event Store tables
  - initial projection checkpoints

Current generated snapshot in this workspace:

- 80 company directories in `documents/`
- 80 company profiles in `data/applicant_profiles.json`
- 1,198 seeded events in `data/seed_events.jsonl`

## Company Generation Rules

Company generation lives in [company_generator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/company_generator.py).

### Industry mix

The generator uses fixed industry buckets with target counts and financial parameter ranges:

- logistics
- manufacturing
- technology
- healthcare
- retail
- professional services
- construction
- other

Each industry defines:

- revenue range
- gross-margin range
- EBITDA-margin range
- asset multiplier range
- debt-ratio range
- NAICS code
- supported loan-purpose set

### Trajectory mix

Every company is assigned one trajectory:

- `GROWTH`
- `STABLE`
- `DECLINING`
- `RECOVERING`
- `VOLATILE`

The distribution is fixed in code so the seeded world always contains a mix of strong, weak, and ambiguous applicants.

### Risk shaping

Risk segment is derived from generated financial posture and compliance/default signals:

- active serious compliance flags or synthetic default cues push a company toward `HIGH`
- strong growth or stable performance with lower leverage pushes toward `LOW`
- the remaining companies are typically `MEDIUM`

### Special compliance case

The generator guarantees at least one Montana company by forcing one applicant to `jurisdiction = "MT"`. This supports the hard-block compliance narrative tied to `REG-003`.

## Financial History

Every generated company receives three fiscal years of GAAP-style financial history for 2022, 2023, and 2024.

Fields include:

- revenue and profit metrics
- EBITDA and depreciation
- assets, liabilities, and equity
- current assets and current liabilities
- debt and leverage ratios
- cash-flow metrics
- balance-sheet consistency flag

The financial history is not copied from static templates. It is synthesized from the company’s industry parameters and trajectory so that later years evolve from earlier ones.

Examples of trajectory behavior:

- `GROWTH` trends upward
- `DECLINING` trends downward
- `RECOVERING` dips and then rebounds
- `VOLATILE` varies materially year to year

## Document Corpus

Document generation is handled by:

- [pdf_generator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/pdf_generator.py)
- [excel_generator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/excel_generator.py)

Each company gets five files:

- `income_statement_2024.pdf`
- `balance_sheet_2024.pdf`
- `application_proposal.pdf`
- `financial_statements.xlsx`
- `financial_summary.csv`

At 80 companies, that produces 400 files.

### PDF variants

The PDF generator intentionally introduces layout variation so the extraction pipeline is not only tested on perfect statements.

Income statement variants:

- `clean`
- `dense`
- `scanned`
- `missing_ebitda`

Balance sheet variants:

- `clean`
- `scanned`

The balance sheet generator also introduces occasional small rounding adjustments to test tolerance for minor discrepancies.

### Workbook structure

The Excel workbook contains three sheets:

- `Income Statement`
- `Balance Sheet`
- `Key Ratios`

This gives the extraction layer a structured alternative to the PDFs and supports cross-source consistency checks.

### CSV summary

The CSV file is a lightweight one-row-per-field export of the latest fiscal year. It is intentionally simple and serves as a structured validation and fallback source.

## Seed Event History

Event simulation lives in [event_simulator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/event_simulator.py).

The simulator seeds event history across the ledger’s stream families rather than writing a single flattened application log. It emits events across:

- `loan-{application_id}`
- `docpkg-{application_id}`
- `credit-{application_id}`
- `fraud-{application_id}`
- `compliance-{application_id}`
- `agent-{agent_type}-{session_id}`

The simulator mirrors the intended agent pipeline by recording:

- application submission and document upload lifecycle
- document-processing sessions and extraction outputs
- credit-analysis sessions and completed analysis
- fraud-screening sessions and outcomes
- compliance evaluation and hard-block behavior
- orchestrated decisions and final approval or decline

### Seed scenario mix

The generator does not seed only happy paths. It creates application histories at multiple target states:

- `SUBMITTED`
- `DOCUMENTS_UPLOADED`
- `DOCUMENTS_PROCESSED`
- `CREDIT_COMPLETE`
- `FRAUD_COMPLETE`
- `APPROVED`
- `DECLINED`
- `DECLINED_COMPLIANCE`
- `REFERRED`

The current scenario counts sum to 29 seeded applications.

### Synthetic agent metadata

The event simulator also writes realistic-looking session metadata such as:

- `model_version`
- `langgraph_graph_version`
- per-node execution events
- tool-call events
- synthetic token counts
- synthetic `llm_cost_usd`

These cost values are simulated metadata for seeded history only. The data generator itself does not call a paid external API.

## Schema Validation

All generated events are validated before they are written.

Validation is implemented in [schema_validator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/schema_validator.py) and uses [events.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/src/models/events.py) as the canonical event catalogue.

For each generated event, the validator checks:

- event type exists in `EVENT_REGISTRY`
- payload can be instantiated by the corresponding Pydantic model

If validation fails, generation stops with an assertion error instead of seeding invalid history.

## Database Writes

The generator writes both reference data and event-store data into PostgreSQL.

### Applicant Registry schema

The generator creates and populates:

- `applicant_registry.companies`
- `applicant_registry.financial_history`
- `applicant_registry.compliance_flags`
- `applicant_registry.loan_relationships`

These tables act as read-only reference data for the ledger.

### Event Store schema

The generator also creates and populates:

- `events`
- `event_streams`
- `projection_checkpoints`
- `outbox`
- `snapshots`

It initializes projection checkpoints for:

- `application_summary`
- `agent_performance`
- `compliance_audit`

### Ordering behavior

When writing seeded events, the generator assigns:

- per-stream position by incrementing within each `stream_id`
- global position through the database identity column

This means the generated history is compatible with replay, projections, and integrity checks from the start.

## Reproducibility

The generator accepts `--random-seed` and defaults to `42`.

This makes the seed world reasonably reproducible:

- company mix
- trajectory mix
- document variants
- application scenario distribution

Not every timestamp or UUID is human-stable, but the overall seeded world is deterministic enough for development and regression testing.

## Cost Model

The data generator itself has no required cloud-model cost.

- company generation is local
- PDF/XLSX/CSV generation is local
- event simulation is local
- schema validation is local
- database writes are local

The only cost-like values written during generation are synthetic session metrics in the seeded events so downstream analytics can operate on realistic agent telemetry.

Optional local summarization elsewhere in the repository uses user-provided local models and is outside the seed generator itself.

## Design Decisions

### Why generate both documents and events

The repository tests two different boundaries:

- external document ingestion
- internal ledger replay

Generating only events would weaken document-processing tests. Generating only documents would weaken event-store and projection tests. The current design supports both.

### Why use staged application outcomes

The seeded world includes incomplete applications, happy paths, declines, compliance hard blocks, and referred cases so the codebase can be tested against partial and terminal states, not just ideal flows.

### Why keep raw files on disk

The ledger stores the interpretation of documents, not the document bytes themselves. Keeping PDFs, workbooks, and CSVs on disk preserves a clean boundary between external evidence and event-sourced business history.

## Limitations

- The generator creates realistic synthetic data, not statistically representative production data.
- The synthetic `llm_cost_usd` values are useful for metrics testing, but they are not measured from real model invocations.
- Some seeded timestamps and identifiers are generated at runtime, so byte-for-byte artifact equality is not the goal.
- The seeded scenarios cover representative lifecycle branches, but they do not replace the full narrative and end-to-end submission artifacts.

## Related Files

- [generate_all.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/generate_all.py)
- [company_generator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/company_generator.py)
- [pdf_generator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/pdf_generator.py)
- [excel_generator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/excel_generator.py)
- [event_simulator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/event_simulator.py)
- [schema_validator.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/datagen/schema_validator.py)
- [events.py](c:/Users/Ab/OneDrive/Desktop/10%20Academy/Week%205/agentic-governance-ledger/src/models/events.py)
