from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, NotRequired, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from src.aggregates import LoanApplicationAggregate
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
from src.event_store import EventStore, InMemoryEventStore
from src.integrity import run_integrity_check
from src.models.events import (
    AgentInputValidated,
    AgentInputValidationFailed,
    AgentNodeExecuted,
    AgentOutputWritten,
    AgentSessionCompleted,
    AgentSessionFailed,
    AgentToolCalled,
    AgentType,
    CreditAnalysisDeferred,
    CreditDecision,
    CreditRecordOpened,
    DocumentType,
    ExtractedFactsConsumed,
    FraudAnomaly,
    FraudAnomalyDetected,
    FraudAnomalyType,
    FraudScreeningInitiated,
    HistoricalProfileConsumed,
    HumanReviewRequested,
    LoanPurpose,
    RiskTier,
)
from src.projections import AgentPerformanceProjection, ApplicationSummaryProjection, ComplianceAuditProjection, ProjectionDaemon
from src.registry import ApplicantRegistryClient, FinancialYear

from .llm import AgentLLMBackend, AgentLLMResult, build_llm_backend


StoreType = EventStore | InMemoryEventStore

ROOT = Path(__file__).resolve().parents[2]
APPLICANT_PROFILES_PATH = ROOT / "data" / "applicant_profiles.json"
DOCUMENTS_ROOT = ROOT / "documents"

NARRATIVE_COMPANIES = {
    "NARR-01": "COMP-031",
    "NARR-02": "COMP-001",
    "NARR-03": "COMP-057",
    "NARR-05": "COMP-068",
}


@dataclass(frozen=True)
class RuntimeApplicantContext:
    company_id: str
    name: str
    industry: str
    naics: str
    jurisdiction: str
    legal_type: str
    founded_year: int | None
    employee_count: int | None
    risk_segment: str
    trajectory: str
    submission_channel: str
    ip_region: str
    compliance_flags: list[dict[str, Any]]
    loan_relationships: list[dict[str, Any]]
    financial_history: list[dict[str, Any]]
    source: str

    def as_profile_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompanyCatalogItem:
    company_id: str
    name: str
    industry: str
    jurisdiction: str
    legal_type: str
    risk_segment: str
    trajectory: str
    document_count: int
    has_complete_package: bool


class DocumentAgentState(TypedDict):
    application_id: str
    company_id: str
    session_id: str
    session_stream_id: str
    include_summaries: bool
    analysis: NotRequired[DocumentPackageAnalysis]
    positions: NotRequired[list[int]]
    quality_caveats: NotRequired[list[str]]
    validation_failed: NotRequired[bool]
    validation_errors: NotRequired[list[str]]
    llm_result: NotRequired[AgentLLMResult]


class CreditAgentState(TypedDict):
    application_id: str
    company_id: str
    session_id: str
    session_stream_id: str
    analysis: DocumentPackageAnalysis
    requested_amount_usd: Decimal
    company_context: NotRequired[RuntimeApplicantContext]
    quality_caveats: NotRequired[list[str]]
    credit_decision: NotRequired[CreditDecision]
    deferred: NotRequired[bool]
    defer_reason: NotRequired[str]
    llm_result: NotRequired[AgentLLMResult]
    positions: NotRequired[dict[str, list[int]]]


class FraudAgentState(TypedDict):
    application_id: str
    company_id: str
    session_id: str
    session_stream_id: str
    analysis: DocumentPackageAnalysis
    company_context: RuntimeApplicantContext
    credit_decision: CreditDecision
    anomalies: NotRequired[list[FraudAnomaly]]
    fraud_score: NotRequired[float]
    risk_level: NotRequired[str]
    recommendation: NotRequired[str]
    llm_result: NotRequired[AgentLLMResult]
    positions: NotRequired[dict[str, list[int]]]


class ComplianceAgentState(TypedDict):
    application_id: str
    company_id: str
    session_id: str
    session_stream_id: str
    company_context: RuntimeApplicantContext
    rule_results: NotRequired[list[ComplianceRuleEvaluation]]
    hard_block: NotRequired[bool]
    positions: NotRequired[dict[str, list[int]]]


class DecisionAgentState(TypedDict):
    application_id: str
    company_id: str
    session_id: str
    session_stream_id: str
    company_context: RuntimeApplicantContext
    quality_caveats: list[str]
    credit_decision: CreditDecision
    fraud_score: float
    credit_session_stream_id: str
    fraud_session_stream_id: str
    compliance_session_stream_id: str
    recommendation: NotRequired[str]
    confidence: NotRequired[float]
    approved_amount_usd: NotRequired[Decimal | None]
    key_risks: NotRequired[list[str]]
    executive_summary: NotRequired[str]
    llm_result: NotRequired[AgentLLMResult]
    positions: NotRequired[dict[str, list[int]]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(json.dumps(_serialize(payload), sort_keys=True).encode("utf-8")).hexdigest()


def _load_profiles() -> list[dict[str, Any]]:
    return json.loads(APPLICANT_PROFILES_PATH.read_text(encoding="utf-8"))


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
    raise LookupError(f"Could not infer company for application_id={application_id}")


def build_client_application_id(company_id: str) -> str:
    return f"APEX-CLIENT-{company_id.split('-')[-1]}-{uuid4().hex[:6].upper()}"


def list_document_companies() -> list[CompanyCatalogItem]:
    companies: list[CompanyCatalogItem] = []
    profiles = {str(item["company_id"]): item for item in _load_profiles()}
    for directory in sorted(DOCUMENTS_ROOT.iterdir()):
        if not directory.is_dir():
            continue
        profile = profiles.get(directory.name)
        if not profile:
            continue
        file_names = {entry.name for entry in directory.iterdir() if entry.is_file()}
        companies.append(
            CompanyCatalogItem(
                company_id=directory.name,
                name=str(profile.get("name", directory.name)),
                industry=str(profile.get("industry", "")),
                jurisdiction=str(profile.get("jurisdiction", "")),
                legal_type=str(profile.get("legal_type", "")),
                risk_segment=str(profile.get("risk_segment", "MEDIUM")),
                trajectory=str(profile.get("trajectory", "STABLE")),
                document_count=len(file_names),
                has_complete_package={
                    "application_proposal.pdf",
                    "income_statement_2024.pdf",
                    "balance_sheet_2024.pdf",
                    "financial_statements.xlsx",
                    "financial_summary.csv",
                }.issubset(file_names),
            )
        )
    return companies


def _registry_client_for_store(store: StoreType) -> ApplicantRegistryClient | None:
    if not isinstance(store, EventStore):
        return None
    if store.pool is None:
        return None
    return ApplicantRegistryClient(store.pool)


def _seed_context(company_id: str) -> RuntimeApplicantContext:
    for profile in _load_profiles():
        if str(profile["company_id"]) == company_id:
            return RuntimeApplicantContext(
                company_id=company_id,
                name=str(profile.get("name", company_id)),
                industry=str(profile.get("industry", "")),
                naics=str(profile.get("naics", "")),
                jurisdiction=str(profile.get("jurisdiction", "")),
                legal_type=str(profile.get("legal_type", "")),
                founded_year=int(profile["founded_year"]) if profile.get("founded_year") is not None else None,
                employee_count=int(profile["employee_count"]) if profile.get("employee_count") is not None else None,
                risk_segment=str(profile.get("risk_segment", "MEDIUM")),
                trajectory=str(profile.get("trajectory", "STABLE")),
                submission_channel=str(profile.get("submission_channel", "seed")),
                ip_region=str(profile.get("ip_region", "")),
                compliance_flags=list(profile.get("compliance_flags", [])),
                loan_relationships=list(profile.get("loan_relationships", [])),
                financial_history=list(profile.get("financial_history", [])),
                source="seed_profiles",
            )
    raise KeyError(f"Unknown company_id: {company_id}")


def _serialize_financial_history(rows: list[FinancialYear]) -> list[dict[str, Any]]:
    return [
        {
            "fiscal_year": row.fiscal_year,
            "total_revenue": row.total_revenue,
            "ebitda": row.ebitda,
            "net_income": row.net_income,
            "total_assets": row.total_assets,
            "total_liabilities": row.total_liabilities,
            "total_equity": row.total_equity,
            "current_ratio": row.current_ratio,
            "debt_to_ebitda": row.debt_to_ebitda,
        }
        for row in rows
    ]


async def _resolve_company_context(store: StoreType, company_id: str) -> RuntimeApplicantContext:
    client = _registry_client_for_store(store)
    if client is None:
        return _seed_context(company_id)

    company = await client.get_company(company_id)
    if company is None:
        return _seed_context(company_id)

    flags = [
        {
            "flag_type": flag.flag_type,
            "severity": flag.severity,
            "is_active": flag.is_active,
            "added_date": flag.added_date,
            "note": flag.note,
        }
        for flag in await client.get_compliance_flags(company_id)
    ]
    financial_history = _serialize_financial_history(await client.get_financial_history(company_id))
    relationships = await client.get_loan_relationships(company_id)
    return RuntimeApplicantContext(
        company_id=company.company_id,
        name=company.name,
        industry=company.industry,
        naics=company.naics,
        jurisdiction=company.jurisdiction,
        legal_type=company.legal_type,
        founded_year=company.founded_year,
        employee_count=company.employee_count,
        risk_segment=company.risk_segment,
        trajectory=company.trajectory,
        submission_channel=company.submission_channel,
        ip_region=company.ip_region,
        compliance_flags=flags,
        loan_relationships=relationships,
        financial_history=financial_history,
        source="applicant_registry",
    )


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


def _base_confidence_from_quality(analysis: DocumentPackageAnalysis) -> float:
    caveats = _quality_caveats(analysis)
    if analysis.merged_financial_facts.ebitda is None or analysis.merged_financial_facts.total_revenue is None:
        return 0.58
    if len(caveats) >= 3:
        return 0.64
    if caveats:
        return 0.72
    return 0.86


def _build_credit_decision(
    company_context: RuntimeApplicantContext,
    analysis: DocumentPackageAnalysis,
    requested_amount_usd: Decimal,
) -> CreditDecision:
    facts = analysis.merged_financial_facts
    leverage = facts.debt_to_ebitda
    current_ratio = facts.current_ratio
    defaults = any(bool(item.get("default_occurred")) for item in company_context.loan_relationships)
    quality_confidence = _base_confidence_from_quality(analysis)
    risk_segment = company_context.risk_segment.upper()
    trajectory = company_context.trajectory.upper()

    if defaults or risk_segment == "HIGH" or trajectory == "DECLINING" or (leverage is not None and leverage > 6.5):
        risk_tier = RiskTier.HIGH
        limit_factor = Decimal("0.72")
    elif trajectory == "GROWTH" and (current_ratio is None or current_ratio >= 1.35):
        risk_tier = RiskTier.LOW
        limit_factor = Decimal("1.00")
    else:
        risk_tier = RiskTier.MEDIUM
        limit_factor = Decimal("0.90")

    if facts.ebitda is not None and facts.ebitda > 0:
        ebitda_based_limit = abs(Decimal(str(facts.ebitda))) * Decimal("3.5")
        recommended_limit = min((requested_amount_usd * limit_factor).quantize(Decimal("1")), ebitda_based_limit.quantize(Decimal("1")))
    else:
        recommended_limit = (requested_amount_usd * limit_factor).quantize(Decimal("1"))

    confidence = quality_confidence
    if risk_tier == RiskTier.HIGH:
        confidence = max(0.60, confidence - 0.08)
    elif risk_tier == RiskTier.LOW:
        confidence = min(0.93, confidence + 0.05)

    key_concerns: list[str] = []
    if leverage is not None and leverage > 5:
        key_concerns.append(f"Debt to EBITDA is elevated at {leverage:.2f}x")
    if defaults:
        key_concerns.append("Prior default exists in applicant registry history")
    if current_ratio is not None and current_ratio < 1:
        key_concerns.append(f"Current ratio is weak at {current_ratio:.2f}x")
    key_concerns.extend(_quality_caveats(analysis))

    rationale = ". ".join(
        [
            f"Registry trajectory={trajectory}",
            f"risk_segment={risk_segment}",
            f"recommended_limit={recommended_limit}",
            *(key_concerns[:2] or ["No blocking underwriting concerns identified"]),
        ]
    )
    return CreditDecision(
        risk_tier=risk_tier,
        recommended_limit_usd=recommended_limit,
        confidence=round(confidence, 2),
        rationale=rationale,
        key_concerns=key_concerns[:4],
        data_quality_caveats=_quality_caveats(analysis),
        policy_overrides_applied=["limit_capped_to_ebitda"] if facts.ebitda is not None and facts.ebitda > 0 else [],
    )


def _build_fraud_anomalies(company_context: RuntimeApplicantContext, analysis: DocumentPackageAnalysis) -> list[FraudAnomaly]:
    anomalies: list[FraudAnomaly] = []
    for note in analysis.consistency_notes:
        lowered = note.lower()
        if "revenue" in lowered:
            anomalies.append(
                FraudAnomaly(
                    anomaly_type=FraudAnomalyType.REVENUE_DISCREPANCY,
                    description=note,
                    severity="MEDIUM",
                    evidence=note,
                    affected_fields=["total_revenue"],
                )
            )
        elif "balance sheet" in lowered:
            anomalies.append(
                FraudAnomaly(
                    anomaly_type=FraudAnomalyType.BALANCE_SHEET_INCONSISTENCY,
                    description=note,
                    severity="MEDIUM",
                    evidence=note,
                    affected_fields=["total_assets", "total_liabilities", "total_equity"],
                )
            )
    if any(flag.get("is_active") for flag in company_context.compliance_flags):
        anomalies.append(
            FraudAnomaly(
                anomaly_type=FraudAnomalyType.UNUSUAL_SUBMISSION_PATTERN,
                description="Active watch flags require a closer review of submission behavior.",
                severity="LOW",
                evidence=f"active_flags={len([flag for flag in company_context.compliance_flags if flag.get('is_active')])}",
                affected_fields=["compliance_flags"],
            )
        )
    return anomalies


def _score_fraud(anomalies: list[FraudAnomaly], company_context: RuntimeApplicantContext) -> tuple[float, str, str]:
    score = 0.08
    for anomaly in anomalies:
        if anomaly.severity == "HIGH":
            score += 0.35
        elif anomaly.severity == "MEDIUM":
            score += 0.18
        else:
            score += 0.08
    if company_context.industry.lower() in {"retail", "logistics"}:
        score += 0.04
    score = round(min(score, 0.95), 2)
    if score >= 0.6:
        return score, "HIGH", "REVIEW"
    if score >= 0.3:
        return score, "MEDIUM", "CLEAR"
    return score, "LOW", "CLEAR"


def _build_compliance_rule_results(company_context: RuntimeApplicantContext) -> tuple[list[ComplianceRuleEvaluation], bool]:
    hard_block = company_context.jurisdiction.upper() == "MT"
    operating_years = (_now().year - company_context.founded_year) if company_context.founded_year else None
    results = [
        ComplianceRuleEvaluation("REG-001", "AML", "PASS", "1", "aml-evidence", evaluation_notes="Customer cleared AML screening."),
        ComplianceRuleEvaluation("REG-002", "OFAC", "PASS", "1", "ofac-evidence", evaluation_notes="No OFAC match present."),
    ]
    if hard_block:
        results.append(
            ComplianceRuleEvaluation(
                "REG-003",
                "Jurisdiction",
                "FAIL",
                "1",
                "jurisdiction-evidence",
                failure_reason="Montana is excluded from this product.",
                is_hard_block=True,
            )
        )
    else:
        results.append(
            ComplianceRuleEvaluation("REG-003", "Jurisdiction", "PASS", "1", "jurisdiction-evidence", evaluation_notes="Jurisdiction is allowed.")
        )

    if company_context.legal_type:
        results.append(
            ComplianceRuleEvaluation("REG-004", "Entity Type", "PASS", "1", "entity-evidence", evaluation_notes="Entity type is eligible.")
        )
    else:
        results.append(
            ComplianceRuleEvaluation(
                "REG-004",
                "Entity Type",
                "FAIL",
                "1",
                "entity-evidence",
                failure_reason="Entity type is missing from the applicant registry.",
            )
        )

    if operating_years is not None and operating_years >= 2:
        results.append(
            ComplianceRuleEvaluation("REG-005", "Operating History", "PASS", "1", "history-evidence", evaluation_notes="Operating history satisfies policy.")
        )
    else:
        results.append(
            ComplianceRuleEvaluation(
                "REG-005",
                "Operating History",
                "FAIL",
                "1",
                "history-evidence",
                failure_reason="Operating history is too short for standard approval.",
                remediation_available=True,
                remediation_description="Escalate for exception review.",
            )
        )

    results.append(
        ComplianceRuleEvaluation("REG-006", "CRA", "NOTE", "1", "cra-evidence", note_type="CRA", note_text="CRA consideration recorded.")
    )
    return results, hard_block


def _recommended_decision(credit_decision: CreditDecision, *, quality_caveats: list[str], fraud_score: float) -> tuple[str, float, Decimal | None, list[str], str]:
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


async def _append_current(store: StoreType, stream_id: str, events: list[dict[str, Any]]) -> list[int]:
    version = await store.stream_version(stream_id)
    return await store.append(
        stream_id,
        events,
        expected_version=version,
        metadata={"outbox_destinations": ["ledger.downstream"]},
    )


async def _start_session(
    store: StoreType,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    agent_id: str,
    model_version: str,
    context_source: str = "fresh",
    context_token_count: int = 0,
    langgraph_graph_version: str = "graph-v2",
) -> str:
    await handle_start_agent_session(
        StartAgentSessionCommand(
            application_id=application_id,
            agent_type=agent_type,
            session_id=session_id,
            agent_id=agent_id,
            model_version=model_version,
            langgraph_graph_version=langgraph_graph_version,
            context_source=context_source,
            context_token_count=context_token_count,
        ),
        store,
    )
    return f"agent-{agent_type.value}-{session_id}"


async def _append_session_event(store: StoreType, agent_type: AgentType, session_id: str, event: dict[str, Any]) -> list[int]:
    return await _append_current(store, f"agent-{agent_type.value}-{session_id}", [event])


async def _record_input_validated(
    store: StoreType,
    agent_type: AgentType,
    session_id: str,
    application_id: str,
    inputs_validated: list[str],
    duration_ms: int = 80,
) -> list[int]:
    return await _append_session_event(
        store,
        agent_type,
        session_id,
        AgentInputValidated(
            session_id=session_id,
            agent_type=agent_type,
            application_id=application_id,
            inputs_validated=inputs_validated,
            validation_duration_ms=duration_ms,
            validated_at=_now(),
        ).to_store_dict(),
    )


async def _record_input_validation_failed(
    store: StoreType,
    agent_type: AgentType,
    session_id: str,
    application_id: str,
    missing_inputs: list[str],
    validation_errors: list[str],
) -> list[int]:
    return await _append_session_event(
        store,
        agent_type,
        session_id,
        AgentInputValidationFailed(
            session_id=session_id,
            agent_type=agent_type,
            application_id=application_id,
            missing_inputs=missing_inputs,
            validation_errors=validation_errors,
            failed_at=_now(),
        ).to_store_dict(),
    )


async def _record_node(
    store: StoreType,
    agent_type: AgentType,
    session_id: str,
    *,
    node_name: str,
    node_sequence: int,
    llm_result: AgentLLMResult | None = None,
    input_keys: list[str] | None = None,
    output_keys: list[str] | None = None,
    duration_ms: int = 150,
) -> list[int]:
    return await _append_session_event(
        store,
        agent_type,
        session_id,
        AgentNodeExecuted(
            session_id=session_id,
            agent_type=agent_type,
            node_name=node_name,
            node_sequence=node_sequence,
            input_keys=input_keys or [],
            output_keys=output_keys or [],
            llm_called=bool(llm_result and llm_result.called),
            llm_tokens_input=(llm_result.tokens_input if llm_result else None),
            llm_tokens_output=(llm_result.tokens_output if llm_result else None),
            llm_cost_usd=(llm_result.cost_usd if llm_result else 0.0),
            duration_ms=duration_ms,
            executed_at=_now(),
        ).to_store_dict(),
    )


async def _record_tool(
    store: StoreType,
    agent_type: AgentType,
    session_id: str,
    *,
    tool_name: str,
    tool_input_summary: str,
    tool_output_summary: str,
    tool_duration_ms: int = 45,
) -> list[int]:
    return await _append_session_event(
        store,
        agent_type,
        session_id,
        AgentToolCalled(
            session_id=session_id,
            agent_type=agent_type,
            tool_name=tool_name,
            tool_input_summary=tool_input_summary,
            tool_output_summary=tool_output_summary,
            tool_duration_ms=tool_duration_ms,
            called_at=_now(),
        ).to_store_dict(),
    )


async def _record_agent_output(
    store: StoreType,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    stream_id: str,
    event_type: str,
    stream_position: int,
) -> list[int]:
    return await _append_session_event(
        store,
        agent_type,
        session_id,
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
            output_summary=f"{event_type} recorded",
            written_at=_now(),
        ).to_store_dict(),
    )


async def _complete_session(
    store: StoreType,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    total_nodes: int,
    llm_results: list[AgentLLMResult] | None = None,
    next_agent_triggered: str | None = None,
    total_duration_ms: int = 650,
) -> list[int]:
    llm_results = llm_results or []
    return await _append_session_event(
        store,
        agent_type,
        session_id,
        AgentSessionCompleted(
            session_id=session_id,
            agent_type=agent_type,
            application_id=application_id,
            total_nodes_executed=total_nodes,
            total_llm_calls=sum(1 for result in llm_results if result.called),
            total_tokens_used=sum(result.tokens_input + result.tokens_output for result in llm_results if result.called),
            total_cost_usd=round(sum(result.cost_usd for result in llm_results if result.called), 6),
            total_duration_ms=total_duration_ms,
            next_agent_triggered=next_agent_triggered,
            completed_at=_now(),
        ).to_store_dict(),
    )


async def _fail_session(
    store: StoreType,
    application_id: str,
    agent_type: AgentType,
    session_id: str,
    *,
    error_type: str,
    error_message: str,
    last_successful_node: str | None = None,
) -> list[int]:
    return await _append_session_event(
        store,
        agent_type,
        session_id,
        AgentSessionFailed(
            session_id=session_id,
            agent_type=agent_type,
            application_id=application_id,
            error_type=error_type,
            error_message=error_message,
            last_successful_node=last_successful_node,
            recoverable=True,
            failed_at=_now(),
        ).to_store_dict(),
    )


def _required_document_paths(company_id: str) -> list[Path]:
    company_dir = DOCUMENTS_ROOT / company_id
    return [
        company_dir / "application_proposal.pdf",
        company_dir / "income_statement_2024.pdf",
        company_dir / "balance_sheet_2024.pdf",
        company_dir / "financial_statements.xlsx",
        company_dir / "financial_summary.csv",
    ]


def decision_result_requires_review(credit_decision: CreditDecision, fraud_score: float, quality_caveats: list[str]) -> bool:
    recommendation, _, _, _, _ = _recommended_decision(credit_decision, quality_caveats=quality_caveats, fraud_score=fraud_score)
    return recommendation in {"APPROVE", "DECLINE", "REFER"}


class LedgerAgentRuntime:
    def __init__(self, store: StoreType, *, llm_backend: AgentLLMBackend | None = None):
        self.store = store
        self.llm_backend = llm_backend or build_llm_backend()
        self._daemon: ProjectionDaemon | None = None

    async def sync(self) -> None:
        if self._daemon is None:
            summary = ApplicationSummaryProjection(self.store)
            performance = AgentPerformanceProjection(self.store)
            compliance = ComplianceAuditProjection(self.store)
            self._daemon = ProjectionDaemon(self.store, [summary, performance, compliance])
            await self._daemon.initialize()
        await self._daemon.run_until_caught_up()

    async def run_document_stage(
        self,
        application_id: str,
        company_id: str,
        *,
        include_summaries: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = session_id or f"sess-doc-{uuid4().hex[:6]}"
        session_stream_id = await _start_session(
            self.store,
            application_id,
            AgentType.DOCUMENT_PROCESSING,
            session_id,
            agent_id=f"doc-{company_id.lower()}",
            model_version="docproc-v2",
            context_token_count=0,
        )
        compiled = self._build_document_graph()
        initial_state: DocumentAgentState = {
            "application_id": application_id,
            "company_id": company_id,
            "session_id": session_id,
            "session_stream_id": session_stream_id,
            "include_summaries": include_summaries,
        }
        try:
            state = await compiled.ainvoke(initial_state)
        except Exception as exc:  # noqa: BLE001
            await _fail_session(
                self.store,
                application_id,
                AgentType.DOCUMENT_PROCESSING,
                session_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            raise

        await self.sync()
        return {
            "application_id": application_id,
            "company_id": company_id,
            "analysis": state["analysis"],
            "positions": state.get("positions", []),
            "quality_caveats": state.get("quality_caveats", []),
            "document_session_stream_id": session_stream_id,
        }

    async def start_application(
        self,
        application_id: str,
        company_id: str,
        *,
        phase: str = "full",
        requested_amount_usd: Decimal | None = None,
        auto_finalize_human_review: bool = False,
        reviewer_id: str = "loan-ops",
    ) -> dict[str, Any]:
        if phase == "document":
            doc_result = await self.run_document_stage(application_id, company_id)
            await self._ensure_submitted(
                application_id,
                company_id,
                doc_result["analysis"],
                requested_amount_usd=requested_amount_usd,
            )
            await self.sync()
            loan_events = await self.store.load_stream(f"loan-{application_id}")
            return {
                "application_id": application_id,
                "company_id": company_id,
                "phase": phase,
                "final_event_type": loan_events[-1].event_type if loan_events else None,
                "quality_caveats": doc_result["quality_caveats"],
            }
        if phase == "credit":
            credit_result = await self.run_credit_stage(
                application_id,
                company_id,
                requested_amount_usd=requested_amount_usd,
            )
            return {
                "application_id": application_id,
                "company_id": company_id,
                "phase": phase,
                "final_event_type": "CreditAnalysisDeferred" if credit_result.get("deferred") else "CreditAnalysisCompleted",
                "quality_caveats": credit_result["quality_caveats"],
                "profile_source": credit_result["profile_source"],
            }
        return await self.run_full_pipeline(
            application_id,
            company_id,
            auto_finalize_human_review=auto_finalize_human_review,
            reviewer_id=reviewer_id,
        )

    async def run_credit_stage(
        self,
        application_id: str,
        company_id: str,
        *,
        requested_amount_usd: Decimal | None = None,
        include_summaries: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        doc_result = await self.run_document_stage(application_id, company_id, include_summaries=include_summaries)
        analysis = doc_result["analysis"]
        await self._ensure_submitted(application_id, company_id, analysis, requested_amount_usd=requested_amount_usd)
        await self._ensure_credit_requested(application_id)

        company_context = await _resolve_company_context(self.store, company_id)
        session_id = session_id or f"sess-credit-{uuid4().hex[:6]}"
        session_stream_id = await _start_session(
            self.store,
            application_id,
            AgentType.CREDIT_ANALYSIS,
            session_id,
            agent_id=f"credit-{company_id.lower()}",
            model_version="credit-v2",
            context_source=company_context.source,
            context_token_count=len(json.dumps(_serialize(company_context.as_profile_dict()))),
        )
        compiled = self._build_credit_graph()
        initial_state: CreditAgentState = {
            "application_id": application_id,
            "company_id": company_id,
            "session_id": session_id,
            "session_stream_id": session_stream_id,
            "analysis": analysis,
            "requested_amount_usd": requested_amount_usd or _requested_amount_from_analysis(analysis, company_id),
        }
        try:
            state = await compiled.ainvoke(initial_state)
        except Exception as exc:  # noqa: BLE001
            await _fail_session(
                self.store,
                application_id,
                AgentType.CREDIT_ANALYSIS,
                session_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            raise

        await self.sync()
        return {
            "application_id": application_id,
            "company_id": company_id,
            "analysis": analysis,
            "profile": state["company_context"].as_profile_dict(),
            "profile_source": state["company_context"].source,
            "credit_session_stream_id": session_stream_id,
            "credit_decision": state.get("credit_decision"),
            "requested_amount_usd": state["requested_amount_usd"],
            "quality_caveats": state.get("quality_caveats", []),
            "deferred": state.get("deferred", False),
            "defer_reason": state.get("defer_reason"),
        }

    async def run_fraud_stage(
        self,
        application_id: str,
        company_id: str,
        *,
        credit_result: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        credit_result = credit_result or await self.run_credit_stage(application_id, company_id)
        if credit_result.get("deferred"):
            raise RuntimeError(f"Credit stage deferred for {application_id}: {credit_result.get('defer_reason')}")

        session_id = session_id or f"sess-fraud-{uuid4().hex[:6]}"
        company_context = await _resolve_company_context(self.store, company_id)
        session_stream_id = await _start_session(
            self.store,
            application_id,
            AgentType.FRAUD_DETECTION,
            session_id,
            agent_id=f"fraud-{company_id.lower()}",
            model_version="fraud-v2",
            context_source=company_context.source,
            context_token_count=len(json.dumps(_serialize(company_context.as_profile_dict()))),
        )
        compiled = self._build_fraud_graph()
        state: FraudAgentState = {
            "application_id": application_id,
            "company_id": company_id,
            "session_id": session_id,
            "session_stream_id": session_stream_id,
            "analysis": credit_result["analysis"],
            "company_context": company_context,
            "credit_decision": credit_result["credit_decision"],
        }
        try:
            result = await compiled.ainvoke(state)
        except Exception as exc:  # noqa: BLE001
            await _fail_session(
                self.store,
                application_id,
                AgentType.FRAUD_DETECTION,
                session_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            raise

        await self.sync()
        return {
            "fraud_session_stream_id": session_stream_id,
            "fraud_score": result["fraud_score"],
            "risk_level": result["risk_level"],
            "recommendation": result["recommendation"],
            "anomalies": [anomaly.model_dump(mode="json") for anomaly in result.get("anomalies", [])],
            "profile_source": company_context.source,
        }

    async def run_compliance_stage(
        self,
        application_id: str,
        company_id: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        company_context = await _resolve_company_context(self.store, company_id)
        session_id = session_id or f"sess-compliance-{uuid4().hex[:6]}"
        session_stream_id = await _start_session(
            self.store,
            application_id,
            AgentType.COMPLIANCE,
            session_id,
            agent_id=f"compliance-{company_id.lower()}",
            model_version="compliance-v2",
            context_source=company_context.source,
            context_token_count=len(json.dumps(_serialize(company_context.as_profile_dict()))),
        )
        compiled = self._build_compliance_graph()
        state: ComplianceAgentState = {
            "application_id": application_id,
            "company_id": company_id,
            "session_id": session_id,
            "session_stream_id": session_stream_id,
            "company_context": company_context,
        }
        try:
            result = await compiled.ainvoke(state)
        except Exception as exc:  # noqa: BLE001
            await _fail_session(
                self.store,
                application_id,
                AgentType.COMPLIANCE,
                session_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            raise

        await self.sync()
        return {
            "compliance_session_stream_id": session_stream_id,
            "hard_block": result["hard_block"],
            "rule_ids": [rule.rule_id for rule in result["rule_results"]],
            "profile_source": company_context.source,
        }

    async def run_decision_stage(
        self,
        application_id: str,
        company_id: str,
        *,
        credit_result: dict[str, Any],
        fraud_result: dict[str, Any],
        compliance_result: dict[str, Any],
        auto_request_review: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        company_context = await _resolve_company_context(self.store, company_id)
        session_id = session_id or f"sess-orch-{uuid4().hex[:6]}"
        session_stream_id = await _start_session(
            self.store,
            application_id,
            AgentType.DECISION_ORCHESTRATOR,
            session_id,
            agent_id=f"orch-{company_id.lower()}",
            model_version="orch-v2",
            context_source=company_context.source,
            context_token_count=len(json.dumps(_serialize(company_context.as_profile_dict()))),
        )
        compiled = self._build_decision_graph(auto_request_review=auto_request_review)
        state: DecisionAgentState = {
            "application_id": application_id,
            "company_id": company_id,
            "session_id": session_id,
            "session_stream_id": session_stream_id,
            "company_context": company_context,
            "quality_caveats": credit_result["quality_caveats"],
            "credit_decision": credit_result["credit_decision"],
            "fraud_score": fraud_result["fraud_score"],
            "credit_session_stream_id": credit_result["credit_session_stream_id"],
            "fraud_session_stream_id": fraud_result["fraud_session_stream_id"],
            "compliance_session_stream_id": compliance_result["compliance_session_stream_id"],
        }
        try:
            result = await compiled.ainvoke(state)
        except Exception as exc:  # noqa: BLE001
            await _fail_session(
                self.store,
                application_id,
                AgentType.DECISION_ORCHESTRATOR,
                session_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            raise

        await self.sync()
        return {
            "decision_session_stream_id": session_stream_id,
            "recommendation": result["recommendation"],
            "confidence": result["confidence"],
            "approved_amount_usd": result["approved_amount_usd"],
            "key_risks": result["key_risks"],
            "executive_summary": result["executive_summary"],
        }

    async def complete_human_review(
        self,
        application_id: str,
        *,
        reviewer_id: str,
        final_decision: str,
        override: bool | None = None,
        override_reason: str | None = None,
        approved_amount_usd: Decimal | None = None,
        interest_rate_pct: float | None = None,
        term_months: int | None = None,
        conditions: list[str] | None = None,
        decline_reasons: list[str] | None = None,
        adverse_action_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        loan = await LoanApplicationAggregate.load(self.store, application_id)
        loan_events = await self.store.load_stream(f"loan-{application_id}")
        decision_event = next((event for event in reversed(loan_events) if event.event_type == "DecisionGenerated"), None)
        if decision_event is None:
            raise RuntimeError(f"No DecisionGenerated event found for {application_id}")

        original_recommendation = str(decision_event.payload["recommendation"])
        if override is None:
            override = final_decision.upper() != original_recommendation.upper()
        recommended_amount = decision_event.payload.get("approved_amount_usd")
        approved_amount = approved_amount_usd
        if approved_amount is None and recommended_amount is not None:
            approved_amount = Decimal(str(recommended_amount))

        result = await handle_human_review_completed(
            HumanReviewCompletedCommand(
                application_id=application_id,
                reviewer_id=reviewer_id,
                override=override,
                override_reason=override_reason,
                original_recommendation=original_recommendation,
                final_decision=final_decision.upper(),
                approved_amount_usd=approved_amount,
                interest_rate_pct=interest_rate_pct,
                term_months=term_months,
                conditions=conditions or [],
                decline_reasons=decline_reasons or [],
                adverse_action_notice_required=True,
                adverse_action_codes=adverse_action_codes or [],
            ),
            self.store,
        )
        await self._snapshot_loan(application_id)
        await self.sync()
        return {
            "application_id": application_id,
            "positions": result,
            "final_decision": final_decision.upper(),
            "reviewer_id": reviewer_id,
        }

    async def run_full_pipeline(
        self,
        application_id: str,
        company_id: str | None = None,
        *,
        auto_finalize_human_review: bool = False,
        reviewer_id: str = "loan-ops",
    ) -> dict[str, Any]:
        company = company_id or default_company_for_application(application_id)
        credit_result = await self.run_credit_stage(application_id, company)
        if credit_result.get("deferred"):
            return {
                "application_id": application_id,
                "company_id": company,
                "profile_source": credit_result["profile_source"],
                "final_event_type": "CreditAnalysisDeferred",
                "credit_risk_tier": None,
                "quality_caveats": credit_result["quality_caveats"],
                "requires_human_review": True,
            }

        fraud_result = await self.run_fraud_stage(application_id, company, credit_result=credit_result)
        compliance_result = await self.run_compliance_stage(application_id, company)
        if compliance_result["hard_block"]:
            await self._snapshot_loan(application_id)
            await self.sync()
            loan_events = await self.store.load_stream(f"loan-{application_id}")
            return {
                "application_id": application_id,
                "company_id": company,
                "profile_source": credit_result["profile_source"],
                "final_event_type": loan_events[-1].event_type if loan_events else None,
                "credit_risk_tier": credit_result["credit_decision"].risk_tier.value,
                "quality_caveats": credit_result["quality_caveats"],
                "requires_human_review": False,
            }

        decision_result = await self.run_decision_stage(
            application_id,
            company,
            credit_result=credit_result,
            fraud_result=fraud_result,
            compliance_result=compliance_result,
            auto_request_review=not auto_finalize_human_review
            or decision_result_requires_review(credit_result["credit_decision"], fraud_result["fraud_score"], credit_result["quality_caveats"]),
        )

        if auto_finalize_human_review:
            final_decision = "APPROVE" if decision_result["recommendation"] != "DECLINE" else "DECLINE"
            await self.complete_human_review(
                application_id,
                reviewer_id=reviewer_id,
                override=False,
                final_decision=final_decision,
                approved_amount_usd=decision_result["approved_amount_usd"],
                interest_rate_pct=8.75 if final_decision == "APPROVE" else None,
                term_months=36 if final_decision == "APPROVE" else None,
                conditions=["Quarterly covenant certificate"] if final_decision == "APPROVE" else [],
                decline_reasons=["Automated decline confirmed"] if final_decision == "DECLINE" else [],
                adverse_action_codes=["AUTO-DECLINE"] if final_decision == "DECLINE" else [],
            )
        else:
            loan = await LoanApplicationAggregate.load(self.store, application_id)
            if loan.state in {loan.state.APPROVED_PENDING_HUMAN, loan.state.DECLINED_PENDING_HUMAN}:
                await self._request_human_review(application_id, reason="Client workspace review required before final disposition.")

        await self._snapshot_loan(application_id)
        await self.sync()
        loan_events = await self.store.load_stream(f"loan-{application_id}")
        return {
            "application_id": application_id,
            "company_id": company,
            "profile_source": credit_result["profile_source"],
            "final_event_type": loan_events[-1].event_type if loan_events else None,
            "credit_risk_tier": credit_result["credit_decision"].risk_tier.value,
            "quality_caveats": credit_result["quality_caveats"],
            "requires_human_review": not auto_finalize_human_review,
            "decision_recommendation": decision_result["recommendation"],
        }

    async def run_integrity(self, application_id: str) -> dict[str, Any]:
        result = await run_integrity_check(self.store, "loan", application_id)
        await self.sync()
        return result

    async def _ensure_submitted(
        self,
        application_id: str,
        company_id: str,
        analysis: DocumentPackageAnalysis,
        *,
        requested_amount_usd: Decimal | None = None,
    ) -> None:
        loan = await LoanApplicationAggregate.load(self.store, application_id)
        if loan.state != loan.state.NEW:
            return

        company_context = await _resolve_company_context(self.store, company_id)
        proposal = analysis.get_document(DocumentType.APPLICATION_PROPOSAL)
        contact_name = str(proposal.structured_data.get("legal_entity_name")) if proposal else company_context.name
        await handle_submit_application(
            SubmitApplicationCommand(
                application_id=application_id,
                applicant_id=company_id,
                requested_amount_usd=requested_amount_usd or _requested_amount_from_analysis(analysis, company_id),
                loan_purpose=_loan_purpose_from_analysis(analysis),
                loan_term_months=36,
                submission_channel="ui_workspace",
                contact_email=f"{company_id.lower()}@example.com",
                contact_name=contact_name,
                application_reference=application_id,
            ),
            self.store,
        )
        await self._snapshot_loan(application_id)

    async def _ensure_credit_requested(self, application_id: str) -> None:
        loan_events = await self.store.load_stream(f"loan-{application_id}")
        if any(event.event_type == "CreditAnalysisRequested" for event in loan_events):
            return
        await _append_current(
            self.store,
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
        await self._snapshot_loan(application_id)

    async def _request_human_review(self, application_id: str, *, reason: str) -> None:
        loan_events = await self.store.load_stream(f"loan-{application_id}")
        if any(event.event_type == "HumanReviewRequested" for event in loan_events):
            return
        decision_event = next(event for event in reversed(loan_events) if event.event_type == "DecisionGenerated")
        await _append_current(
            self.store,
            f"loan-{application_id}",
            [
                HumanReviewRequested(
                    application_id=application_id,
                    reason=reason,
                    decision_event_id=str(decision_event.event_id),
                    assigned_to="loan-operations",
                    requested_at=_now(),
                ).to_store_dict()
            ],
        )
        await self._snapshot_loan(application_id)

    async def _snapshot_loan(self, application_id: str) -> None:
        if not hasattr(self.store, "save_snapshot"):
            return
        loan = await LoanApplicationAggregate.load(self.store, application_id)
        await self.store.save_snapshot(
            stream_id=f"loan-{application_id}",
            stream_position=loan.version,
            aggregate_type="LoanApplication",
            snapshot_version=1,
            state=loan.to_snapshot(),
        )

    def _build_document_graph(self):
        graph = StateGraph(DocumentAgentState)
        graph.add_node("validate_inputs", self._document_validate_inputs)
        graph.add_node("extract_package", self._document_extract_package)
        graph.add_node("write_package", self._document_write_package)
        graph.add_node("assess_quality", self._document_assess_quality)
        graph.add_node("complete_session", self._document_complete_session)
        graph.add_edge(START, "validate_inputs")
        graph.add_conditional_edges(
            "validate_inputs",
            lambda state: "end" if state.get("validation_failed") else "extract_package",
            {"extract_package": "extract_package", "end": END},
        )
        graph.add_edge("extract_package", "write_package")
        graph.add_edge("write_package", "assess_quality")
        graph.add_edge("assess_quality", "complete_session")
        graph.add_edge("complete_session", END)
        return graph.compile()

    def _build_credit_graph(self):
        graph = StateGraph(CreditAgentState)
        graph.add_node("validate_inputs", self._credit_validate_inputs)
        graph.add_node("open_credit_record", self._credit_open_record)
        graph.add_node("load_registry_profile", self._credit_load_registry_profile)
        graph.add_node("load_extracted_facts", self._credit_load_extracted_facts)
        graph.add_node("reason_credit", self._credit_reason_credit)
        graph.add_node("write_deferred", self._credit_write_deferred)
        graph.add_node("write_credit_decision", self._credit_write_decision)
        graph.add_node("complete_session", self._credit_complete_session)
        graph.add_edge(START, "validate_inputs")
        graph.add_edge("validate_inputs", "open_credit_record")
        graph.add_edge("open_credit_record", "load_registry_profile")
        graph.add_edge("load_registry_profile", "load_extracted_facts")
        graph.add_edge("load_extracted_facts", "reason_credit")
        graph.add_conditional_edges(
            "reason_credit",
            lambda state: "write_deferred" if state.get("deferred") else "write_credit_decision",
            {"write_deferred": "write_deferred", "write_credit_decision": "write_credit_decision"},
        )
        graph.add_edge("write_deferred", "complete_session")
        graph.add_edge("write_credit_decision", "complete_session")
        graph.add_edge("complete_session", END)
        return graph.compile()

    async def _document_validate_inputs(self, state: DocumentAgentState) -> dict[str, Any]:
        missing = [str(path.name) for path in _required_document_paths(state["company_id"]) if not path.exists()]
        if missing:
            await _record_input_validation_failed(
                self.store,
                AgentType.DOCUMENT_PROCESSING,
                state["session_id"],
                state["application_id"],
                missing_inputs=missing,
                validation_errors=["Company document package is incomplete."],
            )
            return {"validation_failed": True, "validation_errors": ["Company document package is incomplete."]}

        await _record_input_validated(
            self.store,
            AgentType.DOCUMENT_PROCESSING,
            state["session_id"],
            state["application_id"],
            inputs_validated=["application_id", "company_id", "document_package"],
        )
        await _record_node(
            self.store,
            AgentType.DOCUMENT_PROCESSING,
            state["session_id"],
            node_name="validate_inputs",
            node_sequence=1,
            input_keys=["application_id", "company_id"],
            output_keys=["document_paths"],
            duration_ms=180,
        )
        return {"validation_failed": False}

    async def _document_extract_package(self, state: DocumentAgentState) -> dict[str, Any]:
        processor = DocumentPackageProcessor(documents_root=DOCUMENTS_ROOT)
        analysis = processor.process_company(state["company_id"], include_summaries=state["include_summaries"])
        await _record_node(
            self.store,
            AgentType.DOCUMENT_PROCESSING,
            state["session_id"],
            node_name="extract_package",
            node_sequence=2,
            input_keys=["document_paths"],
            output_keys=["analysis"],
            duration_ms=260,
        )
        return {"analysis": analysis, "quality_caveats": _quality_caveats(analysis)}

    async def _document_write_package(self, state: DocumentAgentState) -> dict[str, Any]:
        stream_id = f"docpkg-{state['application_id']}"
        positions = []
        if await self.store.stream_version(stream_id) == -1:
            positions = await persist_document_package(self.store, state["analysis"], application_id=state["application_id"])
            await _record_agent_output(
                self.store,
                state["application_id"],
                AgentType.DOCUMENT_PROCESSING,
                state["session_id"],
                stream_id=stream_id,
                event_type="PackageReadyForAnalysis",
                stream_position=positions[-1],
            )
        await _record_node(
            self.store,
            AgentType.DOCUMENT_PROCESSING,
            state["session_id"],
            node_name="write_package",
            node_sequence=3,
            input_keys=["analysis"],
            output_keys=["docpkg_events"],
            duration_ms=220,
        )
        return {"positions": positions}

    async def _document_assess_quality(self, state: DocumentAgentState) -> dict[str, Any]:
        quality_caveats = state.get("quality_caveats", [])
        llm_result = await self.llm_backend.infer(
            system_prompt="You are a document-quality summarizer. Use only supplied evidence. Do not invent facts.",
            user_prompt=(
                f"Summarize document quality for application {state['application_id']} and company {state['company_id']}. "
                f"Quality caveats: {quality_caveats or ['none']}."
            ),
            metadata={
                "application_id": state["application_id"],
                "company_id": state["company_id"],
                "stage": "document_processing",
                "highlights": quality_caveats[:3],
            },
        )
        await _record_node(
            self.store,
            AgentType.DOCUMENT_PROCESSING,
            state["session_id"],
            node_name="assess_quality",
            node_sequence=4,
            llm_result=llm_result,
            input_keys=["analysis", "quality_caveats"],
            output_keys=["quality_summary"],
            duration_ms=520,
        )
        return {"llm_result": llm_result}

    async def _document_complete_session(self, state: DocumentAgentState) -> dict[str, Any]:
        await _complete_session(
            self.store,
            state["application_id"],
            AgentType.DOCUMENT_PROCESSING,
            state["session_id"],
            total_nodes=4,
            llm_results=[state["llm_result"]] if state.get("llm_result") else [],
            next_agent_triggered="credit_analysis",
        )
        return {}

    async def _credit_validate_inputs(self, state: CreditAgentState) -> dict[str, Any]:
        missing: list[str] = []
        analysis = state["analysis"]
        if analysis.merged_financial_facts.total_revenue is None:
            missing.append("total_revenue")
        if analysis.get_document(DocumentType.APPLICATION_PROPOSAL) is None:
            missing.append("application_proposal")
        if missing:
            await _record_input_validation_failed(
                self.store,
                AgentType.CREDIT_ANALYSIS,
                state["session_id"],
                state["application_id"],
                missing_inputs=missing,
                validation_errors=["Critical underwriting inputs are missing."],
            )
            return {"deferred": True, "defer_reason": "Critical underwriting inputs are missing."}

        await _record_input_validated(
            self.store,
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            state["application_id"],
            inputs_validated=["document_package", "submission", "requested_amount"],
        )
        await _record_node(
            self.store,
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            node_name="validate_inputs",
            node_sequence=1,
            input_keys=["analysis", "requested_amount_usd"],
            output_keys=["validated_inputs"],
            duration_ms=140,
        )
        return {}

    async def _credit_open_record(self, state: CreditAgentState) -> dict[str, Any]:
        stream_id = f"credit-{state['application_id']}"
        if await self.store.stream_version(stream_id) == -1:
            loan = await LoanApplicationAggregate.load(self.store, state["application_id"])
            positions = await _append_current(
                self.store,
                stream_id,
                [
                    CreditRecordOpened(
                        application_id=state["application_id"],
                        applicant_id=loan.applicant_id or state["company_id"],
                        opened_at=_now(),
                    ).to_store_dict()
                ],
            )
            await _record_agent_output(
                self.store,
                state["application_id"],
                AgentType.CREDIT_ANALYSIS,
                state["session_id"],
                stream_id=stream_id,
                event_type="CreditRecordOpened",
                stream_position=positions[-1],
            )
        await _record_node(
            self.store,
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            node_name="open_credit_record",
            node_sequence=2,
            input_keys=["application_id"],
            output_keys=["credit_stream"],
            duration_ms=120,
        )
        return {}

    async def _credit_load_registry_profile(self, state: CreditAgentState) -> dict[str, Any]:
        company_context = await _resolve_company_context(self.store, state["company_id"])
        await _record_tool(
            self.store,
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            tool_name="registry.lookup",
            tool_input_summary=state["company_id"],
            tool_output_summary=(
                f"industry={company_context.industry}; trajectory={company_context.trajectory}; "
                f"history_years={len(company_context.financial_history)}"
            ),
        )
        positions = await _append_current(
            self.store,
            f"credit-{state['application_id']}",
            [
                HistoricalProfileConsumed(
                    application_id=state["application_id"],
                    session_id=state["session_id"],
                    fiscal_years_loaded=[int(row["fiscal_year"]) for row in company_context.financial_history],
                    has_prior_loans=bool(company_context.loan_relationships),
                    has_defaults=any(bool(item.get("default_occurred")) for item in company_context.loan_relationships),
                    revenue_trajectory=company_context.trajectory,
                    data_hash=_hash_payload(company_context.as_profile_dict()),
                    consumed_at=_now(),
                ).to_store_dict()
            ],
        )
        await _record_agent_output(
            self.store,
            state["application_id"],
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            stream_id=f"credit-{state['application_id']}",
            event_type="HistoricalProfileConsumed",
            stream_position=positions[-1],
        )
        await _record_node(
            self.store,
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            node_name="load_registry_profile",
            node_sequence=3,
            input_keys=["company_id"],
            output_keys=["company_context"],
            duration_ms=140,
        )
        return {"company_context": company_context, "quality_caveats": _quality_caveats(state["analysis"])}

    async def _credit_load_extracted_facts(self, state: CreditAgentState) -> dict[str, Any]:
        document_ids = [f"{state['company_id']}:{document.document_type.value}" for document in state["analysis"].documents]
        positions = await _append_current(
            self.store,
            f"credit-{state['application_id']}",
            [
                ExtractedFactsConsumed(
                    application_id=state["application_id"],
                    session_id=state["session_id"],
                    document_ids_consumed=document_ids,
                    facts_summary=(
                        f"revenue={state['analysis'].merged_financial_facts.total_revenue}; "
                        f"ebitda={state['analysis'].merged_financial_facts.ebitda}; "
                        f"assets={state['analysis'].merged_financial_facts.total_assets}"
                    ),
                    quality_flags_present=bool(state.get("quality_caveats")),
                    consumed_at=_now(),
                ).to_store_dict()
            ],
        )
        await _record_agent_output(
            self.store,
            state["application_id"],
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            stream_id=f"credit-{state['application_id']}",
            event_type="ExtractedFactsConsumed",
            stream_position=positions[-1],
        )
        await _record_node(
            self.store,
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            node_name="load_extracted_facts",
            node_sequence=4,
            input_keys=["analysis"],
            output_keys=["facts_summary"],
            duration_ms=140,
        )
        core_missing = [
            field_name
            for field_name in ("total_revenue", "ebitda", "total_assets")
            if getattr(state["analysis"].merged_financial_facts, field_name) is None
        ]
        if core_missing:
            return {
                "deferred": True,
                "defer_reason": f"Critical merged facts missing: {', '.join(core_missing)}",
            }
        return {}

    async def _credit_reason_credit(self, state: CreditAgentState) -> dict[str, Any]:
        if state.get("deferred"):
            return {}
        decision = _build_credit_decision(state["company_context"], state["analysis"], state["requested_amount_usd"])
        llm_result = await self.llm_backend.infer(
            system_prompt="Summarize a commercial loan credit recommendation using only the supplied facts.",
            user_prompt=(
                f"Application {state['application_id']} for {state['company_id']}. "
                f"Requested amount {state['requested_amount_usd']}. "
                f"Risk tier {decision.risk_tier.value}. "
                f"Recommended limit {decision.recommended_limit_usd}. "
                f"Quality caveats: {state.get('quality_caveats', []) or ['none']}."
            ),
            metadata={
                "application_id": state["application_id"],
                "company_id": state["company_id"],
                "stage": "credit_analysis",
                "highlights": decision.key_concerns[:3],
            },
        )
        await _record_node(
            self.store,
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            node_name="reason_credit",
            node_sequence=5,
            llm_result=llm_result,
            input_keys=["company_context", "analysis", "quality_caveats"],
            output_keys=["credit_decision"],
            duration_ms=720,
        )
        return {"credit_decision": decision, "llm_result": llm_result}

    async def _credit_write_deferred(self, state: CreditAgentState) -> dict[str, Any]:
        positions = await _append_current(
            self.store,
            f"credit-{state['application_id']}",
            [
                CreditAnalysisDeferred(
                    application_id=state["application_id"],
                    session_id=state["session_id"],
                    deferral_reason=state["defer_reason"],
                    quality_issues=state.get("quality_caveats", []),
                    deferred_at=_now(),
                ).to_store_dict()
            ],
        )
        await _record_agent_output(
            self.store,
            state["application_id"],
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            stream_id=f"credit-{state['application_id']}",
            event_type="CreditAnalysisDeferred",
            stream_position=positions[-1],
        )
        return {"positions": {f"credit-{state['application_id']}": positions}}

    async def _credit_write_decision(self, state: CreditAgentState) -> dict[str, Any]:
        fallback_llm = AgentLLMResult("", "deterministic", "deterministic-fallback", False, 0, 0, 0.0)
        positions = await handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=state["application_id"],
                session_id=state["session_id"],
                decision=state["credit_decision"],
                model_version="credit-v2",
                model_deployment_id=state.get("llm_result", fallback_llm).model,
                input_data_hash=_hash_payload(
                    {
                        "analysis": state["analysis"].model_dump(mode="json"),
                        "company_context": state["company_context"].as_profile_dict(),
                        "requested_amount_usd": state["requested_amount_usd"],
                    }
                ),
                analysis_duration_ms=640,
                regulatory_basis=state.get("quality_caveats", []),
            ),
            self.store,
        )
        credit_position = positions[f"credit-{state['application_id']}"][-1]
        await _record_agent_output(
            self.store,
            state["application_id"],
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            stream_id=f"credit-{state['application_id']}",
            event_type="CreditAnalysisCompleted",
            stream_position=credit_position,
        )
        await self._snapshot_loan(state["application_id"])
        return {"positions": positions}

    async def _credit_complete_session(self, state: CreditAgentState) -> dict[str, Any]:
        await _complete_session(
            self.store,
            state["application_id"],
            AgentType.CREDIT_ANALYSIS,
            state["session_id"],
            total_nodes=5,
            llm_results=[state["llm_result"]] if state.get("llm_result") else [],
            next_agent_triggered=None if state.get("deferred") else "fraud_detection",
        )
        return {}

    def _build_fraud_graph(self):
        graph = StateGraph(FraudAgentState)
        graph.add_node("validate_inputs", self._fraud_validate_inputs)
        graph.add_node("initiate_screening", self._fraud_initiate_screening)
        graph.add_node("cross_reference_registry", self._fraud_cross_reference_registry)
        graph.add_node("analyze_anomalies", self._fraud_analyze_anomalies)
        graph.add_node("write_screening", self._fraud_write_screening)
        graph.add_node("complete_session", self._fraud_complete_session)
        graph.add_edge(START, "validate_inputs")
        graph.add_edge("validate_inputs", "initiate_screening")
        graph.add_edge("initiate_screening", "cross_reference_registry")
        graph.add_edge("cross_reference_registry", "analyze_anomalies")
        graph.add_edge("analyze_anomalies", "write_screening")
        graph.add_edge("write_screening", "complete_session")
        graph.add_edge("complete_session", END)
        return graph.compile()

    def _build_compliance_graph(self):
        graph = StateGraph(ComplianceAgentState)
        graph.add_node("validate_inputs", self._compliance_validate_inputs)
        graph.add_node("load_registry_context", self._compliance_load_registry_context)
        graph.add_node("evaluate_rules", self._compliance_evaluate_rules)
        graph.add_node("write_compliance", self._compliance_write_result)
        graph.add_node("complete_session", self._compliance_complete_session)
        graph.add_edge(START, "validate_inputs")
        graph.add_edge("validate_inputs", "load_registry_context")
        graph.add_edge("load_registry_context", "evaluate_rules")
        graph.add_edge("evaluate_rules", "write_compliance")
        graph.add_edge("write_compliance", "complete_session")
        graph.add_edge("complete_session", END)
        return graph.compile()

    def _build_decision_graph(self, *, auto_request_review: bool):
        graph = StateGraph(DecisionAgentState)
        graph.add_node("validate_inputs", self._decision_validate_inputs)
        graph.add_node("synthesize_recommendation", self._decision_synthesize_recommendation)
        graph.add_node("write_decision", self._decision_write_result)
        graph.add_node("request_review", self._decision_request_review if auto_request_review else self._decision_skip_review)
        graph.add_node("complete_session", self._decision_complete_session)
        graph.add_edge(START, "validate_inputs")
        graph.add_edge("validate_inputs", "synthesize_recommendation")
        graph.add_edge("synthesize_recommendation", "write_decision")
        graph.add_edge("write_decision", "request_review")
        graph.add_edge("request_review", "complete_session")
        graph.add_edge("complete_session", END)
        return graph.compile()

    async def _fraud_validate_inputs(self, state: FraudAgentState) -> dict[str, Any]:
        await _record_input_validated(
            self.store,
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            state["application_id"],
            inputs_validated=["credit_decision", "registry_profile", "document_quality"],
        )
        await _record_node(
            self.store,
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            node_name="validate_inputs",
            node_sequence=1,
            input_keys=["credit_decision", "analysis"],
            output_keys=["validated_inputs"],
            duration_ms=110,
        )
        return {}

    async def _fraud_initiate_screening(self, state: FraudAgentState) -> dict[str, Any]:
        positions = await _append_current(
            self.store,
            f"fraud-{state['application_id']}",
            [
                FraudScreeningInitiated(
                    application_id=state["application_id"],
                    session_id=state["session_id"],
                    screening_model_version="fraud-v2",
                    initiated_at=_now(),
                ).to_store_dict()
            ],
        )
        await _record_agent_output(
            self.store,
            state["application_id"],
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            stream_id=f"fraud-{state['application_id']}",
            event_type="FraudScreeningInitiated",
            stream_position=positions[-1],
        )
        await _record_node(
            self.store,
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            node_name="initiate_screening",
            node_sequence=2,
            input_keys=["application_id"],
            output_keys=["fraud_stream"],
            duration_ms=120,
        )
        return {}

    async def _fraud_cross_reference_registry(self, state: FraudAgentState) -> dict[str, Any]:
        active_flags = len([flag for flag in state["company_context"].compliance_flags if flag.get("is_active")])
        await _record_tool(
            self.store,
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            tool_name="registry.lookup",
            tool_input_summary=state["company_id"],
            tool_output_summary=f"flags={active_flags}; industry={state['company_context'].industry}",
        )
        await _record_node(
            self.store,
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            node_name="cross_reference_registry",
            node_sequence=3,
            input_keys=["company_context"],
            output_keys=["registry_flags"],
            duration_ms=120,
        )
        return {}

    async def _fraud_analyze_anomalies(self, state: FraudAgentState) -> dict[str, Any]:
        anomalies = _build_fraud_anomalies(state["company_context"], state["analysis"])
        fraud_score, risk_level, recommendation = _score_fraud(anomalies, state["company_context"])
        for anomaly in anomalies:
            positions = await _append_current(
                self.store,
                f"fraud-{state['application_id']}",
                [
                    FraudAnomalyDetected(
                        application_id=state["application_id"],
                        session_id=state["session_id"],
                        anomaly=anomaly,
                        detected_at=_now(),
                    ).to_store_dict()
                ],
            )
            await _record_agent_output(
                self.store,
                state["application_id"],
                AgentType.FRAUD_DETECTION,
                state["session_id"],
                stream_id=f"fraud-{state['application_id']}",
                event_type="FraudAnomalyDetected",
                stream_position=positions[-1],
            )
        llm_result = await self.llm_backend.infer(
            system_prompt="Summarize fraud findings using only the supplied anomalies and registry context.",
            user_prompt=(
                f"Application {state['application_id']} fraud review. "
                f"Anomalies: {[anomaly.description for anomaly in anomalies] or ['none']}. "
                f"Score={fraud_score}, risk={risk_level}, recommendation={recommendation}."
            ),
            metadata={
                "application_id": state["application_id"],
                "company_id": state["company_id"],
                "stage": "fraud_detection",
                "highlights": [anomaly.description for anomaly in anomalies[:3]],
            },
        )
        await _record_node(
            self.store,
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            node_name="analyze_anomalies",
            node_sequence=4,
            llm_result=llm_result,
            input_keys=["analysis", "registry_flags"],
            output_keys=["anomalies", "fraud_score"],
            duration_ms=560,
        )
        return {
            "anomalies": anomalies,
            "fraud_score": fraud_score,
            "risk_level": risk_level,
            "recommendation": recommendation,
            "llm_result": llm_result,
        }

    async def _fraud_write_screening(self, state: FraudAgentState) -> dict[str, Any]:
        positions = await handle_fraud_screening_completed(
            FraudScreeningCompletedCommand(
                application_id=state["application_id"],
                session_id=state["session_id"],
                fraud_score=state["fraud_score"],
                risk_level=state["risk_level"],
                anomalies_found=len(state.get("anomalies", [])),
                recommendation=state["recommendation"],
                screening_model_version="fraud-v2",
                input_data_hash=_hash_payload(
                    {
                        "company_context": state["company_context"].as_profile_dict(),
                        "anomalies": [anomaly.model_dump(mode="json") for anomaly in state.get("anomalies", [])],
                        "credit_decision": state["credit_decision"].model_dump(mode="json"),
                    }
                ),
            ),
            self.store,
        )
        fraud_position = positions[f"fraud-{state['application_id']}"][-1]
        await _record_agent_output(
            self.store,
            state["application_id"],
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            stream_id=f"fraud-{state['application_id']}",
            event_type="FraudScreeningCompleted",
            stream_position=fraud_position,
        )
        await self._snapshot_loan(state["application_id"])
        return {"positions": positions}

    async def _fraud_complete_session(self, state: FraudAgentState) -> dict[str, Any]:
        await _complete_session(
            self.store,
            state["application_id"],
            AgentType.FRAUD_DETECTION,
            state["session_id"],
            total_nodes=4,
            llm_results=[state["llm_result"]] if state.get("llm_result") else [],
            next_agent_triggered="compliance",
        )
        return {}

    async def _compliance_validate_inputs(self, state: ComplianceAgentState) -> dict[str, Any]:
        await _record_input_validated(
            self.store,
            AgentType.COMPLIANCE,
            state["session_id"],
            state["application_id"],
            inputs_validated=["company_context", "regulation_set"],
        )
        await _record_node(
            self.store,
            AgentType.COMPLIANCE,
            state["session_id"],
            node_name="validate_inputs",
            node_sequence=1,
            input_keys=["company_context"],
            output_keys=["validated_inputs"],
            duration_ms=80,
        )
        return {}

    async def _compliance_load_registry_context(self, state: ComplianceAgentState) -> dict[str, Any]:
        await _record_tool(
            self.store,
            AgentType.COMPLIANCE,
            state["session_id"],
            tool_name="registry.lookup",
            tool_input_summary=state["company_id"],
            tool_output_summary=f"jurisdiction={state['company_context'].jurisdiction}; legal_type={state['company_context'].legal_type}",
        )
        await _record_node(
            self.store,
            AgentType.COMPLIANCE,
            state["session_id"],
            node_name="load_registry_context",
            node_sequence=2,
            input_keys=["company_context"],
            output_keys=["rule_inputs"],
            duration_ms=90,
        )
        return {}

    async def _compliance_evaluate_rules(self, state: ComplianceAgentState) -> dict[str, Any]:
        rule_results, hard_block = _build_compliance_rule_results(state["company_context"])
        await _record_node(
            self.store,
            AgentType.COMPLIANCE,
            state["session_id"],
            node_name="evaluate_rules",
            node_sequence=3,
            input_keys=["rule_inputs"],
            output_keys=["rule_results"],
            duration_ms=180,
        )
        return {"rule_results": rule_results, "hard_block": hard_block}

    async def _compliance_write_result(self, state: ComplianceAgentState) -> dict[str, Any]:
        positions = await handle_compliance_check(
            ComplianceCheckCommand(
                application_id=state["application_id"],
                session_id=state["session_id"],
                regulation_set_version="2026-Q1",
                rules_to_evaluate=[rule.rule_id for rule in state["rule_results"]],
                rule_results=state["rule_results"],
                model_version="compliance-v2",
            ),
            self.store,
        )
        compliance_position = positions[f"compliance-{state['application_id']}"][-1]
        await _record_agent_output(
            self.store,
            state["application_id"],
            AgentType.COMPLIANCE,
            state["session_id"],
            stream_id=f"compliance-{state['application_id']}",
            event_type="ComplianceCheckCompleted",
            stream_position=compliance_position,
        )
        await self._snapshot_loan(state["application_id"])
        return {"positions": positions}

    async def _compliance_complete_session(self, state: ComplianceAgentState) -> dict[str, Any]:
        await _complete_session(
            self.store,
            state["application_id"],
            AgentType.COMPLIANCE,
            state["session_id"],
            total_nodes=3,
            llm_results=[],
            next_agent_triggered=None if state["hard_block"] else "decision_orchestrator",
            total_duration_ms=320,
        )
        return {}

    async def _decision_validate_inputs(self, state: DecisionAgentState) -> dict[str, Any]:
        await _record_input_validated(
            self.store,
            AgentType.DECISION_ORCHESTRATOR,
            state["session_id"],
            state["application_id"],
            inputs_validated=["credit_decision", "fraud_score", "compliance_result"],
        )
        await _record_node(
            self.store,
            AgentType.DECISION_ORCHESTRATOR,
            state["session_id"],
            node_name="validate_inputs",
            node_sequence=1,
            input_keys=["credit_decision", "fraud_score"],
            output_keys=["validated_inputs"],
            duration_ms=80,
        )
        return {}

    async def _decision_synthesize_recommendation(self, state: DecisionAgentState) -> dict[str, Any]:
        recommendation, confidence, approved_amount, key_risks, summary = _recommended_decision(
            state["credit_decision"],
            quality_caveats=state["quality_caveats"],
            fraud_score=state["fraud_score"],
        )
        llm_result = await self.llm_backend.infer(
            system_prompt="Summarize a lending recommendation using only the supplied credit, fraud, and compliance facts.",
            user_prompt=(
                f"Application {state['application_id']} recommendation={recommendation}. "
                f"Confidence={confidence}. Key risks={key_risks}. "
                f"Quality caveats={state['quality_caveats'] or ['none']}."
            ),
            metadata={
                "application_id": state["application_id"],
                "company_id": state["company_id"],
                "stage": "decision_orchestrator",
                "highlights": key_risks[:3],
            },
        )
        executive_summary = llm_result.summary or summary
        await _record_node(
            self.store,
            AgentType.DECISION_ORCHESTRATOR,
            state["session_id"],
            node_name="synthesize_recommendation",
            node_sequence=2,
            llm_result=llm_result,
            input_keys=["credit_decision", "fraud_score", "quality_caveats"],
            output_keys=["recommendation", "executive_summary"],
            duration_ms=640,
        )
        return {
            "recommendation": recommendation,
            "confidence": confidence,
            "approved_amount_usd": approved_amount,
            "key_risks": key_risks,
            "executive_summary": executive_summary,
            "llm_result": llm_result,
        }

    async def _decision_write_result(self, state: DecisionAgentState) -> dict[str, Any]:
        positions = await handle_generate_decision(
            GenerateDecisionCommand(
                application_id=state["application_id"],
                session_id=state["session_id"],
                recommendation=state["recommendation"],
                confidence=state["confidence"],
                approved_amount_usd=state["approved_amount_usd"],
                conditions=[],
                executive_summary=state["executive_summary"],
                key_risks=state["key_risks"],
                contributing_sessions=[
                    state["credit_session_stream_id"],
                    state["fraud_session_stream_id"],
                    state["compliance_session_stream_id"],
                ],
                model_versions={"decision_orchestrator": "orch-v2"},
            ),
            self.store,
        )
        decision_position = positions[f"loan-{state['application_id']}"][0]
        await _record_agent_output(
            self.store,
            state["application_id"],
            AgentType.DECISION_ORCHESTRATOR,
            state["session_id"],
            stream_id=f"loan-{state['application_id']}",
            event_type="DecisionGenerated",
            stream_position=decision_position,
        )
        await self._snapshot_loan(state["application_id"])
        return {"positions": positions}

    async def _decision_request_review(self, state: DecisionAgentState) -> dict[str, Any]:
        await self._request_human_review(state["application_id"], reason="Client workspace review required before final disposition.")
        await _record_node(
            self.store,
            AgentType.DECISION_ORCHESTRATOR,
            state["session_id"],
            node_name="request_review",
            node_sequence=3,
            input_keys=["decision_result"],
            output_keys=["human_review_request"],
            duration_ms=110,
        )
        return {}

    async def _decision_skip_review(self, state: DecisionAgentState) -> dict[str, Any]:
        await _record_node(
            self.store,
            AgentType.DECISION_ORCHESTRATOR,
            state["session_id"],
            node_name="request_review",
            node_sequence=3,
            input_keys=["decision_result"],
            output_keys=["review_skipped"],
            duration_ms=40,
        )
        return {}

    async def _decision_complete_session(self, state: DecisionAgentState) -> dict[str, Any]:
        await _complete_session(
            self.store,
            state["application_id"],
            AgentType.DECISION_ORCHESTRATOR,
            state["session_id"],
            total_nodes=3,
            llm_results=[state["llm_result"]] if state.get("llm_result") else [],
            next_agent_triggered="human_review",
        )
        return {}


__all__ = [
    "CompanyCatalogItem",
    "LedgerAgentRuntime",
    "RuntimeApplicantContext",
    "build_client_application_id",
    "default_company_for_application",
    "decision_result_requires_review",
    "list_document_companies",
]
