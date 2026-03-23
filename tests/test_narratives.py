from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.demo import (
    narr01_occ_collision,
    narr02_missing_ebitda,
    narr03_crash_recovery,
    narr04_montana_hard_block,
    narr05_human_override,
)
from src.event_store import InMemoryEventStore, OptimisticConcurrencyError
from src.regulatory import generate_regulatory_package, verify_regulatory_package


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_narr01_occ_collision_guardrail():
    store = InMemoryEventStore()
    result = await narr01_occ_collision(store)

    assert result["successful_appends"] == 1
    assert result["optimistic_concurrency_failures"] == 1
    assert result["final_stream_length"] == 4
    assert result["final_stream_positions"] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_narr02_missing_ebitda_is_backfilled_and_lowers_confidence():
    store = InMemoryEventStore()
    result = await narr02_missing_ebitda(store)

    assert any("ebitda" in note.lower() for note in result["income_notes"])
    assert result["merged_ebitda"] not in {None, "None"}
    assert result["ebitda_source"] in {"financial_workbook", "financial_summary"}
    assert float(result["credit_confidence"]) < 0.80
    assert "data_quality_caveats" in result["credit_rationale"]


@pytest.mark.asyncio
async def test_narr03_recovery_resumes_without_duplicate_load_facts():
    store = InMemoryEventStore()
    result = await narr03_crash_recovery(store)

    fraud_completed = [event for event in result["fraud_events"] if event["event_type"] == "FraudScreeningCompleted"]
    first_nodes = [event for event in result["first_session_events"] if event["event_type"] == "AgentNodeExecuted"]
    second_nodes = [event for event in result["second_session_events"] if event["event_type"] == "AgentNodeExecuted"]
    recovered = [event for event in result["second_session_events"] if event["event_type"] == "AgentSessionRecovered"]
    load_facts_total = [
        event
        for event in first_nodes + second_nodes
        if event["payload"].get("node_name") == "load_facts"
    ]

    assert len(fraud_completed) == 1
    assert result["reconstructed_context"]["last_successful_node"] == "load_facts"
    assert result["reconstructed_context"]["failed"] is True
    assert result["second_session_events"][0]["payload"]["context_source"].startswith("prior_session_replay:")
    assert recovered
    assert len(load_facts_total) == 1


@pytest.mark.asyncio
async def test_narr04_montana_hard_block_stops_before_decision():
    store = InMemoryEventStore()
    result = await narr04_montana_hard_block(store)

    rule_events = [
        event for event in result["compliance_events"] if event["event_type"] in {"ComplianceRulePassed", "ComplianceRuleFailed"}
    ]
    rule_ids = [event["payload"]["rule_id"] for event in rule_events]
    completed = next(event for event in result["compliance_events"] if event["event_type"] == "ComplianceCheckCompleted")
    declined = next(event for event in result["loan_events"] if event["event_type"] == "ApplicationDeclined")

    assert rule_ids == ["REG-001", "REG-002", "REG-003"]
    assert completed["payload"]["overall_verdict"] == "BLOCKED"
    assert not any(event["event_type"] == "DecisionGenerated" for event in result["loan_events"])
    assert any("REG-003" in reason for reason in declined["payload"]["decline_reasons"])
    assert declined["payload"]["adverse_action_notice_required"] is True


@pytest.mark.asyncio
async def test_narr05_human_override_and_regulatory_package_are_consistent():
    store = InMemoryEventStore()
    result = await narr05_human_override(store)
    loan_events = result["loan_events"]

    decision = next(event for event in loan_events if event["event_type"] == "DecisionGenerated")
    review_requested = next(event for event in loan_events if event["event_type"] == "HumanReviewRequested")
    human_review = next(event for event in loan_events if event["event_type"] == "HumanReviewCompleted")
    approval = next(event for event in loan_events if event["event_type"] == "ApplicationApproved")

    package = await generate_regulatory_package(store, "APEX-NARR05", examination_date=_now())
    verification = verify_regulatory_package(package)

    assert decision["payload"]["recommendation"] == "DECLINE"
    assert review_requested["payload"]["reason"]
    assert human_review["payload"]["override"] is True
    assert human_review["payload"]["reviewer_id"] == "LO-Sarah-Chen"
    assert Decimal(approval["payload"]["approved_amount_usd"]) == Decimal("750000")
    assert len(approval["payload"]["conditions"]) == 2
    assert verification.ok is True
