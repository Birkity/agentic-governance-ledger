from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.demo import (  # noqa: E402
    narr01_occ_collision,
    narr02_missing_ebitda,
    narr03_crash_recovery,
    narr04_montana_hard_block,
    narr05_human_override,
)
from src.demo.scenarios import profile_for_company  # noqa: E402
from src.event_store import EventStore  # noqa: E402
from src.projections import (  # noqa: E402
    AgentPerformanceProjection,
    ApplicationSummaryProjection,
    ComplianceAuditProjection,
    ProjectionDaemon,
)
from src.regulatory import generate_regulatory_package, verify_regulatory_package  # noqa: E402

load_dotenv()


SUPPORT_DOC_COMPANIES: dict[str, str | None] = {
    "narr01": "COMP-031",
    "narr02": "COMP-044",
    "narr03": "COMP-057",
    "narr04": None,
    "narr05": "COMP-068",
}

RUNTIME_COMPANIES: dict[str, str | None] = {
    "narr01": "COMP-031",
    "narr02": "COMP-044",
    "narr03": "COMP-057",
    "narr04": None,
    "narr05": "COMP-068",
}


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp_suffix() -> str:
    return _now().strftime("%Y%m%dT%H%M%SZ")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run all five narrative scenarios against a PostgreSQL ledger.")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"), required=not os.getenv("DATABASE_URL"))
    parser.add_argument("--prefix", default="APEX-DB")
    parser.add_argument("--suffix", default=_timestamp_suffix())
    parser.add_argument("--output", default=None)
    return parser


def _scenario_application_id(prefix: str, suffix: str, scenario: str) -> str:
    return f"{prefix}-{scenario.upper()}-{suffix}"


def _profile_snapshot(company_id: str | None) -> dict[str, Any] | None:
    if company_id is None:
        return None
    profile = profile_for_company(company_id)
    return {
        "company_id": profile["company_id"],
        "industry": profile.get("industry"),
        "jurisdiction": profile.get("jurisdiction"),
        "trajectory": profile.get("trajectory"),
        "risk_segment": profile.get("risk_segment"),
    }


def _assert_narr01(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": (
            result["successful_appends"] == 2
            and result["optimistic_concurrency_failures"] == 1
            and result["credit_analysis_completed_count"] == 2
            and result["credit_completion_offsets_from_open"] == [1, 2]
            and result["agent_failures_logged"] == 0
            and result["second_event_causation_id_resolvable"] is True
        ),
        "successful_appends": result["successful_appends"],
        "optimistic_concurrency_failures": result["optimistic_concurrency_failures"],
        "credit_record_opened_position": result["credit_record_opened_position"],
        "credit_stream_positions": result["credit_stream_positions"],
        "credit_completion_offsets_from_open": result["credit_completion_offsets_from_open"],
        "agent_failures_logged": result["agent_failures_logged"],
        "second_event_causation_id": result["second_event_causation_id"],
        "second_event_causation_id_resolvable": result["second_event_causation_id_resolvable"],
    }


def _assert_narr02(result: dict[str, Any]) -> dict[str, Any]:
    passed = (
        any("ebitda" in note.lower() for note in result["income_notes"])
        and result["merged_ebitda"] not in {None, "None"}
        and result["ebitda_source"] in {"financial_workbook", "financial_summary"}
        and float(result["credit_confidence"]) < 0.80
        and "data_quality_caveats" in result["credit_rationale"]
    )
    return {
        "passed": passed,
        "income_notes": result["income_notes"],
        "merged_ebitda": result["merged_ebitda"],
        "ebitda_source": result["ebitda_source"],
        "credit_confidence": result["credit_confidence"],
    }


def _assert_narr03(result: dict[str, Any]) -> dict[str, Any]:
    fraud_completed = [event for event in result["fraud_events"] if event["event_type"] == "FraudScreeningCompleted"]
    first_nodes = [event for event in result["first_session_events"] if event["event_type"] == "AgentNodeExecuted"]
    second_nodes = [event for event in result["second_session_events"] if event["event_type"] == "AgentNodeExecuted"]
    recovered = [event for event in result["second_session_events"] if event["event_type"] == "AgentSessionRecovered"]
    load_facts_total = [
        event
        for event in first_nodes + second_nodes
        if event["payload"].get("node_name") == "load_facts"
    ]
    passed = (
        len(fraud_completed) == 1
        and result["reconstructed_context"]["last_successful_node"] == "load_facts"
        and result["second_session_events"][0]["payload"]["context_source"].startswith("prior_session_replay:")
        and bool(recovered)
        and len(load_facts_total) == 1
    )
    return {
        "passed": passed,
        "fraud_completed_events": len(fraud_completed),
        "reconstructed_last_successful_node": result["reconstructed_context"]["last_successful_node"],
        "recovered_session_context_source": result["second_session_events"][0]["payload"]["context_source"],
        "load_facts_node_events": len(load_facts_total),
    }


def _assert_narr04(result: dict[str, Any]) -> dict[str, Any]:
    rule_events = [
        event
        for event in result["compliance_events"]
        if event["event_type"] in {"ComplianceRulePassed", "ComplianceRuleFailed"}
    ]
    rule_ids = [event["payload"]["rule_id"] for event in rule_events]
    completed = next(event for event in result["compliance_events"] if event["event_type"] == "ComplianceCheckCompleted")
    declined = next(event for event in result["loan_events"] if event["event_type"] == "ApplicationDeclined")
    passed = (
        rule_ids == ["REG-001", "REG-002", "REG-003"]
        and completed["payload"]["overall_verdict"] == "BLOCKED"
        and not any(event["event_type"] == "DecisionGenerated" for event in result["loan_events"])
        and any("REG-003" in reason for reason in declined["payload"]["decline_reasons"])
        and declined["payload"]["adverse_action_notice_required"] is True
    )
    return {
        "passed": passed,
        "rule_ids_evaluated": rule_ids,
        "overall_verdict": completed["payload"]["overall_verdict"],
        "decline_reasons": declined["payload"]["decline_reasons"],
    }


async def _assert_narr05(store: EventStore, result: dict[str, Any], application_id: str) -> dict[str, Any]:
    decision = next(event for event in result["loan_events"] if event["event_type"] == "DecisionGenerated")
    human_review = next(event for event in result["loan_events"] if event["event_type"] == "HumanReviewCompleted")
    approval = next(event for event in result["loan_events"] if event["event_type"] == "ApplicationApproved")
    package = await generate_regulatory_package(store, application_id, examination_date=_now())
    verification = verify_regulatory_package(package)
    passed = (
        decision["payload"]["recommendation"] == "DECLINE"
        and human_review["payload"]["override"] is True
        and human_review["payload"]["reviewer_id"] == "LO-Sarah-Chen"
        and Decimal(approval["payload"]["approved_amount_usd"]) == Decimal("750000")
        and len(approval["payload"]["conditions"]) == 2
        and verification.ok is True
    )
    return {
        "passed": passed,
        "decision_recommendation": decision["payload"]["recommendation"],
        "review_override": human_review["payload"]["override"],
        "reviewer_id": human_review["payload"]["reviewer_id"],
        "approved_amount_usd": approval["payload"]["approved_amount_usd"],
        "conditions": approval["payload"]["conditions"],
        "regulatory_package_ok": verification.ok,
    }


async def _projection_snapshots(store: EventStore, application_ids: list[str]) -> dict[str, Any]:
    summary = ApplicationSummaryProjection(store)
    performance = AgentPerformanceProjection(store)
    compliance = ComplianceAuditProjection(store)
    daemon = ProjectionDaemon(store, [summary, performance, compliance], batch_size=1000)
    processed = await daemon.run_until_caught_up(max_cycles=200, poll_interval_ms=10)

    records: dict[str, Any] = {}
    for application_id in application_ids:
        records[application_id] = {
            "application_summary": await summary.get(application_id),
            "compliance_audit": await compliance.get_current_compliance(application_id),
        }

    return {
        "events_processed": processed,
        "records": records,
        "lags": {
            name: asdict(lag)
            for name, lag in (await daemon.get_all_lags()).items()
        },
    }


async def main() -> None:
    args = _parser().parse_args()
    output_path = Path(args.output) if args.output else Path("artifacts") / f"narratives_db_run_{args.suffix}.json"

    store = EventStore(args.db_url)
    await store.connect()
    try:
        application_ids = {
            "narr01": _scenario_application_id(args.prefix, args.suffix, "narr01"),
            "narr02": _scenario_application_id(args.prefix, args.suffix, "narr02"),
            "narr03": _scenario_application_id(args.prefix, args.suffix, "narr03"),
            "narr04": _scenario_application_id(args.prefix, args.suffix, "narr04"),
            "narr05": _scenario_application_id(args.prefix, args.suffix, "narr05"),
        }

        results: dict[str, Any] = {}

        narr01_result = await narr01_occ_collision(
            store,
            application_id=application_ids["narr01"],
            company_id=RUNTIME_COMPANIES["narr01"] or "COMP-031",
        )
        results["narr01"] = {
            "application_id": application_ids["narr01"],
            "support_doc_company": SUPPORT_DOC_COMPANIES["narr01"],
            "runtime_company": RUNTIME_COMPANIES["narr01"],
            "support_doc_profile": _profile_snapshot(SUPPORT_DOC_COMPANIES["narr01"]),
            "runtime_profile": _profile_snapshot(RUNTIME_COMPANIES["narr01"]),
            "assertions": _assert_narr01(narr01_result),
        }

        narr02_result = await narr02_missing_ebitda(
            store,
            application_id=application_ids["narr02"],
            company_id=RUNTIME_COMPANIES["narr02"] or "COMP-044",
        )
        results["narr02"] = {
            "application_id": application_ids["narr02"],
            "support_doc_company": SUPPORT_DOC_COMPANIES["narr02"],
            "runtime_company": narr02_result["company_id"],
            "support_doc_profile": _profile_snapshot(SUPPORT_DOC_COMPANIES["narr02"]),
            "runtime_profile": _profile_snapshot(narr02_result["company_id"]),
            "assertions": _assert_narr02(narr02_result),
        }

        narr03_result = await narr03_crash_recovery(
            store,
            application_id=application_ids["narr03"],
            company_id=RUNTIME_COMPANIES["narr03"] or "COMP-057",
        )
        results["narr03"] = {
            "application_id": application_ids["narr03"],
            "support_doc_company": SUPPORT_DOC_COMPANIES["narr03"],
            "runtime_company": narr03_result["company_id"],
            "support_doc_profile": _profile_snapshot(SUPPORT_DOC_COMPANIES["narr03"]),
            "runtime_profile": _profile_snapshot(narr03_result["company_id"]),
            "assertions": _assert_narr03(narr03_result),
        }

        narr04_result = await narr04_montana_hard_block(
            store,
            application_id=application_ids["narr04"],
        )
        results["narr04"] = {
            "application_id": application_ids["narr04"],
            "support_doc_company": "MT company",
            "runtime_company": narr04_result["company_id"],
            "runtime_profile": _profile_snapshot(narr04_result["company_id"]),
            "assertions": _assert_narr04(narr04_result),
        }

        narr05_result = await narr05_human_override(
            store,
            application_id=application_ids["narr05"],
            company_id=RUNTIME_COMPANIES["narr05"] or "COMP-068",
        )
        results["narr05"] = {
            "application_id": application_ids["narr05"],
            "support_doc_company": SUPPORT_DOC_COMPANIES["narr05"],
            "runtime_company": narr05_result["company_id"],
            "support_doc_profile": _profile_snapshot(SUPPORT_DOC_COMPANIES["narr05"]),
            "runtime_profile": _profile_snapshot(narr05_result["company_id"]),
            "assertions": await _assert_narr05(store, narr05_result, application_ids["narr05"]),
        }

        projection_state = await _projection_snapshots(store, list(application_ids.values()))
        overall_passed = all(result["assertions"]["passed"] for result in results.values())

        rendered = {
            "generated_at": _now().isoformat(),
            "db_url_redacted": args.db_url.rsplit("@", 1)[-1],
            "application_ids": application_ids,
            "overall_passed": overall_passed,
            "results": results,
            "projection_state": json.loads(json.dumps(projection_state, default=_json_default)),
            "notes": [
                "Application IDs are unique per run to preserve append-only history in PostgreSQL.",
                "Support-document company references are included alongside the runtime companies currently encoded in the repo.",
                "If support-document company IDs differ from runtime companies, that reflects a synthetic-data mismatch rather than a silent remap.",
            ],
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(rendered, default=_json_default, indent=2), encoding="utf-8")
        print(str(output_path))
        print(json.dumps({"overall_passed": overall_passed, "application_ids": application_ids}, indent=2))
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
