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

from src.agents import LedgerAgentRuntime, build_client_application_id, list_document_companies
from src.event_store import EventStore


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
    parser = argparse.ArgumentParser(description="Bridge UI actions into the Ledger runtime.")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-companies")

    start = subparsers.add_parser("start-application")
    start.add_argument("--company-id", required=True)
    start.add_argument("--application-id", default=None)
    start.add_argument("--phase", choices=["document", "credit", "full"], default="full")
    start.add_argument("--requested-amount-usd", default=None)
    start.add_argument("--auto-finalize-human-review", action="store_true")
    start.add_argument("--reviewer-id", default="loan-ops")

    review = subparsers.add_parser("record-human-review")
    review.add_argument("--application-id", required=True)
    review.add_argument("--reviewer-id", required=True)
    review.add_argument("--final-decision", choices=["APPROVE", "DECLINE"], required=True)
    review.add_argument("--override", action="store_true")
    review.add_argument("--override-reason", default=None)
    review.add_argument("--approved-amount-usd", default=None)
    review.add_argument("--interest-rate-pct", type=float, default=None)
    review.add_argument("--term-months", type=int, default=None)
    review.add_argument("--conditions-json", default="[]")
    review.add_argument("--decline-reasons-json", default="[]")
    review.add_argument("--adverse-action-codes-json", default="[]")

    integrity = subparsers.add_parser("run-integrity")
    integrity.add_argument("--application-id", required=True)
    return parser


async def _build_runtime(db_url: str) -> tuple[EventStore, LedgerAgentRuntime]:
    store = EventStore(db_url)
    await store.connect()
    return store, LedgerAgentRuntime(store)


async def main() -> None:
    args = _parser().parse_args()
    if args.command == "list-companies":
        print(json.dumps({"companies": [company.__dict__ for company in list_document_companies()]}, default=_json_default))
        return

    if not args.db_url:
        raise RuntimeError("DATABASE_URL is required for UI workflow actions")

    store, runtime = await _build_runtime(args.db_url)
    try:
        if args.command == "start-application":
            application_id = args.application_id or build_client_application_id(args.company_id)
            requested_amount = Decimal(args.requested_amount_usd) if args.requested_amount_usd else None
            result = await runtime.start_application(
                application_id,
                args.company_id,
                phase=args.phase,
                requested_amount_usd=requested_amount,
                auto_finalize_human_review=bool(args.auto_finalize_human_review),
                reviewer_id=args.reviewer_id,
            )
            payload = {"ok": True, "application_id": application_id, **result}
        elif args.command == "record-human-review":
            payload = {
                "ok": True,
                **(
                    await runtime.complete_human_review(
                        args.application_id,
                        reviewer_id=args.reviewer_id,
                        final_decision=args.final_decision,
                        override=bool(args.override),
                        override_reason=args.override_reason,
                        approved_amount_usd=Decimal(args.approved_amount_usd) if args.approved_amount_usd else None,
                        interest_rate_pct=args.interest_rate_pct,
                        term_months=args.term_months,
                        conditions=json.loads(args.conditions_json),
                        decline_reasons=json.loads(args.decline_reasons_json),
                        adverse_action_codes=json.loads(args.adverse_action_codes_json),
                    )
                ),
            }
        else:
            payload = {"ok": True, **(await runtime.run_integrity(args.application_id))}
        print(json.dumps(payload, default=_json_default))
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
