from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.demo import generate_narr05_artifacts, generate_runtime_narr05_artifacts
from src.event_store import EventStore


load_dotenv()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return value.__dict__
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the NARR-05 demo and generate its artifacts.")
    parser.add_argument("--mode", choices=["auto", "live", "in-memory"], default="auto")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--application-id", default=None)
    parser.add_argument("--company-id", default="COMP-068")
    parser.add_argument("--requested-amount-usd", default="950000")
    parser.add_argument("--target-approved-amount-usd", default="750000")
    parser.add_argument("--reviewer-id", default="LO-Sarah-Chen")
    parser.add_argument(
        "--override-reason",
        default="15-year customer, prior repayment history, collateral offered",
    )
    parser.add_argument("--interest-rate-pct", type=float, default=9.10)
    parser.add_argument("--term-months", type=int, default=36)
    parser.add_argument(
        "--condition",
        action="append",
        default=None,
        help="Repeat to set approval conditions. Uses the default NARR-05 conditions when omitted.",
    )
    parser.add_argument("--output-dir", default="artifacts")
    return parser


async def main() -> None:
    args = _parser().parse_args()
    requested_amount_usd = Decimal(str(args.requested_amount_usd))
    target_approved_amount_usd = Decimal(str(args.target_approved_amount_usd))

    mode = args.mode
    if mode == "auto":
        mode = "live" if args.db_url else "in-memory"

    if mode == "live":
        if not args.db_url:
            raise RuntimeError("DATABASE_URL or --db-url is required for live NARR-05 demo mode")
        application_id = args.application_id or f"APEX-NARR05-LIVE-{_now().strftime('%Y%m%dT%H%M%SZ')}"
        store = EventStore(args.db_url)
        await store.connect()
        try:
            result = await generate_runtime_narr05_artifacts(
                store,
                output_dir=args.output_dir,
                application_id=application_id,
                company_id=args.company_id,
                requested_amount_usd=requested_amount_usd,
                target_approved_amount_usd=target_approved_amount_usd,
                reviewer_id=args.reviewer_id,
                override_reason=args.override_reason,
                interest_rate_pct=args.interest_rate_pct,
                term_months=args.term_months,
                conditions=args.condition,
            )
        finally:
            await store.close()
    else:
        result = await generate_narr05_artifacts(args.output_dir)

    print(json.dumps(result, default=_json_default, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
