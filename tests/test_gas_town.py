from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.event_store import InMemoryEventStore
from src.integrity import reconstruct_agent_context
from src.models.events import AgentType


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_reconstruct_agent_context_recovers_session_progress():
    store = InMemoryEventStore()
    session_id = f"sess-{uuid4().hex[:6]}"
    stream_id = f"agent-credit_analysis-{session_id}"

    await store.append(
        stream_id,
        [
            {
                "event_type": "AgentSessionStarted",
                "event_version": 1,
                "payload": {
                    "session_id": session_id,
                    "agent_type": AgentType.CREDIT_ANALYSIS.value,
                    "agent_id": "credit-agent-1",
                    "application_id": "APEX-GAS-001",
                    "model_version": "credit-v1",
                    "langgraph_graph_version": "graph-v1",
                    "context_source": "fresh",
                    "context_token_count": 1200,
                    "started_at": _now().isoformat(),
                },
            },
            {
                "event_type": "AgentNodeExecuted",
                "event_version": 1,
                "payload": {
                    "session_id": session_id,
                    "agent_type": AgentType.CREDIT_ANALYSIS.value,
                    "node_name": "load_registry_context",
                    "node_sequence": 1,
                    "input_keys": ["application_id"],
                    "output_keys": ["registry_profile"],
                    "llm_called": False,
                    "duration_ms": 180,
                    "executed_at": _now().isoformat(),
                },
            },
            {
                "event_type": "AgentToolCalled",
                "event_version": 1,
                "payload": {
                    "session_id": session_id,
                    "agent_type": AgentType.CREDIT_ANALYSIS.value,
                    "tool_name": "registry_lookup",
                    "tool_input_summary": "application_id",
                    "tool_output_summary": "profile loaded",
                    "tool_duration_ms": 90,
                    "called_at": _now().isoformat(),
                },
            },
            {
                "event_type": "AgentNodeExecuted",
                "event_version": 1,
                "payload": {
                    "session_id": session_id,
                    "agent_type": AgentType.CREDIT_ANALYSIS.value,
                    "node_name": "score_credit",
                    "node_sequence": 2,
                    "input_keys": ["registry_profile", "financial_facts"],
                    "output_keys": ["credit_decision"],
                    "llm_called": True,
                    "llm_tokens_input": 420,
                    "llm_tokens_output": 110,
                    "duration_ms": 640,
                    "executed_at": _now().isoformat(),
                },
            },
            {
                "event_type": "AgentOutputWritten",
                "event_version": 1,
                "payload": {
                    "session_id": session_id,
                    "agent_type": AgentType.CREDIT_ANALYSIS.value,
                    "application_id": "APEX-GAS-001",
                    "events_written": [
                        {
                            "stream_id": "credit-APEX-GAS-001",
                            "event_type": "CreditAnalysisCompleted",
                            "stream_position": 1,
                        }
                    ],
                    "output_summary": "credit analysis persisted",
                    "written_at": _now().isoformat(),
                },
            },
        ],
        expected_version=-1,
    )

    context = await reconstruct_agent_context(store, AgentType.CREDIT_ANALYSIS.value, session_id, token_budget=4000)

    assert context.needs_reconciliation is False
    assert context.application_id == "APEX-GAS-001"
    assert context.model_version == "credit-v1"
    assert context.last_successful_node == "score_credit"
    assert context.next_node_sequence == 3
    assert context.output_events_written[0]["event_type"] == "CreditAnalysisCompleted"
    assert context.token_budget_remaining == 2800


@pytest.mark.asyncio
async def test_reconstruct_agent_context_flags_missing_anchor():
    store = InMemoryEventStore()
    session_id = f"sess-{uuid4().hex[:6]}"
    stream_id = f"agent-credit_analysis-{session_id}"

    await store.append(
        stream_id,
        [
            {
                "event_type": "AgentNodeExecuted",
                "event_version": 1,
                "payload": {
                    "session_id": session_id,
                    "agent_type": AgentType.CREDIT_ANALYSIS.value,
                    "node_name": "score_credit",
                    "node_sequence": 1,
                    "input_keys": [],
                    "output_keys": ["credit_decision"],
                    "llm_called": True,
                    "duration_ms": 500,
                    "executed_at": _now().isoformat(),
                },
            }
        ],
        expected_version=-1,
    )

    context = await reconstruct_agent_context(store, AgentType.CREDIT_ANALYSIS.value, session_id)

    assert context.needs_reconciliation is True
    assert "missing_gas_town_anchor" in context.reconciliation_reasons
