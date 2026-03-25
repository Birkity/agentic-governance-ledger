from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.cost_reporting import collect_llm_costs, render_live_cost_report
from src.demo import build_api_cost_report
from src.event_store import EventStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate either the demo proxy cost report or a live DB cost report.")
    parser.add_argument("--mode", choices=["auto", "demo", "live"], default="auto")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--application-id", action="append", default=None)
    parser.add_argument("--application-prefix", default=None)
    parser.add_argument("--output", default="artifacts/api_cost_report.txt")
    return parser


async def _write_live_report(*, db_url: str, application_ids: list[str] | None, application_prefix: str | None, output_path: Path) -> None:
    store = EventStore(db_url)
    await store.connect()
    try:
        summary = await collect_llm_costs(
            store,
            application_ids=application_ids,
            application_prefix=application_prefix,
        )
    finally:
        await store.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_live_cost_report(summary), encoding="utf-8")
    print(str(output_path))


async def _write_demo_report(output_path: Path) -> None:
    report = await build_api_cost_report()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(str(output_path))


async def main() -> None:
    load_dotenv()
    args = _parser().parse_args()
    output_path = Path(args.output)

    if args.mode == "demo":
        await _write_demo_report(output_path)
        return

    if args.mode == "live":
        if not args.db_url:
            raise RuntimeError("--db-url or DATABASE_URL is required for live cost reporting")
        await _write_live_report(
            db_url=args.db_url,
            application_ids=args.application_id,
            application_prefix=args.application_prefix,
            output_path=output_path,
        )
        return

    if args.db_url and (args.application_id or args.application_prefix):
        await _write_live_report(
            db_url=args.db_url,
            application_ids=args.application_id,
            application_prefix=args.application_prefix,
            output_path=output_path,
        )
        return

    await _write_demo_report(output_path)


if __name__ == "__main__":
    asyncio.run(main())
