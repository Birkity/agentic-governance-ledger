from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib import error, request


@dataclass(frozen=True)
class AgentLLMResult:
    summary: str
    provider: str
    model: str
    called: bool
    tokens_input: int
    tokens_output: int
    cost_usd: float
    raw_response: dict[str, Any] | None = None


class AgentLLMBackend:
    provider = "deterministic"

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata: dict[str, Any] | None = None) -> AgentLLMResult:
        raise NotImplementedError


class DeterministicAgentLLM(AgentLLMBackend):
    provider = "deterministic"

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata: dict[str, Any] | None = None) -> AgentLLMResult:
        metadata = metadata or {}
        summary_parts = []
        if metadata.get("application_id"):
            summary_parts.append(f"application={metadata['application_id']}")
        if metadata.get("company_id"):
            summary_parts.append(f"company={metadata['company_id']}")
        if metadata.get("stage"):
            summary_parts.append(f"stage={metadata['stage']}")
        if metadata.get("highlights"):
            summary_parts.extend(str(item) for item in metadata["highlights"])

        summary = "; ".join(summary_parts) if summary_parts else user_prompt[:220]
        return AgentLLMResult(
            summary=summary,
            provider=self.provider,
            model="deterministic-fallback",
            called=False,
            tokens_input=0,
            tokens_output=0,
            cost_usd=0.0,
        )


class OllamaAgentLLM(AgentLLMBackend):
    provider = "ollama"

    def __init__(self, *, base_url: str | None = None, model: str | None = None, timeout_seconds: int = 90):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or os.getenv("OLLAMA_AGENT_MODEL") or os.getenv("OLLAMA_PACKAGE_MODEL") or "qwen3:latest"
        self.timeout_seconds = timeout_seconds

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata: dict[str, Any] | None = None) -> AgentLLMResult:
        prompt = f"{system_prompt.strip()}\n\n{user_prompt.strip()}".strip()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, OSError):
            fallback = DeterministicAgentLLM()
            return await fallback.infer(system_prompt=system_prompt, user_prompt=user_prompt, metadata=metadata)

        prompt_eval = int(raw.get("prompt_eval_count") or 0)
        eval_count = int(raw.get("eval_count") or 0)
        return AgentLLMResult(
            summary=str(raw.get("response", "")).strip(),
            provider=self.provider,
            model=self.model,
            called=True,
            tokens_input=prompt_eval,
            tokens_output=eval_count,
            cost_usd=0.0,
            raw_response=raw,
        )


class AnthropicAgentLLM(AgentLLMBackend):
    provider = "anthropic"

    def __init__(self, *, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("LEDGER_AGENT_MODEL") or "claude-3-5-sonnet-20241022"

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata: dict[str, Any] | None = None) -> AgentLLMResult:
        if not self.api_key:
            fallback = DeterministicAgentLLM()
            return await fallback.infer(system_prompt=system_prompt, user_prompt=user_prompt, metadata=metadata)

        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            fallback = DeterministicAgentLLM()
            return await fallback.infer(system_prompt=system_prompt, user_prompt=user_prompt, metadata=metadata)

        client = AsyncAnthropic(api_key=self.api_key)
        response = await client.messages.create(
            model=self.model,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text_parts = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))
        input_tokens = int(getattr(response.usage, "input_tokens", 0))
        output_tokens = int(getattr(response.usage, "output_tokens", 0))
        return AgentLLMResult(
            summary="\n".join(part.strip() for part in text_parts if part.strip()),
            provider=self.provider,
            model=self.model,
            called=True,
            tokens_input=input_tokens,
            tokens_output=output_tokens,
            cost_usd=0.0,
            raw_response=response.model_dump(mode="json"),
        )


def build_llm_backend(preferred: str | None = None) -> AgentLLMBackend:
    choice = (preferred or os.getenv("LEDGER_AGENT_BACKEND") or "").strip().lower()
    if choice == "anthropic":
        return AnthropicAgentLLM()
    if choice == "ollama":
        return OllamaAgentLLM()
    if choice == "deterministic":
        return DeterministicAgentLLM()

    if os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicAgentLLM()
    if os.getenv("OLLAMA_BASE_URL"):
        return OllamaAgentLLM()
    return DeterministicAgentLLM()


__all__ = [
    "AgentLLMBackend",
    "AgentLLMResult",
    "AnthropicAgentLLM",
    "DeterministicAgentLLM",
    "OllamaAgentLLM",
    "build_llm_backend",
]
