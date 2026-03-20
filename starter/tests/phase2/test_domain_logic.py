from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ledger.commands.handlers import (
    CompleteHumanReviewCommand,
    DocumentUploadedCommand,
    GenerateDecisionCommand,
    RequestComplianceCheckCommand,
    RequestCreditAnalysisCommand,
    RequestDecisionCommand,
    RequestFraudScreeningCommand,
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    complete_human_review,
    generate_decision,
    record_document_uploaded,
    request_compliance_check,
    request_credit_analysis,
    request_decision,
    request_fraud_screening,
    start_agent_session,
    submit_application,
)
from ledger.domain.aggregates import AgentSessionAggregate, ApplicationState, LoanApplicationAggregate
from ledger.domain.errors import BusinessRuleViolation, DomainError, InvalidStateTransition
from ledger.event_store import InMemoryEventStore, OptimisticConcurrencyError
from ledger.schema.events import (
    AgentType,
    ComplianceCheckCompleted,
    ComplianceVerdict,
    CreditAnalysisCompleted,
    CreditDecision,
    DocumentFormat,
    DocumentType,
    FraudScreeningCompleted,
    RiskTier,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _submit_and_upload_required_docs(store: InMemoryEventStore, application_id: str) -> None:
    await submit_application(
        store,
        SubmitApplicationCommand(
            application_id=application_id,
            applicant_id="COMP-100",
            requested_amount_usd=Decimal("500000"),
            loan_purpose="working_capital",
            loan_term_months=36,
            submission_channel="portal",
            contact_email="loan@applicant.test",
            contact_name="Taylor Borrower",
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
                application_id=application_id,
                document_id=f"DOC-{idx}",
                document_type=document_type,
                document_format=DocumentFormat.PDF,
                filename=f"{document_type.value}.pdf",
                file_path=f"documents/{application_id}/{document_type.value}.pdf",
                file_size_bytes=1000 + idx,
                file_hash=f"hash-{idx}",
                uploaded_by="applicant",
                fiscal_year=2024 if document_type != DocumentType.APPLICATION_PROPOSAL else None,
                uploaded_at=_now(),
            ),
        )


async def _seed_credit_completed(store: InMemoryEventStore, application_id: str) -> None:
    event = CreditAnalysisCompleted(
        application_id=application_id,
        session_id="sess-credit-1",
        decision=CreditDecision(
            risk_tier=RiskTier.MEDIUM,
            recommended_limit_usd=Decimal("420000"),
            confidence=0.81,
            rationale="Stable financials with moderate leverage.",
            key_concerns=["Leverage remains elevated"],
        ),
        model_version="qwen3-coder",
        model_deployment_id="dep-credit-1",
        input_data_hash="credit-hash",
        analysis_duration_ms=1200,
        completed_at=_now(),
    ).to_store_dict()
    await store.append(f"credit-{application_id}", [event], expected_version=-1)


async def _seed_fraud_completed(store: InMemoryEventStore, application_id: str) -> None:
    event = FraudScreeningCompleted(
        application_id=application_id,
        session_id="sess-fraud-1",
        fraud_score=0.12,
        risk_level="LOW",
        anomalies_found=0,
        recommendation="CLEAR",
        screening_model_version="qwen3-coder",
        input_data_hash="fraud-hash",
        completed_at=_now(),
    ).to_store_dict()
    await store.append(f"fraud-{application_id}", [event], expected_version=-1)


async def _seed_compliance_completed(
    store: InMemoryEventStore,
    application_id: str,
    *,
    verdict: ComplianceVerdict,
    has_hard_block: bool,
) -> None:
    event = ComplianceCheckCompleted(
        application_id=application_id,
        session_id="sess-comp-1",
        rules_evaluated=6,
        rules_passed=5 if verdict != ComplianceVerdict.BLOCKED else 2,
        rules_failed=1 if verdict == ComplianceVerdict.BLOCKED else 0,
        rules_noted=1,
        has_hard_block=has_hard_block,
        overall_verdict=verdict,
        completed_at=_now(),
    ).to_store_dict()
    await store.append(f"compliance-{application_id}", [event], expected_version=-1)


async def _prepare_pending_decision(store: InMemoryEventStore, application_id: str) -> None:
    await _submit_and_upload_required_docs(store, application_id)
    await request_credit_analysis(
        store,
        RequestCreditAnalysisCommand(
            application_id=application_id,
            requested_by="document-processing-agent",
        ),
    )
    await _seed_credit_completed(store, application_id)
    await request_fraud_screening(
        store,
        RequestFraudScreeningCommand(application_id=application_id),
    )
    await _seed_fraud_completed(store, application_id)
    await request_compliance_check(
        store,
        RequestComplianceCheckCommand(
            application_id=application_id,
            regulation_set_version="2026-Q1",
        ),
    )
    await _seed_compliance_completed(
        store,
        application_id,
        verdict=ComplianceVerdict.CLEAR,
        has_hard_block=False,
    )
    await request_decision(
        store,
        RequestDecisionCommand(application_id=application_id),
    )


@pytest.mark.asyncio
async def test_loan_application_rehydrates_from_event_stream():
    store = InMemoryEventStore()
    app_id = "APEX-P2-001"

    await _submit_and_upload_required_docs(store, app_id)
    await request_credit_analysis(
        store,
        RequestCreditAnalysisCommand(application_id=app_id, requested_by="doc-agent"),
    )

    aggregate = await LoanApplicationAggregate.load(store, app_id)
    assert aggregate.state == ApplicationState.CREDIT_ANALYSIS_REQUESTED
    assert aggregate.applicant_id == "COMP-100"
    assert aggregate.has_required_documents() is True
    assert len(aggregate.uploaded_document_ids) == 3
    assert aggregate.version == 5


@pytest.mark.asyncio
async def test_credit_analysis_requires_all_required_documents():
    store = InMemoryEventStore()
    app_id = "APEX-P2-002"

    await submit_application(
        store,
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-200",
            requested_amount_usd=Decimal("300000"),
            loan_purpose="working_capital",
            loan_term_months=24,
            submission_channel="portal",
            contact_email="ops@test.local",
            contact_name="Sam Borrower",
        ),
    )
    await record_document_uploaded(
        store,
        DocumentUploadedCommand(
            application_id=app_id,
            document_id="DOC-ONLY",
            document_type=DocumentType.APPLICATION_PROPOSAL,
            document_format=DocumentFormat.PDF,
            filename="proposal.pdf",
            file_path="documents/proposal.pdf",
            file_size_bytes=1234,
            file_hash="hash-doc-only",
            uploaded_by="applicant",
            uploaded_at=_now(),
        ),
    )

    with pytest.raises(BusinessRuleViolation):
        await request_credit_analysis(
            store,
            RequestCreditAnalysisCommand(application_id=app_id, requested_by="doc-agent"),
        )


@pytest.mark.asyncio
async def test_fraud_screening_requires_credit_completion_stream():
    store = InMemoryEventStore()
    app_id = "APEX-P2-003"

    await _submit_and_upload_required_docs(store, app_id)
    await request_credit_analysis(
        store,
        RequestCreditAnalysisCommand(application_id=app_id, requested_by="doc-agent"),
    )

    with pytest.raises(BusinessRuleViolation):
        await request_fraud_screening(
            store,
            RequestFraudScreeningCommand(application_id=app_id),
        )

    await _seed_credit_completed(store, app_id)
    await request_fraud_screening(
        store,
        RequestFraudScreeningCommand(application_id=app_id),
    )

    aggregate = await LoanApplicationAggregate.load(store, app_id)
    assert aggregate.state == ApplicationState.FRAUD_SCREENING_REQUESTED


@pytest.mark.asyncio
async def test_low_confidence_decision_must_refer():
    store = InMemoryEventStore()
    app_id = "APEX-P2-004"
    await _prepare_pending_decision(store, app_id)

    with pytest.raises(BusinessRuleViolation):
        await generate_decision(
            store,
            GenerateDecisionCommand(
                application_id=app_id,
                orchestrator_session_id="sess-orch-1",
                recommendation="APPROVE",
                confidence=0.55,
                approved_amount_usd=Decimal("200000"),
                executive_summary="Attempted automated approval despite low confidence.",
            ),
        )


@pytest.mark.asyncio
async def test_refer_decision_emits_human_review_request():
    store = InMemoryEventStore()
    app_id = "APEX-P2-005"
    await _prepare_pending_decision(store, app_id)

    result = await generate_decision(
        store,
        GenerateDecisionCommand(
            application_id=app_id,
            orchestrator_session_id="sess-orch-2",
            recommendation="REFER",
            confidence=0.55,
            executive_summary="Low confidence requires human review.",
            human_review_reason="Confidence below the approval threshold.",
            human_review_assignee="LO-Sarah-Chen",
        ),
    )

    assert [event["event_type"] for event in result.events] == [
        "DecisionGenerated",
        "HumanReviewRequested",
    ]
    aggregate = await LoanApplicationAggregate.load(store, app_id)
    assert aggregate.state == ApplicationState.PENDING_HUMAN_REVIEW
    assert aggregate.last_recommendation == "REFER"


@pytest.mark.asyncio
async def test_compliance_block_prevents_decision_request():
    store = InMemoryEventStore()
    app_id = "APEX-P2-006"

    await _submit_and_upload_required_docs(store, app_id)
    await request_credit_analysis(
        store,
        RequestCreditAnalysisCommand(application_id=app_id, requested_by="doc-agent"),
    )
    await _seed_credit_completed(store, app_id)
    await request_fraud_screening(
        store,
        RequestFraudScreeningCommand(application_id=app_id),
    )
    await _seed_fraud_completed(store, app_id)
    await request_compliance_check(
        store,
        RequestComplianceCheckCommand(
            application_id=app_id,
            regulation_set_version="2026-Q1",
        ),
    )
    await _seed_compliance_completed(
        store,
        app_id,
        verdict=ComplianceVerdict.BLOCKED,
        has_hard_block=True,
    )

    with pytest.raises(BusinessRuleViolation):
        await request_decision(
            store,
            RequestDecisionCommand(application_id=app_id),
        )


@pytest.mark.asyncio
async def test_human_review_override_requires_reason_and_can_finalize():
    store = InMemoryEventStore()
    app_id = "APEX-P2-007"
    await _prepare_pending_decision(store, app_id)
    await generate_decision(
        store,
        GenerateDecisionCommand(
            application_id=app_id,
            orchestrator_session_id="sess-orch-3",
            recommendation="REFER",
            confidence=0.58,
            executive_summary="Human review required.",
        ),
    )

    with pytest.raises(BusinessRuleViolation):
        await complete_human_review(
            store,
            CompleteHumanReviewCommand(
                application_id=app_id,
                reviewer_id="LO-Sarah-Chen",
                override=True,
                original_recommendation="REFER",
                final_decision="APPROVE",
                approved_amount_usd=Decimal("300000"),
                interest_rate_pct=7.5,
                term_months=36,
            ),
        )

    result = await complete_human_review(
        store,
        CompleteHumanReviewCommand(
            application_id=app_id,
            reviewer_id="LO-Sarah-Chen",
            override=True,
            original_recommendation="REFER",
            final_decision="APPROVE",
            override_reason="Collateral and repayment history justify approval.",
            approved_amount_usd=Decimal("300000"),
            interest_rate_pct=7.5,
            term_months=36,
            conditions=["Monthly covenant reporting"],
        ),
    )
    assert [event["event_type"] for event in result.events] == [
        "HumanReviewCompleted",
        "ApplicationApproved",
    ]

    aggregate = await LoanApplicationAggregate.load(store, app_id)
    assert aggregate.state == ApplicationState.APPROVED


@pytest.mark.asyncio
async def test_agent_session_start_is_gas_town_bootstrap():
    store = InMemoryEventStore()
    result = await start_agent_session(
        store,
        StartAgentSessionCommand(
            agent_type=AgentType.CREDIT_ANALYSIS,
            session_id="sess-credit-p2",
            agent_id="credit-agent-1",
            application_id="APEX-P2-008",
            model_version="qwen3-coder",
            langgraph_graph_version="1.0.0",
            context_source="fresh",
            context_token_count=1024,
        ),
    )
    assert result.events[0]["event_type"] == "AgentSessionStarted"

    aggregate = await AgentSessionAggregate.load(
        store,
        AgentType.CREDIT_ANALYSIS.value,
        "sess-credit-p2",
    )
    assert aggregate.context_loaded is True
    assert aggregate.model_version == "qwen3-coder"

    with pytest.raises(InvalidStateTransition):
        await start_agent_session(
            store,
            StartAgentSessionCommand(
                agent_type=AgentType.CREDIT_ANALYSIS,
                session_id="sess-credit-p2",
                agent_id="credit-agent-1",
                application_id="APEX-P2-008",
                model_version="deepseek-v3.1",
                langgraph_graph_version="1.0.0",
            ),
        )


@pytest.mark.asyncio
async def test_concurrent_generate_decision_allows_only_one_write():
    store = InMemoryEventStore()
    app_id = "APEX-P2-009"
    await _prepare_pending_decision(store, app_id)

    command = GenerateDecisionCommand(
        application_id=app_id,
        orchestrator_session_id="sess-orch-concurrent",
        recommendation="DECLINE",
        confidence=0.83,
        executive_summary="Debt profile warrants decline.",
        key_risks=["High leverage"],
    )

    results = await asyncio.gather(
        generate_decision(store, command),
        generate_decision(store, command),
        return_exceptions=True,
    )
    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [
        result
        for result in results
        if isinstance(result, (OptimisticConcurrencyError, DomainError))
    ]

    assert len(successes) == 1
    assert len(failures) == 1
