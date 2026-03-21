from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from src.aggregates import AgentSessionAggregate, ComplianceRecordAggregate, LoanApplicationAggregate, LoanLifecycleState
from src.commands.handlers import (
    ComplianceCheckCommand,
    ComplianceRuleEvaluation,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    GenerateDecisionCommand,
    HumanReviewCompletedCommand,
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    handle_compliance_check,
    handle_credit_analysis_completed,
    handle_fraud_screening_completed,
    handle_generate_decision,
    handle_human_review_completed,
    handle_start_agent_session,
    handle_submit_application,
)
from src.event_store import InMemoryEventStore, OptimisticConcurrencyError
from src.models.events import (
    AgentOutputWritten,
    AgentType,
    ApplicationDeclined,
    ComplianceCheckCompleted,
    ComplianceCheckInitiated,
    ComplianceRuleFailed,
    ComplianceRulePassed,
    ComplianceVerdict,
    CreditAnalysisCompleted,
    CreditAnalysisRequested,
    CreditDecision,
    DecisionGenerated,
    DecisionRequested,
    DocumentType,
    FraudScreeningRequested,
    HumanReviewCompleted,
    PackageReadyForAnalysis,
    RiskTier,
    DomainError,
    ComplianceCheckRequested,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _app_id(prefix: str = "APEX-P2") -> str:
    return f"{prefix}-{uuid4().hex[:8].upper()}"


async def _append_current(store: InMemoryEventStore, stream_id: str, events: list[dict]) -> list[int]:
    version = await store.stream_version(stream_id)
    return await store.append(stream_id, events, expected_version=version)


async def _submit_application(store: InMemoryEventStore, application_id: str) -> None:
    cmd = SubmitApplicationCommand(
        application_id=application_id,
        applicant_id="COMP-024",
        requested_amount_usd=Decimal("450000"),
        loan_purpose="working_capital",
        loan_term_months=36,
        submission_channel="portal",
        contact_email="ops@company.test",
        contact_name="Ava Ops",
        application_reference=f"REF-{application_id}",
    )
    await handle_submit_application(cmd, store)


async def _seed_package_ready(store: InMemoryEventStore, application_id: str) -> None:
    event = PackageReadyForAnalysis(
        package_id=f"docpkg-{application_id}",
        application_id=application_id,
        documents_processed=3,
        has_quality_flags=False,
        quality_flag_count=0,
        ready_at=_now(),
    ).to_store_dict()
    await _append_current(store, f"docpkg-{application_id}", [event])


async def _seed_loan_progression(store: InMemoryEventStore, application_id: str, event_type: str) -> None:
    mapping = {
        "CreditAnalysisRequested": CreditAnalysisRequested(
            application_id=application_id,
            requested_at=_now(),
            requested_by="document_processor",
        ).to_store_dict(),
        "FraudScreeningRequested": FraudScreeningRequested(
            application_id=application_id,
            requested_at=_now(),
            triggered_by_event_id="credit-stream:1",
        ).to_store_dict(),
        "ComplianceCheckRequested": ComplianceCheckRequested(
            application_id=application_id,
            requested_at=_now(),
            triggered_by_event_id="fraud-stream:1",
            regulation_set_version="2026-Q1",
            rules_to_evaluate=["REG-001", "REG-002", "REG-003", "REG-004", "REG-005", "REG-006"],
        ).to_store_dict(),
        "DecisionRequested": DecisionRequested(
            application_id=application_id,
            requested_at=_now(),
            all_analyses_complete=True,
            triggered_by_event_id="compliance-stream:1",
        ).to_store_dict(),
    }
    await _append_current(store, f"loan-{application_id}", [mapping[event_type]])


async def _start_session(
    store: InMemoryEventStore,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    model_version: str = "model-v1",
) -> None:
    await handle_start_agent_session(
        StartAgentSessionCommand(
            application_id=application_id,
            agent_type=agent_type,
            session_id=session_id,
            agent_id=f"{agent_type.value}-agent-1",
            model_version=model_version,
            langgraph_graph_version="graph-v1",
            context_source="fresh",
            context_token_count=1024,
        ),
        store,
    )


async def _record_agent_output(
    store: InMemoryEventStore,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    stream_id: str,
    event_type: str,
    stream_position: int = 1,
) -> None:
    event = AgentOutputWritten(
        session_id=session_id,
        agent_type=agent_type,
        application_id=application_id,
        events_written=[
            {
                "stream_id": stream_id,
                "event_type": event_type,
                "stream_position": stream_position,
            }
        ],
        output_summary=f"{event_type} written",
        written_at=_now(),
    ).to_store_dict()
    await _append_current(store, f"agent-{agent_type.value}-{session_id}", [event])


async def _seed_clear_compliance(store: InMemoryEventStore, application_id: str, session_id: str) -> None:
    events = [
        ComplianceCheckInitiated(
            application_id=application_id,
            session_id=session_id,
            regulation_set_version="2026-Q1",
            rules_to_evaluate=["REG-001", "REG-002", "REG-003", "REG-004", "REG-005", "REG-006"],
            initiated_at=_now(),
        ).to_store_dict(),
        ComplianceRulePassed(
            application_id=application_id,
            session_id=session_id,
            rule_id="REG-001",
            rule_name="AML",
            rule_version="1",
            evidence_hash="e1",
            evaluation_notes="ok",
            evaluated_at=_now(),
        ).to_store_dict(),
        ComplianceRulePassed(
            application_id=application_id,
            session_id=session_id,
            rule_id="REG-002",
            rule_name="OFAC",
            rule_version="1",
            evidence_hash="e2",
            evaluation_notes="ok",
            evaluated_at=_now(),
        ).to_store_dict(),
        ComplianceRulePassed(
            application_id=application_id,
            session_id=session_id,
            rule_id="REG-003",
            rule_name="Jurisdiction",
            rule_version="1",
            evidence_hash="e3",
            evaluation_notes="ok",
            evaluated_at=_now(),
        ).to_store_dict(),
        ComplianceRulePassed(
            application_id=application_id,
            session_id=session_id,
            rule_id="REG-004",
            rule_name="Entity Type",
            rule_version="1",
            evidence_hash="e4",
            evaluation_notes="ok",
            evaluated_at=_now(),
        ).to_store_dict(),
        ComplianceRulePassed(
            application_id=application_id,
            session_id=session_id,
            rule_id="REG-005",
            rule_name="Operating History",
            rule_version="1",
            evidence_hash="e5",
            evaluation_notes="ok",
            evaluated_at=_now(),
        ).to_store_dict(),
        {
            "event_type": "ComplianceRuleNoted",
            "event_version": 1,
            "payload": {
                "application_id": application_id,
                "session_id": session_id,
                "rule_id": "REG-006",
                "rule_name": "CRA",
                "note_type": "CRA_CONSIDERATION",
                "note_text": "informational",
                "evaluated_at": _now().isoformat(),
            },
        },
        ComplianceCheckCompleted(
            application_id=application_id,
            session_id=session_id,
            rules_evaluated=6,
            rules_passed=5,
            rules_failed=0,
            rules_noted=1,
            has_hard_block=False,
            overall_verdict=ComplianceVerdict.CLEAR,
            completed_at=_now(),
        ).to_store_dict(),
    ]
    await _append_current(store, f"compliance-{application_id}", events)


async def _seed_credit_limit(
    store: InMemoryEventStore,
    application_id: str,
    session_id: str,
    *,
    recommended_limit_usd: Decimal,
) -> None:
    event = CreditAnalysisCompleted(
        application_id=application_id,
        session_id=session_id,
        decision=CreditDecision(
            risk_tier=RiskTier.LOW,
            recommended_limit_usd=recommended_limit_usd,
            confidence=0.86,
            rationale="seeded credit decision",
        ),
        model_version="credit-v1",
        model_deployment_id="dep-seeded",
        input_data_hash="seeded-credit-hash",
        analysis_duration_ms=250,
        completed_at=_now(),
    ).to_store_dict()
    await _append_current(store, f"credit-{application_id}", [event])


@pytest.mark.asyncio
async def test_submit_application_rebuilds_state():
    store = InMemoryEventStore()
    application_id = _app_id()

    await _submit_application(store, application_id)
    aggregate = await LoanApplicationAggregate.load(store, application_id)

    assert aggregate.state == LoanLifecycleState.SUBMITTED
    assert aggregate.documents_requested is True
    assert aggregate.applicant_id == "COMP-024"
    assert aggregate.requested_amount_usd == Decimal("450000")


@pytest.mark.asyncio
async def test_agent_session_requires_gas_town_anchor():
    store = InMemoryEventStore()
    session = await AgentSessionAggregate.load(store, AgentType.CREDIT_ANALYSIS.value, "missing")

    with pytest.raises(DomainError):
        session.assert_context_loaded("APEX-MISSING")

    await _start_session(store, "APEX-OK", AgentType.CREDIT_ANALYSIS, "sess-credit-1", model_version="credit-v1")
    loaded = await AgentSessionAggregate.load(store, AgentType.CREDIT_ANALYSIS.value, "sess-credit-1")
    loaded.assert_context_loaded("APEX-OK")
    loaded.assert_model_version_current("credit-v1")


@pytest.mark.asyncio
async def test_credit_analysis_requires_package_ready_and_locks_model_version():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _start_session(store, application_id, AgentType.CREDIT_ANALYSIS, "sess-credit-1", model_version="credit-v1")

    command = CreditAnalysisCompletedCommand(
        application_id=application_id,
        session_id="sess-credit-1",
        decision=CreditDecision(
            risk_tier=RiskTier.MEDIUM,
            recommended_limit_usd=Decimal("500000"),
            confidence=0.81,
            rationale="Healthy cash flow",
        ),
        model_version="credit-v1",
        model_deployment_id="dep-1",
        input_data_hash="hash-1",
        analysis_duration_ms=900,
    )

    with pytest.raises(DomainError):
        await handle_credit_analysis_completed(command, store)

    await _seed_package_ready(store, application_id)
    await handle_credit_analysis_completed(command, store)

    await _start_session(store, application_id, AgentType.CREDIT_ANALYSIS, "sess-credit-2", model_version="credit-v2")
    with pytest.raises(DomainError):
        await handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=application_id,
                session_id="sess-credit-2",
                decision=command.decision,
                model_version="credit-v2",
                model_deployment_id="dep-2",
                input_data_hash="hash-2",
                analysis_duration_ms=850,
            ),
            store,
        )

    loan = await LoanApplicationAggregate.load(store, application_id)
    assert loan.state == LoanLifecycleState.ANALYSIS_COMPLETE


@pytest.mark.asyncio
async def test_generate_decision_rejects_low_confidence_approve():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _seed_loan_progression(store, application_id, "FraudScreeningRequested")
    await _seed_loan_progression(store, application_id, "ComplianceCheckRequested")
    await _seed_loan_progression(store, application_id, "DecisionRequested")
    await _seed_clear_compliance(store, application_id, "sess-compliance")

    await _start_session(store, application_id, AgentType.CREDIT_ANALYSIS, "sess-credit", model_version="credit-v1")
    await _record_agent_output(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        "sess-credit",
        stream_id=f"credit-{application_id}",
        event_type="CreditAnalysisCompleted",
    )
    await _start_session(store, application_id, AgentType.FRAUD_DETECTION, "sess-fraud", model_version="fraud-v1")
    await _record_agent_output(
        store,
        application_id,
        AgentType.FRAUD_DETECTION,
        "sess-fraud",
        stream_id=f"fraud-{application_id}",
        event_type="FraudScreeningCompleted",
    )
    await _start_session(store, application_id, AgentType.COMPLIANCE, "sess-compliance", model_version="compliance-v1")
    await _record_agent_output(
        store,
        application_id,
        AgentType.COMPLIANCE,
        "sess-compliance",
        stream_id=f"compliance-{application_id}",
        event_type="ComplianceCheckCompleted",
    )
    await _start_session(store, application_id, AgentType.DECISION_ORCHESTRATOR, "sess-orch", model_version="orch-v1")

    with pytest.raises(DomainError):
        await handle_generate_decision(
            GenerateDecisionCommand(
                application_id=application_id,
                session_id="sess-orch",
                recommendation="APPROVE",
                confidence=0.55,
                executive_summary="Too low confidence",
                key_risks=["low confidence"],
                contributing_sessions=[
                    f"agent-credit_analysis-sess-credit",
                    f"agent-fraud_detection-sess-fraud",
                    f"agent-compliance-sess-compliance",
                ],
                model_versions={"decision_orchestrator": "orch-v1"},
            ),
            store,
        )


@pytest.mark.asyncio
async def test_generate_decision_rejects_invalid_contributing_session():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _seed_loan_progression(store, application_id, "FraudScreeningRequested")
    await _seed_loan_progression(store, application_id, "ComplianceCheckRequested")
    await _seed_loan_progression(store, application_id, "DecisionRequested")
    await _seed_clear_compliance(store, application_id, "sess-compliance")

    await _start_session(store, application_id, AgentType.DECISION_ORCHESTRATOR, "sess-orch", model_version="orch-v1")
    await _start_session(store, "OTHER-APP", AgentType.CREDIT_ANALYSIS, "sess-bad", model_version="credit-v1")
    await _record_agent_output(
        store,
        "OTHER-APP",
        AgentType.CREDIT_ANALYSIS,
        "sess-bad",
        stream_id="credit-OTHER-APP",
        event_type="CreditAnalysisCompleted",
    )

    with pytest.raises(DomainError):
        await handle_generate_decision(
            GenerateDecisionCommand(
                application_id=application_id,
                session_id="sess-orch",
                recommendation="REFER",
                confidence=0.59,
                executive_summary="Needs review",
                key_risks=["bad causal chain"],
                contributing_sessions=[f"agent-credit_analysis-sess-bad"],
                model_versions={"decision_orchestrator": "orch-v1"},
            ),
            store,
        )


@pytest.mark.asyncio
async def test_generate_decision_rejects_duplicate_contributing_sessions_without_writing():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _seed_loan_progression(store, application_id, "FraudScreeningRequested")
    await _seed_loan_progression(store, application_id, "ComplianceCheckRequested")
    await _seed_loan_progression(store, application_id, "DecisionRequested")
    await _seed_clear_compliance(store, application_id, "sess-compliance")

    await _start_session(store, application_id, AgentType.CREDIT_ANALYSIS, "sess-credit", model_version="credit-v1")
    await _record_agent_output(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        "sess-credit",
        stream_id=f"credit-{application_id}",
        event_type="CreditAnalysisCompleted",
    )
    await _start_session(store, application_id, AgentType.DECISION_ORCHESTRATOR, "sess-orch", model_version="orch-v1")
    loan_stream_id = f"loan-{application_id}"
    before = len(await store.load_stream(loan_stream_id))

    with pytest.raises(DomainError):
        await handle_generate_decision(
            GenerateDecisionCommand(
                application_id=application_id,
                session_id="sess-orch",
                recommendation="REFER",
                confidence=0.58,
                executive_summary="duplicate sessions should fail",
                key_risks=["duplicate causal chain"],
                contributing_sessions=[
                    "agent-credit_analysis-sess-credit",
                    "agent-credit_analysis-sess-credit",
                ],
                model_versions={"decision_orchestrator": "orch-v1"},
            ),
            store,
        )

    after = len(await store.load_stream(loan_stream_id))
    assert after == before


@pytest.mark.asyncio
async def test_compliance_hard_block_declines_immediately():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _seed_loan_progression(store, application_id, "FraudScreeningRequested")
    await _seed_loan_progression(store, application_id, "ComplianceCheckRequested")
    await _start_session(store, application_id, AgentType.COMPLIANCE, "sess-compliance", model_version="compliance-v1")

    await handle_compliance_check(
        ComplianceCheckCommand(
            application_id=application_id,
            session_id="sess-compliance",
            regulation_set_version="2026-Q1",
            rules_to_evaluate=["REG-001", "REG-003"],
            rule_results=[
                ComplianceRuleEvaluation(
                    rule_id="REG-001",
                    rule_name="AML",
                    outcome="PASS",
                    rule_version="1",
                    evidence_hash="e1",
                ),
                ComplianceRuleEvaluation(
                    rule_id="REG-003",
                    rule_name="Jurisdiction",
                    outcome="FAIL",
                    rule_version="1",
                    evidence_hash="e3",
                    failure_reason="Montana excluded",
                    is_hard_block=True,
                ),
            ],
            model_version="compliance-v1",
        ),
        store,
    )

    loan = await LoanApplicationAggregate.load(store, application_id)
    compliance = await ComplianceRecordAggregate.load(store, application_id)
    loan_events = await store.load_stream(f"loan-{application_id}")

    assert compliance.has_hard_block is True
    assert loan.state == LoanLifecycleState.DECLINED_COMPLIANCE
    assert loan_events[-1].event_type == "ApplicationDeclined"
    assert not any(event.event_type == "DecisionRequested" for event in loan_events[-2:])


@pytest.mark.asyncio
async def test_human_review_cannot_approve_without_clear_compliance():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _seed_loan_progression(store, application_id, "FraudScreeningRequested")
    await _seed_loan_progression(store, application_id, "ComplianceCheckRequested")
    await _seed_loan_progression(store, application_id, "DecisionRequested")

    compliance_events = [
        ComplianceCheckInitiated(
            application_id=application_id,
            session_id="sess-compliance",
            regulation_set_version="2026-Q1",
            rules_to_evaluate=["REG-001", "REG-004"],
            initiated_at=_now(),
        ).to_store_dict(),
        ComplianceRulePassed(
            application_id=application_id,
            session_id="sess-compliance",
            rule_id="REG-001",
            rule_name="AML",
            rule_version="1",
            evidence_hash="e1",
            evaluation_notes="ok",
            evaluated_at=_now(),
        ).to_store_dict(),
        ComplianceRuleFailed(
            application_id=application_id,
            session_id="sess-compliance",
            rule_id="REG-004",
            rule_name="Entity Type",
            rule_version="1",
            failure_reason="Needs remediation",
            is_hard_block=False,
            remediation_available=True,
            remediation_description="manual docs",
            evidence_hash="e4",
            evaluated_at=_now(),
        ).to_store_dict(),
        ComplianceCheckCompleted(
            application_id=application_id,
            session_id="sess-compliance",
            rules_evaluated=2,
            rules_passed=1,
            rules_failed=1,
            rules_noted=0,
            has_hard_block=False,
            overall_verdict=ComplianceVerdict.CONDITIONAL,
            completed_at=_now(),
        ).to_store_dict(),
    ]
    await _append_current(store, f"compliance-{application_id}", compliance_events)
    await _append_current(
        store,
        f"loan-{application_id}",
        [
            DecisionGenerated(
                application_id=application_id,
                orchestrator_session_id="sess-orch",
                recommendation="REFER",
                confidence=0.72,
                executive_summary="manual review needed",
                key_risks=["conditional compliance"],
                contributing_sessions=[],
                model_versions={},
                generated_at=_now(),
            ).to_store_dict()
        ],
    )

    with pytest.raises(DomainError):
        await handle_human_review_completed(
            HumanReviewCompletedCommand(
                application_id=application_id,
                reviewer_id="LO-1",
                override=False,
                original_recommendation="REFER",
                final_decision="APPROVE",
                approved_amount_usd=Decimal("350000"),
                interest_rate_pct=7.5,
                term_months=24,
            ),
            store,
        )


@pytest.mark.asyncio
async def test_human_review_can_finalize_approval_when_clear_and_within_limit():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_package_ready(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _start_session(store, application_id, AgentType.CREDIT_ANALYSIS, "sess-credit", model_version="credit-v1")

    credit_result = await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=application_id,
            session_id="sess-credit",
            decision=CreditDecision(
                risk_tier=RiskTier.LOW,
                recommended_limit_usd=Decimal("500000"),
                confidence=0.84,
                rationale="Strong fundamentals",
            ),
            model_version="credit-v1",
            model_deployment_id="dep-credit",
            input_data_hash="credit-hash",
            analysis_duration_ms=500,
        ),
        store,
    )
    await _record_agent_output(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        "sess-credit",
        stream_id=f"credit-{application_id}",
        event_type="CreditAnalysisCompleted",
        stream_position=credit_result[f"credit-{application_id}"][-1],
    )

    await _start_session(store, application_id, AgentType.FRAUD_DETECTION, "sess-fraud", model_version="fraud-v1")
    fraud_result = await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=application_id,
            session_id="sess-fraud",
            fraud_score=0.12,
            risk_level="LOW",
            anomalies_found=0,
            recommendation="CLEAR",
            screening_model_version="fraud-v1",
            input_data_hash="fraud-hash",
        ),
        store,
    )
    await _record_agent_output(
        store,
        application_id,
        AgentType.FRAUD_DETECTION,
        "sess-fraud",
        stream_id=f"fraud-{application_id}",
        event_type="FraudScreeningCompleted",
        stream_position=fraud_result[f"fraud-{application_id}"][-1],
    )

    await _start_session(store, application_id, AgentType.COMPLIANCE, "sess-compliance", model_version="compliance-v1")
    compliance_result = await handle_compliance_check(
        ComplianceCheckCommand(
            application_id=application_id,
            session_id="sess-compliance",
            regulation_set_version="2026-Q1",
            rules_to_evaluate=["REG-001", "REG-002", "REG-003", "REG-004", "REG-005", "REG-006"],
            rule_results=[
                ComplianceRuleEvaluation("REG-001", "AML", "PASS", "1", "e1"),
                ComplianceRuleEvaluation("REG-002", "OFAC", "PASS", "1", "e2"),
                ComplianceRuleEvaluation("REG-003", "Jurisdiction", "PASS", "1", "e3"),
                ComplianceRuleEvaluation("REG-004", "Entity Type", "PASS", "1", "e4"),
                ComplianceRuleEvaluation("REG-005", "Operating History", "PASS", "1", "e5"),
                ComplianceRuleEvaluation("REG-006", "CRA", "NOTE", "1", "e6", note_type="CRA", note_text="informational"),
            ],
            model_version="compliance-v1",
        ),
        store,
    )
    await _record_agent_output(
        store,
        application_id,
        AgentType.COMPLIANCE,
        "sess-compliance",
        stream_id=f"compliance-{application_id}",
        event_type="ComplianceCheckCompleted",
        stream_position=compliance_result[f"compliance-{application_id}"][-1],
    )

    await _start_session(store, application_id, AgentType.DECISION_ORCHESTRATOR, "sess-orch", model_version="orch-v1")
    await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=application_id,
            session_id="sess-orch",
            recommendation="APPROVE",
            confidence=0.82,
            approved_amount_usd=Decimal("450000"),
            executive_summary="Approve with standard terms",
            key_risks=["moderate leverage"],
            contributing_sessions=[
                f"agent-credit_analysis-sess-credit",
                f"agent-fraud_detection-sess-fraud",
                f"agent-compliance-sess-compliance",
            ],
            model_versions={"decision_orchestrator": "orch-v1"},
        ),
        store,
    )

    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=application_id,
            reviewer_id="LO-SARAH",
            override=False,
            original_recommendation="APPROVE",
            final_decision="APPROVE",
            approved_amount_usd=Decimal("450000"),
            interest_rate_pct=8.25,
            term_months=36,
            conditions=["standard covenants"],
        ),
        store,
    )

    loan = await LoanApplicationAggregate.load(store, application_id)
    assert loan.state == LoanLifecycleState.FINAL_APPROVED
    assert loan.approved_amount_usd == Decimal("450000")


@pytest.mark.asyncio
async def test_counterfactual_human_approval_above_credit_limit_rejected_without_writing():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _seed_loan_progression(store, application_id, "FraudScreeningRequested")
    await _seed_loan_progression(store, application_id, "ComplianceCheckRequested")
    await _seed_loan_progression(store, application_id, "DecisionRequested")
    await _seed_clear_compliance(store, application_id, "sess-compliance")
    await _seed_credit_limit(
        store,
        application_id,
        "sess-credit",
        recommended_limit_usd=Decimal("500000"),
    )
    await _append_current(
        store,
        f"loan-{application_id}",
        [
            DecisionGenerated(
                application_id=application_id,
                orchestrator_session_id="sess-orch",
                recommendation="APPROVE",
                confidence=0.83,
                approved_amount_usd=Decimal("500000"),
                executive_summary="approve within standard range",
                key_risks=["moderate leverage"],
                contributing_sessions=["agent-credit_analysis-sess-credit"],
                model_versions={"decision_orchestrator": "orch-v1"},
                generated_at=_now(),
            ).to_store_dict()
        ],
    )

    loan_stream_id = f"loan-{application_id}"
    before = len(await store.load_stream(loan_stream_id))
    with pytest.raises(DomainError):
        await handle_human_review_completed(
            HumanReviewCompletedCommand(
                application_id=application_id,
                reviewer_id="LO-COUNTERFACTUAL",
                override=False,
                original_recommendation="APPROVE",
                final_decision="APPROVE",
                approved_amount_usd=Decimal("550000"),
                interest_rate_pct=8.4,
                term_months=36,
            ),
            store,
        )

    after = len(await store.load_stream(loan_stream_id))
    loan = await LoanApplicationAggregate.load(store, application_id)
    assert after == before
    assert loan.state == LoanLifecycleState.APPROVED_PENDING_HUMAN


@pytest.mark.asyncio
async def test_concurrent_credit_analysis_commands_only_one_succeeds():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _submit_application(store, application_id)
    await _seed_package_ready(store, application_id)
    await _seed_loan_progression(store, application_id, "CreditAnalysisRequested")
    await _start_session(store, application_id, AgentType.CREDIT_ANALYSIS, "sess-a", model_version="credit-v1")
    await _start_session(store, application_id, AgentType.CREDIT_ANALYSIS, "sess-b", model_version="credit-v1")

    command_a = CreditAnalysisCompletedCommand(
        application_id=application_id,
        session_id="sess-a",
        decision=CreditDecision(
            risk_tier=RiskTier.MEDIUM,
            recommended_limit_usd=Decimal("300000"),
            confidence=0.75,
            rationale="acceptable risk",
        ),
        model_version="credit-v1",
        model_deployment_id="dep-a",
        input_data_hash="hash-a",
        analysis_duration_ms=100,
    )
    command_b = CreditAnalysisCompletedCommand(
        application_id=application_id,
        session_id="sess-b",
        decision=CreditDecision(
            risk_tier=RiskTier.MEDIUM,
            recommended_limit_usd=Decimal("305000"),
            confidence=0.76,
            rationale="acceptable risk",
        ),
        model_version="credit-v1",
        model_deployment_id="dep-b",
        input_data_hash="hash-b",
        analysis_duration_ms=100,
    )

    results = await asyncio.gather(
        handle_credit_analysis_completed(command_a, store),
        handle_credit_analysis_completed(command_b, store),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, (DomainError, OptimisticConcurrencyError))]
    credit_events = await store.load_stream(f"credit-{application_id}")

    assert len(successes) == 1
    assert len(failures) == 1
    assert [event.event_type for event in credit_events].count("CreditAnalysisCompleted") == 1
