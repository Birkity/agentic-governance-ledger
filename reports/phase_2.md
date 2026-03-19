# Phase 2 Report - Domain Logic and Command Handling

## What Was Implemented

Phase 2 added the domain layer that turns the event store into a real business system.

Implemented files:

- `starter/ledger/domain/aggregates/loan_application.py`
- `starter/ledger/domain/aggregates/agent_session.py`
- `starter/ledger/domain/errors.py`
- `starter/ledger/commands/handlers.py`
- `starter/tests/phase2/test_domain_logic.py`

## Aggregates Implemented

### LoanApplicationAggregate

`LoanApplicationAggregate` now replays the `loan-{application_id}` stream and tracks:

- lifecycle state
- applicant and loan request identity
- required and uploaded document types
- decision request / decision generated flags
- human review state
- final decision state
- compliance-blocked terminal path

It has explicit `_apply_*` handlers for the core loan lifecycle events:

- `ApplicationSubmitted`
- `DocumentUploadRequested`
- `DocumentUploaded`
- `CreditAnalysisRequested`
- `FraudScreeningRequested`
- `ComplianceCheckRequested`
- `DecisionRequested`
- `DecisionGenerated`
- `HumanReviewRequested`
- `HumanReviewCompleted`
- `ApplicationApproved`
- `ApplicationDeclined`

### AgentSessionAggregate

`AgentSessionAggregate` was added to cover the Gas Town boundary in Phase 2.

It enforces:

- session bootstrap must happen first
- context is loaded before work is considered valid
- model version is locked once the session starts
- completed sessions cannot be started again

## How State Reconstruction Works

Both aggregates follow the same pattern:

1. load the stream from the event store
2. replay events in stream order
3. apply each event through explicit event handlers
4. derive the current state entirely from history

No projection is used during command validation. This keeps command correctness aligned with CQRS and the challenge brief.

## Business Rules Enforced

The Phase 2 implementation now enforces these rules in the domain layer before appending events:

- invalid lifecycle transitions are rejected
- credit analysis cannot be requested until all required documents are uploaded
- fraud screening cannot be requested before credit analysis is complete
- compliance cannot be requested before fraud screening is complete
- decisioning cannot start until credit, fraud, and compliance are all complete
- compliance hard block prevents decision request / non-decline progression
- confidence below `0.60` forces `REFER`
- duplicate decision generation is rejected
- human review override requires an override reason
- approvals cannot exceed the requested amount
- approvals cannot exceed the credit analysis recommended limit
- agent sessions must start with the session bootstrap event

## How Command Handling Works

Command handlers follow the challenge flow strictly:

1. receive a command
2. load and replay aggregate state from the event stream
3. load required cross-stream facts from credit / fraud / compliance streams when needed
4. validate business rules
5. build canonical event objects from `ledger/schema/events.py`
6. append to the event store with `expected_version=aggregate.version`

Implemented command handlers include:

- `submit_application`
- `record_document_uploaded`
- `request_credit_analysis`
- `request_fraud_screening`
- `request_compliance_check`
- `request_decision`
- `generate_decision`
- `complete_human_review`
- `approve_application`
- `decline_application`
- `start_agent_session`

## How Concurrency Is Handled

Concurrency protection still comes from Phase 1's EventStore.

The Phase 2 handlers participate correctly by:

- rebuilding aggregate state first
- using the replayed aggregate version as `expected_version`
- allowing the event store to reject stale writes with OCC
- rejecting duplicate decisions if the aggregate has already advanced

This means a racing command either:

- wins the append, or
- loses due to OCC, or
- reloads into a state where the action is no longer valid

All three outcomes are correct according to the challenge's replay-first model.

## Design Decisions

- Commands never use projections.
- The loan aggregate remains the consistency boundary for the `loan-*` stream, while cross-stream prerequisite checks read the authoritative related streams directly.
- `DecisionGenerated` does not silently mutate history into a final approval or decline; finalization is explicit.
- `HumanReviewCompleted` can atomically append the review event and the final terminal event in one write.
- `AgentSessionStarted` is used as the practical Gas Town bootstrap event because that is the canonical starter schema, even though the challenge brief also discusses the invariant as `AgentContextLoaded`.

## Challenges Faced

- The challenge brief describes the lifecycle at a conceptual level, while the support guide and starter schema operationalize it with more concrete event names. The implementation reconciles those by following the canonical schema file and preserving the same invariants.
- The loan lifecycle depends on facts from multiple streams. To keep CQRS correct, the command side reads those streams directly rather than introducing projection shortcuts.
- Concurrent domain tests are not purely deterministic because one contender may fail with OCC while another may fail after seeing the newly advanced state. The test suite accepts either correct rejection path.

## Verification

Verified test suites:

- `starter/tests/phase2/test_domain_logic.py` -> `9 passed`
- `starter/tests/phase1/test_event_store.py` -> `11 passed`
- `starter/tests/test_schema_and_generator.py` -> `10 passed`

## Why This Matters

Phase 1 gave the Ledger durable memory.

Phase 2 gives that memory meaning:

- aggregates define what states are legal
- commands define how decisions become events
- replay proves the system can explain itself later
- OCC keeps concurrent agents from creating conflicting truths
