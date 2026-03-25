from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
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
from src.agents import LedgerAgentRuntime
from src.agents.llm import AgentLLMBackend
from src.agents.cost_reporting import collect_llm_costs, render_live_cost_report
from src.document_processing import DocumentPackageProcessor, persist_document_package
from src.document_processing.models import DocumentPackageAnalysis
from src.event_store import EventStore, InMemoryEventStore, OptimisticConcurrencyError
from src.integrity import reconstruct_agent_context
from src.models.events import (
    AGENT_SESSION_ANCHOR_EVENT_TYPES,
    AgentNodeExecuted,
    AgentOutputWritten,
    AgentSessionCompleted,
    AgentSessionFailed,
    AgentSessionRecovered,
    AgentToolCalled,
    AgentType,
    CreditAnalysisCompleted,
    CreditDecision,
    CreditRecordOpened,
    DocumentType,
    HumanReviewRequested,
    LoanPurpose,
    RiskTier,
)
from src.registry import ApplicantRegistryClient
from src.regulatory import generate_regulatory_package, verify_regulatory_package
from src.what_if import run_what_if


ROOT = Path(__file__).resolve().parents[2]
APPLICANT_PROFILES_PATH = ROOT / "data" / "applicant_profiles.json"
SEED_EVENTS_PATH = ROOT / "data" / "seed_events.jsonl"
DOCUMENTS_ROOT = ROOT / "documents"
ARTIFACTS_ROOT = ROOT / "artifacts"

NARRATIVE_COMPANIES = {
    "NARR-01": "COMP-031",
    "NARR-02": "COMP-044",
    "NARR-03": "COMP-057",
    "NARR-05": "COMP-068",
}

_API_COST_REPORT_CACHE: str | None = None


@dataclass(frozen=True)
class RuntimeApplicantContext:
    company_id: str
    name: str
    industry: str
    jurisdiction: str
    risk_segment: str
    trajectory: str
    compliance_flags: list[dict[str, Any]]
    loan_relationships: list[dict[str, Any]]
    source: str

    def as_profile_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "name": self.name,
            "industry": self.industry,
            "jurisdiction": self.jurisdiction,
            "risk_segment": self.risk_segment,
            "trajectory": self.trajectory,
            "compliance_flags": list(self.compliance_flags),
            "loan_relationships": list(self.loan_relationships),
            "source": self.source,
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


async def _latest_credit_limit(store: EventStore | InMemoryEventStore, application_id: str) -> Decimal | None:
    credit_events = await store.load_stream(f"credit-{application_id}")
    for event in reversed(credit_events):
        if event.event_type == "CreditAnalysisCompleted":
            decision = event.payload.get("decision", {})
            raw = decision.get("recommended_limit_usd")
            if raw is not None:
                return Decimal(str(raw))
    return None


def _scenario_session_id(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    base_session_id: str,
) -> str:
    if not isinstance(store, EventStore):
        return base_session_id
    application_slug = application_id.lower().replace("_", "-")
    if base_session_id.endswith(application_slug):
        return base_session_id
    return f"{base_session_id}-{application_slug}"


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


def _registry_client_for_store(store: EventStore | InMemoryEventStore) -> ApplicantRegistryClient | None:
    if not isinstance(store, EventStore):
        return None
    if store.pool is None:
        return None
    return ApplicantRegistryClient(store.pool)


def _seed_context(company_id: str) -> RuntimeApplicantContext:
    profile = profile_for_company(company_id)
    return RuntimeApplicantContext(
        company_id=str(profile["company_id"]),
        name=str(profile.get("name", company_id)),
        industry=str(profile.get("industry", "")),
        jurisdiction=str(profile.get("jurisdiction", "")),
        risk_segment=str(profile.get("risk_segment", "MEDIUM")),
        trajectory=str(profile.get("trajectory", "STABLE")),
        compliance_flags=list(profile.get("compliance_flags", [])),
        loan_relationships=list(profile.get("loan_relationships", [])),
        source="seed_profiles",
    )


async def _resolve_company_context(
    store: EventStore | InMemoryEventStore,
    company_id: str,
) -> RuntimeApplicantContext:
    client = _registry_client_for_store(store)
    if client is None:
        return _seed_context(company_id)

    company = await client.get_company(company_id)
    if company is None:
        return _seed_context(company_id)

    compliance_flags = [
        {
            "flag_type": flag.flag_type,
            "severity": flag.severity,
            "is_active": flag.is_active,
            "added_date": flag.added_date,
            "note": flag.note,
        }
        for flag in await client.get_compliance_flags(company_id)
    ]
    loan_relationships = await client.get_loan_relationships(company_id)
    return RuntimeApplicantContext(
        company_id=company.company_id,
        name=company.name,
        industry=company.industry,
        jurisdiction=company.jurisdiction,
        risk_segment=company.risk_segment,
        trajectory=company.trajectory,
        compliance_flags=compliance_flags,
        loan_relationships=loan_relationships,
        source="applicant_registry",
    )


async def resolve_montana_company_id(store: EventStore | InMemoryEventStore) -> str:
    client = _registry_client_for_store(store)
    if client is None:
        return find_montana_company_id()

    company = await client.find_company_by_jurisdiction("MT")
    if company is not None:
        return company.company_id
    return find_montana_company_id()


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
    llm_tokens_input: int | None = None,
    llm_tokens_output: int | None = None,
    llm_cost_usd: float | None = None,
) -> list[int]:
    event = AgentNodeExecuted(
        session_id=session_id,
        agent_type=agent_type,
        node_name=node_name,
        node_sequence=node_sequence,
        input_keys=input_keys or [],
        output_keys=output_keys or [],
        llm_called=llm_called,
        llm_tokens_input=(llm_tokens_input if llm_tokens_input is not None else 480) if llm_called else None,
        llm_tokens_output=(llm_tokens_output if llm_tokens_output is not None else 120) if llm_called else None,
        llm_cost_usd=(llm_cost_usd if llm_cost_usd is not None else 0.04) if llm_called else 0.0,
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


def _confidence_cap_from_quality(
    analysis: DocumentPackageAnalysis,
    caveats: list[str] | None = None,
) -> float | None:
    quality_caveats = caveats if caveats is not None else _quality_caveats(analysis)
    if analysis.merged_financial_facts.ebitda is None or analysis.merged_financial_facts.total_revenue is None:
        return 0.58
    if len(quality_caveats) >= 3:
        return 0.70
    if quality_caveats:
        return 0.75
    return None


def _credit_decision_from_profile(
    company_id: str,
    profile: dict[str, Any],
    analysis: DocumentPackageAnalysis,
    requested_amount_usd: Decimal,
    *,
    quality_caveats: list[str] | None = None,
) -> CreditDecision:
    risk_segment = str(profile.get("risk_segment", "MEDIUM")).upper()
    trajectory = str(profile.get("trajectory", "STABLE")).upper()
    caveats = quality_caveats if quality_caveats is not None else _quality_caveats(analysis)

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
        quality_cap = _confidence_cap_from_quality(analysis, caveats)
        if quality_cap is not None:
            confidence = min(confidence, quality_cap)
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
    profile = (await _resolve_company_context(store, company_id)).as_profile_dict()
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
    analysis: DocumentPackageAnalysis | None = None,
) -> dict[str, Any]:
    if analysis is None:
        processor = DocumentPackageProcessor(documents_root=DOCUMENTS_ROOT)
        analysis = processor.process_company(company_id, include_summaries=include_summaries)
    quality_caveats = _quality_caveats(analysis)
    stream_id = f"docpkg-{application_id}"
    existing_version = await store.stream_version(stream_id)
    positions: list[int] = []
    session_stream_id: str | None = None

    if existing_version == -1:
        session_id = _scenario_session_id(store, application_id, "sess-doc")
        session_stream_id = await _start_session(
            store,
            application_id,
            AgentType.DOCUMENT_PROCESSING,
            session_id,
            agent_id=f"doc-{company_id.lower()}",
            model_version="docproc-v1",
        )
        await _record_node(
            store,
            AgentType.DOCUMENT_PROCESSING,
            session_id,
            node_name="validate_inputs",
            node_sequence=1,
            llm_called=False,
            input_keys=["application_id", "documents"],
            output_keys=["validated_inputs", "document_paths"],
            duration_ms=180,
        )
        positions = await persist_document_package(store, analysis, application_id=application_id)
        await _record_node(
            store,
            AgentType.DOCUMENT_PROCESSING,
            session_id,
            node_name="run_extraction",
            node_sequence=2,
            llm_called=False,
            input_keys=["document_paths"],
            output_keys=["raw_facts", "structured_documents"],
            duration_ms=240,
        )
        caveat_count = sum(len(document.extraction_notes) for document in analysis.documents) + len(analysis.consistency_notes)
        await _record_node(
            store,
            AgentType.DOCUMENT_PROCESSING,
            session_id,
            node_name="assess_quality",
            node_sequence=3,
            llm_called=True,
            input_keys=["raw_facts", "company_profile"],
            output_keys=["quality_assessment", "package_summary"],
            duration_ms=520,
            llm_tokens_input=1400 + (100 * caveat_count),
            llm_tokens_output=260 + (30 * caveat_count),
            llm_cost_usd=round(0.055 + (0.009 * caveat_count), 6),
        )
        await _record_agent_output(
            store,
            application_id,
            AgentType.DOCUMENT_PROCESSING,
            session_id,
            stream_id=stream_id,
            event_type="PackageReadyForAnalysis",
            stream_position=positions[-1],
        )
        await _complete_session(
            store,
            application_id,
            AgentType.DOCUMENT_PROCESSING,
            session_id,
            next_agent_triggered="credit_analysis",
            total_nodes=3,
            total_llm_calls=1,
            total_tokens_used=1200 + (90 * caveat_count),
            total_cost_usd=round(0.075 + (0.01 * caveat_count), 4),
        )

    return {
        "application_id": application_id,
        "company_id": company_id,
        "analysis": analysis,
        "positions": positions,
        "quality_caveats": quality_caveats,
        "document_session_stream_id": session_stream_id,
    }


async def run_credit_phase(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str | None = None,
    *,
    requested_amount_usd: Decimal | None = None,
    document_analysis: DocumentPackageAnalysis | None = None,
) -> dict[str, Any]:
    company = company_id or default_company_for_application(application_id)
    doc_result = await run_document_phase(store, application_id, company, analysis=document_analysis)
    analysis = doc_result["analysis"]
    quality_caveats = list(doc_result["quality_caveats"])
    company_context = await _resolve_company_context(store, company)
    profile = company_context.as_profile_dict()

    await _submit_application(
        store,
        application_id,
        company,
        analysis,
        requested_amount_usd=requested_amount_usd,
    )
    await _ensure_credit_requested(store, application_id)

    session_id = _scenario_session_id(store, application_id, "sess-credit")
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
    decision = _credit_decision_from_profile(
        company,
        profile,
        analysis,
        requested,
        quality_caveats=quality_caveats,
    )
    credit_llm_cost = round(
        0.18
        + (0.08 if decision.risk_tier == RiskTier.HIGH else 0.04 if decision.risk_tier == RiskTier.MEDIUM else 0.02)
        + (0.03 if quality_caveats else 0.0),
        4,
    )
    await _record_node(
        store,
        AgentType.CREDIT_ANALYSIS,
        session_id,
        node_name="compose_recommendation",
        node_sequence=2,
        llm_called=True,
        input_keys=["financial_facts", "registry_profile", "quality_caveats"],
        output_keys=["credit_decision"],
        duration_ms=760,
        llm_tokens_input=2400 + (180 * len(quality_caveats)),
        llm_tokens_output=360,
        llm_cost_usd=credit_llm_cost,
    )
    positions = await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=application_id,
            session_id=session_id,
            decision=decision,
            model_version="credit-v1",
            model_deployment_id="credit-dep-v1",
            input_data_hash=f"credit-{application_id.lower()}",
            analysis_duration_ms=640,
            regulatory_basis=quality_caveats,
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
        total_nodes=2,
        total_llm_calls=1,
        total_tokens_used=2600 + (180 * len(quality_caveats)),
        total_cost_usd=credit_llm_cost,
    )
    return {
        "application_id": application_id,
        "company_id": company,
        "analysis": analysis,
        "profile": profile,
        "profile_source": company_context.source,
        "credit_session_stream_id": stream_id,
        "credit_decision": decision,
        "requested_amount_usd": requested,
        "quality_caveats": quality_caveats,
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
    session_id = _scenario_session_id(store, application_id, session_id)
    company_context = await _resolve_company_context(store, company_id)
    profile = company_context.as_profile_dict()
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
    fraud_llm_cost = round(
        0.09
        + (0.05 if profile.get("industry") in {"retail", "logistics"} else 0.02)
        + (0.04 if fraud_score >= 0.30 else 0.0),
        4,
    )
    await _record_node(
        store,
        AgentType.FRAUD_DETECTION,
        session_id,
        node_name="score_anomalies",
        node_sequence=3 if context_source.startswith("prior_session_replay:") else 2,
        llm_called=True,
        input_keys=["registry_context", "credit_output"],
        output_keys=["fraud_score", "anomaly_summary"],
        duration_ms=580,
        llm_tokens_input=1800,
        llm_tokens_output=240,
        llm_cost_usd=fraud_llm_cost,
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
        total_nodes=2 if not context_source.startswith("prior_session_replay:") else 3,
        total_llm_calls=1,
        total_tokens_used=2040,
        total_cost_usd=fraud_llm_cost,
    )
    return {
        "fraud_session_stream_id": stream_id,
        "fraud_score": fraud_score,
        "risk_level": risk_level,
        "profile_source": company_context.source,
    }


async def _run_compliance_stage(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str,
    *,
    session_id: str = "sess-compliance",
    hard_block: bool = False,
) -> dict[str, Any]:
    session_id = _scenario_session_id(store, application_id, session_id)
    company_context = await _resolve_company_context(store, company_id)
    profile = company_context.as_profile_dict()
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
        "profile_source": company_context.source,
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


async def _run_decision_and_review(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str,
    *,
    recommendation: str,
    confidence: float,
    approved_amount_usd: Decimal | None,
    review_approved_amount_usd: Decimal | None = None,
    executive_summary: str,
    key_risks: list[str],
    credit_session_stream_id: str,
    fraud_session_stream_id: str,
    compliance_session_stream_id: str,
    quality_caveats: list[str],
    review_reason: str,
    final_decision: str,
    override: bool,
    reviewer_id: str,
    override_reason: str | None = None,
    interest_rate_pct: float | None = None,
    term_months: int | None = None,
    conditions: list[str] | None = None,
    decline_reasons: list[str] | None = None,
    adverse_action_notice_required: bool = True,
    adverse_action_codes: list[str] | None = None,
    session_id: str = "sess-orch",
    agent_id: str | None = None,
    model_version: str = "orch-v1",
    duration_ms: int = 690,
    llm_tokens_input: int | None = None,
    llm_tokens_output: int = 320,
    llm_cost_usd: float | None = None,
) -> dict[str, Any]:
    session_id = _scenario_session_id(store, application_id, session_id)
    orchestrator_agent_id = agent_id or f"orch-{company_id.lower()}"
    total_input_tokens = llm_tokens_input if llm_tokens_input is not None else 2200 + (150 * len(quality_caveats))
    computed_cost = (
        llm_cost_usd
        if llm_cost_usd is not None
        else round(
            0.14
            + (0.05 if recommendation == "DECLINE" else 0.03 if recommendation == "REFER" else 0.02)
            + (0.03 if quality_caveats else 0.0),
            4,
        )
    )
    stream_id = await _start_session(
        store,
        application_id,
        AgentType.DECISION_ORCHESTRATOR,
        session_id,
        agent_id=orchestrator_agent_id,
        model_version=model_version,
    )
    await _record_node(
        store,
        AgentType.DECISION_ORCHESTRATOR,
        session_id,
        node_name="synthesize_recommendation",
        node_sequence=1,
        llm_called=True,
        input_keys=["credit_decision", "fraud_assessment", "compliance_verdict"],
        output_keys=["decision_summary", "recommendation"],
        duration_ms=duration_ms,
        llm_tokens_input=total_input_tokens,
        llm_tokens_output=llm_tokens_output,
        llm_cost_usd=computed_cost,
    )
    positions = await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=application_id,
            session_id=session_id,
            recommendation=recommendation,
            confidence=confidence,
            approved_amount_usd=approved_amount_usd,
            conditions=[],
            executive_summary=executive_summary,
            key_risks=key_risks,
            contributing_sessions=[
                credit_session_stream_id,
                fraud_session_stream_id,
                compliance_session_stream_id,
            ],
            model_versions={"decision_orchestrator": model_version},
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
        total_nodes=1,
        total_llm_calls=1,
        total_tokens_used=total_input_tokens + llm_tokens_output,
        total_cost_usd=computed_cost,
    )

    await _request_human_review(store, application_id, reason=review_reason)
    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=application_id,
            reviewer_id=reviewer_id,
            override=override,
            override_reason=override_reason,
            original_recommendation=recommendation,
            final_decision=final_decision,
            approved_amount_usd=review_approved_amount_usd if review_approved_amount_usd is not None else (approved_amount_usd or Decimal("0")),
            interest_rate_pct=interest_rate_pct,
            term_months=term_months,
            conditions=conditions or [],
            decline_reasons=decline_reasons or [],
            adverse_action_notice_required=adverse_action_notice_required,
            adverse_action_codes=adverse_action_codes or [],
        ),
        store,
    )
    return {
        "decision_session_stream_id": stream_id,
        "recommendation": recommendation,
        "final_decision": final_decision,
    }


async def run_full_pipeline(
    store: EventStore | InMemoryEventStore,
    application_id: str,
    company_id: str | None = None,
    *,
    document_analysis: DocumentPackageAnalysis | None = None,
) -> dict[str, Any]:
    company = company_id or default_company_for_application(application_id)
    credit_result = await run_credit_phase(store, application_id, company, document_analysis=document_analysis)
    fraud_result = await _run_fraud_stage(store, application_id, company, fraud_score=0.11)
    compliance_result = await _run_compliance_stage(store, application_id, company, hard_block=False)

    recommendation, confidence, approved_amount, key_risks, summary = _recommended_decision(
        credit_result["credit_decision"],
        quality_caveats=credit_result["quality_caveats"],
        fraud_score=0.11,
    )

    final_decision = "APPROVE" if recommendation != "DECLINE" else "DECLINE"
    await _run_decision_and_review(
        store,
        application_id,
        company,
        recommendation=recommendation,
        confidence=confidence,
        approved_amount_usd=approved_amount,
        executive_summary=summary,
        key_risks=key_risks,
        credit_session_stream_id=credit_result["credit_session_stream_id"],
        fraud_session_stream_id=fraud_result["fraud_session_stream_id"],
        compliance_session_stream_id=compliance_result["compliance_session_stream_id"],
        quality_caveats=credit_result["quality_caveats"],
        review_reason="Standard post-decision adjudication.",
        final_decision=final_decision,
        override=False,
        reviewer_id="loan-ops",
        interest_rate_pct=8.75 if final_decision == "APPROVE" else None,
        term_months=36 if final_decision == "APPROVE" else None,
        conditions=["Quarterly covenant certificate"] if final_decision == "APPROVE" else [],
        decline_reasons=["Automated decline confirmed"] if final_decision == "DECLINE" else [],
        adverse_action_codes=["AUTO-DECLINE"] if final_decision == "DECLINE" else [],
    )

    loan_events = await store.load_stream(f"loan-{application_id}")
    return {
        "application_id": application_id,
        "company_id": company,
        "profile_source": credit_result["profile_source"],
        "final_event_type": loan_events[-1].event_type if loan_events else None,
        "credit_risk_tier": credit_result["credit_decision"].risk_tier.value,
        "quality_caveats": credit_result["quality_caveats"],
    }


async def narr01_occ_collision(
    store: EventStore | InMemoryEventStore,
    application_id: str = "APEX-NARR01",
    company_id: str = "COMP-031",
) -> dict[str, Any]:
    document_result = await run_document_phase(store, application_id, company_id)
    analysis = document_result["analysis"]
    await _submit_application(store, application_id, company_id, analysis)
    await _ensure_credit_requested(store, application_id)

    company_context = await _resolve_company_context(store, company_id)
    requested_amount = _requested_amount_from_analysis(analysis, company_id)
    decision_template = _credit_decision_from_profile(
        company_id,
        company_context.as_profile_dict(),
        analysis,
        requested_amount,
    )

    credit_stream_id = f"credit-{application_id}"
    await store.append(
        credit_stream_id,
        [
            CreditRecordOpened(
                application_id=application_id,
                applicant_id=company_id,
                opened_at=_now(),
            ).to_store_dict()
        ],
        expected_version=-1,
    )
    initial_version = await store.stream_version(credit_stream_id)

    async def _resolve_causation_id(causation_id: str | None) -> bool:
        if not causation_id or ":" not in causation_id:
            return False
        stream_ref, _, position_text = causation_id.rpartition(":")
        try:
            position = int(position_text)
        except ValueError:
            return False
        events = await store.load_stream(stream_ref, from_position=position, to_position=position, apply_upcasters=False)
        return bool(events)

    async def _credit_agent(marker: str) -> dict[str, Any]:
        session_id = _scenario_session_id(store, application_id, f"sess-credit-{marker.lower()}")
        agent_stream_id = await _start_session(
            store,
            application_id,
            AgentType.CREDIT_ANALYSIS,
            session_id,
            agent_id=f"credit-{company_id.lower()}-{marker.lower()}",
            model_version="credit-v1",
        )
        await _record_node(
            store,
            AgentType.CREDIT_ANALYSIS,
            session_id,
            node_name="load_facts",
            node_sequence=1,
            llm_called=False,
            input_keys=["docpkg", "credit_stream"],
            output_keys=["financial_facts"],
        )
        await _record_tool(
            store,
            AgentType.CREDIT_ANALYSIS,
            session_id,
            tool_name="registry.lookup",
            tool_input_summary=company_id,
            tool_output_summary=f"industry={company_context.industry}; trajectory={company_context.trajectory}",
        )
        compose_positions = await _record_node(
            store,
            AgentType.CREDIT_ANALYSIS,
            session_id,
            node_name="compose_recommendation",
            node_sequence=2,
            llm_called=True,
            input_keys=["financial_facts", "registry_profile"],
            output_keys=["credit_decision"],
            duration_ms=720,
            llm_tokens_input=2200,
            llm_tokens_output=320,
            llm_cost_usd=0.21,
        )
        decision = CreditDecision(
            risk_tier=decision_template.risk_tier,
            recommended_limit_usd=decision_template.recommended_limit_usd,
            confidence=decision_template.confidence,
            rationale=f"{decision_template.rationale} Concurrent agent {marker} completed underwriting.",
        )
        credit_event = CreditAnalysisCompleted(
            application_id=application_id,
            session_id=session_id,
            decision=decision,
            model_version="credit-v1",
            model_deployment_id="credit-dep-v1",
            input_data_hash=f"credit-occ-{application_id.lower()}-{marker.lower()}",
            analysis_duration_ms=640,
            regulatory_basis=_quality_caveats(analysis),
            completed_at=_now(),
        ).to_store_dict()
        causation_id = f"{agent_stream_id}:{compose_positions[-1]}"

        optimistic_concurrency_failure = False
        retry_version: int | None = None
        retry_confirmed = False
        try:
            positions = await store.append(
                credit_stream_id,
                [credit_event],
                expected_version=initial_version,
                causation_id=causation_id,
            )
        except OptimisticConcurrencyError:
            optimistic_concurrency_failure = True
            reload_positions = await _record_node(
                store,
                AgentType.CREDIT_ANALYSIS,
                session_id,
                node_name="reload_after_occ",
                node_sequence=3,
                llm_called=False,
                input_keys=["credit_stream"],
                output_keys=["credit_events"],
                duration_ms=140,
            )
            causation_id = f"{agent_stream_id}:{reload_positions[-1]}"
            current_credit_events = await store.load_stream(credit_stream_id, apply_upcasters=False)
            retry_version = await store.stream_version(credit_stream_id)
            retry_confirmed = any(
                event.event_type == "CreditAnalysisCompleted"
                and event.payload.get("application_id") == application_id
                for event in current_credit_events
            )
            positions = await store.append(
                credit_stream_id,
                [credit_event],
                expected_version=retry_version,
                causation_id=causation_id,
            )

        await _record_agent_output(
            store,
            application_id,
            AgentType.CREDIT_ANALYSIS,
            session_id,
            stream_id=credit_stream_id,
            event_type="CreditAnalysisCompleted",
            stream_position=positions[-1],
        )
        await _complete_session(
            store,
            application_id,
            AgentType.CREDIT_ANALYSIS,
            session_id,
            next_agent_triggered=None,
            total_nodes=3 if optimistic_concurrency_failure else 2,
            total_llm_calls=1,
            total_tokens_used=2520,
            total_cost_usd=0.21,
        )
        session_events = await store.load_stream(agent_stream_id, apply_upcasters=False)
        return {
            "session_id": session_id,
            "agent_stream_id": agent_stream_id,
            "marker": marker,
            "read_version": initial_version,
            "optimistic_concurrency_failure": optimistic_concurrency_failure,
            "retry_version": retry_version,
            "retry_confirmed_same_application": retry_confirmed,
            "credit_stream_position": positions[-1],
            "session_events": [_serialize(event.model_dump(mode="json")) for event in session_events],
        }

    agent_results = await asyncio.gather(_credit_agent("A"), _credit_agent("B"))
    credit_events = await store.load_stream(credit_stream_id, apply_upcasters=False)
    credit_record_opened = next(event for event in credit_events if event.event_type == "CreditRecordOpened")
    credit_completed_events = [event for event in credit_events if event.event_type == "CreditAnalysisCompleted"]
    second_event_causation_id = (
        str(credit_completed_events[1].metadata.get("causation_id"))
        if len(credit_completed_events) >= 2 and credit_completed_events[1].metadata.get("causation_id") is not None
        else None
    )
    return {
        "application_id": application_id,
        "company_id": company_id,
        "stream_id": credit_stream_id,
        "initial_credit_version": initial_version,
        "successful_appends": len(credit_completed_events),
        "optimistic_concurrency_failures": sum(
            1 for result in agent_results if result["optimistic_concurrency_failure"]
        ),
        "credit_analysis_completed_count": len(credit_completed_events),
        "credit_record_opened_position": credit_record_opened.stream_position,
        "credit_stream_positions": [event.stream_position for event in credit_completed_events],
        "credit_completion_offsets_from_open": [
            event.stream_position - credit_record_opened.stream_position for event in credit_completed_events
        ],
        "credit_events": [_serialize(event.model_dump(mode="json")) for event in credit_events],
        "agent_results": agent_results,
        "agent_failures_logged": sum(
            1
            for result in agent_results
            for event in result["session_events"]
            if event["event_type"] == "AgentSessionFailed"
        ),
        "second_event_causation_id": second_event_causation_id,
        "second_event_causation_id_resolvable": await _resolve_causation_id(second_event_causation_id),
    }


async def narr02_missing_ebitda(
    store: EventStore | InMemoryEventStore,
    application_id: str = "APEX-NARR02",
    company_id: str = "COMP-044",
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

    crashed_session_id = _scenario_session_id(store, application_id, "sess-fraud-crashed")
    recovered_session_id = _scenario_session_id(store, application_id, "sess-fraud-recovered")
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
    company = company_id or await resolve_montana_company_id(store)
    credit_result = await run_credit_phase(store, application_id, company)
    await _run_fraud_stage(store, application_id, company, fraud_score=0.12)
    await _run_compliance_stage(store, application_id, company, hard_block=True)

    compliance_events = await store.load_stream(f"compliance-{application_id}")
    loan_events = await store.load_stream(f"loan-{application_id}")
    return {
        "application_id": application_id,
        "company_id": company,
        "profile_source": credit_result["profile_source"],
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
    credit_session_id = _scenario_session_id(store, application_id, "sess-credit")
    fraud_session_id = _scenario_session_id(store, application_id, "sess-fraud")
    compliance_session_id = _scenario_session_id(store, application_id, "sess-compliance")
    orchestrator_session_id = _scenario_session_id(store, application_id, "sess-orch")
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
        credit_session_id,
        agent_id="credit-agent-068",
        model_version="credit-v1",
    )
    await _record_node(
        store,
        AgentType.CREDIT_ANALYSIS,
        credit_session_id,
        node_name="load_facts",
        node_sequence=1,
        llm_called=False,
        input_keys=["docpkg"],
        output_keys=["financial_facts"],
    )
    await _record_node(
        store,
        AgentType.CREDIT_ANALYSIS,
        credit_session_id,
        node_name="compose_recommendation",
        node_sequence=2,
        llm_called=True,
        input_keys=["financial_facts", "customer_history"],
        output_keys=["credit_decision"],
        duration_ms=820,
        llm_tokens_input=2600,
        llm_tokens_output=340,
        llm_cost_usd=0.31,
    )
    credit_positions = await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=application_id,
            session_id=credit_session_id,
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
        credit_session_id,
        stream_id=f"credit-{application_id}",
        event_type="CreditAnalysisCompleted",
        stream_position=credit_positions[f"credit-{application_id}"][-1],
    )
    await _complete_session(
        store,
        application_id,
        AgentType.CREDIT_ANALYSIS,
        credit_session_id,
        next_agent_triggered="fraud_detection",
        total_nodes=2,
        total_llm_calls=1,
        total_tokens_used=2940,
        total_cost_usd=0.31,
    )

    await _run_fraud_stage(store, application_id, company_id, fraud_score=0.08, session_id=fraud_session_id)
    await _run_compliance_stage(store, application_id, company_id, hard_block=False, session_id=compliance_session_id)
    await _run_decision_and_review(
        store,
        application_id,
        company_id,
        recommendation="DECLINE",
        confidence=0.82,
        approved_amount_usd=None,
        review_approved_amount_usd=Decimal("750000"),
        executive_summary="Automated decline recommendation due to high credit risk despite clean compliance.",
        key_risks=["high leverage", "declining revenue trajectory"],
        credit_session_stream_id=f"agent-credit_analysis-{credit_session_id}",
        fraud_session_stream_id=f"agent-fraud_detection-{fraud_session_id}",
        compliance_session_stream_id=f"agent-compliance-{compliance_session_id}",
        quality_caveats=[],
        review_reason="Relationship manager requested manual adjudication.",
        final_decision="APPROVE",
        override=True,
        reviewer_id="LO-Sarah-Chen",
        override_reason="15-year customer, prior repayment history, collateral offered",
        interest_rate_pct=9.10,
        term_months=36,
        conditions=[
            "Monthly revenue reporting for 12 months",
            "Personal guarantee from CEO",
        ],
        session_id=orchestrator_session_id,
        agent_id="orch-agent-068",
        duration_ms=710,
        llm_tokens_input=2350,
        llm_tokens_output=300,
        llm_cost_usd=0.22,
    )

    loan_events = await store.load_stream(f"loan-{application_id}")
    company_context = await _resolve_company_context(store, company_id)
    return {
        "application_id": application_id,
        "company_id": company_id,
        "profile_source": company_context.source,
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


def _analysis_cache(
    company_id: str,
    cache: dict[str, DocumentPackageAnalysis],
    *,
    processor: DocumentPackageProcessor,
) -> DocumentPackageAnalysis:
    if company_id not in cache:
        cache[company_id] = processor.process_company(company_id, include_summaries=False)
    return cache[company_id]


def _event_to_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    raise TypeError(f"Unsupported event value: {type(event)!r}")


def _collect_cost_metrics_from_event_dicts(events: list[dict[str, Any]]) -> dict[str, Any]:
    session_context: dict[str, dict[str, str]] = {}
    totals_by_agent = {
        "document_processing": 0.0,
        "credit_analysis": 0.0,
        "fraud_detection": 0.0,
        "compliance": 0.0,
        "decision_orchestrator": 0.0,
    }
    totals_by_application: dict[str, float] = {}
    most_expensive: tuple[str, str, float, int] = ("", "", 0.0, 0)

    for event in events:
        event_type = event.get("event_type")
        stream_id = str(event.get("stream_id", ""))
        payload = event.get("payload", {})
        if event_type in AGENT_SESSION_ANCHOR_EVENT_TYPES:
            session_context[stream_id] = {
                "application_id": str(payload.get("application_id", "")),
                "agent_type": str(payload.get("agent_type", "")),
            }
        elif event_type == "AgentSessionCompleted":
            context = session_context.setdefault(
                stream_id,
                {
                    "application_id": str(payload.get("application_id", "")),
                    "agent_type": str(payload.get("agent_type", "")),
                },
            )
            application_id = context["application_id"]
            agent_type = context["agent_type"]
            total_cost = float(payload.get("total_cost_usd") or 0.0)
            if agent_type in totals_by_agent:
                totals_by_agent[agent_type] += total_cost
            totals_by_application[application_id] = totals_by_application.get(application_id, 0.0) + total_cost
        elif event_type == "AgentNodeExecuted" and payload.get("llm_called"):
            context = session_context.get(
                stream_id,
                {
                    "application_id": "",
                    "agent_type": str(payload.get("agent_type", "")),
                },
            )
            llm_cost = float(payload.get("llm_cost_usd") or 0.0)
            if llm_cost > most_expensive[2]:
                most_expensive = (
                    context.get("application_id", ""),
                    context.get("agent_type", ""),
                    llm_cost,
                    int(payload.get("llm_tokens_input") or 0),
                )

    return {
        "totals_by_agent": {agent: round(cost, 2) for agent, cost in totals_by_agent.items()},
        "totals_by_application": {app: round(cost, 2) for app, cost in totals_by_application.items()},
        "most_expensive_call": most_expensive,
    }


async def _load_seed_cost_metrics() -> dict[str, Any]:
    seed_events: list[dict[str, Any]] = []
    analysis_cache: dict[str, DocumentPackageAnalysis] = {}
    processor = DocumentPackageProcessor(documents_root=DOCUMENTS_ROOT, prefer_docling=False)
    for row in _seed_application_rows():
        store = InMemoryEventStore()
        analysis = _analysis_cache(str(row["company_id"]), analysis_cache, processor=processor)
        await run_full_pipeline(
            store,
            application_id=str(row["application_id"]),
            company_id=str(row["company_id"]),
            document_analysis=analysis,
        )
        async for event in store.load_all(from_position=0, apply_upcasters=False):
            seed_events.append(_event_to_dict(event))
    return _collect_cost_metrics_from_event_dicts(seed_events)


async def _load_narrative_cost_metrics() -> dict[str, Any]:
    narrative_events: list[dict[str, Any]] = []
    narrative_app_ids = [
        "APEX-NARR01",
        "APEX-NARR02",
        "APEX-NARR03",
        "APEX-NARR04",
        "APEX-NARR05",
    ]
    scenarios = [
        lambda store: narr01_occ_collision(store, application_id=narrative_app_ids[0]),
        lambda store: narr02_missing_ebitda(store, application_id=narrative_app_ids[1], company_id=NARRATIVE_COMPANIES["NARR-02"]),
        lambda store: narr03_crash_recovery(store, application_id=narrative_app_ids[2], company_id=NARRATIVE_COMPANIES["NARR-03"]),
        lambda store: narr04_montana_hard_block(store, application_id=narrative_app_ids[3], company_id=find_montana_company_id()),
        lambda store: narr05_human_override(store, application_id=narrative_app_ids[4], company_id=NARRATIVE_COMPANIES["NARR-05"]),
    ]
    for runner in scenarios:
        store = InMemoryEventStore()
        await runner(store)
        async for event in store.load_all(from_position=0, apply_upcasters=False):
            narrative_events.append(_event_to_dict(event))
    metrics = _collect_cost_metrics_from_event_dicts(narrative_events)
    for application_id in narrative_app_ids:
        metrics["totals_by_application"].setdefault(application_id, 0.0)
    return metrics


async def build_api_cost_report() -> str:
    global _API_COST_REPORT_CACHE
    if _API_COST_REPORT_CACHE is not None:
        return _API_COST_REPORT_CACHE

    application_rows = _seed_application_rows()
    seed_metrics = await _load_seed_cost_metrics()
    narrative_metrics = await _load_narrative_cost_metrics()
    for row in application_rows:
        seed_metrics["totals_by_application"].setdefault(str(row["application_id"]), 0.0)

    totals_by_agent = {
        agent: round(seed_metrics["totals_by_agent"].get(agent, 0.0) + narrative_metrics["totals_by_agent"].get(agent, 0.0), 2)
        for agent in {"document_processing", "credit_analysis", "fraud_detection", "compliance", "decision_orchestrator"}
    }
    total_per_application = {
        **seed_metrics["totals_by_application"],
        **narrative_metrics["totals_by_application"],
    }
    all_costs = list(total_per_application.values())
    grand_total = round(sum(all_costs), 2)
    avg_cost = round(grand_total / len(all_costs), 2)
    min_cost = min(all_costs)
    max_cost = max(all_costs)

    seed_max = seed_metrics["most_expensive_call"]
    narrative_max = narrative_metrics["most_expensive_call"]
    most_expensive = seed_max if seed_max[2] >= narrative_max[2] else narrative_max
    narrative_count = 5
    report = "\n".join(
        [
            "Sentinel Cost Attribution Report",
            f"Generated at: {_now().isoformat()}",
            "Billable API Cost Status: honest external billing measurement is $0.00 for the reproduced local workflow in this repository.",
            "Reason: the standard seeded pipeline, narrative scenarios, and committed artifacts do not call a paid external model provider.",
            "Optional Ollama usage is local inference and is not treated here as external billable API spend.",
            "",
            "Telemetry Attribution Methodology:",
            "Seed applications are replayed through the in-repo demo pipeline to produce session telemetry.",
            "Narrative applications use their dedicated scenario runners and their emitted session telemetry.",
            "The telemetry numbers below are workflow cost proxies derived from stored agent-session events.",
            f"Applications processed: {len(application_rows)} seed + {narrative_count} narrative = {len(all_costs)} total",
            "",
            "Actual measured external API cost:",
            "- total $0.00",
            "- average per application $0.00",
            "- provider calls observed in reproduced workflow: 0",
            "",
            "Workflow telemetry cost proxy:",
            f"- total for all 34 applications: ${grand_total:.2f}",
            f"- average per application: avg ${avg_cost:.2f}, range ${min_cost:.2f}-${max_cost:.2f}",
            "Telemetry by agent:",
            f"- DocProc ${totals_by_agent['document_processing']:.2f}",
            f"- Credit ${totals_by_agent['credit_analysis']:.2f}",
            f"- Fraud ${totals_by_agent['fraud_detection']:.2f}",
            f"- Compliance ${totals_by_agent['compliance']:.2f}",
            f"- Orchestrator ${totals_by_agent['decision_orchestrator']:.2f}",
            f"Most expensive single telemetry call: {most_expensive[0]} {most_expensive[1].replace('_', ' ').title()}: ${most_expensive[2]:.2f} ({most_expensive[3]} input tokens)",
        ]
    )
    _API_COST_REPORT_CACHE = report
    return report


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

    api_cost_report = await build_api_cost_report()
    api_cost_path = output_root / "api_cost_report.txt"
    api_cost_path.write_text(api_cost_report, encoding="utf-8")

    return {
        "application_id": application_id,
        "package_path": str(package_path),
        "counterfactual_path": str(counterfactual_path),
        "api_cost_report_path": str(api_cost_path),
        "verification": _serialize(verification),
    }


async def generate_runtime_narr05_artifacts(
    store: EventStore | InMemoryEventStore,
    output_dir: str | Path = ARTIFACTS_ROOT,
    *,
    application_id: str,
    company_id: str = NARRATIVE_COMPANIES["NARR-05"],
    requested_amount_usd: Decimal = Decimal("950000"),
    target_approved_amount_usd: Decimal = Decimal("750000"),
    reviewer_id: str = "LO-Sarah-Chen",
    override_reason: str = "15-year customer, prior repayment history, collateral offered",
    interest_rate_pct: float = 9.10,
    term_months: int = 36,
    conditions: list[str] | None = None,
    llm_backend: AgentLLMBackend | None = None,
) -> dict[str, Any]:
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    runtime = LedgerAgentRuntime(store, llm_backend=llm_backend)
    conditions = conditions or [
        "Monthly revenue reporting for 12 months",
        "Personal guarantee from CEO",
    ]

    workflow = await runtime.start_application(
        application_id,
        company_id,
        phase="full",
        requested_amount_usd=requested_amount_usd,
        auto_finalize_human_review=False,
        reviewer_id=reviewer_id,
    )

    latest_credit_limit = await _latest_credit_limit(store, application_id)
    approved_amount_usd = min(target_approved_amount_usd, latest_credit_limit or target_approved_amount_usd)
    review = await runtime.complete_human_review(
        application_id,
        reviewer_id=reviewer_id,
        final_decision="APPROVE",
        override=True,
        override_reason=override_reason,
        approved_amount_usd=approved_amount_usd,
        interest_rate_pct=interest_rate_pct,
        term_months=term_months,
        conditions=conditions,
        decline_reasons=[],
        adverse_action_codes=[],
    )

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
                session_id="whatif-credit-session",
                decision=CreditDecision(
                    risk_tier=RiskTier.MEDIUM,
                    recommended_limit_usd=max(target_approved_amount_usd, approved_amount_usd),
                    confidence=0.88,
                    rationale="Counterfactual replay assumes relationship and collateral support a medium-risk credit posture.",
                ),
                model_version="credit-whatif-v1",
                model_deployment_id="credit-whatif-dep",
                input_data_hash="whatif-input-narr05-live",
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

    cost_summary = await collect_llm_costs(store, application_ids=[application_id])
    api_cost_report = render_live_cost_report(cost_summary)
    api_cost_path = output_root / "api_cost_report.txt"
    api_cost_path.write_text(api_cost_report, encoding="utf-8")

    return {
        "application_id": application_id,
        "company_id": company_id,
        "workflow": _serialize(workflow),
        "review": _serialize(review),
        "requested_amount_usd": str(requested_amount_usd),
        "target_approved_amount_usd": str(target_approved_amount_usd),
        "approved_amount_usd": str(approved_amount_usd),
        "approval_capped_by_credit_limit": approved_amount_usd != target_approved_amount_usd,
        "latest_credit_limit_usd": str(latest_credit_limit) if latest_credit_limit is not None else None,
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
    "generate_runtime_narr05_artifacts",
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
