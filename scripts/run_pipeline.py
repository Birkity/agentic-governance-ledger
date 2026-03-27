from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.demo import (
    narr01_occ_collision,
    narr02_missing_ebitda,
    narr03_crash_recovery,
    narr04_montana_hard_block,
    narr05_human_override,
)
from src.agents import LedgerAgentRuntime, default_company_for_application
from src.event_store import EventStore, InMemoryEventStore


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one application through the ledger demo pipeline.")
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--company-id", default=None)
    parser.add_argument("--phase", choices=["document", "credit", "fraud", "compliance", "decision", "full"], default="full")
    parser.add_argument(
        "--scenario",
        choices=["default", "narr01", "narr02", "narr03", "narr04", "narr05"],
        default="default",
    )
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--output", default=None)
    return parser


async def _build_store(db_url: str | None):
    if db_url:
        store = EventStore(db_url)
        await store.connect()
        return store
    return InMemoryEventStore()


async def main() -> None:
    args = _parser().parse_args()
    company_id = args.company_id or default_company_for_application(args.application_id)
    store = await _build_store(args.db_url)
    try:
        if args.scenario == "narr01":
            result = await narr01_occ_collision(store, application_id=args.application_id)
        elif args.scenario == "narr02":
            result = await narr02_missing_ebitda(store, application_id=args.application_id, company_id=company_id)
        elif args.scenario == "narr03":
            result = await narr03_crash_recovery(store, application_id=args.application_id, company_id=company_id)
        elif args.scenario == "narr04":
            result = await narr04_montana_hard_block(store, application_id=args.application_id, company_id=company_id)
        elif args.scenario == "narr05":
            result = await narr05_human_override(store, application_id=args.application_id, company_id=company_id)
        else:
            runtime = LedgerAgentRuntime(store)
            result = await runtime.start_application(
                args.application_id,
                company_id,
                phase=args.phase,
                auto_finalize_human_review=True,
            )

        rendered = json.dumps(result, default=_json_default, indent=2)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
            print(str(output_path))
        else:
            print(rendered)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
