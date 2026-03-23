from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from src.mcp.runtime import MCPRuntime, parse_iso_datetime, parse_resource_query, to_json_text


def _to_payload(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_to_payload(item) for item in value]
    return value


async def _load_audit_events(runtime: MCPRuntime, application_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for stream_id in (f"audit-loan-{application_id}", f"audit-application-{application_id}"):
        loaded = await runtime.store.load_stream(stream_id)
        for event in loaded:
            events.append(event.model_dump(mode="json"))
    events.sort(key=lambda item: (item.get("stream_position", 0), item.get("recorded_at", "")))
    return events


async def _find_agent_session_stream_id(runtime: MCPRuntime, session_id: str) -> str | None:
    if getattr(runtime.store, "_pool", None) is not None:
        async with runtime.store._pool.acquire() as conn:  # type: ignore[union-attr]
            stream_id = await conn.fetchval(
                """
                SELECT stream_id
                FROM agent_session_projection_index
                WHERE session_id = $1
                """,
                session_id,
            )
        return str(stream_id) if stream_id else None

    for stream_id, events in getattr(runtime.store, "_streams", {}).items():
        if stream_id.startswith("agent-") and any(
            event.payload.get("session_id") == session_id for event in events
        ):
            return stream_id
    return None


async def read_application_resource(runtime: MCPRuntime, application_id: str) -> dict[str, Any]:
    await runtime.sync()
    record = await runtime.application_summary.get(application_id)
    return {
        "resource_uri": f"ledger://applications/{application_id}",
        "data": _to_payload(record),
    }


async def read_compliance_resource(
    runtime: MCPRuntime,
    application_id: str,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    await runtime.sync()
    state = (
        await runtime.compliance_audit.get_compliance_at(application_id, as_of)
        if as_of is not None
        else await runtime.compliance_audit.get_current_compliance(application_id)
    )
    return {
        "resource_uri": f"ledger://applications/{application_id}/compliance",
        "as_of": as_of.isoformat() if as_of else None,
        "data": _to_payload(state),
    }


async def read_audit_trail_resource(
    runtime: MCPRuntime,
    application_id: str,
    *,
    from_position: int | None = None,
    to_position: int | None = None,
) -> dict[str, Any]:
    events = await _load_audit_events(runtime, application_id)
    filtered = [
        event
        for event in events
        if (from_position is None or int(event["stream_position"]) >= from_position)
        and (to_position is None or int(event["stream_position"]) <= to_position)
    ]
    return {
        "resource_uri": f"ledger://applications/{application_id}/audit-trail",
        "data": filtered,
    }


async def read_agent_performance_resource(runtime: MCPRuntime, agent_id: str) -> dict[str, Any]:
    await runtime.sync()
    records = await runtime.agent_performance.list_all()
    filtered = [asdict(record) for record in records if record.agent_id == agent_id]
    return {
        "resource_uri": f"ledger://agents/{agent_id}/performance",
        "data": filtered,
    }


async def read_agent_session_resource(
    runtime: MCPRuntime,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    stream_id = await _find_agent_session_stream_id(runtime, session_id)
    if stream_id is None:
        data: list[dict[str, Any]] = []
    else:
        loaded = await runtime.store.load_stream(stream_id)
        if loaded:
            anchor = loaded[0].payload
            if anchor.get("agent_id") != agent_id and anchor.get("agent_type") != agent_id:
                data = []
            else:
                data = [event.model_dump(mode="json") for event in loaded]
        else:
            data = []
    return {
        "resource_uri": f"ledger://agents/{agent_id}/sessions/{session_id}",
        "data": data,
    }


async def read_health_resource(runtime: MCPRuntime) -> dict[str, Any]:
    lags = await runtime.daemon.get_all_lags()
    return {
        "resource_uri": "ledger://ledger/health",
        "data": {name: asdict(lag) for name, lag in lags.items()},
    }


async def read_resource_uri(runtime: MCPRuntime, uri: str) -> dict[str, Any]:
    base_uri, query = parse_resource_query(uri)
    if base_uri.startswith("ledger://applications/") and base_uri.endswith("/compliance"):
        application_id = base_uri.split("/")[3]
        as_of_values = query.get("as_of", [])
        as_of = parse_iso_datetime(as_of_values[0]) if as_of_values else None
        return await read_compliance_resource(runtime, application_id, as_of=as_of)
    if base_uri.startswith("ledger://applications/") and base_uri.endswith("/audit-trail"):
        application_id = base_uri.split("/")[3]
        from_values = query.get("from", [])
        to_values = query.get("to", [])
        return await read_audit_trail_resource(
            runtime,
            application_id,
            from_position=int(from_values[0]) if from_values else None,
            to_position=int(to_values[0]) if to_values else None,
        )
    if base_uri.startswith("ledger://applications/") and base_uri.count("/") == 3:
        application_id = base_uri.split("/")[3]
        return await read_application_resource(runtime, application_id)
    if base_uri.startswith("ledger://agents/") and base_uri.endswith("/performance"):
        agent_id = base_uri.split("/")[3]
        return await read_agent_performance_resource(runtime, agent_id)
    if base_uri.startswith("ledger://agents/") and "/sessions/" in base_uri:
        parts = base_uri.split("/")
        return await read_agent_session_resource(runtime, parts[3], parts[5])
    if base_uri == "ledger://ledger/health":
        return await read_health_resource(runtime)
    raise ValueError(f"Unknown MCP resource URI: {uri}")


def register_resources(app: FastMCP, runtime: MCPRuntime) -> None:
    @app.resource(
        "ledger://applications/{application_id}",
        name="ledger_application",
        description="Read the current application summary from the ApplicationSummary projection.",
    )
    async def application_resource(application_id: str) -> str:
        return to_json_text(await read_application_resource(runtime, application_id))

    @app.resource(
        "ledger://applications/{application_id}/compliance",
        name="ledger_application_compliance",
        description=(
            "Read the compliance projection. Use ledger://applications/{id}/compliance?as_of=<ISO timestamp> "
            "for temporal reads when calling through the gateway or an MCP client that preserves query strings."
        ),
    )
    async def compliance_resource(application_id: str) -> str:
        return to_json_text(await read_compliance_resource(runtime, application_id))

    @app.resource(
        "ledger://applications/{application_id}/audit-trail",
        name="ledger_application_audit_trail",
        description=(
            "Read the audit stream directly. This is a justified CQRS exception for integrity evidence. "
            "Supports ?from=<stream_position>&to=<stream_position> when the client preserves query strings."
        ),
    )
    async def audit_trail_resource(application_id: str) -> str:
        return to_json_text(await read_audit_trail_resource(runtime, application_id))

    @app.resource(
        "ledger://agents/{agent_id}/performance",
        name="ledger_agent_performance",
        description="Read the current AgentPerformance projection for one agent_id.",
    )
    async def agent_performance_resource(agent_id: str) -> str:
        return to_json_text(await read_agent_performance_resource(runtime, agent_id))

    @app.resource(
        "ledger://agents/{agent_id}/sessions/{session_id}",
        name="ledger_agent_session",
        description=(
            "Read an agent session stream directly. This is a justified exception because full replay is required "
            "for crash recovery and Gas Town reconstruction."
        ),
    )
    async def agent_session_resource(agent_id: str, session_id: str) -> str:
        return to_json_text(await read_agent_session_resource(runtime, agent_id, session_id))

    @app.resource(
        "ledger://ledger/health",
        name="ledger_health",
        description="Read projection lag and freshness information from the ProjectionDaemon.",
    )
    async def health_resource() -> str:
        return to_json_text(await read_health_resource(runtime))
