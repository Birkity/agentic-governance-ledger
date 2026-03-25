from __future__ import annotations

from uuid import uuid4

import pytest

import src.document_processing.pipeline as pipeline_module
from src.event_store import InMemoryEventStore
from src.mcp.server import build_gateway_from_store


def _app_id() -> str:
    return f"APEX-MCP-{uuid4().hex[:8].upper()}"


@pytest.mark.asyncio
async def test_full_lifecycle_via_mcp_tools_and_resources():
    store = InMemoryEventStore()
    await store.connect()
    gateway = await build_gateway_from_store(store)
    application_id = _app_id()

    submitted = await gateway.call_tool(
        "submit_application",
        {
            "application_id": application_id,
            "applicant_id": "COMP-024",
            "requested_amount_usd": "450000",
            "loan_purpose": "working_capital",
            "loan_term_months": 36,
            "submission_channel": "portal",
            "contact_email": "ops@company.test",
            "contact_name": "Ava Ops",
            "application_reference": f"REF-{application_id}",
            "bootstrap_document_package": True,
        },
    )
    assert submitted["ok"] is True
    assert f"loan-{application_id}" in submitted["bootstrap"]

    credit_session = await gateway.call_tool(
        "start_agent_session",
        {
            "application_id": application_id,
            "agent_type": "credit_analysis",
            "session_id": "sess-credit",
            "agent_id": "credit-agent-1",
            "model_version": "credit-v1",
            "langgraph_graph_version": "graph-v1",
            "context_source": "fresh",
            "context_token_count": 512,
        },
    )
    assert credit_session["ok"] is True

    credit = await gateway.call_tool(
        "record_credit_analysis",
        {
            "application_id": application_id,
            "session_id": "sess-credit",
            "risk_tier": "LOW",
            "recommended_limit_usd": "500000",
            "confidence": 0.84,
            "rationale": "Strong repayment capacity",
            "model_version": "credit-v1",
            "model_deployment_id": "dep-credit-v1",
            "input_data_hash": "credit-input-hash",
            "analysis_duration_ms": 640,
        },
    )
    assert credit["ok"] is True

    fraud_session = await gateway.call_tool(
        "start_agent_session",
        {
            "application_id": application_id,
            "agent_type": "fraud_detection",
            "session_id": "sess-fraud",
            "agent_id": "fraud-agent-1",
            "model_version": "fraud-v1",
            "langgraph_graph_version": "graph-v1",
            "context_source": "fresh",
            "context_token_count": 256,
        },
    )
    assert fraud_session["ok"] is True

    fraud = await gateway.call_tool(
        "record_fraud_screening",
        {
            "application_id": application_id,
            "session_id": "sess-fraud",
            "fraud_score": 0.08,
            "risk_level": "LOW",
            "anomalies_found": 0,
            "recommendation": "CLEAR",
            "screening_model_version": "fraud-v1",
            "input_data_hash": "fraud-input-hash",
        },
    )
    assert fraud["ok"] is True

    compliance_session = await gateway.call_tool(
        "start_agent_session",
        {
            "application_id": application_id,
            "agent_type": "compliance",
            "session_id": "sess-compliance",
            "agent_id": "compliance-agent-1",
            "model_version": "compliance-v1",
            "langgraph_graph_version": "graph-v1",
            "context_source": "fresh",
            "context_token_count": 300,
        },
    )
    assert compliance_session["ok"] is True

    compliance = await gateway.call_tool(
        "record_compliance_check",
        {
            "application_id": application_id,
            "session_id": "sess-compliance",
            "regulation_set_version": "2026-Q1",
            "rules_to_evaluate": ["REG-001", "REG-002", "REG-003", "REG-004", "REG-005", "REG-006"],
            "rule_results": [
                {"rule_id": "REG-001", "rule_name": "AML", "outcome": "PASS", "rule_version": "1", "evidence_hash": "e1"},
                {"rule_id": "REG-002", "rule_name": "OFAC", "outcome": "PASS", "rule_version": "1", "evidence_hash": "e2"},
                {"rule_id": "REG-003", "rule_name": "Jurisdiction", "outcome": "PASS", "rule_version": "1", "evidence_hash": "e3"},
                {"rule_id": "REG-004", "rule_name": "Entity Type", "outcome": "PASS", "rule_version": "1", "evidence_hash": "e4"},
                {"rule_id": "REG-005", "rule_name": "Operating History", "outcome": "PASS", "rule_version": "1", "evidence_hash": "e5"},
                {
                    "rule_id": "REG-006",
                    "rule_name": "CRA",
                    "outcome": "NOTE",
                    "rule_version": "1",
                    "evidence_hash": "e6",
                    "note_type": "CRA",
                    "note_text": "informational",
                },
            ],
            "model_version": "compliance-v1",
        },
    )
    assert compliance["ok"] is True

    orchestrator_session = await gateway.call_tool(
        "start_agent_session",
        {
            "application_id": application_id,
            "agent_type": "decision_orchestrator",
            "session_id": "sess-orch",
            "agent_id": "orch-agent-1",
            "model_version": "orch-v1",
            "langgraph_graph_version": "graph-v1",
            "context_source": "fresh",
            "context_token_count": 420,
        },
    )
    assert orchestrator_session["ok"] is True

    decision = await gateway.call_tool(
        "generate_decision",
        {
            "application_id": application_id,
            "session_id": "sess-orch",
            "recommendation": "APPROVE",
            "confidence": 0.82,
            "approved_amount_usd": "450000",
            "executive_summary": "Approve with standard terms",
            "key_risks": ["moderate leverage"],
            "contributing_sessions": ["sess-credit", "sess-fraud", "sess-compliance"],
            "model_versions": {"decision_orchestrator": "orch-v1"},
            "conditions": ["standard covenants"],
        },
    )
    assert decision["ok"] is True

    human_review = await gateway.call_tool(
        "record_human_review",
        {
            "application_id": application_id,
            "reviewer_id": "LO-Sarah-Chen",
            "override": False,
            "original_recommendation": "APPROVE",
            "final_decision": "APPROVE",
            "approved_amount_usd": "450000",
            "interest_rate_pct": 8.25,
            "term_months": 36,
            "conditions": ["standard covenants"],
        },
    )
    assert human_review["ok"] is True

    application_resource = await gateway.read_resource(f"ledger://applications/{application_id}")
    compliance_resource = await gateway.read_resource(f"ledger://applications/{application_id}/compliance")
    temporal_compliance = await gateway.read_resource(
        f"ledger://applications/{application_id}/compliance?as_of=2099-01-01T00:00:00+00:00"
    )
    performance_resource = await gateway.read_resource("ledger://agents/orch-agent-1/performance")
    session_resource = await gateway.read_resource("ledger://agents/orch-agent-1/sessions/sess-orch")
    health_resource = await gateway.read_resource("ledger://ledger/health")

    assert application_resource["data"]["state"] == "FINAL_APPROVED"
    assert application_resource["data"]["decision"] == "APPROVE"
    assert compliance_resource["data"]["overall_verdict"] == "CLEAR"
    assert temporal_compliance["data"]["completed"] is True
    assert len(performance_resource["data"]) == 1
    assert performance_resource["data"][0]["decisions_generated"] == 1
    assert session_resource["data"][-1]["event_type"] == "AgentSessionCompleted"
    assert "application_summary" in health_resource["data"]


@pytest.mark.asyncio
async def test_mcp_tools_return_structured_errors():
    store = InMemoryEventStore()
    await store.connect()
    gateway = await build_gateway_from_store(store)

    failed = await gateway.call_tool(
        "record_credit_analysis",
        {
            "application_id": "APEX-NOSESSION",
            "session_id": "missing",
            "risk_tier": "LOW",
            "recommended_limit_usd": "100000",
            "confidence": 0.8,
            "rationale": "should fail",
            "model_version": "credit-v1",
            "model_deployment_id": "dep",
            "input_data_hash": "hash",
            "analysis_duration_ms": 100,
        },
    )

    assert failed["ok"] is False
    assert failed["error"]["error_type"] == "DomainError"
    assert "suggested_action" in failed["error"]


@pytest.mark.asyncio
async def test_mcp_runtime_workflow_tool_and_company_catalog_resource(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline_module, "_try_docling_extract", lambda path: None)

    store = InMemoryEventStore()
    await store.connect()
    gateway = await build_gateway_from_store(store)

    catalog = await gateway.call_tool("list_document_companies")
    assert catalog["ok"] is True
    assert catalog["count"] >= 80

    catalog_resource = await gateway.read_resource("ledger://companies/catalog")
    assert len(catalog_resource["data"]) == catalog["count"]

    application_id = f"APEX-CLIENT-{uuid4().hex[:8].upper()}"
    workflow = await gateway.call_tool(
        "run_application_workflow",
        {
            "application_id": application_id,
            "company_id": "COMP-024",
            "phase": "credit",
        },
    )

    assert workflow["ok"] is True
    assert workflow["application_id"] == application_id
    assert workflow["final_event_type"] == "CreditAnalysisCompleted"
    assert workflow["profile_source"] in {"seed_profiles", "applicant_registry"}
