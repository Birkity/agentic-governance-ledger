from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.event_store import EventStore
from src.models.events import CreditAnalysisCompleted, CreditDecision, RiskTier
from src.what_if import run_what_if


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a counterfactual replay for a loan application.")
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--branch-event-type", default="CreditAnalysisCompleted")
    parser.add_argument("--risk-tier", choices=[tier.value for tier in RiskTier], default=RiskTier.LOW.value)
    parser.add_argument("--recommended-limit-usd", required=True)
    parser.add_argument("--confidence", type=float, default=0.9)
    parser.add_argument("--rationale", default="Counterfactual replay adjustment")
    parser.add_argument("--session-id", default="what-if-credit-session")
    parser.add_argument("--model-version", default="credit-whatif-v1")
    parser.add_argument("--model-deployment-id", default="counterfactual-deployment")
    parser.add_argument("--input-data-hash", default="counterfactual-input-hash")
    return parser


async def main() -> None:
    args = _parser().parse_args()
    if not args.db_url:
        raise ValueError("DATABASE_URL or --db-url is required")

    store = EventStore(args.db_url)
    await store.connect()
    try:
        counterfactual = CreditAnalysisCompleted(
            application_id=args.application_id,
            session_id=args.session_id,
            decision=CreditDecision(
                risk_tier=RiskTier(args.risk_tier),
                recommended_limit_usd=Decimal(str(args.recommended_limit_usd)),
                confidence=args.confidence,
                rationale=args.rationale,
            ),
            model_version=args.model_version,
            model_deployment_id=args.model_deployment_id,
            input_data_hash=args.input_data_hash,
            analysis_duration_ms=1,
            completed_at=datetime.now(timezone.utc),
        )
        result = await run_what_if(
            store,
            args.application_id,
            branch_at_event_type=args.branch_event_type,
            counterfactual_events=[counterfactual],
        )
        print(json.dumps(asdict(result), default=_json_default, indent=2))
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
