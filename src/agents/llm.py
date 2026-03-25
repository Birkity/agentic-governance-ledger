from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from dotenv import dotenv_values, load_dotenv


_ENV_LOADED = False
_DOTENV_VALUES: dict[str, str] = {}

_CRITICAL_STAGES = {
    "credit_analysis",
    "fraud_detection",
    "decision_orchestrator",
}

_STAGE_ENV_KEYS = {
    "document_processing": "DOCUMENT_AGENT",
    "credit_analysis": "CREDIT_AGENT",
    "fraud_detection": "FRAUD_AGENT",
    "decision_orchestrator": "DECISION_AGENT",
}


def _ensure_env_loaded() -> None:
    global _ENV_LOADED, _DOTENV_VALUES
    if not _ENV_LOADED:
        load_dotenv()
        # Keep a direct copy of .env defaults so lookups remain stable even if
        # tests temporarily override and then unset process env vars.
        _DOTENV_VALUES = {
            key: value
            for key, value in dotenv_values().items()
            if isinstance(value, str) and value.strip()
        }
        _ENV_LOADED = True


def _env(*names: str) -> str | None:
    _ensure_env_loaded()
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
        dot_value = _DOTENV_VALUES.get(name)
        if dot_value is not None and dot_value.strip():
            return dot_value.strip()
    return None


def _stage_env_name(stage: str, suffix: str) -> str:
    key = _STAGE_ENV_KEYS[stage]
    return f"LEDGER_{key}_{suffix}"


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


@dataclass(frozen=True)
class AgentLLMDescriptor:
    provider: str
    model: str


class AgentLLMBackend:
    provider = "deterministic"

    def describe(self, stage: str | None = None) -> AgentLLMDescriptor:
        return AgentLLMDescriptor(provider=self.provider, model=getattr(self, "model", "deterministic-fallback"))

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata: dict[str, Any] | None = None) -> AgentLLMResult:
        raise NotImplementedError


class DeterministicAgentLLM(AgentLLMBackend):
    provider = "deterministic"
    model = "deterministic-fallback"

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
            model=self.model,
            called=False,
            tokens_input=0,
            tokens_output=0,
            cost_usd=0.0,
        )


class OllamaAgentLLM(AgentLLMBackend):
    provider = "ollama"

    def __init__(self, *, base_url: str | None = None, model: str | None = None, timeout_seconds: int = 90):
        self.base_url = (base_url or _env("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or _env("OLLAMA_AGENT_MODEL", "OLLAMA_PACKAGE_MODEL") or "qwen3:latest"
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
        self.api_key = api_key or _env("ANTHROPIC_API_KEY")
        self.model = model or _env("LEDGER_AGENT_MODEL") or "claude-3-5-sonnet-20241022"

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


class OpenRouterAgentLLM(AgentLLMBackend):
    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 120,
        http_referer: str | None = None,
        app_name: str | None = None,
    ):
        self.api_key = api_key or _env("OPENROUTER_API_KEY", "api_key")
        self.base_url = (base_url or _env("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
        self.model = model or _env("OPENROUTER_MODEL", "OPENROUTER_AGENT_MODEL", "LEDGER_AGENT_MODEL", "api_model", "API_MODEL") or "openai/gpt-4.1"
        self.timeout_seconds = timeout_seconds
        self.http_referer = http_referer or _env("OPENROUTER_HTTP_REFERER", "OPENROUTER_SITE_URL")
        self.app_name = app_name or _env("OPENROUTER_APP_NAME", "OPENROUTER_SITE_NAME", "OPENROUTER_X_TITLE")

    @staticmethod
    def _extract_message_text(raw: dict[str, Any]) -> str:
        choices = raw.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text", "")).strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts)
        return ""

    @staticmethod
    def _extract_usage_cost(raw: dict[str, Any]) -> float:
        usage = raw.get("usage") or {}
        if usage.get("cost") is not None:
            return float(usage["cost"])
        cost_details = usage.get("cost_details") or {}
        prompt_cost = float(cost_details.get("prompt") or 0.0)
        completion_cost = float(cost_details.get("completion") or 0.0)
        return round(prompt_cost + completion_cost, 10)

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata: dict[str, Any] | None = None) -> AgentLLMResult:
        if not self.api_key:
            fallback = DeterministicAgentLLM()
            return await fallback.infer(system_prompt=system_prompt, user_prompt=user_prompt, metadata=metadata)

        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 400,
            }
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.http_referer:
            headers["HTTP-Referer"] = self.http_referer
        if self.app_name:
            headers["X-Title"] = self.app_name
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (error.HTTPError, error.URLError, TimeoutError, OSError):
            fallback = DeterministicAgentLLM()
            return await fallback.infer(system_prompt=system_prompt, user_prompt=user_prompt, metadata=metadata)

        usage = raw.get("usage") or {}
        return AgentLLMResult(
            summary=self._extract_message_text(raw),
            provider=self.provider,
            model=str(raw.get("model") or self.model),
            called=True,
            tokens_input=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            tokens_output=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            cost_usd=round(self._extract_usage_cost(raw), 10),
            raw_response=raw,
        )


class StageAwareAgentLLM(AgentLLMBackend):
    provider = "stage-router"

    def __init__(self, *, default_backend: AgentLLMBackend, stage_backends: dict[str, AgentLLMBackend] | None = None):
        self.default_backend = default_backend
        self.stage_backends = dict(stage_backends or {})

    def backend_for_stage(self, stage: str | None) -> AgentLLMBackend:
        if stage and stage in self.stage_backends:
            return self.stage_backends[stage]
        return self.default_backend

    def describe(self, stage: str | None = None) -> AgentLLMDescriptor:
        return self.backend_for_stage(stage).describe(stage)

    async def infer(self, *, system_prompt: str, user_prompt: str, metadata: dict[str, Any] | None = None) -> AgentLLMResult:
        metadata = metadata or {}
        return await self.backend_for_stage(str(metadata.get("stage") or "")).infer(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            metadata=metadata,
        )


def _build_single_backend(
    choice: str | None,
    *,
    stage: str | None = None,
    model: str | None = None,
) -> AgentLLMBackend:
    normalized = (choice or "").strip().lower()
    if normalized == "anthropic":
        return AnthropicAgentLLM(model=model)
    if normalized == "ollama":
        if stage == "document_processing":
            return OllamaAgentLLM(model=model or _env(_stage_env_name(stage, "MODEL"), "OLLAMA_PACKAGE_MODEL", "OLLAMA_AGENT_MODEL"))
        if stage:
            return OllamaAgentLLM(model=model or _env(_stage_env_name(stage, "MODEL"), "OLLAMA_AGENT_MODEL", "OLLAMA_PACKAGE_MODEL"))
        return OllamaAgentLLM(model=model or _env("OLLAMA_AGENT_MODEL", "OLLAMA_PACKAGE_MODEL"))
    if normalized == "openrouter":
        if stage:
            return OpenRouterAgentLLM(model=model or _env(_stage_env_name(stage, "MODEL"), "OPENROUTER_MODEL", "OPENROUTER_AGENT_MODEL", "api_model", "API_MODEL"))
        return OpenRouterAgentLLM(model=model or _env("OPENROUTER_MODEL", "OPENROUTER_AGENT_MODEL", "api_model", "API_MODEL"))
    return DeterministicAgentLLM()


def _build_default_backend() -> AgentLLMBackend:
    choice = _env("LEDGER_AGENT_BACKEND")
    if choice:
        return _build_single_backend(choice, model=_env("LEDGER_AGENT_MODEL"))
    if _env("ANTHROPIC_API_KEY"):
        return AnthropicAgentLLM()
    if _env("OLLAMA_BASE_URL"):
        return OllamaAgentLLM()
    return DeterministicAgentLLM()


def build_llm_backend(preferred: str | None = None) -> AgentLLMBackend:
    if preferred:
        return _build_single_backend(preferred)

    default_backend = _build_default_backend()
    stage_backends: dict[str, AgentLLMBackend] = {}
    critical_choice = _env("LEDGER_CRUCIAL_AGENT_BACKEND")
    critical_model = _env("LEDGER_CRUCIAL_AGENT_MODEL")

    for stage in _STAGE_ENV_KEYS:
        stage_choice = _env(_stage_env_name(stage, "BACKEND"))
        stage_model = _env(_stage_env_name(stage, "MODEL"))
        if stage_choice:
            stage_backends[stage] = _build_single_backend(stage_choice, stage=stage, model=stage_model)
            continue
        if stage in _CRITICAL_STAGES and critical_choice:
            stage_backends[stage] = _build_single_backend(critical_choice, stage=stage, model=stage_model or critical_model)

    if not stage_backends:
        return default_backend
    return StageAwareAgentLLM(default_backend=default_backend, stage_backends=stage_backends)


__all__ = [
    "AgentLLMBackend",
    "AgentLLMDescriptor",
    "AgentLLMResult",
    "AnthropicAgentLLM",
    "DeterministicAgentLLM",
    "OllamaAgentLLM",
    "OpenRouterAgentLLM",
    "StageAwareAgentLLM",
    "build_llm_backend",
]
