from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from src.event_store import EventStore
from src.models.events import AgentSessionStarted, AgentType
from src.upcasting import build_default_registry


DB_URL = os.environ.get("TEST_DB_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="Set TEST_DB_URL to run PostgreSQL upcasting tests")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_payload(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


@pytest_asyncio.fixture
async def store() -> EventStore:
    event_store = EventStore(DB_URL, upcaster_registry=build_default_registry())
    await event_store.connect()
    yield event_store
    await event_store.close()


@pytest.mark.asyncio
async def test_credit_analysis_upcast_leaves_raw_row_immutable(store: EventStore):
    application_id = f"APEX-UPCAST-{uuid4().hex[:8].upper()}"
    stream_id = f"credit-{application_id}"
    recorded_at = datetime(2026, 2, 10, tzinfo=timezone.utc)
    original_payload = {
        "application_id": application_id,
        "session_id": "sess-credit-legacy",
        "decision": {
            "risk_tier": "MEDIUM",
            "recommended_limit_usd": "250000",
            "confidence": 0.71,
            "rationale": "Legacy analyzer output",
            "key_concerns": [],
            "data_quality_caveats": [],
            "policy_overrides_applied": [],
        },
        "model_deployment_id": "legacy-deployment",
        "input_data_hash": "legacy-credit-hash",
        "analysis_duration_ms": 780,
        "completed_at": recorded_at.isoformat(),
    }

    await store.append(
        stream_id,
        [
            {
                "event_type": "CreditAnalysisCompleted",
                "event_version": 1,
                "payload": original_payload,
                "recorded_at": recorded_at,
            }
        ],
        expected_version=-1,
    )

    async with store._pool.acquire() as conn:  # type: ignore[union-attr]
        raw_before = await conn.fetchrow(
            """
            SELECT event_version, payload
            FROM events
            WHERE stream_id = $1 AND stream_position = 1
            """,
            stream_id,
        )

    loaded = await store.load_stream(stream_id)
    upcasted = loaded[0]
    assert upcasted.event_version == 2
    assert upcasted.payload["model_version"] == "credit-v1"
    assert upcasted.payload["regulatory_basis"] == [
        "APEX-CREDIT-POLICY-2026.1",
        "US-GAAP-BASELINE-2026.1",
    ]

    async with store._pool.acquire() as conn:  # type: ignore[union-attr]
        raw_after = await conn.fetchrow(
            """
            SELECT event_version, payload
            FROM events
            WHERE stream_id = $1 AND stream_position = 1
            """,
            stream_id,
        )

    assert raw_before["event_version"] == 1
    assert raw_after["event_version"] == 1
    assert _normalize_payload(raw_before["payload"]) == original_payload
    assert _normalize_payload(raw_after["payload"]) == original_payload


@pytest.mark.asyncio
async def test_decision_generated_upcast_reconstructs_model_versions_from_sessions(store: EventStore):
    application_id = f"APEX-UPCAST-{uuid4().hex[:8].upper()}"
    suffix = uuid4().hex[:6]
    orchestrator_session_id = f"sess-orch-{suffix}"
    credit_session_id = f"sess-credit-{suffix}"
    fraud_session_id = f"sess-fraud-{suffix}"
    orchestrator_session_stream = f"agent-decision_orchestrator-{orchestrator_session_id}"
    credit_session_stream = f"agent-credit_analysis-{credit_session_id}"
    fraud_session_stream = f"agent-fraud_detection-{fraud_session_id}"

    for stream_id, session_id, agent_type, model_version in [
        (orchestrator_session_stream, orchestrator_session_id, AgentType.DECISION_ORCHESTRATOR, "orch-v1"),
        (credit_session_stream, credit_session_id, AgentType.CREDIT_ANALYSIS, "credit-v1"),
        (fraud_session_stream, fraud_session_id, AgentType.FRAUD_DETECTION, "fraud-v2"),
    ]:
        event = AgentSessionStarted(
            session_id=session_id,
            agent_type=agent_type,
            agent_id=f"{agent_type.value}-agent-1",
            application_id=application_id,
            model_version=model_version,
            langgraph_graph_version="graph-v1",
            context_source="fresh",
            context_token_count=512,
            started_at=_now(),
        ).to_store_dict()
        await store.append(stream_id, [event], expected_version=-1)

    loan_stream = f"loan-{application_id}"
    await store.append(
        loan_stream,
        [
            {
                "event_type": "DecisionGenerated",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "orchestrator_session_id": "sess-orch-v1",
                    "recommendation": "APPROVE",
                    "confidence": 0.88,
                    "approved_amount_usd": str(Decimal("200000")),
                    "conditions": ["standard covenants"],
                    "executive_summary": "Legacy orchestrator output",
                    "key_risks": ["customer concentration"],
                    "contributing_sessions": [
                        credit_session_stream,
                        fraud_session_stream,
                        orchestrator_session_stream,
                    ],
                    "generated_at": _now().isoformat(),
                },
            }
        ],
        expected_version=-1,
    )

    loaded = await store.load_stream(loan_stream)
    upcasted = loaded[0]

    assert upcasted.event_version == 2
    assert upcasted.payload["model_versions"] == {
        "credit_analysis": "credit-v1",
        "fraud_detection": "fraud-v2",
        "decision_orchestrator": "orch-v1",
    }
