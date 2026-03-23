from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.event_store import EventStore
from src.regulatory import generate_regulatory_package, verify_regulatory_package


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a regulatory audit package for one application.")
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--examination-date", default=None)
    parser.add_argument("--output", default=None)
    return parser


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def main() -> None:
    args = _parser().parse_args()
    if not args.db_url:
        raise ValueError("DATABASE_URL or --db-url is required")

    output_path = Path(args.output) if args.output else Path("artifacts") / f"regulatory_package_{args.application_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    store = EventStore(args.db_url)
    await store.connect()
    try:
        package = await generate_regulatory_package(
            store,
            application_id=args.application_id,
            examination_date=_parse_datetime(args.examination_date),
        )
        verification = verify_regulatory_package(package)
        document = {
            "package": package,
            "verification": {
                "package_hash_valid": verification.package_hash_valid,
                "audit_chain_valid": verification.audit_chain_valid,
                "latest_integrity_hash_matches": verification.latest_integrity_hash_matches,
                "tamper_detected": verification.tamper_detected,
                "ok": verification.ok,
            },
        }
        output_path.write_text(json.dumps(document, default=_json_default, indent=2), encoding="utf-8")
        print(str(output_path))
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
