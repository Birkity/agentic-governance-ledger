from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.document_processing.pipeline as pipeline_module
from src.agents.llm import AgentLLMBackend, AgentLLMResult
from src.demo import build_api_cost_report, generate_narr05_artifacts, generate_runtime_narr05_artifacts, run_document_phase
from src.event_store import InMemoryEventStore
from src.regulatory import verify_regulatory_package


class FakeAgentLLM(AgentLLMBackend):
    provider = "test"

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata=None) -> AgentLLMResult:
        stage = str((metadata or {}).get("stage", "unknown"))
        return AgentLLMResult(
            summary=f"{stage} summary",
            provider=self.provider,
            model=f"test-{stage}",
            called=True,
            tokens_input=100,
            tokens_output=20,
            cost_usd=0.01,
        )


@pytest.mark.asyncio
async def test_run_document_phase_records_document_processing_session():
    store = InMemoryEventStore()

    result = await run_document_phase(store, "APEX-DOC-DEMO", "COMP-024")
    session_events = await store.load_stream("agent-document_processing-sess-doc")

    assert result["positions"]
    assert result["document_session_stream_id"] == "agent-document_processing-sess-doc"
    assert session_events[0].event_type == "AgentSessionStarted"
    assert session_events[-1].event_type == "AgentSessionCompleted"
    assert any(event.event_type == "AgentNodeExecuted" for event in session_events)
    assert any(event.event_type == "AgentOutputWritten" for event in session_events)


@pytest.mark.asyncio
async def test_api_cost_report_is_event_based_and_complete(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline_module, "_try_docling_extract", lambda path: None)

    report = await build_api_cost_report()

    assert "honest external billing measurement is $0.00" in report
    assert "Applications processed: 29 seed + 5 narrative = 34 total" in report
    assert "Actual measured external API cost:" in report
    assert "provider calls observed in reproduced workflow: 0" in report
    assert "Workflow telemetry cost proxy:" in report
    assert "DocProc" in report
    assert "Orchestrator" in report
    assert "Most expensive single telemetry call:" in report


@pytest.mark.asyncio
async def test_generate_narr05_artifacts_writes_verifiable_outputs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline_module, "_try_docling_extract", lambda path: None)

    output_dir = Path(".tmp") / "submission_artifact_test"
    output_dir.mkdir(parents=True, exist_ok=True)
    result = await generate_narr05_artifacts(output_dir)

    package_document = json.loads((output_dir / "regulatory_package_NARR05.json").read_text(encoding="utf-8"))
    verification = verify_regulatory_package(package_document["package"])

    assert verification.ok is True
    assert (output_dir / "counterfactual_narr05.json").exists()
    assert (output_dir / "api_cost_report.txt").exists()
    assert "regulatory_package_NARR05.json" in result["package_path"]


@pytest.mark.asyncio
async def test_generate_runtime_narr05_artifacts_writes_runtime_backed_outputs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline_module, "_try_docling_extract", lambda path: None)

    output_dir = Path(".tmp") / "submission_runtime_artifact_test"
    output_dir.mkdir(parents=True, exist_ok=True)
    store = InMemoryEventStore()

    result = await generate_runtime_narr05_artifacts(
        store,
        output_dir,
        application_id="APEX-NARR05-RUNTIME-TEST",
        llm_backend=FakeAgentLLM(),
    )

    package_document = json.loads((output_dir / "regulatory_package_NARR05.json").read_text(encoding="utf-8"))
    verification = verify_regulatory_package(package_document["package"])
    loan_events = await store.load_stream("loan-APEX-NARR05-RUNTIME-TEST")
    submitted = next(event for event in loan_events if event.event_type == "ApplicationSubmitted")

    assert verification.ok is True
    assert (output_dir / "counterfactual_narr05.json").exists()
    assert (output_dir / "api_cost_report.txt").exists()
    assert any(event.event_type == "HumanReviewCompleted" for event in loan_events)
    assert loan_events[-1].event_type == "ApplicationApproved"
    assert str(submitted.payload["requested_amount_usd"]) == "950000"
    assert result["workflow"]["final_event_type"] == "HumanReviewRequested"
    assert result["review"]["final_decision"] == "APPROVE"


def test_final_report_source_and_build_script_exist():
    assert Path("reports/final_submission.tex").exists()
    assert Path("scripts/build_final_report.ps1").exists()
