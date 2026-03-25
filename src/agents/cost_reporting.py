from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.event_store import EventStore, InMemoryEventStore
from src.models.events import AGENT_SESSION_ANCHOR_EVENT_TYPES


StoreType = EventStore | InMemoryEventStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _provider_from_model(model: str | None) -> str:
    normalized = (model or "").strip().lower()
    if not normalized:
        return "unknown"
    if normalized.startswith("openai/") or normalized.startswith("google/") or normalized.startswith("anthropic/"):
        return "openrouter"
    if normalized.startswith("claude"):
        return "anthropic"
    if normalized.startswith("qwen") or ":" in normalized:
        return "ollama"
    if "deterministic" in normalized:
        return "deterministic"
    return "unknown"


async def collect_llm_costs(
    store: StoreType,
    *,
    application_ids: list[str] | None = None,
    application_prefix: str | None = None,
) -> dict[str, Any]:
    wanted_ids = set(application_ids or [])
    filter_by_prefix = application_prefix is not None and application_prefix != ""
    filter_by_ids = bool(wanted_ids)

    session_context: dict[str, dict[str, str]] = {}
    included_applications: set[str] = set()
    calls_observed = 0
    billable_calls = 0
    total_cost = 0.0
    totals_by_application: dict[str, float] = defaultdict(float)
    totals_by_agent: dict[str, float] = defaultdict(float)
    totals_by_model: dict[str, float] = defaultdict(float)
    totals_by_provider: dict[str, float] = defaultdict(float)
    tokens_by_provider: dict[str, int] = defaultdict(int)

    async for event in store.load_all(from_position=0, apply_upcasters=False):
        payload = event.payload
        if event.event_type in AGENT_SESSION_ANCHOR_EVENT_TYPES:
            application_id = str(payload.get("application_id", ""))
            should_include = (
                (not filter_by_ids or application_id in wanted_ids)
                and (not filter_by_prefix or application_id.startswith(application_prefix or ""))
            )
            session_context[event.stream_id] = {
                "application_id": application_id,
                "agent_type": str(payload.get("agent_type", "")),
                "model_version": str(payload.get("model_version", "")),
                "include": "1" if should_include else "0",
            }
            if should_include:
                included_applications.add(application_id)
            continue

        if event.event_type != "AgentNodeExecuted":
            continue

        context = session_context.get(event.stream_id)
        if context is None or context.get("include") != "1":
            continue
        if not payload.get("llm_called"):
            continue

        application_id = context["application_id"]
        agent_type = context["agent_type"]
        provider = str(payload.get("llm_provider") or _provider_from_model(context.get("model_version")))
        model = str(payload.get("llm_model") or context.get("model_version") or "unknown")
        cost = float(payload.get("llm_cost_usd") or 0.0)
        tokens = int(payload.get("llm_tokens_input") or 0) + int(payload.get("llm_tokens_output") or 0)

        calls_observed += 1
        if cost > 0:
            billable_calls += 1
        total_cost += cost
        totals_by_application[application_id] += cost
        totals_by_agent[agent_type] += cost
        totals_by_model[model] += cost
        totals_by_provider[provider] += cost
        tokens_by_provider[provider] += tokens

    application_count = len(included_applications)
    average_cost = round(total_cost / application_count, 6) if application_count else 0.0
    return {
        "generated_at": _now().isoformat(),
        "application_ids": sorted(included_applications),
        "application_count": application_count,
        "provider_calls_observed": calls_observed,
        "billable_calls_observed": billable_calls,
        "total_cost_usd": round(total_cost, 6),
        "average_cost_usd": average_cost,
        "totals_by_application": {key: round(value, 6) for key, value in sorted(totals_by_application.items())},
        "totals_by_agent": {key: round(value, 6) for key, value in sorted(totals_by_agent.items())},
        "totals_by_model": {key: round(value, 6) for key, value in sorted(totals_by_model.items())},
        "totals_by_provider": {key: round(value, 6) for key, value in sorted(totals_by_provider.items())},
        "tokens_by_provider": dict(sorted(tokens_by_provider.items())),
    }


def render_live_cost_report(summary: dict[str, Any]) -> str:
    application_ids = summary["application_ids"]
    totals_by_application = summary["totals_by_application"]
    totals_by_agent = summary["totals_by_agent"]
    totals_by_model = summary["totals_by_model"]
    totals_by_provider = summary["totals_by_provider"]
    tokens_by_provider = summary["tokens_by_provider"]

    lines = [
        "Live API Cost Report",
        f"Generated at: {summary['generated_at']}",
        f"Applications included: {summary['application_count']}",
        f"Provider calls observed: {summary['provider_calls_observed']}",
        f"Billable calls observed: {summary['billable_calls_observed']}",
        f"Total billable external cost: ${summary['total_cost_usd']:.6f}",
        f"Average cost per application: ${summary['average_cost_usd']:.6f}",
        "",
        "Applications:",
    ]
    if application_ids:
        lines.extend(f"- {application_id}: ${totals_by_application.get(application_id, 0.0):.6f}" for application_id in application_ids)
    else:
        lines.append("- none")

    lines.extend(["", "By agent:"])
    lines.extend(f"- {agent}: ${cost:.6f}" for agent, cost in totals_by_agent.items())
    if not totals_by_agent:
        lines.append("- none")

    lines.extend(["", "By provider:"])
    if totals_by_provider:
        for provider, cost in totals_by_provider.items():
            lines.append(f"- {provider}: ${cost:.6f} ({tokens_by_provider.get(provider, 0)} tokens)")
    else:
        lines.append("- none")

    lines.extend(["", "By model:"])
    if totals_by_model:
        lines.extend(f"- {model}: ${cost:.6f}" for model, cost in totals_by_model.items())
    else:
        lines.append("- none")

    if summary["total_cost_usd"] == 0:
        lines.extend(
            [
                "",
                "Note:",
                "- No billable provider usage was observed for the selected applications.",
                "- Deterministic fallbacks and local Ollama runs are treated as non-billable external cost.",
            ]
        )

    return "\n".join(lines)


__all__ = [
    "collect_llm_costs",
    "render_live_cost_report",
]
