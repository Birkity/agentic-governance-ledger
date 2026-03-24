# DESIGN.md

This document is organized around the six required architectural sections from the challenge brief. I also include appendices for the supporting-document topics that do not map neatly onto those six headings but still matter for the final submission quality.

The guiding principle of this repository is simple: the event stream is the source of truth, and everything else is either an external input or a derived view. That decision drives the schema, aggregate boundaries, retry behavior, projection design, upcasting strategy, integrity model, MCP surface, and the Phase 6 counterfactual and regulatory packaging work.

## 1. Aggregate Boundary Justification

### LoanApplication

`loan-{application_id}` is the business decision boundary. It owns lifecycle transitions such as submission, analysis progression, recommendation generation, human review, approval, and decline. The most important reason to keep this boundary narrow is concurrency: every event in this stream changes the official application state machine, so writes here must be rare, meaningful, and heavily protected by optimistic concurrency control.

I considered folding compliance evidence directly into the loan stream. I rejected that because it would couple two different write patterns:

- business lifecycle transitions, which are sparse and stateful
- compliance rule evidence, which is detailed and potentially noisy

If those were merged, every compliance rule result would increase contention on the same stream version that decision-generation and human-review commands need. The concrete failure mode is a compliance worker writing intermediate rule events while a decision worker tries to advance the application state, producing unnecessary OCC collisions on a stream that should only represent canonical lifecycle milestones.

### AgentSession

`agent-{agent_type}-{session_id}` is the recovery and model-identity boundary. It exists to preserve Gas Town context, the agent’s model version, and the causal record of what that session wrote elsewhere.

I considered embedding agent-session facts in the domain streams they touch. I rejected that because recovery would then require reconstructing one agent’s memory by scanning multiple aggregates and inferring which events belonged to which invocation. That is fragile. A dedicated session stream means one replay path answers:

- which model ran
- what application it belonged to
- what outputs it wrote
- where recovery should resume

The concurrency implication is also favorable: agent-session bookkeeping can proceed without consuming the same version budget as the application stream.

### ComplianceRecord

`compliance-{application_id}` is a separate aggregate because compliance is not just another application status; it is a regulatory evidence trail. The system needs both a current compliance verdict and a temporally queryable compliance history.

I considered storing only the final compliance verdict on `loan-{application_id}` and projecting the rest from logs or summaries. I rejected that because regulators do not ask only for the final answer. They ask which rules were evaluated, when they were evaluated, which rule version applied, and whether a hard block existed at a particular time.

The OCC implication is important: separating compliance from the loan stream allows rule-by-rule evidence to accumulate without fighting with human-review or orchestrator writes on the core lifecycle stream.

### CreditRecord

`credit-{application_id}` holds the credit analysis work product rather than the decision-state machine. It exists because credit analysis evolves independently of final approval. A recommendation limit, risk tier, confidence, and rationale are analytic facts, not the application’s final state.

I considered writing `CreditAnalysisCompleted` directly to the loan stream. I rejected that because credit analysis and final decision are different concerns with different evolution rates. The credit stream lets the system:

- preserve analytic detail
- rerun decision logic against the latest analytic output
- protect the final application stream from becoming a data dump

The concurrency benefit is that a credit-analysis rerun or update does not need to compete with the same version counter used for final decision or human-review events.

### FraudScreening

`fraud-{application_id}` follows the same reasoning as the credit stream. Fraud analysis has its own lifecycle, input sources, and failure modes. It is analytically adjacent to the application but not identical to it.

I considered merging credit and fraud into one “analysis” stream. I rejected that because fraud and credit have different retry semantics, different domain meanings, and different downstream consumers. Combining them would create artificial coupling between two independently useful histories and would increase OCC collisions whenever one analytic worker retried after the other had already advanced the shared stream.

### DocumentPackage

`docpkg-{application_id}` exists because document ingestion is a conversion process from external files to structured facts. The raw files live on disk, but the ingestion lifecycle belongs in the ledger:

- file registered
- format validated
- extraction started
- extraction completed
- quality assessed
- package ready

I considered storing extraction events on the loan stream because documents conceptually belong to the application. I rejected that because extraction is operationally noisy and should not contend with application-state writes. The separate document-package stream preserves a detailed audit of how external evidence became usable facts without bloating the core application boundary.

### AuditLedger

`audit-{entity_type}-{entity_id}` exists because integrity checks are append-only attestations about another stream. They should not mutate or crowd the source stream they verify.

I considered storing integrity hashes as metadata on the source stream’s latest row. I rejected that because it would violate the append-only model and destroy the ability to reason about the history of integrity checks over time. A dedicated audit stream lets the system prove:

- when each integrity check ran
- what prior hash it chained from
- how many events it covered
- whether tampering had already been detected

### Boundary Summary

The common pattern is that each aggregate boundary is chosen to reduce false contention, preserve replay semantics, and keep one stream responsible for one kind of truth. The system gains concurrency safety not by avoiding shared state entirely, but by making sure unrelated work does not advance the same stream version counter.

## 2. Projection Strategy

### Why Projections Are Async

All three main projections in this repository are async rather than inline:

- `ApplicationSummaryProjection`
- `AgentPerformanceProjection`
- `ComplianceAuditProjection`

I chose async projections because the event store is authoritative and projections are explicitly rebuildable. Inline projection updates would reduce freshness lag, but they would also expand the failure surface of the command transaction. In a regulated system, I prefer a successful authoritative append plus a measurable lagging read side over a wider write transaction that can fail because a dashboard view update encountered an unrelated issue.

### SLO Commitments

The current repository already measures projection lag and stores per-projection checkpoints. The lag report artifact shows:

- 50 concurrent submissions
- `lag_ms = 0`
- `lag_positions = 0`
- `<800ms` target met

The design SLOs I would commit to are:

- `ApplicationSummaryProjection`: under 500ms in normal operation
- `AgentPerformanceProjection`: under 1s in normal operation
- `ComplianceAuditProjection`: under 2s in normal operation

The application summary is user-facing and should be freshest. Compliance audit can tolerate slightly more lag because its primary purpose is correctness and explainability, not sub-second interaction.

### Checkpoint Strategy

Each projection tracks its own checkpoint in `projection_checkpoints`. The daemon resumes from the last committed position. This design is intentionally simple:

- one projection can lag or fail without blocking the others
- restart safety is explicit
- rebuild-from-scratch remains possible because projections are derived only from events

### ComplianceAudit Temporal Query And Snapshot Strategy

The compliance projection uses an event-triggered snapshot strategy rather than a time-trigger or manual snapshot.

Each handled compliance event:

- updates the current compliance row
- increments `snapshot_version`
- appends a row to `compliance_audit_history`

I chose event-triggered snapshots because compliance timelines are short, semantically meaningful, and regulator-facing. The benefit is that `get_compliance_at(application_id, timestamp)` does not need to replay the stream every time. The cost is extra storage, but that tradeoff is acceptable because compliance histories are narrow and the storage overhead is small compared to the audit value.

There is no invalidation in the CRUD sense because the source stream is append-only. “Invalidation” means one of two things:

- the current row is replaced by a more recent derived state
- a rebuild resets both current and history tables and replays from event zero

That is simpler and safer than maintaining partial mutable snapshots with in-place correction.

### Week 3 Integration Contract

The document pipeline is the boundary between external files and the event-sourced write side.

Input to the pipeline:

- document path
- detected document type
- application or package identity

Output from the pipeline:

- raw extracted text where available
- structured `FinancialFacts`
- document-level notes
- document-level summary

If extraction is partial, the output is still usable. Missing values remain `None`, quality notes are preserved, and downstream credit logic must respond to that uncertainty rather than pretending the input was complete. This is why `ExtractionCompleted` and `QualityAssessmentCompleted` are separate events: the system can distinguish “the extractor produced a partial structured result” from “the package is high enough quality to analyze confidently.”

## 3. Concurrency Analysis

### Expected OCC Error Rate

Under peak load with 100 concurrent applications and four analytic actors per application, I expect OCC collisions on `loan-{id}` streams to be low, not high, because the stream partitioning is application-centric. Most work happens on side streams:

- `docpkg-{id}`
- `credit-{id}`
- `fraud-{id}`
- `compliance-{id}`
- `agent-*`

The loan stream becomes hot mainly at these boundaries:

- `ApplicationSubmitted`
- follow-up lifecycle requests
- `DecisionGenerated`
- `HumanReviewCompleted`
- `ApplicationApproved` / `ApplicationDeclined`

In ordinary operation I would expect low single-digit OCC errors per minute across 100 concurrent applications, not dozens, because collisions require two writers to target the same application stream at nearly the same time. The current test suite proves the collision behavior for one hot stream; it does not yet provide a measured minute-by-minute fleet rate, so this number is an engineering estimate rather than a benchmark result.

### Retry Strategy

The right retry behavior after `OptimisticConcurrencyError` is:

1. reload the stream
2. replay the aggregate
3. re-evaluate the command against the new state
4. only retry if the command is still valid

This matters because “retry the same write blindly” is unsafe in an event-sourced system. If another writer already changed the state, the command may now be invalid or unnecessary.

My intended retry policy is:

- attempt 1: immediate reload
- attempt 2: after 25ms + jitter
- attempt 3: after 75ms + jitter
- attempt 4: after 150ms + jitter

If all attempts fail, return a structured error to the caller. The maximum retry budget should be four total attempts for user-facing calls. Beyond that, the latency cost outweighs the benefit, and the caller needs to decide whether to resubmit or surface a conflict.

### Concrete Failure Modes

The concurrency story is only part of the operational picture. The system also needs to handle:

- LLM timeout in document summarization
  - current behavior: treat summarization as optional and fail without corrupting extraction facts
- document extraction partial success
  - current behavior: preserve partial fields and notes rather than failing closed
- registry query failure
  - design intent: return a structured domain error and do not append dependent decision events
- projection daemon failure on one event
  - current behavior: retry and then skip, without crashing the whole system
- integrity chain mismatch
  - current behavior: append `AuditIntegrityCheckRun` with `tamper_detected=True`
- Gas Town recovery on incomplete session history
  - current behavior: reconstruct context and flag `NEEDS_RECONCILIATION`

### NARR-03 Style Recovery

The support document calls out crash recovery as a narrative-quality signal. The current repository models this through session history rather than an in-memory agent object. That is the right design. If a worker crashes mid-session, the authoritative recovery inputs are:

- `AgentSessionStarted`
- per-session progress events
- `AgentOutputWritten`
- any recorded failure or recovery markers

Reconstruction from the session stream is safer than “resume from whatever the process happened to have in RAM.”

## 4. Upcasting Inference Decisions

### CreditAnalysisCompleted v1 -> v2

The upcaster infers:

- `model_version`
- `regulatory_basis`

and deliberately does not fabricate anything analogous to a missing confidence field.

#### `model_version`

Inference method:

- map `recorded_at` to a deployment window

Likely error rate:

- low if deployment windows are maintained accurately
- effectively 0 for coarse historical windows with one active deployed model
- non-trivial if model rollouts overlapped or backfills were replayed late

Downstream consequence of a wrong inference:

- audit misattribution of which model produced the result
- misleading regulator-facing provenance

When to choose null instead:

- whenever the timestamp window does not map deterministically to one deployed model

#### `regulatory_basis`

Inference method:

- map `recorded_at` to the active policy and GAAP baseline set

Likely error rate:

- very low if policy change dates are controlled

Downstream consequence of a wrong inference:

- incorrect explanation of which policy governed the decision
- audit friction, but not direct corruption of the financial numbers

When to choose null instead:

- if policy history is incomplete or multiple rule sets were active simultaneously

### DecisionGenerated v1 -> v2

The upcaster infers:

- `model_versions`

Inference method:

- replay contributing session streams and read `AgentSessionStarted.model_version`

Likely error rate:

- near zero if the referenced session streams exist and are intact
- effectively 100% unknown if the session stream is missing or the reference is invalid

Downstream consequence of a wrong inference:

- wrong attribution of the decision synthesis model
- weak regulator package provenance

When to choose null or empty over inference:

- if the contributing sessions are missing, return an empty map rather than fabricate a model inventory

### Why Null Is Sometimes The Right Answer

In an event-sourced system, “unknown” is often better than “plausible but invented.” The line I use is:

- infer only when there is a deterministic evidence chain
- otherwise preserve the uncertainty explicitly

That is why this repository prefers null or empty values for genuinely unknowable historical fields instead of smoothing the data into something that looks complete.

## 5. EventStoreDB Comparison

### Concept Mapping

The PostgreSQL implementation maps to EventStoreDB concepts like this:

- `stream_id` -> EventStoreDB stream ID
- `load_stream()` -> stream read by revision
- `load_all()` -> EventStoreDB `$all`
- `projection_checkpoints` -> persistent subscription checkpoint state
- `ProjectionDaemon` -> consumer group / persistent subscription worker

### What EventStoreDB Would Give More Naturally

EventStoreDB would give first-class event-store ergonomics that PostgreSQL does not provide out of the box:

- native append semantics with stream revisions
- built-in `$all` subscriptions
- persistent subscription primitives
- event-store-specific operational tooling
- retention, scavenging, and stream tombstone semantics that are part of the product

### What PostgreSQL Makes This Implementation Work Harder To Achieve

With PostgreSQL, this repository has to build or justify more infrastructure manually:

- advisory-lock based concurrency coordination
- daemon checkpointing
- global-order scanning logic
- distributed projection ownership analysis
- outbox publication discipline
- integrity-chain conventions

I still think PostgreSQL is a reasonable choice here because:

- it is already the operational center of the repository
- the challenge explicitly expects table design and SQL reasoning
- it keeps the stack simple for a student project

But if the system were headed toward production-scale subscription fan-out, EventStoreDB would reduce custom infrastructure in the hottest part of the architecture.

### MCP Implication

The MCP layer benefits from the PostgreSQL choice because the same database can serve:

- authoritative event history
- projection-backed resources
- health and lag introspection

The cost is that the application must be careful not to blur CQRS boundaries. Just because the same database stores both truth and views does not mean command handlers should query projection tables for correctness.

## 6. What I Would Do Differently

### The Single Biggest Architectural Change

With another full day, I would introduce a dedicated service boundary between the core ledger and the outer AI-facing orchestration layer.

The current repository is clean and testable, but it still puts several concerns close together in one Python application:

- event store
- projections
- MCP surface
- document extraction
- What-If analysis
- regulatory package generation

That is fine for the challenge, but the best production version would separate:

- the ledger core
- the projection daemon
- the document ingestion worker
- the MCP server

The main benefit would be operational clarity: each service would scale, fail, and deploy independently. The tradeoff is complexity. For this project, a single-repo single-runtime structure was the right tradeoff. For a real enterprise deployment, it would eventually become a liability.

### The Most Honest “We Got This Wrong” Reflection

The biggest mismatch between the final codebase and the original support document is the AI-agent layer. The starter kit emphasized five LangGraph agents as first-class runtime components. The cleaned repository instead centers deterministic command handlers, MCP tools, and the document-processing boundary, and it removed the unused starter-only agent scaffolding.

That made the repository leaner and more honest, but it also means the final codebase is stronger on event-sourcing, projections, integrity, and MCP than it is on fully realized in-repo LangGraph prompt orchestration.

Given another full day, I would not bring back the old dead scaffolding. I would add one real production-grade agent module with:

- typed input contract
- prompt versioning
- explicit prompt iteration history
- node-by-node event capture
- cost attribution

That would provide the prompt-design evidence the support rubric wants without polluting the core repository with placeholder classes.

## 7. Current Maturity Framing

### What This Repository Proves Today

This repository already proves the challenge architecture at the code and test level:

- append-only event storage with optimistic concurrency and replay
- projection checkpoints, rebuild validation, and lag evidence
- read-time upcasting without mutating stored rows
- audit-chain verification and Gas Town recovery
- MCP command/query separation
- counterfactual replay and self-verifiable regulatory packaging
- live-backed runtime preference for the read-only Applicant Registry adapter
- a thin evented pipeline runner that records durable telemetry for five modeled agent roles

### What Remains Production Hardening

The following items should be described as next steps rather than as finished production guarantees:

- auth and RBAC for UI and MCP access
- a real outbox publisher and snapshot compaction strategy
- distributed projection ownership beyond the single-process daemon model
- browser end-to-end tests, failover drills, and longer-running soak tests
- external provider billing telemetry instead of workflow cost proxies when paid model APIs are used

## Appendix A. Data Boundary Decisions

The repository follows a strict data-boundary model:

| Data type | Lives in | Reason |
| --- | --- | --- |
| company profile and historical financials | Applicant Registry | pre-existing reference data, not application lifecycle history |
| compliance flags on the customer master | Applicant Registry | enterprise reference facts rather than events created by this application |
| raw PDF/XLSX/CSV files | filesystem under `documents/` | large external artifacts; the ledger records their interpretation, not their bytes |
| extraction results and quality outcomes | event store | created during the workflow and must be replayable |
| lifecycle state transitions | event store | core business truth |
| dashboards and audit views | projections | rebuildable read models |
| publication state | outbox | transactional boundary for downstream delivery |

The specific question “why are `compliance_flags` in the registry and not the event store?” has a clean answer: they are not created by the current application workflow. They are pre-existing enterprise context queried by the system. The event store should record what the system did with those flags, not claim ownership of the flags themselves.

## Appendix B. Week 3 Integration Architecture

The contract between document ingestion and the ledger is:

- inputs
  - file path
  - document type
  - company or application context
- outputs
  - extracted text where available
  - normalized `FinancialFacts`
  - extraction notes
  - optional summary

Partial extraction is allowed. `ExtractionCompleted` can contain a partially populated `FinancialFacts` object. Missing fields stay null. `QualityAssessmentCompleted` captures whether those gaps materially reduce confidence.

The downstream implication is that credit logic must react to uncertainty, not erase it. A low-quality package should reduce confidence or require review instead of being treated as a clean input.

## Appendix C. Prompt Design And Iteration Notes

The cleaned repository no longer ships the unused LangGraph starter stubs. The only live LLM-facing component in the codebase is the optional document summarizer. That means I cannot honestly claim a production-grade in-repo `CreditAnalysisAgent` prompt history today.

The prompt boundary I would use for a real `CreditAnalysisAgent` is:

- system prompt
  - role, regulatory caution, output contract, and prohibition on fabricating missing numbers
- user message
  - extracted financial facts
  - quality notes
  - historical context
  - requested amount and purpose
- neither
  - hidden policy overrides or mutable downstream state that the model should not be allowed to invent around

The key lesson from the document summarizer prompt already in the repo is the same one I would apply to a credit agent:

- keep the prompt grounded in structured facts
- ask for explanation, not hidden state mutation
- keep hard business constraints in deterministic Python code, not in prompt prose

That separation is intentional. I trust prompts to summarize, synthesize, and explain. I do not trust prompts to be the final enforcement point for lending invariants.

## Appendix D. Failure Modes And Recovery Matrix

| Failure mode | Current recovery posture |
| --- | --- |
| PostgreSQL OCC collision | reload, replay, and retry within a bounded budget |
| partial document extraction | keep partial facts, preserve notes, and continue with explicit quality caveats |
| LLM summarizer timeout | fail summarization without corrupting extraction facts |
| projection handler failure | retry, then skip the bad event rather than crash the daemon |
| integrity mismatch | append an `AuditIntegrityCheckRun` with tamper detection |
| incomplete agent context | reconstruct from session stream and flag `NEEDS_RECONCILIATION` |

The system is strongest where the authoritative state is event-sourced and deterministic. Where the system depends on optional AI behavior, the design tries to degrade gracefully and keep the ledger authoritative.
