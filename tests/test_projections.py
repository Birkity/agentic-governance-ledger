from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
    AgentSessionCompleted,
    AgentType,
    ComplianceCheckCompleted,
    ComplianceCheckInitiated,
    ComplianceRulePassed,
    ComplianceVerdict,
    CreditAnalysisRequested,
    CreditDecision,
    DecisionGenerated,
    DocumentType,
    FraudScreeningRequested,
    PackageReadyForAnalysis,
    RiskTier,
)
from src.projections import (
    AgentPerformanceProjection,
    ApplicationSummaryProjection,
    ComplianceAuditProjection,
    ProjectionDaemon,
)
from src.projections.base import Projection


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _app_id(prefix: str = "APEX-P3") -> str:
    return f"{prefix}-{uuid4().hex[:8].upper()}"


async def _append_current(store: InMemoryEventStore, stream_id: str, events: list[dict]) -> list[int]:
    version = await store.stream_version(stream_id)
    return await store.append(stream_id, events, expected_version=version)


async def _submit_application(store: InMemoryEventStore, application_id: str) -> None:
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=application_id,
            applicant_id="COMP-024",
            requested_amount_usd=Decimal("450000"),
            loan_purpose="working_capital",
            loan_term_months=36,
            submission_channel="portal",
            contact_email="ops@company.test",
            contact_name="Ava Ops",
            application_reference=f"REF-{application_id}",
        ),
        store,
    )


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


async def _request_credit(store: InMemoryEventStore, application_id: str) -> None:
    await _append_current(
        store,
        f"loan-{application_id}",
        [
            CreditAnalysisRequested(
                application_id=application_id,
                requested_at=_now(),
                requested_by="document_processor",
            ).to_store_dict()
        ],
    )


async def _start_session(
    store: InMemoryEventStore,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    model_version: str,
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
    await _append_current(
        store,
        f"agent-{agent_type.value}-{session_id}",
        [
            AgentOutputWritten(
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
            ).to_store_dict(),
            AgentSessionCompleted(
                session_id=session_id,
                agent_type=agent_type,
                application_id=application_id,
                total_nodes_executed=4,
                total_llm_calls=1 if agent_type != AgentType.COMPLIANCE else 0,
                total_tokens_used=420,
                total_cost_usd=0.02,
                total_duration_ms=900,
                completed_at=_now(),
            ).to_store_dict(),
        ],
    )


async def _build_approved_application(store: InMemoryEventStore, application_id: str) -> None:
    await _submit_application(store, application_id)
    await _seed_package_ready(store, application_id)
    await _request_credit(store, application_id)

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
            analysis_duration_ms=600,
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
            fraud_score=0.11,
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
        stream_position=await store.stream_version(f"loan-{application_id}"),
    )

    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=application_id,
            reviewer_id="LO-Sarah-Chen",
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


async def _build_blocked_application(store: InMemoryEventStore, application_id: str) -> None:
    await _submit_application(store, application_id)
    await _seed_package_ready(store, application_id)
    await _request_credit(store, application_id)

    await _start_session(store, application_id, AgentType.CREDIT_ANALYSIS, "blocked-credit", model_version="credit-v1")
    credit_result = await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=application_id,
            session_id="blocked-credit",
            decision=CreditDecision(
                risk_tier=RiskTier.MEDIUM,
                recommended_limit_usd=Decimal("300000"),
                confidence=0.77,
                rationale="acceptable risk",
            ),
            model_version="credit-v1",
            model_deployment_id="dep-credit",
            input_data_hash="credit-hash",
            analysis_duration_ms=550,
        ),
        store,
    )
    await _record_agent_output(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        "blocked-credit",
        stream_id=f"credit-{application_id}",
        event_type="CreditAnalysisCompleted",
        stream_position=credit_result[f"credit-{application_id}"][-1],
    )

    await _start_session(store, application_id, AgentType.FRAUD_DETECTION, "blocked-fraud", model_version="fraud-v1")
    fraud_result = await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=application_id,
            session_id="blocked-fraud",
            fraud_score=0.21,
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
        "blocked-fraud",
        stream_id=f"fraud-{application_id}",
        event_type="FraudScreeningCompleted",
        stream_position=fraud_result[f"fraud-{application_id}"][-1],
    )

    await _start_session(store, application_id, AgentType.COMPLIANCE, "blocked-compliance", model_version="compliance-v1")
    compliance_result = await handle_compliance_check(
        ComplianceCheckCommand(
            application_id=application_id,
            session_id="blocked-compliance",
            regulation_set_version="2026-Q1",
            rules_to_evaluate=["REG-001", "REG-003"],
            rule_results=[
                ComplianceRuleEvaluation("REG-001", "AML", "PASS", "1", "e1"),
                ComplianceRuleEvaluation(
                    "REG-003",
                    "Jurisdiction",
                    "FAIL",
                    "1",
                    "e3",
                    failure_reason="Montana excluded",
                    is_hard_block=True,
                ),
            ],
            model_version="compliance-v1",
        ),
        store,
    )
    await _record_agent_output(
        store,
        application_id,
        AgentType.COMPLIANCE,
        "blocked-compliance",
        stream_id=f"compliance-{application_id}",
        event_type="ComplianceCheckCompleted",
        stream_position=compliance_result[f"compliance-{application_id}"][-1],
    )


class FlakyProjection(Projection):
    name = "flaky_projection"

    def __init__(self, store) -> None:
        super().__init__(store)
        self.failures = 0
        self.seen: list[str] = []

    def handles(self, event) -> bool:
        return event.event_type == "ApplicationSubmitted"

    async def apply(self, event) -> None:
        if self.failures < 2:
            self.failures += 1
            raise RuntimeError("synthetic projection failure")
        self.seen.append(event.payload["application_id"])

    async def reset(self) -> None:
        self.failures = 0
        self.seen.clear()


@pytest.mark.asyncio
async def test_projections_build_read_models_for_terminal_states():
    store = InMemoryEventStore()
    approved_id = _app_id("APEX-P3A")
    blocked_id = _app_id("APEX-P3B")

    await _build_approved_application(store, approved_id)
    await _build_blocked_application(store, blocked_id)

    summary = ApplicationSummaryProjection(store)
    performance = AgentPerformanceProjection(store)
    compliance = ComplianceAuditProjection(store)
    daemon = ProjectionDaemon(store, [summary, performance, compliance], batch_size=500)
    await daemon.run_until_caught_up()

    approved = await summary.get(approved_id)
    blocked = await summary.get(blocked_id)
    current_compliance = await compliance.get_current_compliance(approved_id)
    performance_rows = await performance.list_all()

    assert approved is not None
    assert blocked is not None
    assert approved.state == "FINAL_APPROVED"
    assert approved.compliance_status == "CLEAR"
    assert blocked.state == "DECLINED_COMPLIANCE"
    assert blocked.compliance_status == "BLOCKED"
    assert current_compliance is not None
    assert current_compliance.completed is True
    assert current_compliance.overall_verdict == "CLEAR"
    assert any(row.agent_type == "decision_orchestrator" and row.decisions_generated == 1 for row in performance_rows)
    assert any(row.agent_type == "credit_analysis" and row.analyses_completed >= 1 for row in performance_rows)


@pytest.mark.asyncio
async def test_compliance_audit_supports_temporal_queries_and_rebuild():
    store = InMemoryEventStore()
    application_id = _app_id("APEX-P3T")
    base_time = _now()

    initiated = ComplianceCheckInitiated(
        application_id=application_id,
        session_id="sess-comp",
        regulation_set_version="2026-Q1",
        rules_to_evaluate=["REG-001", "REG-002"],
        initiated_at=base_time,
    ).to_store_dict()
    initiated["recorded_at"] = base_time

    passed = ComplianceRulePassed(
        application_id=application_id,
        session_id="sess-comp",
        rule_id="REG-001",
        rule_name="AML",
        rule_version="1",
        evidence_hash="e1",
        evaluation_notes="ok",
        evaluated_at=base_time + timedelta(seconds=1),
    ).to_store_dict()
    passed["recorded_at"] = base_time + timedelta(seconds=1)

    completed = ComplianceCheckCompleted(
        application_id=application_id,
        session_id="sess-comp",
        rules_evaluated=2,
        rules_passed=1,
        rules_failed=0,
        rules_noted=0,
        has_hard_block=False,
        overall_verdict=ComplianceVerdict.CLEAR,
        completed_at=base_time + timedelta(seconds=3),
    ).to_store_dict()
    completed["recorded_at"] = base_time + timedelta(seconds=3)

    await _append_current(store, f"compliance-{application_id}", [initiated, passed, completed])

    projection = ComplianceAuditProjection(store)
    daemon = ProjectionDaemon(store, [projection], batch_size=100)
    await daemon.run_until_caught_up()

    mid_state = await projection.get_compliance_at(application_id, base_time + timedelta(seconds=2))
    current = await projection.get_current_compliance(application_id)
    assert mid_state is not None
    assert current is not None
    assert mid_state.completed is False
    assert len(mid_state.passed_rules) == 1
    assert current.completed is True
    assert current.overall_verdict == "CLEAR"

    await projection.rebuild_from_scratch()
    rebuilt = await projection.get_current_compliance(application_id)
    assert rebuilt is not None
    assert rebuilt.completed is True
    assert rebuilt.overall_verdict == "CLEAR"
    assert len(rebuilt.passed_rules) == 1


@pytest.mark.asyncio
async def test_projection_daemon_uses_checkpoints_and_resumes():
    store = InMemoryEventStore()
    application_id = _app_id("APEX-P3R")
    await _submit_application(store, application_id)

    projection = ApplicationSummaryProjection(store)
    daemon = ProjectionDaemon(store, [projection], batch_size=50)
    await daemon.run_until_caught_up()
    first_checkpoint = await store.load_checkpoint(projection.name)
    first_view = await projection.get(application_id)

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

    resumed_projection = ApplicationSummaryProjection(store)
    resumed_daemon = ProjectionDaemon(store, [resumed_projection], batch_size=50)
    await resumed_daemon.run_until_caught_up()

    second_checkpoint = await store.load_checkpoint(resumed_projection.name)
    resumed_view = await resumed_projection.get(application_id)

    assert first_view is not None
    assert resumed_view is not None
    assert first_checkpoint > 0
    assert second_checkpoint > first_checkpoint
    assert resumed_view.state == "DOCUMENTS_PROCESSED"


@pytest.mark.asyncio
async def test_projection_daemon_retries_then_skips_bad_event_without_crashing_other_projections():
    store = InMemoryEventStore()
    application_id = _app_id("APEX-P3F")
    await _submit_application(store, application_id)

    summary = ApplicationSummaryProjection(store)
    flaky = FlakyProjection(store)
    daemon = ProjectionDaemon(store, [summary, flaky], batch_size=50, max_retries=1)

    await daemon.process_batch()
    await daemon.process_batch()
    await daemon.run_until_caught_up()

    summary_view = await summary.get(application_id)
    skipped = daemon.skipped_events(flaky.name)

    assert summary_view is not None
    assert summary_view.state in {"SUBMITTED", "DOCUMENTS_PENDING"}
    assert len(skipped) == 1
    assert skipped[0]["event_type"] == "ApplicationSubmitted"


@pytest.mark.asyncio
async def test_projection_lag_stays_low_under_50_concurrent_submissions():
    store = InMemoryEventStore()
    summary = ApplicationSummaryProjection(store)
    daemon = ProjectionDaemon(store, [summary], batch_size=500)

    application_ids = [_app_id("APEX-P3L") for _ in range(50)]
    await asyncio.gather(*[_submit_application(store, application_id) for application_id in application_ids])
    await daemon.run_until_caught_up(max_cycles=20)

    lag = await daemon.get_lag(summary.name)
    records = await summary.list_all()

    assert len(records) == 50
    assert lag.lag_positions == 0
    assert lag.lag_ms <= 800
