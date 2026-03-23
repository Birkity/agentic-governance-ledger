from __future__ import annotations

from dataclasses import dataclass, field

from src.models.events import StoredEvent


@dataclass(frozen=True)
class ReconstructedAgentContext:
    stream_id: str
    session_id: str | None
    agent_type: str | None
    agent_id: str | None
    application_id: str | None
    model_version: str | None
    context_source: str | None
    context_token_count: int
    token_budget: int
    token_budget_remaining: int
    last_event_type: str | None
    last_successful_node: str | None
    next_node_sequence: int
    tools_called: list[str] = field(default_factory=list)
    output_events_written: list[dict] = field(default_factory=list)
    completed: bool = False
    failed: bool = False
    recovered: bool = False
    needs_reconciliation: bool = False
    reconciliation_reasons: list[str] = field(default_factory=list)


async def reconstruct_agent_context(
    store,
    agent_type: str,
    session_id: str,
    *,
    token_budget: int = 8_000,
) -> ReconstructedAgentContext:
    stream_id = f"agent-{agent_type}-{session_id}"
    events = await store.load_stream(stream_id)
    reasons: list[str] = []

    anchor = events[0] if events else None
    if anchor is None:
        reasons.append("session_stream_missing")
    elif anchor.event_type != "AgentSessionStarted":
        reasons.append("missing_gas_town_anchor")

    session_payload = anchor.payload if anchor and anchor.event_type == "AgentSessionStarted" else {}
    last_node = None
    next_node_sequence = 1
    tools_called: list[str] = []
    output_events_written: list[dict] = []
    completed = False
    failed = False
    recovered = False

    for event in events:
        if event.event_type == "AgentNodeExecuted":
            last_node = event.payload.get("node_name")
            next_node_sequence = int(event.payload.get("node_sequence", 0)) + 1
        elif event.event_type == "AgentToolCalled":
            tool_name = event.payload.get("tool_name")
            if tool_name:
                tools_called.append(str(tool_name))
        elif event.event_type == "AgentOutputWritten":
            output_events_written.extend(list(event.payload.get("events_written", [])))
        elif event.event_type == "AgentSessionCompleted":
            completed = True
        elif event.event_type == "AgentSessionFailed":
            failed = True
        elif event.event_type == "AgentSessionRecovered":
            recovered = True

    context_token_count = int(session_payload.get("context_token_count", 0) or 0)
    if context_token_count > token_budget:
        reasons.append("token_budget_exceeded")

    return ReconstructedAgentContext(
        stream_id=stream_id,
        session_id=session_payload.get("session_id"),
        agent_type=session_payload.get("agent_type"),
        agent_id=session_payload.get("agent_id"),
        application_id=session_payload.get("application_id"),
        model_version=session_payload.get("model_version"),
        context_source=session_payload.get("context_source"),
        context_token_count=context_token_count,
        token_budget=token_budget,
        token_budget_remaining=max(0, token_budget - context_token_count),
        last_event_type=events[-1].event_type if events else None,
        last_successful_node=last_node,
        next_node_sequence=next_node_sequence,
        tools_called=tools_called,
        output_events_written=output_events_written,
        completed=completed,
        failed=failed,
        recovered=recovered,
        needs_reconciliation=bool(reasons),
        reconciliation_reasons=reasons,
    )
