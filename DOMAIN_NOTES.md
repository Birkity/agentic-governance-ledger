# DOMAIN_NOTES.md
## Phase 0 - Domain Reconnaissance
## The Ledger: Event-Sourced Governance Backbone for Apex Financial Services

## 1. Scope and synthesis

- the main challenge brief, which defines the Ledger as the append-only event store, CQRS query layer, projection daemon, upcasting path, audit chain, and MCP surface for downstream consumers
- the support guide, which expands that same architecture into a full document-to-decision platform with a read-only Applicant Registry, a document corpus, five LangGraph agents, seven stream families, seeded data, and narrative failure scenarios

The key Phase 0 conclusion is that Week 5 is not "build an audit log." It is "build the operating model in which history is the system." The event store is not an after-the-fact trace. It is the authoritative record from which state, explanation, recovery, and compliance evidence are derived.

## 2. Architectural thesis

The Ledger exists because enterprise AI systems fail at the moment they need to answer any of the following:

- What exactly happened?
- What did the agent know at that moment?
- Which model, rule set, and document facts produced the decision?
- Can we replay the decision path and verify it was not tampered with?

CRUD tables and ad hoc logs answer these questions inconsistently. Event sourcing answers them structurally. Every meaningful fact in the application lifecycle becomes an immutable event. Current state is reconstructed from those events. Query performance comes from projections built from those events. Recovery uses those events. Auditability uses those events. The events are the system.

## 3. Data placement and source-of-truth decisions

The support guide adds an important boundary that the challenge brief only implies: not all data belongs in the event store.

| Data type | System of record | Why it lives there |
| --- | --- | --- |
| Company profile, historical financials, prior relationships | Applicant Registry | This is pre-existing business data. It existed before the current application and must remain read-only from the agentic system. |
| Compliance flags tied to the customer master record | Applicant Registry | These are external enterprise facts, not lifecycle events created by the current loan decision. |
| Raw uploaded documents and generated financial files | Filesystem / document corpus | PDFs, spreadsheets, and CSVs are large binary artifacts. The ledger records their presence and processing outcomes, not the raw file bytes themselves. |
| Document extraction outcomes and quality assessments | Event store | These are lifecycle facts created during processing and must be replayable and auditable. |
| Loan decision lifecycle facts | Event store | Submission, analysis, fraud screening, compliance decisions, orchestration, human review, approval, and decline are the core business history. |
| Agent execution memory | Agent session streams in the event store | Gas Town recovery requires each agent to rebuild its context from persisted session history. |
| Read models for dashboards and APIs | Projection tables | These are derived views optimized for query performance and should be rebuildable from the event stream. |
| Downstream publication state | Outbox table | This is the safety boundary between database commits and external message delivery. |

The rule is simple: if the fact is created during the application lifecycle and must be replayed, audited, or recovered from, it belongs in the event store. If it is pre-existing reference data, it stays in its own source system and is only queried.

## 4. The two non-negotiable rules from the support guide

### 4.1 The data boundary

Agents may query the Applicant Registry, but they never write to it. The event store captures what happens during the loan application lifecycle. The Applicant Registry captures who the company already is. Breaking this boundary would blur ownership, create synchronization problems, and make the Ledger less trustworthy because it would start behaving like a partially authoritative master-data store.

### 4.2 Gas Town session start before any work

The support guide phrases the recovery rule as `AgentSessionStarted` first. The challenge brief phrases the same idea as `AgentContextLoaded` before any decision event. The naming differs, but the architectural requirement is the same: the first persisted event in an agent session must anchor context, model identity, and recovery position before meaningful work begins. If the process crashes after that point, the next process can replay the stream and continue safely.

## 5. Reconciling the two Week 5 documents

The challenge brief and support guide are aligned, but they speak at different levels of abstraction.

| Topic | Main challenge brief | Support guide | Resolution |
| --- | --- | --- | --- |
| Aggregate model | Focuses on 4 governance-critical aggregates: LoanApplication, AgentSession, ComplianceRecord, AuditLedger | Operationalizes the platform as 7 stream families by adding DocumentPackage, CreditRecord, and FraudScreening work-product streams | Treat the 4 as the core governance aggregates and the 7 as the practical stream layout used by agents and projections. |
| Agent bootstrap event | Emphasizes `AgentContextLoaded` | Emphasizes `AgentSessionStarted` | Follow the eventual canonical schema file once implementation begins, but preserve the same invariant: no work before session bootstrap is persisted. |
| Event catalogue | Intentionally incomplete to force domain reasoning | Expands to 45 event types and describes who writes them | Use Phase 0 to reason from the conceptual catalogue, then defer final naming and completeness to the canonical schema source named in the support guide. |

This is not a contradiction. It is progressive disclosure: the challenge brief describes the architecture you must understand, and the support guide describes the operating environment you must survive.

## 6. Core concepts that Phase 0 must get exactly right

### 6.1 Event Sourcing vs Event-Driven Architecture

Event-Driven Architecture uses events as messages between components. Event Sourcing uses events as the source of truth. The Week 5 system may use both patterns, but The Ledger itself is event sourced. That means decisions are not reconstructed from "whatever rows happen to be current" or "whatever logs we still have." They are reconstructed from the ordered event history.

### 6.2 CQRS

Commands write facts. Queries read projections. The write side protects invariants and ordering. The read side optimizes for speed and usability. MCP tools naturally sit on the command side. MCP resources naturally sit on the query side.

### 6.3 Optimistic concurrency control

If two agents write to the same stream at the same time, the system must reject one writer rather than silently accepting both. This is not an implementation detail. It is how the architecture prevents conflicting truths.

### 6.4 Projections and lag

Read models are derived from the event stream and are therefore eventually consistent. That lag must be measured, surfaced, and designed for. A projection with no lag metric is not production-ready.

### 6.5 Upcasting

Old events are immutable. New readers change. Upcasting is the mechanism that lets newer code understand older event shapes without mutating stored history.

### 6.6 Outbox and audit chain

The outbox protects downstream publishing consistency. The audit chain protects tamper evidence. Together they make the Ledger both operationally safe and regulator-friendly.

## 7. Required answer 1 - EDA vs ES distinction

### Short answer

A callback-based tracing component such as LangChain traces is Event-Driven Architecture or telemetry, not Event Sourcing.

### Why

In a callback system, events describe work that happened somewhere else. They are observational. If the callback pipeline is disabled, delayed, dropped, or sampled, the application still runs. That means the callback event log cannot be the system of record.

In The Ledger design, the application lifecycle facts are written directly into the event store as part of the business transaction. The events are not a mirror of the system. They are the canonical state transition history.

### What would change if redesigned using The Ledger

1. The callback stream would stop being a passive trace and become a first-class append to the event store.
2. Domain events such as `ApplicationSubmitted`, `CreditAnalysisCompleted`, `ComplianceRulePassed`, and `HumanReviewCompleted` would be written as authoritative facts, not monitoring artifacts.
3. Read models such as `ApplicationSummary` and `ComplianceAuditView` would be built from those authoritative facts.
4. Agent session history would become replayable memory rather than transient trace output.

### What would be gained

- reproducibility of decision paths
- temporal queries such as "what did the system know at time T?"
- formal concurrency control on writes
- crash recovery through session replay
- an audit trail that regulators can treat as evidence rather than telemetry

## 8. Required answer 2 - The aggregate question

### Chosen aggregates

The challenge brief identifies four governance-critical aggregates:

- `LoanApplication`
- `AgentSession`
- `ComplianceRecord`
- `AuditLedger`

The support guide then operationalizes additional stream families (`DocumentPackage`, `CreditRecord`, `FraudScreening`) that are useful as dedicated work-product histories. Those do not invalidate the four core boundaries; they refine them.

### Alternative boundary considered and rejected

I considered merging `ComplianceRecord` into `LoanApplication` so that the application stream would hold both business-state transitions and rule-by-rule compliance evaluation.

### Why I rejected it

That merge creates three kinds of coupling:

1. **Write contention coupling**  
   Compliance evaluation and application-state mutation would fight over the same stream version. A long-running compliance sequence would increase unnecessary OCC collisions for unrelated application writes.

2. **Schema evolution coupling**  
   Regulatory evidence fields, rule versions, and compliance rationales evolve differently from business-lifecycle fields. Merging them would force one stream to carry two unrelated rates of change.

3. **Query-model coupling**  
   Compliance officers need a temporally queryable regulatory record. Loan officers need the current application state. A separate `ComplianceRecord` aggregate makes that projection strategy cleaner and reduces the temptation to over-read the loan stream for regulatory queries.

### Failure mode the chosen boundary prevents

The chosen boundary prevents a case where a compliance hard block and a business-state transition race on the same stream and leave the application lifecycle coupled to the internal sequencing of regulatory checks. In the support guide's Montana hard-block narrative, this separation matters: compliance can halt the path decisively without forcing the entire application lifecycle stream to become a dumping ground for every intermediate rule evaluation.

## 9. Required answer 3 - Concurrency in practice

Two AI agents process the same loan application. Both read stream version 3 and both attempt `append_events(..., expected_version=3)`.

### Exact sequence

1. Agent A loads `loan-{application_id}` and sees `current_version = 3`.
2. Agent B loads the same stream and also sees `current_version = 3`.
3. Both agents compute their candidate events independently.
4. Agent A opens a transaction, verifies that the stream's current version is still 3, inserts its event at `stream_position = 4`, updates `event_streams.current_version` to 4, and commits.
5. Agent B opens its transaction and performs the same version check, but now the stream's current version is 4.
6. The store rejects Agent B's append and raises `OptimisticConcurrencyError`.
7. Agent B must reload the stream, rebuild aggregate state, and decide whether the command is still valid or whether the new state makes the intended append unnecessary or invalid.

### What the losing agent should receive

At minimum:

- `stream_id`
- `expected_version = 3`
- `actual_version = 4`
- an error type such as `OptimisticConcurrencyError`
- a suggested action such as `reload_stream_and_retry`

### What the losing agent must do next

It must not blindly retry the same write. It must:

1. reload the stream
2. rehydrate the aggregate from the new history
3. re-run business-rule validation against the new state
4. either append a new valid event with the new expected version or abort because the state has already changed in a way that supersedes the command

That is exactly the behavior the support guide's OCC collision narrative is designed to test.

## 10. Required answer 4 - Projection lag and its consequences

### Scenario

The application command has committed a disbursement event, but the `LoanApplication` projection is 200 ms behind and still shows the old available credit limit.

### What the system should do

The system should treat the event store as correct and the projection as temporarily stale. The write succeeded. The read model has not caught up yet.

### What the user interface should do

The UI should not pretend the old limit is authoritative. It should expose freshness:

- show the projection's last-updated timestamp or "updating" status
- optionally overlay the command acknowledgement if the user just initiated the action
- refresh automatically when the projection catches up

### What the system should not do

It should not quietly bypass the projection architecture for every read. Read-your-write exceptions should be explicit and rare. The normal query path should still be projection-driven, because that is the whole point of CQRS.

### Recommended communication

A good message is: "Your change was accepted and is being reflected in the live view. Current read model age: 200 ms."

That tells the user the operation succeeded, the projection is catching up, and the stale view is a consistency-timing issue rather than a business failure.

## 11. Required answer 5 - The upcasting scenario

The prompt uses `CreditDecisionMade`; the later challenge brief examples use `CreditAnalysisCompleted` and `DecisionGenerated`. The design rule is the same in all cases: transform old payloads on read without mutating the stored row.

### Upcaster

```python
from datetime import datetime
from typing import Callable


def upcast_credit_decision_v1_to_v2(
    payload: dict,
    *,
    recorded_at: datetime,
    infer_model_version: Callable[[datetime], str | None],
    lookup_regulatory_basis: Callable[[datetime], str | None],
) -> dict:
    inferred_model_version = infer_model_version(recorded_at)
    return {
        **payload,
        "model_version": inferred_model_version,
        "confidence_score": None,
        "regulatory_basis": lookup_regulatory_basis(recorded_at),
    }
```

### Inference strategy

- `model_version`: infer only when the deployment timeline makes the answer deterministic. If the entire relevant time window maps to one legacy model, a value such as `legacy-pre-2026` is acceptable. If more than one model could have produced the event, return `None` and record the ambiguity in metadata or design notes.
- `confidence_score`: use `None`. This value is genuinely unknowable if v1 never stored it. Fabricating a number would create false precision and contaminate downstream analysis.
- `regulatory_basis`: infer from the rule set or regulation version active at `recorded_at`, because that inference is usually traceable to configuration history rather than guesswork.

### Non-negotiable guarantee

The stored v1 event row stays unchanged. Only the loaded representation becomes v2-shaped.

## 12. Required answer 6 - Marten Async Daemon parallel

### Goal

Marten's distributed Async Daemon lets multiple nodes process projections without two nodes advancing the same projection shard simultaneously. The Python equivalent should preserve that safety property.

### Python approach

I would use PostgreSQL advisory locks keyed by `(projection_name, shard_id)` plus the existing `projection_checkpoints` table.

### Processing pattern

1. A projection worker tries `pg_try_advisory_lock()` for the projection shard it wants to own.
2. If lock acquisition fails, another worker already owns that shard, so this worker moves on.
3. If the lock succeeds, the worker loads from that shard's checkpoint, processes a batch, updates the checkpoint transactionally, and keeps going.
4. If the worker crashes, the database connection drops and PostgreSQL releases the advisory lock automatically.
5. Another node can then acquire the lock and continue from the last committed checkpoint.

### Coordination primitive

PostgreSQL advisory locks are the core coordination primitive.

### Failure mode it guards against

It guards against split-brain projection execution where two daemon instances process the same shard at the same time, race checkpoint updates, and either double-apply events or skip positions unpredictably.

### Why this fits Week 5

The challenge already centers PostgreSQL. Using database-native coordination keeps the system operationally simple and aligned with the same data platform that owns the event store itself.

## 13. Phase 0 implementation implications

These are the practical consequences I should carry into Phase 1 and beyond:

1. Never write business-master data back into the Applicant Registry.
2. Start every agent session with the canonical session bootstrap event before any meaningful work.
3. Keep invariants in aggregates and command handlers, not in the UI or MCP layer.
4. Treat projections as disposable, rebuildable views and measure their lag continuously.
5. Treat upcasting as a read-time compatibility mechanism, not a migration that mutates history.
6. Treat the support-guide narrative scenarios as architecture tests, not just QA stories.

## 14. Final summary

The Ledger is the governance backbone for a multi-agent, document-to-decision platform.

The main challenge brief provides the conceptual architecture:

- event sourcing as the source of truth
- CQRS for read/write separation
- optimistic concurrency
- projections
- upcasting
- audit integrity
- MCP exposure

The support guide adds the operating context that makes the design concrete:

- read-only Applicant Registry
- document corpus
- five LangGraph agents
- seven stream families
- seeded data and narrative scenarios

Taken together, the correct Phase 0 conclusion is this:

**The Ledger is not an audit feature attached to an AI workflow. It is the event-sourced operating model that makes the workflow recoverable, explainable, and safe enough to use in a regulated financial decision system.**
