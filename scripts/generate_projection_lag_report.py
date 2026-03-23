from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.commands.handlers import SubmitApplicationCommand, handle_submit_application
from src.event_store import InMemoryEventStore
from src.projections import (
    AgentPerformanceProjection,
    ApplicationSummaryProjection,
    ComplianceAuditProjection,
    ProjectionDaemon,
)
from src.projections.seed_validation import load_seed_events_into_store, validate_seed_projection_rebuild


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _app_id(prefix: str = "APEX-P3RPT") -> str:
    return f"{prefix}-{uuid4().hex[:8].upper()}"


async def _submit_application(store: InMemoryEventStore, application_id: str) -> None:
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=application_id,
            applicant_id="COMP-024",
            requested_amount_usd=Decimal("450000"),
            loan_purpose="working_capital",
            loan_term_months=36,
            submission_channel="portal",
            contact_email="ops@company.test",
            contact_name="Ava Ops",
            application_reference=f"REF-{application_id}",
        ),
        store,
    )


async def _build_lag_section() -> dict[str, object]:
    store = InMemoryEventStore()
    summary = ApplicationSummaryProjection(store)
    daemon = ProjectionDaemon(store, [summary], batch_size=500)

    application_ids = [_app_id() for _ in range(50)]
    await asyncio.gather(*[_submit_application(store, application_id) for application_id in application_ids])
    await daemon.run_until_caught_up(max_cycles=20)

    lag = await daemon.get_lag(summary.name)
    records = await summary.list_all()
    return {
        "application_count": len(records),
        "lag_positions": lag.lag_positions,
        "lag_ms": lag.lag_ms,
        "slo_target_ms": 800,
        "slo_met": lag.lag_positions == 0 and lag.lag_ms <= 800 and len(records) == 50,
    }


async def _build_seed_section(seed_path: str) -> dict[str, object]:
    store = InMemoryEventStore()
    seed_events = await load_seed_events_into_store(store, seed_path)

    summary = ApplicationSummaryProjection(store)
    performance = AgentPerformanceProjection(store)
    compliance = ComplianceAuditProjection(store)
    daemon = ProjectionDaemon(store, [summary, performance, compliance], batch_size=1000)
    await daemon.run_until_caught_up(max_cycles=100)

    validation = await validate_seed_projection_rebuild(summary, performance, compliance, seed_events)
    return {
        "total_seed_events": validation.total_seed_events,
        "total_applications": validation.total_applications,
        "application_summary_records": validation.application_summary_records,
        "compliance_current_records": validation.compliance_current_records,
        "agent_performance_rows": validation.agent_performance_rows,
        "state_counts": validation.state_counts,
        "compliance_verdict_counts": validation.compliance_verdict_counts,
        "agent_type_counts": validation.agent_type_counts,
        "mismatches": validation.mismatches,
        "ok": validation.ok,
    }


def _render_report(lag_section: dict[str, object], seed_section: dict[str, object]) -> str:
    mismatch_lines = seed_section["mismatches"] or ["none"]
    mismatch_text = "\n".join(f"- {line}" for line in mismatch_lines)

    return "\n".join(
        [
            "Projection Lag and Seed Rebuild Report",
            f"Generated at: {_now().isoformat()}",
            "",
            "Lag SLO Check",
            f"- concurrent_submissions: 50",
            f"- projected_applications: {lag_section['application_count']}",
            f"- lag_positions: {lag_section['lag_positions']}",
            f"- lag_ms: {lag_section['lag_ms']}",
            f"- slo_target_ms: {lag_section['slo_target_ms']}",
            f"- slo_met: {lag_section['slo_met']}",
            "",
            "Seed Rebuild Validation",
            f"- total_seed_events: {seed_section['total_seed_events']}",
            f"- total_applications: {seed_section['total_applications']}",
            f"- application_summary_records: {seed_section['application_summary_records']}",
            f"- compliance_current_records: {seed_section['compliance_current_records']}",
            f"- agent_performance_rows: {seed_section['agent_performance_rows']}",
            f"- state_counts: {seed_section['state_counts']}",
            f"- compliance_verdict_counts: {seed_section['compliance_verdict_counts']}",
            f"- agent_type_counts: {seed_section['agent_type_counts']}",
            f"- rebuild_matches_seed_expectations: {seed_section['ok']}",
            "",
            "Mismatches",
            mismatch_text,
        ]
    )


async def _main(seed_path: str, output_path: str) -> Path:
    lag_section = await _build_lag_section()
    seed_section = await _build_seed_section(seed_path)
    report_text = _render_report(lag_section, seed_section)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report_text + "\n", encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Phase 3 projection lag report artifact.")
    parser.add_argument("--seed-path", default="data/seed_events.jsonl")
    parser.add_argument("--output", default="artifacts/projection_lag_report.txt")
    args = parser.parse_args()

    output = asyncio.run(_main(args.seed_path, args.output))
    print(f"Wrote projection lag report to {output}")


if __name__ == "__main__":
    main()
