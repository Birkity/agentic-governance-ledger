from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.document_processing import (
    DocumentPackageProcessor,
    OllamaSummarizer,
    persist_document_package,
)
from src.event_store import EventStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze a generated company document package.")
    parser.add_argument("--company", required=True, help="Company folder under documents/, for example COMP-024")
    parser.add_argument("--documents-dir", default="documents", help="Root directory containing generated company folders")
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="Use the configured local Ollama models to summarize each document and the full package",
    )
    parser.add_argument("--application-id", help="Application id used when persisting package events")
    parser.add_argument(
        "--persist-events",
        action="store_true",
        help="Persist the analyzed package as docpkg-* events in the Event Store",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Database URL used with --persist-events. Defaults to DATABASE_URL from the environment.",
    )
    return parser


async def _persist_if_requested(args, analysis) -> tuple[str, list[int]] | None:
    if not args.persist_events:
        return None
    if not args.application_id:
        raise ValueError("--application-id is required when --persist-events is used")

    import os

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL or --db-url is required when --persist-events is used")

    store = EventStore(db_url)
    await store.connect()
    try:
        positions = await persist_document_package(store, analysis, application_id=args.application_id)
    finally:
        await store.close()
    return f"docpkg-{args.application_id}", positions


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    summarizer = OllamaSummarizer.from_env() if args.with_llm else None
    processor = DocumentPackageProcessor(documents_root=args.documents_dir, summarizer=summarizer)
    analysis = processor.process_company(args.company, include_summaries=args.with_llm)
    persisted = await _persist_if_requested(args, analysis)

    payload = analysis.model_dump(mode="json")
    if persisted is not None:
        stream_id, positions = persisted
        payload["persisted_stream_id"] = stream_id
        payload["persisted_positions"] = positions
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
