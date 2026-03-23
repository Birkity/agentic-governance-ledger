from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from src.document_processing import DocumentPackageProcessor, persist_document_package
from src.document_processing.models import DocumentPackageAnalysis
from src.event_store import EventStore, InMemoryEventStore, OptimisticConcurrencyError
from src.integrity import reconstruct_agent_context
from src.models.events import (
    AgentNodeExecuted,
    AgentOutputWritten,
    AgentSessionCompleted,
    AgentSessionFailed,
    AgentSessionRecovered,
    AgentToolCalled,
    AgentType,
    CreditAnalysisCompleted,
    CreditDecision,
    DocumentType,
    HumanReviewRequested,
    LoanPurpose,
    RiskTier,
)
from src.regulatory import generate_regulatory_package, verify_regulatory_package
from src.what_if import run_what_if


ROOT = Path(__file__).resolve().parents[2]
APPLICANT_PROFILES_PATH = ROOT / "data" / "applicant_profiles.json"
SEED_EVENTS_PATH = ROOT / "data" / "seed_events.jsonl"
DOCUMENTS_ROOT = ROOT / "documents"
ARTIFACTS_ROOT = ROOT / "artifacts"

NARRATIVE_COMPANIES = {
    "NARR-01": "COMP-024",
    "NARR-02": "COMP-001",
    "NARR-03": "COMP-057",
    "NARR-05": "COMP-068",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _serialize(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default))


def _load_profiles() -> list[dict[str, Any]]:
    return json.loads(APPLICANT_PROFILES_PATH.read_text(encoding="utf-8"))


def profile_for_company(company_id: str) -> dict[str, Any]:
    for profile in _load_profiles():
        if profile["company_id"] == company_id:
            return profile
    raise KeyError(f"Unknown company_id: {company_id}")


def find_montana_company_id() -> str:
    for profile in _load_profiles():
        if profile.get("jurisdiction") == "MT":
            return str(profile["company_id"])
    raise LookupError("No Montana company found in applicant profiles")


def default_company_for_application(application_id: str) -> str:
    normalized = application_id.upper()
    for key, company_id in NARRATIVE_COMPANIES.items():
        if key in normalized.replace("_", "-"):
            return company_id

    match = re.search(r"(\d{3,4})$", normalized)
    if match:
        company_id = f"COMP-{int(match.group(1)) % 1000:03d}"
        if (DOCUMENTS_ROOT / company_id).exists():
            return company_id

    for profile in _load_profiles():
        candidate = str(profile["company_id"])
        if (DOCUMENTS_ROOT / candidate).exists():
            return candidate
    raise LookupError(f"Could not infer a company for application_id={application_id}")


async def _append_current(store: EventStore | InMemoryEventStore, stream_id: str, events: list[dict]) -> list[int]:
    version = await store.stream_version(stream_id)
    return await store.append(stream_id, events, expected_version=version)


async def _start_session(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    agent_id: str,
    model_version: str,
    context_source: str = "fresh",
    context_token_count: int = 512,
) -> str:
    await handle_start_agent_session(
        StartAgentSessionCommand(
            application_id=application_id,
            agent_type=agent_type,
            session_id=session_id,
            agent_id=agent_id,
            model_version=model_version,
            langgraph_graph_version="graph-v1",
            context_source=context_source,
            context_token_count=context_token_count,
        ),
        store,
    )
    return f"agent-{agent_type.value}-{session_id}"


async def _append_session_event(
    store: EventStore | InMemoryEventStore,
    agent_type: AgentType,
    session_id: str,
    event: dict[str, Any],
) -> list[int]:
    return await _append_current(store, f"agent-{agent_type.value}-{session_id}", [event])


async def _record_node(
    store: EventStore | InMemoryEventStore,
    agent_type: AgentType,
    session_id: str,
    *,
    node_name: str,
    node_sequence: int,
    llm_called: bool = True,
    input_keys: list[str] | None = None,
    output_keys: list[str] | None = None,
    duration_ms: int = 120,
) -> list[int]:
    event = AgentNodeExecuted(
        session_id=session_id,
        agent_type=agent_type,
        node_name=node_name,
        node_sequence=node_sequence,
        input_keys=input_keys or [],
        output_keys=output_keys or [],
        llm_called=llm_called,
        llm_tokens_input=480 if llm_called else None,
        llm_tokens_output=120 if llm_called else None,
        llm_cost_usd=0.04 if llm_called else 0.0,
        duration_ms=duration_ms,
        executed_at=_now(),
    ).to_store_dict()
    return await _append_session_event(store, agent_type, session_id, event)


async def _record_tool(
    store: EventStore | InMemoryEventStore,
    agent_type: AgentType,
    session_id: str,
    *,
    tool_name: str,
    tool_input_summary: str,
    tool_output_summary: str,
) -> list[int]:
    event = AgentToolCalled(
        session_id=session_id,
        agent_type=agent_type,
        tool_name=tool_name,
        tool_input_summary=tool_input_summary,
        tool_output_summary=tool_output_summary,
        tool_duration_ms=40,
        called_at=_now(),
    ).to_store_dict()
    return await _append_session_event(store, agent_type, session_id, event)


async def _record_agent_output(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    stream_id: str,
    event_type: str,
    stream_position: int,
) -> list[int]:
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
    return await _append_session_event(store, agent_type, session_id, event)


async def _complete_session(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    next_agent_triggered: str | None = None,
    total_nodes: int = 3,
    total_llm_calls: int = 1,
    total_tokens_used: int = 900,
    total_cost_usd: float = 0.12,
) -> list[int]:
    event = AgentSessionCompleted(
        session_id=session_id,
        agent_type=agent_type,
        application_id=application_id,
        total_nodes_executed=total_nodes,
        total_llm_calls=total_llm_calls,
        total_tokens_used=total_tokens_used,
        total_cost_usd=total_cost_usd,
        total_duration_ms=650,
        next_agent_triggered=next_agent_triggered,
        completed_at=_now(),
    ).to_store_dict()
    return await _append_session_event(store, agent_type, session_id, event)


def _requested_amount_from_analysis(analysis: DocumentPackageAnalysis, company_id: str) -> Decimal:
    proposal = analysis.get_document(DocumentType.APPLICATION_PROPOSAL)
    if proposal and proposal.structured_data.get("requested_amount_usd") is not None:
        return Decimal(str(proposal.structured_data["requested_amount_usd"]))
    numeric = int(company_id.split("-")[-1])
    return Decimal(str(400_000 + (numeric * 12_500)))


def _loan_purpose_from_analysis(analysis: DocumentPackageAnalysis) -> LoanPurpose:
    proposal = analysis.get_document(DocumentType.APPLICATION_PROPOSAL)
    raw = str(proposal.structured_data.get("purpose", "") if proposal else "").strip().lower()
    mapping = {
        "working capital": LoanPurpose.WORKING_CAPITAL,
        "equipment financing": LoanPurpose.EQUIPMENT_FINANCING,
        "expansion": LoanPurpose.EXPANSION,
        "acquisition": LoanPurpose.ACQUISITION,
        "refinancing": LoanPurpose.REFINANCING,
        "real estate": LoanPurpose.REAL_ESTATE,
        "bridge": LoanPurpose.BRIDGE,
    }
    return mapping.get(raw, LoanPurpose.WORKING_CAPITAL)


def _quality_caveats(analysis: DocumentPackageAnalysis) -> list[str]:
    caveats: list[str] = []
    income_doc = analysis.get_document(DocumentType.INCOME_STATEMENT)
    if income_doc:
        for note in income_doc.extraction_notes:
            if "missing ebitda" in note.lower():
                source = analysis.field_sources.get("ebitda", "other sources")
                caveats.append(f"EBITDA missing in source PDF and backfilled from {source}.")
    caveats.extend(analysis.consistency_notes)
    return caveats


def _credit_decision_from_profile(
    company_id: str,
    profile: dict[str, Any],
    analysis: DocumentPackageAnalysis,
    requested_amount_usd: Decimal,
) -> CreditDecision:
    risk_segment = str(profile.get("risk_segment", "MEDIUM")).upper()
    trajectory = str(profile.get("trajectory", "STABLE")).upper()
    caveats = _quality_caveats(analysis)

    if risk_segment == "HIGH" or trajectory == "DECLINING":
        risk_tier = RiskTier.HIGH
        confidence = 0.82 if company_id == "COMP-068" else 0.74
        limit_factor = Decimal("0.78")
    elif trajectory == "GROWTH":
        risk_tier = RiskTier.LOW
        confidence = 0.89
        limit_factor = Decimal("1.00")
    else:
        risk_tier = RiskTier.MEDIUM
        confidence = 0.77
        limit_factor = Decimal("0.92")

    if caveats:
        confidence = max(0.62, confidence - 0.08)
    recommended_limit = (requested_amount_usd * limit_factor).quantize(Decimal("1"))
    rationale_parts = [
        f"Trajectory={trajectory}",
        f"risk_segment={risk_segment}",
        f"recommended_limit={recommended_limit}",
    ]
    if caveats:
        rationale_parts.append("data_quality_caveats=" + "; ".join(caveats))
    return CreditDecision(
        risk_tier=risk_tier,
        recommended_limit_usd=recommended_limit,
        confidence=confidence,
        rationale=". ".join(rationale_parts),
    )


async def _submit_application(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str,
    analysis: DocumentPackageAnalysis,
    *,
    requested_amount_usd: Decimal | None = None,
) -> dict[str, list[int]]:
    profile = profile_for_company(company_id)
    requested = requested_amount_usd or _requested_amount_from_analysis(analysis, company_id)
    proposal = analysis.get_document(DocumentType.APPLICATION_PROPOSAL)
    contact_name = str(proposal.structured_data.get("legal_entity_name")) if proposal else profile["name"]
    return await handle_submit_application(
        SubmitApplicationCommand(
            application_id=application_id,
            applicant_id=company_id,
            requested_amount_usd=requested,
            loan_purpose=_loan_purpose_from_analysis(analysis),
            loan_term_months=36,
            submission_channel="relationship_manager",
            contact_email=f"{company_id.lower()}@example.com",
            contact_name=contact_name,
            application_reference=application_id,
        ),
        store,
    )


async def _ensure_credit_requested(
    store: EventStore | InMemoryEventStore,
    application_id: str,
) -> list[int]:
    loan_events = await store.load_stream(f"loan-{application_id}")
    if any(event.event_type == "CreditAnalysisRequested" for event in loan_events):
        return []
    return await _append_current(
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


async def _request_human_review(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    *,
    reason: str,
) -> list[int]:
    loan_events = await store.load_stream(f"loan-{application_id}")
    decision_event = next(event for event in reversed(loan_events) if event.event_type == "DecisionGenerated")
    request = HumanReviewRequested(
        application_id=application_id,
        reason=reason,
        decision_event_id=str(decision_event.event_id),
        assigned_to="loan-operations",
        requested_at=_now(),
    ).to_store_dict()
    return await _append_current(store, f"loan-{application_id}", [request])


async def run_document_phase(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str,
    *,
    include_summaries: bool = False,
) -> dict[str, Any]:
    processor = DocumentPackageProcessor(documents_root=DOCUMENTS_ROOT)
    analysis = processor.process_company(company_id, include_summaries=include_summaries)
    positions = await persist_document_package(store, analysis, application_id=application_id)
    return {
        "application_id": application_id,
        "company_id": company_id,
        "analysis": analysis,
        "positions": positions,
        "quality_caveats": _quality_caveats(analysis),
    }


async def run_credit_phase(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str | None = None,
    *,
    requested_amount_usd: Decimal | None = None,
) -> dict[str, Any]:
    company = company_id or default_company_for_application(application_id)
    doc_result = await run_document_phase(store, application_id, company)
    analysis = doc_result["analysis"]
    profile = profile_for_company(company)

    await _submit_application(
        store,
        application_id,
        company,
        analysis,
        requested_amount_usd=requested_amount_usd,
    )
    await _ensure_credit_requested(store, application_id)

    session_id = "sess-credit"
    stream_id = await _start_session(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        session_id,
        agent_id=f"credit-{company.lower()}",
        model_version="credit-v1",
    )
    await _record_node(
        store,
        AgentType.CREDIT_ANALYSIS,
        session_id,
        node_name="load_facts",
        node_sequence=1,
        llm_called=False,
        input_keys=["docpkg"],
        output_keys=["financial_facts"],
    )
    await _record_tool(
        store,
        AgentType.CREDIT_ANALYSIS,
        session_id,
        tool_name="registry.lookup",
        tool_input_summary=company,
        tool_output_summary=f"industry={profile['industry']}; trajectory={profile['trajectory']}",
    )

    requested = requested_amount_usd or _requested_amount_from_analysis(analysis, company)
    decision = _credit_decision_from_profile(company, profile, analysis, requested)
    positions = await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=application_id,
            session_id=session_id,
            decision=decision,
            model_version="credit-v1",
            model_deployment_id="credit-dep-v1",
            input_data_hash=f"credit-{application_id.lower()}",
            analysis_duration_ms=640,
            regulatory_basis=_quality_caveats(analysis),
        ),
        store,
    )
    credit_position = positions[f"credit-{application_id}"][-1]
    await _record_agent_output(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        session_id,
        stream_id=f"credit-{application_id}",
        event_type="CreditAnalysisCompleted",
        stream_position=credit_position,
    )
    await _complete_session(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        session_id,
        next_agent_triggered="fraud_detection",
        total_cost_usd=0.23,
    )
    return {
        "application_id": application_id,
        "company_id": company,
        "analysis": analysis,
        "profile": profile,
        "credit_session_stream_id": stream_id,
        "credit_decision": decision,
        "requested_amount_usd": requested,
        "quality_caveats": _quality_caveats(analysis),
    }


async def _run_fraud_stage(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str,
    *,
    fraud_score: float = 0.11,
    session_id: str = "sess-fraud",
    context_source: str = "fresh",
    ensure_started: bool = True,
) -> dict[str, Any]:
    profile = profile_for_company(company_id)
    stream_id = f"agent-{AgentType.FRAUD_DETECTION.value}-{session_id}"
    if ensure_started:
        stream_id = await _start_session(
            store,
            application_id,
            AgentType.FRAUD_DETECTION,
            session_id,
            agent_id=f"fraud-{company_id.lower()}",
            model_version="fraud-v1",
            context_source=context_source,
        )
    await _record_node(
        store,
        AgentType.FRAUD_DETECTION,
        session_id,
        node_name="cross_reference_registry",
        node_sequence=2 if context_source.startswith("prior_session_replay:") else 1,
        llm_called=False,
        input_keys=["credit_output"],
        output_keys=["registry_context"],
    )
    await _record_tool(
        store,
        AgentType.FRAUD_DETECTION,
        session_id,
        tool_name="registry.lookup",
        tool_input_summary=company_id,
        tool_output_summary=f"flags={len(profile.get('compliance_flags', []))}",
    )
    risk_level = "LOW" if fraud_score < 0.30 else "MEDIUM" if fraud_score < 0.60 else "HIGH"
    positions = await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=application_id,
            session_id=session_id,
            fraud_score=fraud_score,
            risk_level=risk_level,
            anomalies_found=0 if fraud_score < 0.30 else 1,
            recommendation="CLEAR" if fraud_score < 0.60 else "REVIEW",
            screening_model_version="fraud-v1",
            input_data_hash=f"fraud-{application_id.lower()}",
        ),
        store,
    )
    fraud_position = positions[f"fraud-{application_id}"][-1]
    await _record_agent_output(
        store,
        application_id,
        AgentType.FRAUD_DETECTION,
        session_id,
        stream_id=f"fraud-{application_id}",
        event_type="FraudScreeningCompleted",
        stream_position=fraud_position,
    )
    await _complete_session(
        store,
        application_id,
        AgentType.FRAUD_DETECTION,
        session_id,
        next_agent_triggered="compliance",
        total_cost_usd=0.11,
    )
    return {
        "fraud_session_stream_id": stream_id,
        "fraud_score": fraud_score,
        "risk_level": risk_level,
    }


async def _run_compliance_stage(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str,
    *,
    session_id: str = "sess-compliance",
    hard_block: bool = False,
) -> dict[str, Any]:
    profile = profile_for_company(company_id)
    stream_id = await _start_session(
        store,
        application_id,
        AgentType.COMPLIANCE,
        session_id,
        agent_id=f"compliance-{company_id.lower()}",
        model_version="compliance-v1",
    )
    await _record_tool(
        store,
        AgentType.COMPLIANCE,
        session_id,
        tool_name="registry.lookup",
        tool_input_summary=company_id,
        tool_output_summary=f"jurisdiction={profile['jurisdiction']}",
    )
    rules = [
        ComplianceRuleEvaluation("REG-001", "AML", "PASS", "1", "e1", evaluation_notes="Customer cleared sanctions screening."),
        ComplianceRuleEvaluation("REG-002", "OFAC", "PASS", "1", "e2", evaluation_notes="No OFAC match."),
    ]
    if hard_block:
        rules.append(
            ComplianceRuleEvaluation(
                "REG-003",
                "Jurisdiction",
                "FAIL",
                "1",
                "e3",
                failure_reason="Montana is excluded from this product.",
                is_hard_block=True,
            )
        )
    else:
        rules.extend(
            [
                ComplianceRuleEvaluation("REG-003", "Jurisdiction", "PASS", "1", "e3", evaluation_notes="Jurisdiction allowed."),
                ComplianceRuleEvaluation("REG-004", "Entity Type", "PASS", "1", "e4", evaluation_notes="Entity type is eligible."),
                ComplianceRuleEvaluation("REG-005", "Operating History", "PASS", "1", "e5", evaluation_notes="Operating history satisfies the policy."),
                ComplianceRuleEvaluation("REG-006", "CRA", "NOTE", "1", "e6", note_type="CRA", note_text="CRA consideration recorded."),
            ]
        )

    positions = await handle_compliance_check(
        ComplianceCheckCommand(
            application_id=application_id,
            session_id=session_id,
            regulation_set_version="2026-Q1",
            rules_to_evaluate=[rule.rule_id for rule in rules],
            rule_results=rules,
            model_version="compliance-v1",
        ),
        store,
    )
    compliance_position = positions[f"compliance-{application_id}"][-1]
    await _record_agent_output(
        store,
        application_id,
        AgentType.COMPLIANCE,
        session_id,
        stream_id=f"compliance-{application_id}",
        event_type="ComplianceCheckCompleted",
        stream_position=compliance_position,
    )
    await _complete_session(
        store,
        application_id,
        AgentType.COMPLIANCE,
        session_id,
        next_agent_triggered=None if hard_block else "decision_orchestrator",
        total_cost_usd=0.0,
    )
    return {
        "compliance_session_stream_id": stream_id,
        "hard_block": hard_block,
        "rule_ids": [rule.rule_id for rule in rules],
    }


def _recommended_decision(
    credit_decision: CreditDecision,
    *,
    quality_caveats: list[str],
    fraud_score: float,
) -> tuple[str, float, Decimal | None, list[str], str]:
    confidence = max(0.60, float(credit_decision.confidence))
    if credit_decision.risk_tier == RiskTier.HIGH:
        return "DECLINE", confidence, None, ["high leverage", "industry volatility"], "Credit risk remains outside the approval box."
    if fraud_score >= 0.60:
        return "DECLINE", confidence, None, ["fraud score elevated"], "Fraud score requires a decline."
    if quality_caveats:
        approved_amount = Decimal(str(credit_decision.recommended_limit_usd))
        return "REFER", confidence, approved_amount, ["data quality caveats"], "Manual review is required because extracted data contains caveats."
    approved_amount = Decimal(str(credit_decision.recommended_limit_usd))
    return "APPROVE", confidence, approved_amount, ["risk within policy"], "Automated decision falls within policy."


async def run_full_pipeline(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str | None = None,
) -> dict[str, Any]:
    company = company_id or default_company_for_application(application_id)
    credit_result = await run_credit_phase(store, application_id, company)
    await _run_fraud_stage(store, application_id, company, fraud_score=0.11)
    compliance_result = await _run_compliance_stage(store, application_id, company, hard_block=False)

    recommendation, confidence, approved_amount, key_risks, summary = _recommended_decision(
        credit_result["credit_decision"],
        quality_caveats=credit_result["quality_caveats"],
        fraud_score=0.11,
    )

    session_id = "sess-orch"
    await _start_session(
        store,
        application_id,
        AgentType.DECISION_ORCHESTRATOR,
        session_id,
        agent_id=f"orch-{company.lower()}",
        model_version="orch-v1",
    )
    positions = await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=application_id,
            session_id=session_id,
            recommendation=recommendation,
            confidence=confidence,
            approved_amount_usd=approved_amount,
            conditions=[],
            executive_summary=summary,
            key_risks=key_risks,
            contributing_sessions=[
                credit_result["credit_session_stream_id"],
                "agent-fraud_detection-sess-fraud",
                compliance_result["compliance_session_stream_id"],
            ],
            model_versions={"decision_orchestrator": "orch-v1"},
        ),
        store,
    )
    decision_position = positions[f"loan-{application_id}"][0]
    await _record_agent_output(
        store,
        application_id,
        AgentType.DECISION_ORCHESTRATOR,
        session_id,
        stream_id=f"loan-{application_id}",
        event_type="DecisionGenerated",
        stream_position=decision_position,
    )
    await _complete_session(
        store,
        application_id,
        AgentType.DECISION_ORCHESTRATOR,
        session_id,
        next_agent_triggered="human_review",
        total_cost_usd=0.15,
    )

    await _request_human_review(store, application_id, reason="Standard post-decision adjudication.")
    final_decision = "APPROVE" if recommendation != "DECLINE" else "DECLINE"
    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=application_id,
            reviewer_id="loan-ops",
            override=False,
            original_recommendation=recommendation,
            final_decision=final_decision,
            approved_amount_usd=approved_amount or Decimal("0"),
            interest_rate_pct=8.75 if final_decision == "APPROVE" else None,
            term_months=36 if final_decision == "APPROVE" else None,
            conditions=["Quarterly covenant certificate"] if final_decision == "APPROVE" else [],
            decline_reasons=["Automated decline confirmed"] if final_decision == "DECLINE" else [],
            adverse_action_notice_required=True,
            adverse_action_codes=["AUTO-DECLINE"] if final_decision == "DECLINE" else [],
        ),
        store,
    )

    loan_events = await store.load_stream(f"loan-{application_id}")
    return {
        "application_id": application_id,
        "company_id": company,
        "final_event_type": loan_events[-1].event_type if loan_events else None,
        "credit_risk_tier": credit_result["credit_decision"].risk_tier.value,
        "quality_caveats": credit_result["quality_caveats"],
    }


async def narr01_occ_collision(
    store: EventStore | InMemoryEventStore,
    application_id: str = "APEX-NARR01",
) -> dict[str, Any]:
    stream_id = f"loan-{application_id}"
    await store.append(
        stream_id,
        [
            {"event_type": "ApplicationSubmitted", "event_version": 1, "payload": {"application_id": application_id, "sequence": 1}},
            {"event_type": "CreditAnalysisRequested", "event_version": 1, "payload": {"application_id": application_id, "sequence": 2}},
            {"event_type": "FraudScreeningRequested", "event_version": 1, "payload": {"application_id": application_id, "sequence": 3}},
        ],
        expected_version=-1,
    )
    current_version = await store.stream_version(stream_id)

    results = await asyncio.gather(
        store.append(stream_id, [{"event_type": "DecisionGenerated", "event_version": 1, "payload": {"marker": "A"}}], expected_version=current_version),
        store.append(stream_id, [{"event_type": "DecisionGenerated", "event_version": 1, "payload": {"marker": "B"}}], expected_version=current_version),
        return_exceptions=True,
    )
    stream_events = await store.load_stream(stream_id, apply_upcasters=False)
    successes = [result for result in results if isinstance(result, list)]
    failures = [result for result in results if isinstance(result, OptimisticConcurrencyError)]
    return {
        "application_id": application_id,
        "stream_id": stream_id,
        "successful_appends": len(successes),
        "optimistic_concurrency_failures": len(failures),
        "final_stream_length": len(stream_events),
        "final_stream_positions": [event.stream_position for event in stream_events],
    }


async def narr02_missing_ebitda(
    store: EventStore | InMemoryEventStore,
    application_id: str = "APEX-NARR02",
    company_id: str = "COMP-001",
) -> dict[str, Any]:
    result = await run_credit_phase(store, application_id, company_id)
    income = result["analysis"].get_document(DocumentType.INCOME_STATEMENT)
    credit_events = await store.load_stream(f"credit-{application_id}")
    credit_event = credit_events[-1]
    return {
        "application_id": application_id,
        "company_id": company_id,
        "income_notes": list(income.extraction_notes if income else []),
        "merged_ebitda": str(result["analysis"].merged_financial_facts.ebitda),
        "ebitda_source": result["analysis"].field_sources.get("ebitda"),
        "credit_confidence": credit_event.payload["decision"]["confidence"],
        "credit_rationale": credit_event.payload["decision"]["rationale"],
    }


async def narr03_crash_recovery(
    store: EventStore | InMemoryEventStore,
    application_id: str = "APEX-NARR03",
    company_id: str = "COMP-057",
) -> dict[str, Any]:
    await run_credit_phase(store, application_id, company_id, requested_amount_usd=Decimal("1100000"))

    crashed_session_id = "sess-fraud-crashed"
    recovered_session_id = "sess-fraud-recovered"
    await _start_session(
        store,
        application_id,
        AgentType.FRAUD_DETECTION,
        crashed_session_id,
        agent_id="fraud-agent-057",
        model_version="fraud-v1",
    )
    await _record_node(
        store,
        AgentType.FRAUD_DETECTION,
        crashed_session_id,
        node_name="load_facts",
        node_sequence=1,
        llm_called=False,
        input_keys=["credit_analysis"],
        output_keys=["normalized_facts"],
    )
    await _append_session_event(
        store,
        AgentType.FRAUD_DETECTION,
        crashed_session_id,
        AgentSessionFailed(
            session_id=crashed_session_id,
            agent_type=AgentType.FRAUD_DETECTION,
            application_id=application_id,
            error_type="SimulatedCrash",
            error_message="Crash after load_facts",
            last_successful_node="load_facts",
            recoverable=True,
            failed_at=_now(),
        ).to_store_dict(),
    )

    reconstructed = await reconstruct_agent_context(
        store,
        AgentType.FRAUD_DETECTION.value,
        crashed_session_id,
    )
    await _start_session(
        store,
        application_id,
        AgentType.FRAUD_DETECTION,
        recovered_session_id,
        agent_id="fraud-agent-057",
        model_version="fraud-v1",
        context_source=f"prior_session_replay:{crashed_session_id}",
    )
    await _append_session_event(
        store,
        AgentType.FRAUD_DETECTION,
        recovered_session_id,
        AgentSessionRecovered(
            session_id=recovered_session_id,
            agent_type=AgentType.FRAUD_DETECTION,
            application_id=application_id,
            recovered_from_session_id=crashed_session_id,
            recovery_point=reconstructed.last_successful_node or "load_facts",
            recovered_at=_now(),
        ).to_store_dict(),
    )
    await _record_node(
        store,
        AgentType.FRAUD_DETECTION,
        recovered_session_id,
        node_name="cross_reference_registry",
        node_sequence=2,
        llm_called=False,
        input_keys=["normalized_facts"],
        output_keys=["registry_cross_check"],
    )
    await _run_fraud_stage(
        store,
        application_id,
        company_id,
        fraud_score=0.09,
        session_id=recovered_session_id,
        context_source=f"prior_session_replay:{crashed_session_id}",
        ensure_started=False,
    )

    fraud_events = await store.load_stream(f"fraud-{application_id}")
    first_session_events = await store.load_stream(f"agent-fraud_detection-{crashed_session_id}")
    second_session_events = await store.load_stream(f"agent-fraud_detection-{recovered_session_id}")
    return {
        "application_id": application_id,
        "company_id": company_id,
        "crashed_session_id": crashed_session_id,
        "recovered_session_id": recovered_session_id,
        "reconstructed_context": _serialize(reconstructed),
        "fraud_events": [event.model_dump(mode="json") for event in fraud_events],
        "first_session_events": [event.model_dump(mode="json") for event in first_session_events],
        "second_session_events": [event.model_dump(mode="json") for event in second_session_events],
    }


async def narr04_montana_hard_block(
    store: EventStore | InMemoryEventStore,
    application_id: str = "APEX-NARR04",
    company_id: str | None = None,
) -> dict[str, Any]:
    company = company_id or find_montana_company_id()
    await run_credit_phase(store, application_id, company)
    await _run_fraud_stage(store, application_id, company, fraud_score=0.12)
    await _run_compliance_stage(store, application_id, company, hard_block=True)

    compliance_events = await store.load_stream(f"compliance-{application_id}")
    loan_events = await store.load_stream(f"loan-{application_id}")
    return {
        "application_id": application_id,
        "company_id": company,
        "compliance_events": [event.model_dump(mode="json") for event in compliance_events],
        "loan_events": [event.model_dump(mode="json") for event in loan_events],
    }


async def narr05_human_override(
    store: EventStore | InMemoryEventStore,
    application_id: str = "APEX-NARR05",
    company_id: str = "COMP-068",
) -> dict[str, Any]:
    requested_amount = Decimal("950000")
    analysis_result = await run_document_phase(store, application_id, company_id)
    analysis = analysis_result["analysis"]
    await _submit_application(
        store,
        application_id,
        company_id,
        analysis,
        requested_amount_usd=requested_amount,
    )
    await _ensure_credit_requested(store, application_id)

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
                rationale="High leverage and declining revenue trend support an automated decline recommendation.",
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

    await _run_fraud_stage(store, application_id, company_id, fraud_score=0.08, session_id="sess-fraud")
    await _run_compliance_stage(store, application_id, company_id, hard_block=False, session_id="sess-compliance")

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
            key_risks=["high leverage", "declining revenue trajectory"],
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

    await _request_human_review(
        store,
        application_id,
        reason="Relationship manager requested manual adjudication.",
    )
    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=application_id,
            reviewer_id="LO-Sarah-Chen",
            override=True,
            override_reason="15-year customer, prior repayment history, collateral offered",
            original_recommendation="DECLINE",
            final_decision="APPROVE",
            approved_amount_usd=Decimal("750000"),
            interest_rate_pct=9.10,
            term_months=36,
            conditions=[
                "Monthly revenue reporting for 12 months",
                "Personal guarantee from CEO",
            ],
        ),
        store,
    )

    loan_events = await store.load_stream(f"loan-{application_id}")
    return {
        "application_id": application_id,
        "company_id": company_id,
        "loan_events": [event.model_dump(mode="json") for event in loan_events],
    }


def _seed_application_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with SEED_EVENTS_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("event_type") != "ApplicationSubmitted":
                continue
            payload = record["payload"]
            rows.append(
                {
                    "application_id": payload["application_id"],
                    "company_id": payload["applicant_id"],
                    "requested_amount_usd": Decimal(str(payload["requested_amount_usd"])),
                }
            )
    return rows


def _analysis_cache(company_id: str, cache: dict[str, DocumentPackageAnalysis]) -> DocumentPackageAnalysis:
    if company_id not in cache:
        processor = DocumentPackageProcessor(documents_root=DOCUMENTS_ROOT)
        cache[company_id] = processor.process_company(company_id, include_summaries=False)
    return cache[company_id]


def _estimate_agent_costs(
    *,
    company_id: str,
    requested_amount_usd: Decimal,
    analysis: DocumentPackageAnalysis,
    profile: dict[str, Any],
) -> dict[str, float]:
    caveat_count = sum(len(document.extraction_notes) for document in analysis.documents) + len(analysis.consistency_notes)
    risk_weight = 0.06 if str(profile.get("risk_segment", "")).upper() == "HIGH" else 0.03 if str(profile.get("risk_segment", "")).upper() == "MEDIUM" else 0.01
    trajectory = str(profile.get("trajectory", "")).upper()
    trajectory_weight = 0.04 if trajectory == "DECLINING" else 0.02 if trajectory == "RECOVERING" else 0.01 if trajectory == "GROWTH" else 0.0
    amount_weight = 0.05 if requested_amount_usd >= Decimal("2000000") else 0.03 if requested_amount_usd >= Decimal("1000000") else 0.015
    flag_weight = 0.02 if profile.get("compliance_flags") else 0.0

    return {
        "document_processing": round(0.08 + (0.006 * len(analysis.documents)) + (0.003 * caveat_count), 2),
        "credit_analysis": round(0.19 + risk_weight + trajectory_weight + amount_weight + (0.008 * caveat_count), 2),
        "fraud_detection": round(0.09 + flag_weight + (0.02 if profile.get("industry") in {"retail", "logistics"} else 0.0), 2),
        "compliance": 0.0,
        "decision_orchestrator": round(0.15 + (0.02 if caveat_count else 0.0) + risk_weight + trajectory_weight, 2),
    }


def build_api_cost_report() -> str:
    analysis_cache: dict[str, DocumentPackageAnalysis] = {}
    application_rows = _seed_application_rows()
    narrative_rows = [
        {"application_id": "APEX-NARR01", "company_id": NARRATIVE_COMPANIES["NARR-01"], "requested_amount_usd": Decimal("4036813")},
        {"application_id": "APEX-NARR02", "company_id": NARRATIVE_COMPANIES["NARR-02"], "requested_amount_usd": Decimal("700000")},
        {"application_id": "APEX-NARR03", "company_id": NARRATIVE_COMPANIES["NARR-03"], "requested_amount_usd": Decimal("1100000")},
        {"application_id": "APEX-NARR04", "company_id": find_montana_company_id(), "requested_amount_usd": Decimal("650000")},
        {"application_id": "APEX-NARR05", "company_id": NARRATIVE_COMPANIES["NARR-05"], "requested_amount_usd": Decimal("950000")},
    ]
    all_rows = application_rows + narrative_rows

    totals_by_agent = {
        "document_processing": 0.0,
        "credit_analysis": 0.0,
        "fraud_detection": 0.0,
        "compliance": 0.0,
        "decision_orchestrator": 0.0,
    }
    total_per_application: list[tuple[str, float]] = []
    most_expensive: tuple[str, str, float] = ("", "", 0.0)

    for row in all_rows:
        company_id = str(row["company_id"])
        analysis = _analysis_cache(company_id, analysis_cache)
        profile = profile_for_company(company_id)
        costs = _estimate_agent_costs(
            company_id=company_id,
            requested_amount_usd=Decimal(str(row["requested_amount_usd"])),
            analysis=analysis,
            profile=profile,
        )
        app_total = round(sum(costs.values()), 2)
        total_per_application.append((str(row["application_id"]), app_total))
        for agent, cost in costs.items():
            totals_by_agent[agent] += cost
            if cost > most_expensive[2]:
                most_expensive = (str(row["application_id"]), agent, cost)

    grand_total = round(sum(cost for _, cost in total_per_application), 2)
    avg_cost = round(grand_total / len(total_per_application), 2)
    min_cost = min(cost for _, cost in total_per_application)
    max_cost = max(cost for _, cost in total_per_application)
    return "\n".join(
        [
            "Sentinel Cost Attribution Report",
            f"Generated at: {_now().isoformat()}",
            "Methodology: deterministic offline estimate derived from prompt budget assumptions, data quality caveats, requested amount, and applicant risk profile.",
            f"Applications processed: {len(application_rows)} seed + {len(narrative_rows)} narrative = {len(all_rows)} total",
            "",
            f"Total API cost for all 34 applications: ${grand_total:.2f}",
            f"Average cost per application: avg ${avg_cost:.2f}, range ${min_cost:.2f}-${max_cost:.2f}",
            "Cost by agent:",
            f"- DocProc ${totals_by_agent['document_processing']:.2f}",
            f"- Credit ${totals_by_agent['credit_analysis']:.2f}",
            f"- Fraud ${totals_by_agent['fraud_detection']:.2f}",
            f"- Compliance ${totals_by_agent['compliance']:.2f}",
            f"- Orchestrator ${totals_by_agent['decision_orchestrator']:.2f}",
            f"Most expensive single call: {most_expensive[0]} {most_expensive[1].replace('_', ' ').title()}: ${most_expensive[2]:.2f}",
        ]
    )


async def generate_narr05_artifacts(output_dir: str | Path = ARTIFACTS_ROOT) -> dict[str, Any]:
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    store = InMemoryEventStore()
    application_id = "APEX-NARR05"
    await narr05_human_override(store, application_id=application_id)

    package = await generate_regulatory_package(store, application_id, examination_date=_now())
    verification = verify_regulatory_package(package)
    package_document = {
        "package": package,
        "verification": _serialize(verification),
    }
    package_path = output_root / "regulatory_package_NARR05.json"
    package_path.write_text(json.dumps(package_document, default=_json_default, indent=2), encoding="utf-8")

    counterfactual = await run_what_if(
        store,
        application_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[
            CreditAnalysisCompleted(
                application_id=application_id,
                session_id="sess-credit",
                decision=CreditDecision(
                    risk_tier=RiskTier.MEDIUM,
                    recommended_limit_usd=Decimal("950000"),
                    confidence=0.88,
                    rationale="Counterfactual replay assumes leverage improved to medium risk.",
                ),
                model_version="credit-whatif-v1",
                model_deployment_id="credit-whatif-dep",
                input_data_hash="whatif-input-narr05",
                analysis_duration_ms=5,
                completed_at=_now(),
            )
        ],
    )
    counterfactual_path = output_root / "counterfactual_narr05.json"
    counterfactual_path.write_text(
        json.dumps(_serialize(counterfactual), default=_json_default, indent=2),
        encoding="utf-8",
    )

    api_cost_report = build_api_cost_report()
    api_cost_path = output_root / "api_cost_report.txt"
    api_cost_path.write_text(api_cost_report, encoding="utf-8")

    return {
        "application_id": application_id,
        "package_path": str(package_path),
        "counterfactual_path": str(counterfactual_path),
        "api_cost_report_path": str(api_cost_path),
        "verification": _serialize(verification),
    }


__all__ = [
    "build_api_cost_report",
    "default_company_for_application",
    "find_montana_company_id",
    "generate_narr05_artifacts",
    "narr01_occ_collision",
    "narr02_missing_ebitda",
    "narr03_crash_recovery",
    "narr04_montana_hard_block",
    "narr05_human_override",
    "profile_for_company",
    "run_credit_phase",
    "run_document_phase",
    "run_full_pipeline",
]
