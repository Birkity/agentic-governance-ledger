# Starter Blueprint Report

## Purpose Of This Report

This report explains the `starter/` folder as the Apex Ledger starter kit for the Week 5 challenge, shows what was actually executed in this workspace, and maps each major starter component back to the challenge brief and the supporting document.

The goal is to make the starter feel less like a random scaffold and more like a blueprint for the system we are expected to build.

## What Was Run In This Workspace

The starter was executed successfully from this repository with local PostgreSQL.

Run details:

- Database created: `apex_ledger`
- Generator command used:

```powershell
.\.venv\Scripts\python.exe starter\datagen\generate_all.py --db-url postgresql://postgres:newcode@localhost/apex_ledger --output-dir starter\data --docs-dir starter\documents
```

- Phase 0 validation command used:

```powershell
.\.venv\Scripts\python.exe -m pytest starter\tests\test_schema_and_generator.py -q -p no:cacheprovider
```

Observed results:

- 80 companies generated
- 400 document files generated under `starter/documents/`
- 29 seeded applications simulated
- 1,198 events generated
- Schema validation passed with 0 errors
- Database write completed successfully
- Phase 0 test suite passed: `10 passed`

Database counts after generation:

- `applicant_registry.companies`: 80
- `applicant_registry.financial_history`: 240
- `applicant_registry.compliance_flags`: 8
- `event_streams`: 151
- `events`: 1,198
- `projection_checkpoints`: 3
- `outbox`: 0
- `snapshots`: 0

## Why The Starter Exists

The challenge brief says the Ledger is the shared append-only memory that earlier systems were missing. The support document then turns that into a concrete implementation plan: read-only Applicant Registry, append-only Event Store, agent session memory, CQRS projections, and MCP interfaces.

The `starter/` folder exists to encode those architectural constraints before we write any implementation code.

It gives us:

- the canonical event language
- the seeded business world the agents operate in
- the reference implementation pattern for one real agent
- the phase-by-phase tests that act as the grading gates

So the starter is not a finished submission. It is the official skeleton that keeps our implementation aligned with the challenge's rules.

## How The Starter Maps To The Challenge

The challenge and support doc describe five major architectural ideas. The starter mirrors each of them directly.

### 1. Event Store As The Single Source Of Truth

Challenge intent:

- all application lifecycle decisions must be written as immutable events
- writes must be append-only
- state must be reconstructable by replay

Starter mapping:

- `starter/ledger/event_store.py`
- `starter/ledger/schema/events.py`
- `starter/tests/phase1/test_event_store.py`
- `starter/tests/test_event_store.py`

Why it matters:

- this is the foundation for replay, audit, projections, and concurrency safety
- if this layer is wrong, every later phase is built on unstable ground

### 2. The Data Boundary

Support doc rule:

- the Applicant Registry is read-only
- it contains pre-existing company and financial data
- the event store contains application lifecycle history only

Starter mapping:

- `starter/datagen/company_generator.py`
- `starter/datagen/generate_all.py`
- `starter/ledger/registry/client.py`

Why it matters:

- agents are supposed to query external truth from the registry and write new decisions only to the event store
- this prevents confusing historical company master data with application workflow history

### 3. Gas Town Persistent Ledger Pattern

Challenge intent:

- an agent must write its session-start event before it does work
- the session stream is the agent's memory and recovery source

Starter mapping:

- `starter/ledger/agents/base_agent.py`
- `starter/ledger/agents/credit_analysis_agent.py`
- agent event types in `starter/ledger/schema/events.py`

Why it matters:

- crash recovery depends on replaying the session stream
- agent behavior is auditable only if node execution and tool calls are recorded as events

### 4. CQRS And Projections

Challenge intent:

- commands append events to the write side
- queries read from projections
- projections are rebuilt from the event log using checkpoints and global ordering

Starter mapping:

- `starter/ledger/projections/`
- `starter/ledger/event_store.py`
- projection checkpoint table creation in `starter/datagen/generate_all.py`

Why it matters:

- dashboards, compliance views, and MCP resources should not reconstruct state from streams on every read
- command validation must still stay on the write side, not in projections

### 5. Narrative Testing And Rubric Alignment

Challenge intent:

- passing happy-path code is not enough
- the system must handle concurrency, compliance hard blocks, crash recovery, and human review correctly

Starter mapping:

- `starter/tests/test_schema_and_generator.py`
- `starter/tests/phase1/test_event_store.py`
- `starter/tests/test_narratives.py`

Why it matters:

- the starter's tests are not just unit tests
- they are the phase gates that reflect the rubric

## The Seven Aggregate Stream Families

The support material emphasizes seven aggregate stream types. The starter encodes those stream families in `starter/ledger/schema/events.py`.

| Stream Pattern | Aggregate Role | Why It Exists |
|---|---|---|
| `loan-{application_id}` | LoanApplication | Main lifecycle stream from submission through final decision |
| `docpkg-{application_id}` | DocumentPackage | Tracks uploaded documents, extraction, validation, and package readiness |
| `agent-{agent_type}-{session_id}` | AgentSession | Stores the agent's session memory, per-node work, tool calls, and completion or failure |
| `credit-{application_id}` | CreditRecord | Holds the credit agent's work products and output |
| `compliance-{application_id}` | ComplianceCase | Holds rule-by-rule compliance evaluation history |
| `fraud-{application_id}` | FraudCase | Holds fraud screening facts and results |
| `audit-{entity_id}` | AuditLedger | Reserved for integrity and cross-stream audit evidence |

This is one of the most important things the starter gives us for free: the system's consistency boundaries are already named.

## Starter Folder Breakdown

## `starter/README.md`

Purpose:

- explains the intended setup flow
- describes what works out of the box
- shows what the student is supposed to implement phase by phase

Why it matters to the challenge:

- it is the starter's operating manual
- it tells us which files are provided as reference and which are graded implementation work

Important note from this workspace:

- the README assumes `postgresql://postgres:apex@localhost/apex_ledger`
- this machine used `postgresql://postgres:newcode@localhost/apex_ledger`
- the local run succeeded with the local password

## `starter/.env.example`

Purpose:

- documents the expected runtime variables

What it represents:

- LLM key
- database URL
- Applicant Registry URL
- generated documents location
- regulation version
- log level

Why it matters:

- it shows that the Week 5 solution is expected to integrate documents, DB state, compliance versioning, and agent execution in one environment

## `starter/requirements.txt`

Purpose:

- documents the intended technology stack for the starter

Why it matters:

- it shows the planned architecture, not just dependencies:
- `asyncpg` for PostgreSQL access
- `pydantic` for schema contracts
- `langgraph` and `anthropic` for agents
- `fastmcp` for the Phase 5 interface
- `reportlab` and `openpyxl` for the generated document corpus

Practical note from this workspace:

- the pinned `asyncpg>=0.29,<0.30` range is not compatible with Python 3.13 here
- the working environment already had `asyncpg 0.31.0`, which allowed the starter to run successfully

## `starter/data/`

Purpose:

- holds generated seed data such as `seed_events.jsonl`

Why it matters:

- this is the replayable seed event history used for validation, development, and projection rebuilding
- it gives the challenge a realistic baseline instead of an empty system

## `starter/documents/`

Purpose:

- stores the generated document corpus

What was generated here:

- per-company income statement PDFs
- balance sheet PDFs
- application proposal PDFs
- Excel workbooks
- CSV summaries

Why it matters:

- the challenge is not only about event storage
- it is a document-to-decision platform, so the document corpus is part of the system's input reality

## `starter/datagen/`

This folder is the world builder for the challenge.

### `starter/datagen/generate_all.py`

Purpose:

- main orchestration script for company generation, document generation, event simulation, schema validation, and DB writes

Why it matters:

- it is effectively the Day 1 environment bootstrap the support doc describes
- it creates both sides of the architecture:
- the external Applicant Registry
- the internal Event Store schema and seed history

### `starter/datagen/company_generator.py`

Purpose:

- creates 80 realistic companies with financials, trajectories, jurisdictions, flags, and loan context

Why it matters:

- the support document explicitly says the Applicant Registry is read-only, realistic, and pre-existing
- this file creates that pre-existing world

### `starter/datagen/pdf_generator.py`

Purpose:

- creates financial statement and proposal PDFs

Why it matters:

- these simulate the messy business documents the pipeline must ingest
- the challenge expects document extraction and quality logic later

### `starter/datagen/excel_generator.py`

Purpose:

- creates multi-sheet spreadsheet financial statements

Why it matters:

- the document-processing layer must handle more than one input shape
- this makes the scaffold more realistic and closer to enterprise workflow

### `starter/datagen/event_simulator.py`

Purpose:

- simulates realistic event histories across the loan lifecycle

Why it matters:

- it turns the static registry into application workflow history
- it reflects the support doc's sequence of document processing, credit, fraud, compliance, orchestration, and final outcome

### `starter/datagen/schema_validator.py`

Purpose:

- validates generated events against the canonical event schema

Why it matters:

- it protects the event contract from drift
- if generated events do not match `EVENT_REGISTRY`, later phases become misleading or invalid

## `starter/ledger/`

This is the actual application package we are meant to build out.

### `starter/ledger/schema/events.py`

Purpose:

- defines the canonical event contract
- defines enums, payload models, and the event registry
- encodes the stream vocabulary the whole system uses

Why it matters:

- this is the most important file in the starter
- every agent, generator, validator, and test depends on it
- the support doc explicitly says all 45 event types live here and should not be redefined elsewhere

### `starter/ledger/event_store.py`

Purpose:

- provides the main Phase 1 implementation seam
- includes the production `EventStore` stub and the `InMemoryEventStore` used by tests

Why it matters:

- this is where append-only writes, optimistic concurrency control, stream replay, global replay, and checkpoint support belong
- the challenge rubric treats this as foundational infrastructure

Important observation:

- the file is intentionally incomplete and still includes TODOs and duplicate in-memory store scaffolding
- that is a signal that this is a teaching starter, not production-ready code

### `starter/ledger/upcasters.py`

Purpose:

- provides the schema-evolution seam

Why it matters:

- the challenge requires immutable history
- when event shapes evolve, old rows should be adapted on read, not mutated in place

### `starter/ledger/registry/client.py`

Purpose:

- defines the read-only Applicant Registry access layer

Why it matters:

- it enforces the data-boundary rule from the support doc
- agents should query company history here rather than hardcoding or duplicating registry data

### `starter/ledger/domain/aggregates/loan_application.py`

Purpose:

- defines the loan application aggregate and replay-based state transition seam

Why it matters:

- Phase 2 business rules belong here
- commands are supposed to replay stream history, reconstruct state, validate rules, and then append new events

### `starter/ledger/projections/`

Purpose:

- placeholder package for the Phase 3 read side

Why it matters:

- this folder is where ApplicationSummary, AgentPerformance, ComplianceAudit, and the daemon should live
- it represents the CQRS query side the challenge expects

## `starter/ledger/agents/`

This folder encodes the agent architecture described in the support document.

### `starter/ledger/agents/base_agent.py`

Purpose:

- defines the common base class for all agents
- handles session-start recording, node-execution recording, tool-call recording, and session completion scaffolding

Why it matters:

- this is where the Gas Town pattern becomes real
- the support doc says every agent session must start with a session event before work begins

### `starter/ledger/agents/credit_analysis_agent.py`

Purpose:

- provides the full reference implementation pattern for one agent

Why it matters:

- this is the canonical example of how an agent should:
- validate prerequisites
- load data from the registry and event store
- run reasoning
- apply hard policy constraints in Python
- emit new events and trigger downstream work

### `starter/ledger/agents/stub_agents.py`

Purpose:

- contains the remaining four agent blueprints as structured stubs

Agents represented:

- DocumentProcessingAgent
- FraudDetectionAgent
- ComplianceAgent
- DecisionOrchestratorAgent

Why it matters:

- these stubs are not empty placeholders
- they encode the intended node sequences, dependencies, and expected write-side behavior for later phases

## `starter/scripts/`

### `starter/scripts/run_pipeline.py`

Purpose:

- intended CLI entry point for running one application through the system

Why it matters:

- this is the bridge from isolated components to end-to-end workflow execution

## `starter/tests/`

The tests folder is the starter's grading map.

### `starter/tests/test_schema_and_generator.py`

Purpose:

- validates the event schema and generator correctness

Why it matters:

- this is the Phase 0 gate
- the support doc explicitly says the generator is a deliverable, not optional setup

### `starter/tests/phase1/test_event_store.py`

Purpose:

- defines the contract for the Event Store, especially optimistic concurrency behavior

Why it matters:

- this is where the core append and OCC expectations are made concrete
- the "exactly one wins" concurrent write test is central to the challenge

### `starter/tests/test_event_store.py`

Purpose:

- DB-backed event-store tests

Why it matters:

- this is where the in-memory teaching store must eventually become a real Postgres-backed implementation

### `starter/tests/test_narratives.py`

Purpose:

- defines the five business narrative scenarios

Why it matters:

- these tests are closer to acceptance tests than unit tests
- they represent real failure modes and business outcomes from the challenge brief

## What The Starter Already Gives Us Vs What We Must Build

Already provided:

- canonical event schema
- company and document generator
- event simulator
- schema validator
- one reference agent pattern
- base agent scaffolding
- phase-oriented tests
- Event Store interface and in-memory teaching store

Expected for us to implement:

- real `EventStore` methods
- real `ApplicantRegistryClient`
- aggregate replay and business rules
- the remaining agent implementations
- projections and daemon
- upcasters
- MCP server
- final operational packaging

This split is important. The starter gives us the architectural rails, but the challenge grade comes from implementing the behavior that runs on those rails.

## Why The Starter Is A Good Blueprint

The starter is valuable because it reduces ambiguity in the parts that matter most:

- event names are fixed
- stream boundaries are fixed
- seed data shape is fixed
- test expectations are fixed
- agent orchestration pattern is fixed

That means we can focus our effort on correctness, not on inventing the system from scratch.

In other words:

- the challenge doc defines the architecture conceptually
- the support document explains the operating model
- the starter turns both into code structure, data shape, and executable tests

## Recommended Reading Order Inside The Starter

If the goal is to understand the blueprint before coding, this is the best reading path:

1. `starter/README.md`
2. `starter/ledger/schema/events.py`
3. `starter/tests/test_schema_and_generator.py`
4. `starter/datagen/generate_all.py`
5. `starter/datagen/event_simulator.py`
6. `starter/ledger/event_store.py`
7. `starter/ledger/domain/aggregates/loan_application.py`
8. `starter/ledger/agents/base_agent.py`
9. `starter/ledger/agents/credit_analysis_agent.py`
10. `starter/ledger/agents/stub_agents.py`
11. `starter/tests/test_narratives.py`

This order moves from architecture contract, to generated world, to persistence, to domain logic, to agent behavior, to acceptance criteria.

## Final Takeaway

The `starter/` folder is the Apex Ledger system blueprint.

It is not just sample code and it is not just boilerplate.

It defines:

- what data exists before the application starts
- what events can happen during the application lifecycle
- where state is supposed to live
- how agents are supposed to remember and recover
- how the write side and read side are supposed to stay separated
- how the project is graded phase by phase

After running it in this workspace, we can say the starter is operational as a setup baseline:

- data generation works
- document generation works
- event simulation works
- Postgres seeding works
- Phase 0 schema tests pass

That gives us a solid base for implementing the later phases without drifting away from the challenge's intended architecture.
