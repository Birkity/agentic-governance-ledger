from __future__ import annotations

import pytest

from src.demo import scenarios
from src.event_store import InMemoryEventStore
from src.models.events import RiskTier
from src.registry import CompanyProfile, ComplianceFlag


class FakeRuntimeRegistryClient:
    async def get_company(self, company_id: str) -> CompanyProfile | None:
        if company_id != "COMP-024":
            return None
        return CompanyProfile(
            company_id="COMP-024",
            name="Atlas Fabrication",
            industry="manufacturing",
            naics="332710",
            jurisdiction="TX",
            legal_type="LLC",
            founded_year=2010,
            employee_count=120,
            risk_segment="HIGH",
            trajectory="DECLINING",
            submission_channel="rm",
            ip_region="US-TX",
        )

    async def get_compliance_flags(self, company_id: str, active_only: bool = False) -> list[ComplianceFlag]:
        return [
            ComplianceFlag(
                flag_type="WATCH",
                severity="MEDIUM",
                is_active=True,
                added_date="2025-01-01",
                note="monitor closely",
            )
        ]

    async def get_loan_relationships(self, company_id: str) -> list[dict[str, object]]:
        return [
            {
                "loan_amount": 250000.0,
                "loan_year": 2022,
                "was_repaid": True,
                "default_occurred": False,
                "note": "prior facility repaid",
            }
        ]

    async def find_company_by_jurisdiction(self, jurisdiction: str) -> CompanyProfile | None:
        company = await self.get_company("COMP-024")
        if company and company.jurisdiction == jurisdiction:
            return company
        return None


@pytest.mark.asyncio
async def test_run_credit_phase_prefers_registry_context_when_available(monkeypatch: pytest.MonkeyPatch):
    store = InMemoryEventStore()
    registry_client = FakeRuntimeRegistryClient()
    monkeypatch.setattr(scenarios, "_registry_client_for_store", lambda store: registry_client)

    result = await scenarios.run_credit_phase(store, "APEX-REG-DEMO", "COMP-024")
    session_events = await store.load_stream("agent-credit_analysis-sess-credit")
    tool_event = next(event for event in session_events if event.event_type == "AgentToolCalled")

    assert result["profile_source"] == "applicant_registry"
    assert result["profile"]["industry"] == "manufacturing"
    assert result["credit_decision"].risk_tier == RiskTier.HIGH
    assert "industry=manufacturing" in tool_event.payload["tool_output_summary"]
    assert "trajectory=DECLINING" in tool_event.payload["tool_output_summary"]


@pytest.mark.asyncio
async def test_resolve_montana_company_id_uses_registry_when_available(monkeypatch: pytest.MonkeyPatch):
    store = InMemoryEventStore()

    class MontanaRegistryClient(FakeRuntimeRegistryClient):
        async def find_company_by_jurisdiction(self, jurisdiction: str) -> CompanyProfile | None:
            if jurisdiction != "MT":
                return None
            return CompanyProfile(
                company_id="COMP-777",
                name="Montana Outfitters",
                industry="retail",
                naics="451110",
                jurisdiction="MT",
                legal_type="LLC",
                founded_year=2014,
                employee_count=30,
                risk_segment="MEDIUM",
                trajectory="STABLE",
                submission_channel="portal",
                ip_region="US-MT",
            )

    monkeypatch.setattr(scenarios, "_registry_client_for_store", lambda store: MontanaRegistryClient())

    company_id = await scenarios.resolve_montana_company_id(store)

    assert company_id == "COMP-777"
