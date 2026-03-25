from __future__ import annotations

import pytest

from src.agents.cost_reporting import collect_llm_costs
from src.event_store import InMemoryEventStore
from src.models.events import AgentNodeExecuted, AgentSessionStarted, AgentType


@pytest.mark.asyncio
async def test_collect_llm_costs_aggregates_live_agent_telemetry():
    store = InMemoryEventStore()
    stream_id = "agent-credit_analysis-sess-cost"
    application_id = "APEX-LIVE-COST-001"
    await store.append(
        stream_id,
        [
            AgentSessionStarted(
                session_id="sess-cost",
                agent_type=AgentType.CREDIT_ANALYSIS,
                agent_id="credit-comp-024",
                application_id=application_id,
                model_version="openai/gpt-4.1",
                langgraph_graph_version="graph-v2",
                context_source="applicant_registry",
                context_token_count=128,
                started_at="2026-03-25T12:00:00+00:00",
            ).to_store_dict(),
            AgentNodeExecuted(
                session_id="sess-cost",
                agent_type=AgentType.CREDIT_ANALYSIS,
                node_name="reason_credit",
                node_sequence=5,
                input_keys=["company_context"],
                output_keys=["credit_decision"],
                llm_called=True,
                llm_provider="openrouter",
                llm_model="openai/gpt-4.1",
                llm_tokens_input=600,
                llm_tokens_output=150,
                llm_cost_usd=0.012345,
                duration_ms=700,
                executed_at="2026-03-25T12:00:01+00:00",
            ).to_store_dict(),
        ],
        expected_version=-1,
    )

    summary = await collect_llm_costs(store, application_ids=[application_id])

    assert summary["application_count"] == 1
    assert summary["provider_calls_observed"] == 1
    assert summary["billable_calls_observed"] == 1
    assert summary["total_cost_usd"] == pytest.approx(0.012345)
    assert summary["totals_by_agent"]["credit_analysis"] == pytest.approx(0.012345)
    assert summary["totals_by_model"]["openai/gpt-4.1"] == pytest.approx(0.012345)
    assert summary["totals_by_provider"]["openrouter"] == pytest.approx(0.012345)
