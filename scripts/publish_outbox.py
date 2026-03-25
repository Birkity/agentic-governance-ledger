from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.event_store import EventStore
from src.outbox import JsonlOutboxSink, OutboxPublisher


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish pending outbox records to a JSONL sink.")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--destination", default="ledger.downstream")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output-path", default="artifacts/outbox_published.jsonl")
    return parser


async def main() -> None:
    args = _parser().parse_args()
    if not args.db_url:
        raise RuntimeError("DATABASE_URL is required to publish the outbox")

    store = EventStore(args.db_url)
    await store.connect()
    try:
        publisher = OutboxPublisher(store)
        sink = JsonlOutboxSink(args.output_path)
        result = await publisher.publish_pending(sink.publish, destination=args.destination, limit=args.limit)
        print(json.dumps(result))
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
