from __future__ import annotations

import json

import pytest

import src.agents.llm as llm_module
from src.agents.llm import OpenRouterAgentLLM, build_llm_backend


def test_build_llm_backend_routes_critical_stages_to_openrouter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LEDGER_AGENT_BACKEND", "deterministic")
    monkeypatch.setenv("LEDGER_CRUCIAL_AGENT_BACKEND", "openrouter")
    monkeypatch.setenv("api_model", "openai/gpt-4.1")

    backend = build_llm_backend()

    assert backend.describe("document_processing").provider == "deterministic"
    assert backend.describe("credit_analysis").provider == "openrouter"
    assert backend.describe("fraud_detection").provider == "openrouter"
    assert backend.describe("decision_orchestrator").model == "openai/gpt-4.1"


@pytest.mark.asyncio
async def test_openrouter_backend_parses_summary_usage_and_cost(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "model": "openai/gpt-4.1",
                    "choices": [
                        {
                            "message": {
                                "content": "Credit summary from OpenRouter",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 321,
                        "completion_tokens": 87,
                        "cost": 0.004321,
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(llm_module.request, "urlopen", lambda req, timeout=0: FakeResponse())

    backend = OpenRouterAgentLLM(api_key="test-key", model="openai/gpt-4.1")
    result = await backend.infer(
        system_prompt="system",
        user_prompt="user",
        metadata={"stage": "credit_analysis"},
    )

    assert result.called is True
    assert result.provider == "openrouter"
    assert result.model == "openai/gpt-4.1"
    assert result.summary == "Credit summary from OpenRouter"
    assert result.tokens_input == 321
    assert result.tokens_output == 87
    assert result.cost_usd == pytest.approx(0.004321)
