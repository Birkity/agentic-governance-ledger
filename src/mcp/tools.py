from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastmcp import FastMCP

from src.agents import LedgerAgentRuntime, build_client_application_id, list_document_companies
from src.aggregates.loan_application import LoanApplicationAggregate
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
from src.integrity import run_integrity_check
from src.models.events import (
    AgentOutputWritten,
    AgentSessionCompleted,
    AgentType,
    CreditAnalysisRequested,
    CreditDecision,
    DomainError,
    OptimisticConcurrencyError,
    PackageReadyForAnalysis,
    RiskTier,
)
from src.mcp.runtime import MCPRuntime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_decimal(value: Decimal | str | int | float | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _structured_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, OptimisticConcurrencyError):
        return exc.to_dict()
    if isinstance(exc, DomainError):
        return {
            "error_type": "DomainError",
            "message": str(exc),
            "suggested_action": "fix_preconditions_and_retry",
        }
    return {
        "error_type": exc.__class__.__name__,
        "message": str(exc),
        "suggested_action": "inspect_server_logs",
    }


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _failed(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": _structured_error(exc)}


async def _append_session_provenance(
    runtime: MCPRuntime,
    *,
    agent_type: AgentType,
    session_id: str,
    application_id: str,
    result: dict[str, list[int]],
    output_summary: str,
    next_agent_triggered: str | None = None,
    total_llm_calls: int = 1,
    total_tokens_used: int = 0,
    total_cost_usd: float = 0.0,
    total_duration_ms: int = 0,
) -> list[int]:
    events_written: list[dict[str, Any]] = []
    for stream_id, positions in result.items():
        if not positions:
            continue
        loaded = await runtime.store.load_stream(
            stream_id,
            from_position=min(positions),
            to_position=max(positions),
            apply_upcasters=False,
        )
        positions_set = set(positions)
        for event in loaded:
            if event.stream_position in positions_set:
                events_written.append(
                    {
                        "stream_id": stream_id,
                        "event_type": event.event_type,
                        "stream_position": event.stream_position,
                    }
                )

    session_stream = f"agent-{agent_type.value}-{session_id}"
    expected_version = await runtime.store.stream_version(session_stream)
    provenance_events = [
        AgentOutputWritten(
            session_id=session_id,
            agent_type=agent_type,
            application_id=application_id,
            events_written=events_written,
            output_summary=output_summary,
            written_at=_now(),
        ).to_store_dict(),
        AgentSessionCompleted(
            session_id=session_id,
            agent_type=agent_type,
            application_id=application_id,
            total_nodes_executed=max(1, len(events_written)),
            total_llm_calls=total_llm_calls,
            total_tokens_used=total_tokens_used,
            total_cost_usd=total_cost_usd,
            total_duration_ms=total_duration_ms,
            next_agent_triggered=next_agent_triggered,
            completed_at=_now(),
        ).to_store_dict(),
    ]
    return await runtime.store.append(session_stream, provenance_events, expected_version=expected_version)


async def _bootstrap_submission_path(runtime: MCPRuntime, application_id: str, documents_processed: int) -> dict[str, list[int]]:
    results: dict[str, list[int]] = {}
    package_stream = f"docpkg-{application_id}"
    if await runtime.store.stream_version(package_stream) == -1:
        results[package_stream] = await runtime.store.append(
            package_stream,
            [
                PackageReadyForAnalysis(
                    package_id=package_stream,
                    application_id=application_id,
                    documents_processed=documents_processed,
                    has_quality_flags=False,
                    quality_flag_count=0,
                    ready_at=_now(),
                ).to_store_dict()
            ],
            expected_version=-1,
        )

    loan = await LoanApplicationAggregate.load(runtime.store, application_id)
    if not loan.credit_requested:
        stream_id = f"loan-{application_id}"
        results[stream_id] = await runtime.store.append(
            stream_id,
            [
                CreditAnalysisRequested(
                    application_id=application_id,
                    requested_at=_now(),
                    requested_by="mcp_bootstrap",
                ).to_store_dict()
            ],
            expected_version=loan.version,
        )

    return results


async def _resolve_contributing_sessions(runtime: MCPRuntime, contributing_sessions: list[str]) -> list[str]:
    resolved: list[str] = []
    for item in contributing_sessions:
        if item.startswith("agent-"):
            resolved.append(item)
            continue
        candidate = await _find_agent_session_stream_id(runtime, item)
        resolved.append(candidate or item)
    return resolved


async def _find_agent_session_stream_id(runtime: MCPRuntime, session_id: str) -> str | None:
    records = await runtime.agent_performance.list_all()
    _ = records  # ensure projection is initialized for DB-backed runs

    if getattr(runtime.store, "_pool", None) is not None:
        async with runtime.store._pool.acquire() as conn:  # type: ignore[union-attr]
            stream_id = await conn.fetchval(
                """
                SELECT stream_id
                FROM agent_session_projection_index
                WHERE session_id = $1
                """,
                session_id,
            )
        return str(stream_id) if stream_id else None

    for stream_id, events in getattr(runtime.store, "_streams", {}).items():
        if stream_id.startswith("agent-") and any(
            event.payload.get("session_id") == session_id for event in events
        ):
            return stream_id
    return None


def register_tools(app: FastMCP, runtime: MCPRuntime) -> None:
    @app.tool(
        name="list_document_companies",
        description=(
            "List the seeded company document packages that can be launched into the runtime workflow. "
            "Use this to drive client-facing application intake without inventing company ids."
        ),
    )
    async def list_document_companies_tool() -> dict[str, Any]:
        companies = [item.__dict__ for item in list_document_companies()]
        return _ok(companies=companies, count=len(companies))

    @app.tool(
        name="run_application_workflow",
        description=(
            "Launch or continue an application from a company document package using the LangGraph-backed runtime. "
            "This path keeps AgentSessionStarted, document processing, compliance, and human-review rules intact."
        ),
    )
    async def run_application_workflow(
        company_id: str,
        application_id: str | None = None,
        phase: str = "full",
        requested_amount_usd: str | None = None,
        auto_finalize_human_review: bool = False,
        reviewer_id: str = "loan-ops",
    ) -> dict[str, Any]:
        try:
            ledger_runtime = LedgerAgentRuntime(runtime.store)
            resolved_application_id = application_id or build_client_application_id(company_id)
            result = await ledger_runtime.start_application(
                resolved_application_id,
                company_id,
                phase=phase,
                requested_amount_usd=_to_decimal(requested_amount_usd),
                auto_finalize_human_review=auto_finalize_human_review,
                reviewer_id=reviewer_id,
            )
            await runtime.sync()
            payload = dict(result)
            payload["application_id"] = resolved_application_id
            return _ok(**payload)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @app.tool(
        name="submit_application",
        description=(
            "Submit a new loan application. Preconditions: application_id must be unique. "
            "For the generated Apex document corpus, keep bootstrap_document_package=true so the "
            "analysis tools have a ready package and an initial CreditAnalysisRequested event."
        ),
    )
    async def submit_application(
        application_id: str,
        applicant_id: str,
        requested_amount_usd: str,
        loan_purpose: str,
        loan_term_months: int,
        submission_channel: str,
        contact_email: str,
        contact_name: str,
        application_reference: str,
        bootstrap_document_package: bool = True,
        bootstrap_documents_processed: int = 3,
    ) -> dict[str, Any]:
        try:
            result = await handle_submit_application(
                SubmitApplicationCommand(
                    application_id=application_id,
                    applicant_id=applicant_id,
                    requested_amount_usd=Decimal(requested_amount_usd),
                    loan_purpose=loan_purpose,
                    loan_term_months=loan_term_months,
                    submission_channel=submission_channel,
                    contact_email=contact_email,
                    contact_name=contact_name,
                    application_reference=application_reference,
                ),
                runtime.store,
            )
            bootstrap = {}
            if bootstrap_document_package:
                bootstrap = await _bootstrap_submission_path(
                    runtime,
                    application_id,
                    bootstrap_documents_processed,
                )
            await runtime.sync()
            return _ok(
                application_id=application_id,
                loan_stream=f"loan-{application_id}",
                positions=result,
                bootstrap=bootstrap,
            )
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @app.tool(
        name="start_agent_session",
        description=(
            "Start an agent session. Preconditions: call this before any agent decision tool. "
            "This is the Gas Town anchor for durable agent memory and model-version locking."
        ),
    )
    async def start_agent_session(
        application_id: str,
        agent_type: str,
        session_id: str,
        agent_id: str,
        model_version: str,
        langgraph_graph_version: str,
        context_source: str = "fresh",
        context_token_count: int = 0,
    ) -> dict[str, Any]:
        try:
            result = await handle_start_agent_session(
                StartAgentSessionCommand(
                    application_id=application_id,
                    agent_type=AgentType(agent_type),
                    session_id=session_id,
                    agent_id=agent_id,
                    model_version=model_version,
                    langgraph_graph_version=langgraph_graph_version,
                    context_source=context_source,
                    context_token_count=context_token_count,
                ),
                runtime.store,
            )
            await runtime.sync()
            return _ok(
                session_id=session_id,
                stream_id=f"agent-{agent_type}-{session_id}",
                positions=result,
            )
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @app.tool(
        name="record_credit_analysis",
        description=(
            "Record a completed credit analysis. Preconditions: an active credit-analysis agent "
            "session must exist, and the application must have a ready document package. "
            "The tool writes to the credit stream, records agent output provenance, and triggers fraud screening."
        ),
    )
    async def record_credit_analysis(
        application_id: str,
        session_id: str,
        risk_tier: str,
        recommended_limit_usd: str,
        confidence: float,
        rationale: str,
        model_version: str,
        model_deployment_id: str,
        input_data_hash: str,
        analysis_duration_ms: int,
        regulatory_basis: list[str] | None = None,
        key_concerns: list[str] | None = None,
        data_quality_caveats: list[str] | None = None,
        policy_overrides_applied: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = await handle_credit_analysis_completed(
                CreditAnalysisCompletedCommand(
                    application_id=application_id,
                    session_id=session_id,
                    decision=CreditDecision(
                        risk_tier=RiskTier(risk_tier),
                        recommended_limit_usd=Decimal(recommended_limit_usd),
                        confidence=confidence,
                        rationale=rationale,
                        key_concerns=key_concerns or [],
                        data_quality_caveats=data_quality_caveats or [],
                        policy_overrides_applied=policy_overrides_applied or [],
                    ),
                    model_version=model_version,
                    model_deployment_id=model_deployment_id,
                    input_data_hash=input_data_hash,
                    analysis_duration_ms=analysis_duration_ms,
                    regulatory_basis=regulatory_basis or [],
                ),
                runtime.store,
            )
            await _append_session_provenance(
                runtime,
                agent_type=AgentType.CREDIT_ANALYSIS,
                session_id=session_id,
                application_id=application_id,
                result=result,
                output_summary="Credit analysis recorded",
                next_agent_triggered="fraud_detection",
                total_duration_ms=analysis_duration_ms,
            )
            await runtime.sync()
            return _ok(application_id=application_id, positions=result)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @app.tool(
        name="record_fraud_screening",
        description=(
            "Record a completed fraud screening. Preconditions: an active fraud-detection session must exist "
            "and the application must already have completed credit analysis. Writes the fraud result and "
            "triggers compliance evaluation."
        ),
    )
    async def record_fraud_screening(
        application_id: str,
        session_id: str,
        fraud_score: float,
        risk_level: str,
        anomalies_found: int,
        recommendation: str,
        screening_model_version: str,
        input_data_hash: str,
    ) -> dict[str, Any]:
        try:
            if not 0.0 <= fraud_score <= 1.0:
                raise DomainError("fraud_score must be between 0.0 and 1.0")
            result = await handle_fraud_screening_completed(
                FraudScreeningCompletedCommand(
                    application_id=application_id,
                    session_id=session_id,
                    fraud_score=fraud_score,
                    risk_level=risk_level,
                    anomalies_found=anomalies_found,
                    recommendation=recommendation,
                    screening_model_version=screening_model_version,
                    input_data_hash=input_data_hash,
                ),
                runtime.store,
            )
            await _append_session_provenance(
                runtime,
                agent_type=AgentType.FRAUD_DETECTION,
                session_id=session_id,
                application_id=application_id,
                result=result,
                output_summary="Fraud screening recorded",
                next_agent_triggered="compliance",
            )
            await runtime.sync()
            return _ok(application_id=application_id, positions=result)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @app.tool(
        name="record_compliance_check",
        description=(
            "Record compliance rule evaluations. Preconditions: an active compliance session must exist, "
            "the application must be awaiting compliance review, and every rule result must use PASS, FAIL, or NOTE. "
            "This tool writes the compliance evidence stream and then either blocks the application or requests a decision."
        ),
    )
    async def record_compliance_check(
        application_id: str,
        session_id: str,
        regulation_set_version: str,
        rules_to_evaluate: list[str],
        rule_results: list[dict[str, Any]],
        model_version: str | None = None,
    ) -> dict[str, Any]:
        try:
            result = await handle_compliance_check(
                ComplianceCheckCommand(
                    application_id=application_id,
                    session_id=session_id,
                    regulation_set_version=regulation_set_version,
                    rules_to_evaluate=rules_to_evaluate,
                    rule_results=[
                        ComplianceRuleEvaluation(
                            rule_id=item["rule_id"],
                            rule_name=item["rule_name"],
                            outcome=item["outcome"],
                            rule_version=item["rule_version"],
                            evidence_hash=item["evidence_hash"],
                            evaluation_notes=item.get("evaluation_notes"),
                            failure_reason=item.get("failure_reason"),
                            is_hard_block=bool(item.get("is_hard_block", False)),
                            remediation_available=bool(item.get("remediation_available", False)),
                            remediation_description=item.get("remediation_description"),
                            note_type=item.get("note_type"),
                            note_text=item.get("note_text"),
                        )
                        for item in rule_results
                    ],
                    model_version=model_version,
                ),
                runtime.store,
            )
            await _append_session_provenance(
                runtime,
                agent_type=AgentType.COMPLIANCE,
                session_id=session_id,
                application_id=application_id,
                result=result,
                output_summary="Compliance evaluation recorded",
                next_agent_triggered="decision_orchestrator",
                total_llm_calls=0,
            )
            await runtime.sync()
            return _ok(application_id=application_id, positions=result)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @app.tool(
        name="generate_decision",
        description=(
            "Generate a lending recommendation. Preconditions: all required analyses must already be present, "
            "compliance must be complete, and contributing_sessions must reference valid agent sessions. "
            "If confidence is below the floor, the recommendation must be REFER."
        ),
    )
    async def generate_decision(
        application_id: str,
        session_id: str,
        recommendation: str,
        confidence: float,
        executive_summary: str,
        key_risks: list[str],
        contributing_sessions: list[str],
        model_versions: dict[str, str] | None = None,
        approved_amount_usd: str | None = None,
        conditions: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            resolved_sessions = await _resolve_contributing_sessions(runtime, contributing_sessions)
            result = await handle_generate_decision(
                GenerateDecisionCommand(
                    application_id=application_id,
                    session_id=session_id,
                    recommendation=recommendation,
                    confidence=confidence,
                    executive_summary=executive_summary,
                    key_risks=key_risks,
                    contributing_sessions=resolved_sessions,
                    model_versions=model_versions or {"decision_orchestrator": "unknown"},
                    approved_amount_usd=_to_decimal(approved_amount_usd),
                    conditions=conditions or [],
                ),
                runtime.store,
            )
            await _append_session_provenance(
                runtime,
                agent_type=AgentType.DECISION_ORCHESTRATOR,
                session_id=session_id,
                application_id=application_id,
                result=result,
                output_summary="Decision generated",
                next_agent_triggered="human_review" if recommendation.upper() == "REFER" else None,
            )
            await runtime.sync()
            return _ok(application_id=application_id, positions=result)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @app.tool(
        name="record_human_review",
        description=(
            "Record the human review outcome. Preconditions: the application must be pending human resolution. "
            "If override is true, override_reason is required. Approval also requires amount, rate, and term."
        ),
    )
    async def record_human_review(
        application_id: str,
        reviewer_id: str,
        override: bool,
        original_recommendation: str,
        final_decision: str,
        override_reason: str | None = None,
        approved_amount_usd: str | None = None,
        interest_rate_pct: float | None = None,
        term_months: int | None = None,
        conditions: list[str] | None = None,
        decline_reasons: list[str] | None = None,
        adverse_action_notice_required: bool = True,
        adverse_action_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = await handle_human_review_completed(
                HumanReviewCompletedCommand(
                    application_id=application_id,
                    reviewer_id=reviewer_id,
                    override=override,
                    original_recommendation=original_recommendation,
                    final_decision=final_decision,
                    override_reason=override_reason,
                    approved_amount_usd=_to_decimal(approved_amount_usd),
                    interest_rate_pct=interest_rate_pct,
                    term_months=term_months,
                    conditions=conditions or [],
                    decline_reasons=decline_reasons or [],
                    adverse_action_notice_required=adverse_action_notice_required,
                    adverse_action_codes=adverse_action_codes or [],
                ),
                runtime.store,
            )
            await runtime.sync()
            return _ok(application_id=application_id, positions=result)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @app.tool(
        name="run_integrity_check",
        description=(
            "Run a SHA-256 integrity check for one entity stream. Preconditions: requested_by_role must be "
            "compliance. The tool verifies the existing audit chain and appends a new AuditIntegrityCheckRun event."
        ),
    )
    async def run_integrity_check_tool(
        entity_type: str,
        entity_id: str,
        requested_by_role: str = "compliance",
    ) -> dict[str, Any]:
        try:
            if requested_by_role.lower() != "compliance":
                raise DomainError("run_integrity_check is restricted to the compliance role")
            result = await run_integrity_check(runtime.store, entity_type, entity_id)
            return _ok(
                entity_type=result.entity_type,
                entity_id=result.entity_id,
                source_stream_id=result.source_stream_id,
                audit_stream_id=result.audit_stream_id,
                events_verified=result.events_verified,
                chain_valid=result.chain_valid,
                tamper_detected=result.tamper_detected,
                integrity_hash=result.integrity_hash,
                previous_hash=result.previous_hash,
                check_stream_position=result.check_stream_position,
            )
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
