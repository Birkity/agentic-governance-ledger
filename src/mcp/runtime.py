from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from fastmcp import FastMCP

from src.projections import (
    AgentPerformanceProjection,
    ApplicationSummaryProjection,
    ComplianceAuditProjection,
    ProjectionDaemon,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def to_json_text(payload: Any) -> str:
    return json.dumps(payload, default=_json_default, sort_keys=True)


def parse_json_text(payload: str) -> Any:
    return json.loads(payload)


@dataclass
class MCPRuntime:
    store: Any
    application_summary: ApplicationSummaryProjection
    agent_performance: AgentPerformanceProjection
    compliance_audit: ComplianceAuditProjection
    daemon: ProjectionDaemon

    @classmethod
    def build(cls, store: Any) -> "MCPRuntime":
        application_summary = ApplicationSummaryProjection(store)
        agent_performance = AgentPerformanceProjection(store)
        compliance_audit = ComplianceAuditProjection(store)
        daemon = ProjectionDaemon(
            store,
            [application_summary, agent_performance, compliance_audit],
        )
        return cls(
            store=store,
            application_summary=application_summary,
            agent_performance=agent_performance,
            compliance_audit=compliance_audit,
            daemon=daemon,
        )

    async def initialize(self) -> None:
        await self.daemon.initialize()

    async def sync(self) -> None:
        await self.daemon.run_until_caught_up()


class LedgerMCPGateway:
    """Thin helper for in-process MCP tests and query-string aware resource access."""

    def __init__(
        self,
        app: FastMCP,
        runtime: MCPRuntime,
        *,
        resource_reader: Any,
    ) -> None:
        self.app = app
        self.runtime = runtime
        self._resource_reader = resource_reader

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self.app.call_tool(name, arguments or {})
        return dict(result.structured_content or {})

    async def read_resource(self, uri: str) -> dict[str, Any]:
        parsed = urlparse(uri)
        if parsed.query:
            return await self._resource_reader(self.runtime, uri)

        result = await self.app.read_resource(uri)
        if not result.contents:
            return {}
        return parse_json_text(result.contents[0].content)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    if " " in normalized and "T" in normalized:
        normalized = normalized.replace(" ", "+")
    return datetime.fromisoformat(normalized)


def parse_resource_query(uri: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlparse(uri)
    base_uri = parsed._replace(query="", fragment="").geturl()
    return base_uri, parse_qs(parsed.query)
