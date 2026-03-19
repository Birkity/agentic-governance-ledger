from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.commands.handlers import (
    ApproveApplicationCommand,
    DocumentUploadedCommand,
    GenerateDecisionCommand,
    RequestComplianceCheckCommand,
    RequestCreditAnalysisCommand,
    RequestDecisionCommand,
    RequestFraudScreeningCommand,
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    approve_application,
    generate_decision,
    record_document_uploaded,
    request_compliance_check,
    request_credit_analysis,
    request_decision,
    request_fraud_screening,
    start_agent_session,
    submit_application,
)
from ledger.event_store import InMemoryEventStore
from ledger.projections import (
    AgentPerformanceProjection,
    ApplicationSummaryProjection,
    ComplianceAuditProjection,
    ProjectionDaemon,
)
from ledger.schema.events import (
    AgentSessionCompleted,
    AgentType,
    ApplicationDeclined,
    ComplianceCheckCompleted,
    ComplianceCheckInitiated,
    ComplianceRuleFailed,
    ComplianceRulePassed,
    ComplianceVerdict,
    CreditAnalysisCompleted,
    CreditDecision,
    DocumentFormat,
    DocumentType,
    FraudScreeningCompleted,
    PackageReadyForAnalysis,
    RiskTier,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_dict(event_obj) -> dict:
    event = event_obj.to_store_dict()
    if getattr(event_obj, "recorded_at", None) is not None:
        event["recorded_at"] = event_obj.recorded_at
    return event


async def _append_event(store: InMemoryEventStore, stream_id: str, event_obj) -> None:
    expected_version = await store.stream_version(stream_id)
    await store.append(
        stream_id,
        [_event_dict(event_obj)],
        expected_version=expected_version,
    )


async def _drain_daemon(daemon: ProjectionDaemon, *, max_rounds: int = 20) -> None:
    for _ in range(max_rounds):
        result = await daemon.process_once()
        if result["events_seen"] == 0:
            return
    raise AssertionError("Projection daemon did not reach a steady state")


async def _load_seed_history(store: InMemoryEventStore) -> None:
    seed_path = Path(__file__).resolve().parents[2] / "data" / "seed_events.jsonl"
    with seed_path.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            expected_version = await store.stream_version(record["stream_id"])
            await store.append(
                record["stream_id"],
                [
                    {
                        "event_type": record["event_type"],
                        "event_version": record.get("event_version", 1),
                        "payload": record["payload"],
                        "metadata": record.get("metadata", {}),
                        "recorded_at": record.get("recorded_at"),
                    }
                ],
                expected_version=expected_version,
            )


@pytest.mark.asyncio
async def test_projection_daemon_builds_application_summary_and_agent_performance():
    store = InMemoryEventStore()
    app_id = "APEX-P3-001"
    base_time = _now()

    await submit_application(
        store,
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-900",
            requested_amount_usd=Decimal("350000"),
            loan_purpose="working_capital",
            loan_term_months=36,
            submission_channel="portal",
            contact_email="owner@example.test",
            contact_name="Jordan Founder",
            submitted_at=base_time,
        ),
    )

    for idx, document_type in enumerate(
        [
            DocumentType.APPLICATION_PROPOSAL,
            DocumentType.INCOME_STATEMENT,
            DocumentType.BALANCE_SHEET,
        ],
        start=1,
    ):
        await record_document_uploaded(
            store,
            DocumentUploadedCommand(
                application_id=app_id,
                document_id=f"DOC-{idx}",
                document_type=document_type,
                document_format=DocumentFormat.PDF,
                filename=f"{document_type.value}.pdf",
                file_path=f"documents/{app_id}/{document_type.value}.pdf",
                file_size_bytes=2048 + idx,
                file_hash=f"hash-{idx}",
                uploaded_by="applicant",
                fiscal_year=2024 if document_type != DocumentType.APPLICATION_PROPOSAL else None,
                uploaded_at=base_time + timedelta(minutes=idx),
            ),
        )

    await _append_event(
        store,
        f"docpkg-{app_id}",
        PackageReadyForAnalysis(
            package_id=app_id,
            application_id=app_id,
            documents_processed=3,
            has_quality_flags=False,
            quality_flag_count=0,
            ready_at=base_time + timedelta(minutes=5),
            recorded_at=base_time + timedelta(minutes=5),
        ),
    )

    await request_credit_analysis(
        store,
        RequestCreditAnalysisCommand(
            application_id=app_id,
            requested_by="document-processing-agent",
            requested_at=base_time + timedelta(minutes=6),
        ),
    )

    await start_agent_session(
        store,
        StartAgentSessionCommand(
            agent_type=AgentType.CREDIT_ANALYSIS,
            session_id="sess-credit-p3",
            agent_id="credit-agent-1",
            application_id=app_id,
            model_version="qwen3-coder",
            langgraph_graph_version="1.0.0",
            started_at=base_time + timedelta(minutes=7),
        ),
    )

    await _append_event(
        store,
        f"credit-{app_id}",
        CreditAnalysisCompleted(
            application_id=app_id,
            session_id="sess-credit-p3",
            decision=CreditDecision(
                risk_tier=RiskTier.MEDIUM,
                recommended_limit_usd=Decimal("320000"),
                confidence=0.82,
                rationale="Healthy repayment capacity with moderate leverage.",
                key_concerns=["Customer concentration"],
            ),
            model_version="qwen3-coder",
            model_deployment_id="dep-credit-p3",
            input_data_hash="credit-hash-p3",
            analysis_duration_ms=1500,
            completed_at=base_time + timedelta(minutes=8),
            recorded_at=base_time + timedelta(minutes=8),
        ),
    )
    await _append_event(
        store,
        "agent-credit_analysis-sess-credit-p3",
        AgentSessionCompleted(
            session_id="sess-credit-p3",
            agent_type=AgentType.CREDIT_ANALYSIS,
            application_id=app_id,
            total_nodes_executed=5,
            total_llm_calls=1,
            total_tokens_used=1800,
            total_cost_usd=0.02,
            total_duration_ms=1500,
            next_agent_triggered="fraud_detection",
            completed_at=base_time + timedelta(minutes=8, seconds=5),
            recorded_at=base_time + timedelta(minutes=8, seconds=5),
        ),
    )

    await request_fraud_screening(
        store,
        RequestFraudScreeningCommand(
            application_id=app_id,
            requested_at=base_time + timedelta(minutes=9),
        ),
    )
    await _append_event(
        store,
        f"fraud-{app_id}",
        FraudScreeningCompleted(
            application_id=app_id,
            session_id="sess-fraud-p3",
            fraud_score=0.14,
            risk_level="LOW",
            anomalies_found=0,
            recommendation="PROCEED",
            screening_model_version="qwen3-coder",
            input_data_hash="fraud-hash-p3",
            completed_at=base_time + timedelta(minutes=10),
            recorded_at=base_time + timedelta(minutes=10),
        ),
    )

    await request_compliance_check(
        store,
        RequestComplianceCheckCommand(
            application_id=app_id,
            regulation_set_version="2026-Q1",
            requested_at=base_time + timedelta(minutes=11),
        ),
    )
    await _append_event(
        store,
        f"compliance-{app_id}",
        ComplianceCheckCompleted(
            application_id=app_id,
            session_id="sess-compliance-p3",
            rules_evaluated=6,
            rules_passed=5,
            rules_failed=0,
            rules_noted=1,
            has_hard_block=False,
            overall_verdict=ComplianceVerdict.CLEAR,
            completed_at=base_time + timedelta(minutes=12),
            recorded_at=base_time + timedelta(minutes=12),
        ),
    )

    await request_decision(
        store,
        RequestDecisionCommand(
            application_id=app_id,
            requested_at=base_time + timedelta(minutes=13),
        ),
    )
    await start_agent_session(
        store,
        StartAgentSessionCommand(
            agent_type=AgentType.DECISION_ORCHESTRATOR,
            session_id="sess-orch-p3",
            agent_id="decision-orchestrator-1",
            application_id=app_id,
            model_version="deepseek-v3.1",
            langgraph_graph_version="1.0.0",
            started_at=base_time + timedelta(minutes=14),
        ),
    )
    await generate_decision(
        store,
        GenerateDecisionCommand(
            application_id=app_id,
            orchestrator_session_id="sess-orch-p3",
            recommendation="APPROVE",
            confidence=0.88,
            executive_summary="Approve with covenant monitoring.",
            approved_amount_usd=Decimal("300000"),
            conditions=["Quarterly covenant testing"],
            generated_at=base_time + timedelta(minutes=15),
        ),
    )
    await _append_event(
        store,
        "agent-decision_orchestrator-sess-orch-p3",
        AgentSessionCompleted(
            session_id="sess-orch-p3",
            agent_type=AgentType.DECISION_ORCHESTRATOR,
            application_id=app_id,
            total_nodes_executed=4,
            total_llm_calls=1,
            total_tokens_used=2400,
            total_cost_usd=0.03,
            total_duration_ms=2200,
            next_agent_triggered=None,
            completed_at=base_time + timedelta(minutes=15, seconds=5),
            recorded_at=base_time + timedelta(minutes=15, seconds=5),
        ),
    )
    await approve_application(
        store,
        ApproveApplicationCommand(
            application_id=app_id,
            approved_amount_usd=Decimal("300000"),
            interest_rate_pct=7.9,
            term_months=36,
            approved_by="auto",
            conditions=["Quarterly covenant testing"],
            approved_at=base_time + timedelta(minutes=16),
        ),
    )

    summary = ApplicationSummaryProjection(store)
    performance = AgentPerformanceProjection(store)
    compliance = ComplianceAuditProjection(store)
    daemon = ProjectionDaemon(store, [summary, performance, compliance], batch_size=200)
    await _drain_daemon(daemon)

    application_row = await summary.get(app_id)
    assert application_row is not None
    assert application_row["state"] == "APPROVED"
    assert application_row["final_decision"] == "APPROVE"
    assert application_row["risk_tier"] == "MEDIUM"
    assert application_row["fraud_score"] == pytest.approx(0.14)
    assert application_row["compliance_status"] == "CLEAR"
    assert application_row["agent_sessions_completed"] == 2

    rows = await performance.list_all()
    by_type = {row["agent_type"]: row for row in rows}
    assert by_type["credit_analysis"]["average_confidence"] == pytest.approx(0.82)
    assert by_type["decision_orchestrator"]["decision_approve_count"] == 1
    assert by_type["decision_orchestrator"]["average_confidence"] == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_projection_daemon_resumes_from_checkpoints_after_restart():
    store = InMemoryEventStore()

    await submit_application(
        store,
        SubmitApplicationCommand(
            application_id="APEX-P3-RESUME-1",
            applicant_id="COMP-001",
            requested_amount_usd=Decimal("100000"),
            loan_purpose="working_capital",
            loan_term_months=24,
            submission_channel="portal",
            contact_email="a@example.test",
            contact_name="A Borrower",
        ),
    )

    first_summary = ApplicationSummaryProjection(store)
    first_daemon = ProjectionDaemon(store, [first_summary], batch_size=50)
    await _drain_daemon(first_daemon)
    first_checkpoint = await store.load_checkpoint(first_summary.projection_name)

    await submit_application(
        store,
        SubmitApplicationCommand(
            application_id="APEX-P3-RESUME-2",
            applicant_id="COMP-002",
            requested_amount_usd=Decimal("125000"),
            loan_purpose="expansion",
            loan_term_months=36,
            submission_channel="branch",
            contact_email="b@example.test",
            contact_name="B Borrower",
        ),
    )

    second_summary = ApplicationSummaryProjection(store)
    second_daemon = ProjectionDaemon(store, [second_summary], batch_size=50)
    await _drain_daemon(second_daemon)

    second_checkpoint = await store.load_checkpoint(second_summary.projection_name)
    rows = await second_summary.list_all()

    assert len(rows) == 2
    assert second_checkpoint > first_checkpoint
    assert await second_summary.get("APEX-P3-RESUME-1") is not None
    assert await second_summary.get("APEX-P3-RESUME-2") is not None


@pytest.mark.asyncio
async def test_compliance_audit_supports_temporal_queries():
    store = InMemoryEventStore()
    app_id = "APEX-P3-COMP-1"
    t0 = _now()
    t1 = t0 + timedelta(seconds=10)
    t2 = t0 + timedelta(seconds=20)
    t3 = t0 + timedelta(seconds=30)
    t4 = t0 + timedelta(seconds=40)

    await _append_event(
        store,
        f"compliance-{app_id}",
        ComplianceCheckInitiated(
            application_id=app_id,
            session_id="sess-comp-p3",
            regulation_set_version="2026-Q1",
            rules_to_evaluate=["REG-001", "REG-002"],
            initiated_at=t0,
            recorded_at=t0,
        ),
    )
    await _append_event(
        store,
        f"compliance-{app_id}",
        ComplianceRulePassed(
            application_id=app_id,
            session_id="sess-comp-p3",
            rule_id="REG-001",
            rule_name="BSA",
            rule_version="2026-Q1-v1",
            evidence_hash="hash-pass",
            evaluation_notes="Clear.",
            evaluated_at=t1,
            recorded_at=t1,
        ),
    )
    await _append_event(
        store,
        f"compliance-{app_id}",
        ComplianceRuleFailed(
            application_id=app_id,
            session_id="sess-comp-p3",
            rule_id="REG-002",
            rule_name="OFAC",
            rule_version="2026-Q1-v1",
            failure_reason="Sanctions review hit.",
            is_hard_block=True,
            remediation_available=False,
            remediation_description=None,
            evidence_hash="hash-fail",
            evaluated_at=t2,
            recorded_at=t2,
        ),
    )
    await _append_event(
        store,
        f"compliance-{app_id}",
        ComplianceCheckCompleted(
            application_id=app_id,
            session_id="sess-comp-p3",
            rules_evaluated=2,
            rules_passed=1,
            rules_failed=1,
            rules_noted=0,
            has_hard_block=True,
            overall_verdict=ComplianceVerdict.BLOCKED,
            completed_at=t3,
            recorded_at=t3,
        ),
    )
    await _append_event(
        store,
        f"loan-{app_id}",
        ApplicationDeclined(
            application_id=app_id,
            decline_reasons=["Compliance hard block: REG-002"],
            declined_by="compliance-system",
            adverse_action_notice_required=True,
            adverse_action_codes=["COMPLIANCE_BLOCK"],
            declined_at=t4,
            recorded_at=t4,
        ),
    )

    compliance = ComplianceAuditProjection(store)
    daemon = ProjectionDaemon(store, [compliance], batch_size=50)
    await _drain_daemon(daemon)

    before_completion = await compliance.get_compliance_at(app_id, t2 + timedelta(milliseconds=1))
    assert before_completion is not None
    assert before_completion["rules_evaluated"] == 2
    assert before_completion["overall_verdict"] is None
    assert before_completion["has_hard_block"] is True

    current = await compliance.get(app_id)
    assert current is not None
    assert current["overall_verdict"] == "BLOCKED"
    assert current["declined_due_to_compliance"] is True


@pytest.mark.asyncio
async def test_projection_lag_reports_backlog_and_catchup():
    store = InMemoryEventStore()
    await submit_application(
        store,
        SubmitApplicationCommand(
            application_id="APEX-P3-LAG-1",
            applicant_id="COMP-LAG",
            requested_amount_usd=Decimal("180000"),
            loan_purpose="working_capital",
            loan_term_months=24,
            submission_channel="portal",
            contact_email="lag@example.test",
            contact_name="Lag Borrower",
        ),
    )

    summary = ApplicationSummaryProjection(store)
    daemon = ProjectionDaemon(store, [summary], batch_size=50)

    lag_before = await daemon.get_lag(summary.projection_name)
    assert lag_before["position_lag"] > 0

    await _drain_daemon(daemon)

    lag_after = await daemon.get_lag(summary.projection_name)
    assert lag_after["position_lag"] == 0
    assert lag_after["checkpoint"] == lag_after["latest_position"]


@pytest.mark.asyncio
async def test_seed_history_rebuilds_application_summary_without_drift():
    store = InMemoryEventStore()
    await _load_seed_history(store)

    summary = ApplicationSummaryProjection(store)
    compliance = ComplianceAuditProjection(store)
    performance = AgentPerformanceProjection(store)
    daemon = ProjectionDaemon(store, [summary, compliance, performance], batch_size=5000)
    await _drain_daemon(daemon)

    counts = await summary.count_by_state()
    assert len(await summary.list_all()) == 29
    assert counts.get("APPROVED", 0) == 4
    assert counts.get("DECLINED", 0) == 2
    assert counts.get("DECLINED_COMPLIANCE", 0) == 2
    assert counts.get("REFERRED", 0) == 1
    assert (await summary.get("APEX-0029"))["state"] == "REFERRED"
    assert (await compliance.get("APEX-0028"))["overall_verdict"] == "BLOCKED"

    await summary.rebuild_from_scratch()
    rebuild_daemon = ProjectionDaemon(store, [summary], batch_size=5000)
    await _drain_daemon(rebuild_daemon)

    rebuilt_counts = await summary.count_by_state()
    assert rebuilt_counts == counts
