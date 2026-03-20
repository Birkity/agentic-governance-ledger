# The Ledger

This repository is now organized around the assessed Week 5 deliverable layout.

## Current Structure

- `src/`: submission-facing implementation modules
- `datagen/`: the provided data generator and simulator
- `data/`: generated seed outputs
- `documents/`: generated financial document corpus
- `tests/`: test suite and phase gates
- `scripts/`: pipeline entrypoints and demos
- `artifacts/`: generated submission artifacts
- `reports/`: working notes and supporting reports

## Core Deliverable Paths

The main code paths now align with the challenge deliverables:

- `src/event_store.py`
- `src/models/events.py`
- `src/aggregates/loan_application.py`
- `src/schema.sql`

The next implementation files will live here as phases continue:

- `src/aggregates/agent_session.py`
- `src/commands/handlers.py`
- `src/projections/`
- `src/upcasting/`
- `src/integrity/`
- `src/mcp/`

## Data And Generation

The generator has already been run successfully in this workspace.

Generated outputs currently present:

- `data/applicant_profiles.json`
- `data/seed_events.jsonl`
- `documents/COMP-*/...`

To regenerate from scratch:

```powershell
.\.venv\Scripts\python.exe datagen\generate_all.py --db-url postgresql://postgres:newcode@localhost/apex_ledger --output-dir data --docs-dir documents
```

To validate the schema and generator:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_schema_and_generator.py -q -p no:cacheprovider
```

## Notes On The Restructure

- The old `starter/` scaffold has been absorbed into the repo layout rather than used as a parallel fallback.
- The support document still matters architecturally, but the repo layout now follows the flatter `src/...` paths used by the deliverables.
- `DOMAIN_NOTES.md` remains at the repository root as a graded document deliverable.

## Next Implementation Step

The next code phase is Phase 1:

- extract and finalize the PostgreSQL schema in `src/schema.sql`
- implement the async `EventStore` in `src/event_store.py`
- keep `src/models/events.py` as the canonical event contract
- add the concurrency-focused test path required for the interim checkpoint
