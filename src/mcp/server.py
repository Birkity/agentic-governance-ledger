from __future__ import annotations

import argparse
import asyncio
import os

from fastmcp import FastMCP

from src.event_store import EventStore
from src.mcp.resources import read_resource_uri, register_resources
from src.mcp.runtime import LedgerMCPGateway, MCPRuntime
from src.mcp.tools import register_tools


def create_mcp_app(runtime: MCPRuntime) -> FastMCP:
    app = FastMCP(
        "The Ledger MCP",
        instructions=(
            "Tools are command-side writes to the event-sourced ledger. "
            "Resources are query-side reads from projections, except for the justified "
            "audit-trail and agent-session replay resources."
        ),
    )
    register_tools(app, runtime)
    register_resources(app, runtime)
    return app


def create_gateway(runtime: MCPRuntime) -> LedgerMCPGateway:
    app = create_mcp_app(runtime)
    return LedgerMCPGateway(app, runtime, resource_reader=read_resource_uri)


async def build_gateway_from_store(store) -> LedgerMCPGateway:
    runtime = MCPRuntime.build(store)
    await runtime.initialize()
    return create_gateway(runtime)


async def build_default_gateway_from_env() -> LedgerMCPGateway:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is required to start the MCP server")
    store = EventStore(db_url)
    await store.connect()
    return await build_gateway_from_store(store)


async def run_from_env(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    gateway = await build_default_gateway_from_env()
    try:
        if transport == "http":
            await gateway.app.run_http_async(show_banner=True, host=host, port=port)
        else:
            await gateway.app.run_stdio_async(show_banner=True)
    finally:
        await gateway.runtime.store.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run The Ledger MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(run_from_env(transport=args.transport, host=args.host, port=args.port))


if __name__ == "__main__":
    main()
