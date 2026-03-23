from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

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
from src.event_store import InMemoryEventStore
from src.models.events import (
    AgentOutputWritten,
    AgentType,
    CreditAnalysisCompleted,
    CreditDecision,
    LoanPurpose,
    PackageReadyForAnalysis,
    RiskTier,
)
from src.regulatory import generate_regulatory_package, verify_regulatory_package
from src.what_if import run_what_if


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _app_id() -> str:
    return f"NARR-05-{uuid4().hex[:8].upper()}"


async def _append_current(store: InMemoryEventStore, stream_id: str, events: list[dict]) -> list[int]:
    version = await store.stream_version(stream_id)
    return await store.append(stream_id, events, expected_version=version)


async def _start_session(
    store: InMemoryEventStore,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    agent_id: str,
    model_version: str,
) -> None:
    await handle_start_agent_session(
        StartAgentSessionCommand(
            application_id=application_id,
            agent_type=agent_type,
            session_id=session_id,
            agent_id=agent_id,
            model_version=model_version,
            langgraph_graph_version="graph-v1",
            context_source="fresh",
            context_token_count=512,
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
    stream_position: int,
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
        output_summary=f"{event_type} recorded",
        written_at=_now(),
    ).to_store_dict()
    await _append_current(store, f"agent-{agent_type.value}-{session_id}", [event])


async def _seed_phase6_override_case(store: InMemoryEventStore, application_id: str) -> None:
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=application_id,
            applicant_id="COMP-068",
            requested_amount_usd=Decimal("750000"),
            loan_purpose=LoanPurpose.WORKING_CAPITAL,
            loan_term_months=36,
            submission_channel="relationship_manager",
            contact_email="finance@comp068.test",
            contact_name="Casey Ledger",
            application_reference=f"REF-{application_id}",
        ),
        store,
    )

    await _append_current(
        store,
        f"docpkg-{application_id}",
        [
            PackageReadyForAnalysis(
                package_id=f"docpkg-{application_id}",
                application_id=application_id,
                documents_processed=3,
                has_quality_flags=False,
                quality_flag_count=0,
                ready_at=_now(),
            ).to_store_dict()
        ],
    )
    await _append_current(
        store,
        f"loan-{application_id}",
        [
            {
                "event_type": "CreditAnalysisRequested",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "requested_at": _now().isoformat(),
                    "requested_by": "document_processing",
                    "priority": "NORMAL",
                },
            }
        ],
    )

    await _start_session(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        "sess-credit",
        agent_id="credit-agent-068",
        model_version="credit-v1",
    )
    credit_positions = await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=application_id,
            session_id="sess-credit",
            decision=CreditDecision(
                risk_tier=RiskTier.HIGH,
                recommended_limit_usd=Decimal("750000"),
                confidence=0.82,
                rationale="High leverage and industry volatility warrant a decline recommendation.",
            ),
            model_version="credit-v1",
            model_deployment_id="credit-dep-v1",
            input_data_hash="credit-input-068",
            analysis_duration_ms=640,
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
        stream_position=credit_positions[f"credit-{application_id}"][-1],
    )

    await _start_session(
        store,
        application_id,
        AgentType.FRAUD_DETECTION,
        "sess-fraud",
        agent_id="fraud-agent-068",
        model_version="fraud-v1",
    )
    fraud_positions = await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=application_id,
            session_id="sess-fraud",
            fraud_score=0.08,
            risk_level="LOW",
            anomalies_found=0,
            recommendation="CLEAR",
            screening_model_version="fraud-v1",
            input_data_hash="fraud-input-068",
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
        stream_position=fraud_positions[f"fraud-{application_id}"][-1],
    )

    await _start_session(
        store,
        application_id,
        AgentType.COMPLIANCE,
        "sess-compliance",
        agent_id="compliance-agent-068",
        model_version="compliance-v1",
    )
    compliance_positions = await handle_compliance_check(
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
                ComplianceRuleEvaluation("REG-006", "CRA", "NOTE", "1", "e6", note_type="CRA", note_text="noted"),
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
        stream_position=compliance_positions[f"compliance-{application_id}"][-1],
    )

    await _start_session(
        store,
        application_id,
        AgentType.DECISION_ORCHESTRATOR,
        "sess-orch",
        agent_id="orch-agent-068",
        model_version="orch-v1",
    )
    loan_positions = await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=application_id,
            session_id="sess-orch",
            recommendation="DECLINE",
            confidence=0.82,
            executive_summary="Automated decline recommendation due to high credit risk despite clean compliance.",
            key_risks=["high leverage", "sector cyclicality"],
            contributing_sessions=[
                "agent-credit_analysis-sess-credit",
                "agent-fraud_detection-sess-fraud",
                "agent-compliance-sess-compliance",
            ],
            model_versions={"decision_orchestrator": "orch-v1"},
        ),
        store,
    )
    await _record_agent_output(
        store,
        application_id,
        AgentType.DECISION_ORCHESTRATOR,
        "sess-orch",
        stream_id=f"loan-{application_id}",
        event_type="DecisionGenerated",
        stream_position=loan_positions[f"loan-{application_id}"][0],
    )

    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=application_id,
            reviewer_id="LO-Sarah-Chen",
            override=True,
            override_reason="Relationship manager confirmed collateral package and approved exception path.",
            original_recommendation="DECLINE",
            final_decision="APPROVE",
            approved_amount_usd=Decimal("750000"),
            interest_rate_pct=9.10,
            term_months=36,
            conditions=[
                "Monthly borrowing base certificate",
                "Quarterly management reporting package",
            ],
        ),
        store,
    )


@pytest.mark.asyncio
async def test_what_if_projector_replays_counterfactual_without_mutating_store():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _seed_phase6_override_case(store, application_id)
    before_count = len([event async for event in store.load_all(from_position=0, apply_upcasters=False)])

    result = await run_what_if(
        store,
        application_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[
            CreditAnalysisCompleted(
                application_id=application_id,
                session_id="sess-credit",
                decision=CreditDecision(
                    risk_tier=RiskTier.LOW,
                    recommended_limit_usd=Decimal("750000"),
                    confidence=0.91,
                    rationale="Counterfactual credit view assumes strong collateral support and lower leverage.",
                ),
                model_version="credit-whatif-v1",
                model_deployment_id="whatif-credit-dep",
                input_data_hash="whatif-credit-hash",
                analysis_duration_ms=5,
                completed_at=_now(),
            )
        ],
    )

    after_count = len([event async for event in store.load_all(from_position=0, apply_upcasters=False)])

    assert before_count == after_count
    assert result.real_outcome.decision_recommendation == "DECLINE"
    assert result.counterfactual_outcome.projected_recommendation == "APPROVE"
    assert result.counterfactual_outcome.projected_approved_amount_usd == "750000.00"
    assert any(
        item["kind"] == "skipped_dependent" and item["event_type"] == "DecisionGenerated"
        for item in result.divergence_events
    )


@pytest.mark.asyncio
async def test_regulatory_package_is_self_contained_and_independently_verifiable():
    store = InMemoryEventStore()
    application_id = _app_id()
    await _seed_phase6_override_case(store, application_id)

    package = await generate_regulatory_package(store, application_id, examination_date=_now())
    verification = verify_regulatory_package(package)

    assert verification.ok is True
    assert package["application_id"] == application_id
    assert package["projection_states_as_of"]["application_summary"]["state"] == "FINAL_APPROVED"
    assert package["integrity_verification"]["chain_valid"] is True
    assert package["audit_stream"][-1]["event_type"] == "AuditIntegrityCheckRun"
    assert any("with override" in sentence for sentence in package["narrative"])
    assert any("The application was approved for $750000" in sentence for sentence in package["narrative"])
    assert len(package["agent_model_metadata"]) == 4
    assert any(
        item["event_type"] == "DecisionGenerated"
        and item.get("model_versions", {}).get("decision_orchestrator") == "orch-v1"
        for item in package["agent_model_metadata"]
    )

    tampered = deepcopy(package)
    tampered["projection_states_as_of"]["application_summary"]["state"] = "FINAL_DECLINED"
    assert verify_regulatory_package(tampered).ok is False
