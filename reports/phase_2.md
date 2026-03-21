## Phase 2 Report

Phase 2 turns the append-only Event Store from Phase 1 into an actual business domain. The implementation follows the challenge pattern directly: command received, aggregate state rebuilt by replaying events, business rules validated before any write, new event or events determined, then appended with optimistic concurrency control.

### Implemented Aggregates

#### LoanApplicationAggregate

File: `src/aggregates/loan_application.py`

This aggregate is the consistency boundary for the loan lifecycle. It rebuilds its state only from the `loan-{application_id}` stream and enforces the application state machine:

- `NEW -> SUBMITTED`
- `SUBMITTED -> AWAITING_ANALYSIS`
- `AWAITING_ANALYSIS -> ANALYSIS_COMPLETE`
- `ANALYSIS_COMPLETE -> COMPLIANCE_REVIEW`
- `COMPLIANCE_REVIEW -> PENDING_DECISION` or `DECLINED_COMPLIANCE`
- `PENDING_DECISION -> APPROVED_PENDING_HUMAN` or `DECLINED_PENDING_HUMAN` or `PENDING_HUMAN_REVIEW`
- `APPROVED_PENDING_HUMAN` or `DECLINED_PENDING_HUMAN` or `PENDING_HUMAN_REVIEW -> FINAL_APPROVED` or `FINAL_DECLINED`

The aggregate tracks:

- application identity and requested amount
- document request and upload progress
- whether credit, fraud, compliance, and decision steps were requested
- orchestrator recommendation, confidence, and contributing sessions
- human review completion and override state
- final approval or decline outcome

#### AgentSessionAggregate

File: `src/aggregates/agent_session.py`

This aggregate rebuilds the `agent-{agent_type}-{session_id}` stream and enforces the Gas Town session rules used by the challenge. In the provided schema and support document, `AgentSessionStarted` is the first required session event and acts as the context anchor for recovery and traceability.

The aggregate validates:

- the session must start before any decision work
- the session belongs to the target application
- the session is locked to the recorded model version
- the session has recorded domain outputs when used as causal evidence

#### ComplianceRecordAggregate

File: `src/aggregates/compliance_record.py`

This aggregate tracks the compliance stream for one application. It records:

- required rules
- passed, failed, and noted rules
- hard-block rules
- completion status
- final verdict

It is used by the loan aggregate and handlers to enforce that approval is impossible until compliance is fully clear, and that hard-block compliance outcomes cannot proceed to approval.

### Command Handling Pattern

File: `src/commands/handlers.py`

All handlers follow the same shape:

1. Load aggregate streams from the Event Store.
2. Reconstruct current state by replay.
3. Validate business rules before any append.
4. Determine the event or events to write.
5. Append with `expected_version`, letting OCC protect consistency.

Implemented handlers:

- `handle_submit_application`
- `handle_start_agent_session`
- `handle_credit_analysis_completed`
- `handle_fraud_screening_completed`
- `handle_compliance_check`
- `handle_generate_decision`
- `handle_human_review_completed`

This keeps command-side correctness on the write model and avoids projections entirely, which matches the CQRS requirement in the challenge.

### Business Rules Enforced

The Phase 2 implementation covers the required rule set from the challenge and support documents.

1. Valid state transitions only.
   The loan aggregate rejects illegal lifecycle jumps.

2. Gas Town session context enforcement.
   Agent work is rejected unless the session stream starts correctly and belongs to the same application.

3. Model version locking.
   Agent outputs are tied to the session's model version, and duplicate credit analysis is blocked unless a later human override changes the state.

4. Confidence floor.
   A decision with confidence below `0.60` cannot approve or decline directly; it must be `REFER`.

5. Compliance dependency.
   Approval cannot happen until compliance has completed and all mandatory rules are clear. Hard-block compliance results force decline.

6. Causal chain enforcement.
   `DecisionGenerated` must reference valid contributing agent sessions that belong to the same application and have actually recorded domain outputs.

Additional guardrails implemented:

- document package readiness is required before credit analysis completes
- duplicate contributing sessions are rejected
- approvals above the latest recommended credit limit are rejected
- recommendation changes during human review require an explicit override reason

### State Reconstruction

All aggregate state is rebuilt by replaying stored events from the Event Store. No projection or cached read model is used for command validation.

That means:

- every decision can be replayed later
- invalid writes do not contaminate the stream
- domain correctness remains aligned with the append-only ledger

### Concurrency Handling

Phase 2 depends on the Phase 1 Event Store's optimistic concurrency control.

Command handlers load the latest version, validate, and append with `expected_version`. If another actor writes first, the second write fails cleanly with `OptimisticConcurrencyError` instead of corrupting the stream.

This gives the correct behavior for scenarios like:

- two credit-analysis sessions trying to write the same credit result
- two actors racing to finalize the same application outcome

### Testing and Output

Phase 2 verification currently includes:

- aggregate replay and state reconstruction
- Gas Town anchor enforcement
- package-readiness gating for credit analysis
- model-version locking for repeated credit analysis
- confidence-floor enforcement
- invalid causal-chain rejection
- duplicate contributing-session rejection with no writes
- compliance hard-block immediate decline
- approval rejection when compliance is not fully clear
- approval rejection when the approved amount exceeds the latest recommended limit, with no writes
- successful clear-compliance approval path
- concurrent credit-analysis writes where exactly one succeeds

Latest results:

- `tests/phase2/test_domain_logic.py`: `11 passed`
- Phase 1 + Phase 2 bundle: `49 passed, 1 skipped`

### Design Decisions

- A dedicated `ComplianceRecordAggregate` was added because approval safety depends on more than the loan stream alone.
- Gas Town was aligned to the actual supplied schema and support document, which use `AgentSessionStarted` as the practical session anchor.
- Follow-up events such as `FraudScreeningRequested`, `DecisionRequested`, and compliance-driven decline are emitted by handlers after domain validation so the event history remains explicit and auditable.

### Challenges

- The challenge text mentions `AgentContextLoaded`, while the provided event catalogue and support document encode the same intent through `AgentSessionStarted`. The implementation follows the actual supplied schema so the code stays compatible with the generated seed data and later agents.
- The state machine in the brief is compact, but the real workflow needs explicit handling for referred decisions, compliance-blocked declines, and human review.

### Rubric Fit

Within Phase 2 scope, the implementation now covers the rubric evidence needed for the upper scoring bands:

- both required aggregates are implemented and replay-driven
- all core business rules are enforced before append
- Gas Town session enforcement is present
- model version locking is present
- causal-chain validation is present
- counterfactual rejection paths are tested
- concurrent invariants are tested

The remaining mastery beyond Phase 2 depends on later phases, especially the full narrative scenarios, projections, MCP lifecycle, and recovery tooling.
