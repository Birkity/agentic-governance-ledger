from __future__ import annotations

import pytest

import src.document_processing.pipeline as pipeline_module
from src.agents import LedgerAgentRuntime
from src.agents.llm import AgentLLMBackend, AgentLLMResult
from src.aggregates import LoanApplicationAggregate
from src.event_store import InMemoryEventStore
from src.models.events import DomainError
from src.outbox import OutboxPublisher


class FakeAgentLLM(AgentLLMBackend):
    provider = "test"

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata=None) -> AgentLLMResult:
        stage = str((metadata or {}).get("stage", "unknown"))
        return AgentLLMResult(
            summary=f"{stage} summary",
            provider=self.provider,
            model=f"test-{stage}",
            called=True,
            tokens_input=120,
            tokens_output=30,
            cost_usd=0.0125,
        )


@pytest.mark.asyncio
async def test_outbox_publisher_marks_pending_records_as_published():
    store = InMemoryEventStore()
    await store.append(
        "loan-APEX-OUTBOX",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": "APEX-OUTBOX"},
            }
        ],
        expected_version=-1,
        metadata={"outbox_destinations": ["ledger.downstream"]},
    )

    pending = await store.list_outbox_pending(destination="ledger.downstream")
    assert len(pending) == 1

    published_ids: list[int] = []

    async def capture(record):
        published_ids.append(record.id)

    result = await OutboxPublisher(store).publish_pending(capture, destination="ledger.downstream")

    assert result["published"] == 1
    assert published_ids == [pending[0].id]
    assert await store.list_outbox_pending(destination="ledger.downstream") == []


@pytest.mark.asyncio
async def test_langgraph_runtime_creates_reviewable_application_and_snapshot(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline_module, "_try_docling_extract", lambda path: None)

    store = InMemoryEventStore()
    runtime = LedgerAgentRuntime(store, llm_backend=FakeAgentLLM())
    application_id = "APEX-CLIENT-RUNTIME-001"

    result = await runtime.start_application(
        application_id,
        "COMP-024",
        phase="full",
        auto_finalize_human_review=False,
    )

    assert result["application_id"] == application_id
    assert result["final_event_type"] == "HumanReviewRequested"
    assert result["requires_human_review"] is True

    loan_events = await store.load_stream(f"loan-{application_id}")
    assert loan_events[-1].event_type == "HumanReviewRequested"

    session_streams = sorted(
        stream_id for stream_id in getattr(store, "_streams", {}) if stream_id.startswith("agent-")
    )
    assert len(session_streams) == 5
    for stream_id in session_streams:
        events = await store.load_stream(stream_id)
        assert events[0].event_type == "AgentSessionStarted"

    snapshot = await store.load_latest_snapshot(f"loan-{application_id}")
    assert snapshot is not None
    assert snapshot.aggregate_type == "LoanApplication"
    assert snapshot.stream_position == loan_events[-1].stream_position
    assert snapshot.snapshot_version == 1

    pending = await store.list_outbox_pending(destination="ledger.downstream")
    assert pending

    review_result = await runtime.complete_human_review(
        application_id,
        reviewer_id="loan-ops",
        final_decision="DECLINE",
        decline_reasons=["Manual decline confirmed"],
        adverse_action_codes=["MANUAL-DECLINE"],
    )

    assert review_result["final_decision"] == "DECLINE"

    loan_events = await store.load_stream(f"loan-{application_id}")
    assert loan_events[-2].event_type == "HumanReviewCompleted"
    assert loan_events[-1].event_type == "ApplicationDeclined"


@pytest.mark.asyncio
async def test_malformed_legacy_loan_stream_raises_domain_error_on_replay():
    store = InMemoryEventStore()
    application_id = "APEX-LEGACY-BROKEN-001"

    await store.append(
        f"loan-{application_id}",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": application_id},
            }
        ],
        expected_version=-1,
    )

    with pytest.raises(DomainError, match="Malformed event ApplicationSubmitted.*applicant_id"):
        await LoanApplicationAggregate.load(store, application_id)


@pytest.mark.asyncio
async def test_runtime_review_rejects_malformed_legacy_application_history():
    store = InMemoryEventStore()
    runtime = LedgerAgentRuntime(store, llm_backend=FakeAgentLLM())
    application_id = "APEX-LEGACY-BROKEN-REVIEW"

    await store.append(
        f"loan-{application_id}",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": application_id},
            },
            {
                "event_type": "DecisionGenerated",
                "event_version": 2,
                "payload": {"marker": "legacy"},
            },
        ],
        expected_version=-1,
    )

    with pytest.raises(DomainError, match="Malformed event ApplicationSubmitted.*applicant_id"):
        await runtime.complete_human_review(
            application_id,
            reviewer_id="loan-ops",
            final_decision="APPROVE",
            approved_amount_usd=None,
        )


@pytest.mark.asyncio
async def test_runtime_review_rejects_decision_events_missing_recommendation():
    store = InMemoryEventStore()
    runtime = LedgerAgentRuntime(store, llm_backend=FakeAgentLLM())
    application_id = "APEX-LEGACY-BROKEN-DECISION"

    await store.append(
        f"loan-{application_id}",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "applicant_id": "COMP-024",
                    "requested_amount_usd": "450000",
                    "loan_term_months": 36,
                    "loan_purpose": "working_capital",
                    "submission_channel": "portal",
                    "application_reference": application_id,
                },
            },
            {
                "event_type": "DecisionGenerated",
                "event_version": 2,
                "payload": {"model_versions": {}},
            },
        ],
        expected_version=-1,
    )

    with pytest.raises(DomainError, match="Malformed event DecisionGenerated.*recommendation"):
        await runtime.complete_human_review(
            application_id,
            reviewer_id="loan-ops",
            final_decision="APPROVE",
            approved_amount_usd=None,
        )
