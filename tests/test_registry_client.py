from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.registry import ApplicantRegistryClient


class FakePool:
    def __init__(self) -> None:
        self.company = {
            "company_id": "COMP-001",
            "name": "Acme Manufacturing",
            "industry": "Manufacturing",
            "naics": "332710",
            "jurisdiction": "TX",
            "legal_type": "LLC",
            "founded_year": 2012,
            "employee_count": 84,
            "risk_segment": "LOW",
            "trajectory": "GROWTH",
            "submission_channel": "portal",
            "ip_region": "US-TX",
        }
        self.financial_rows = [
            {
                "fiscal_year": 2023,
                "total_revenue": Decimal("1000.00"),
                "gross_profit": Decimal("400.00"),
                "operating_income": Decimal("120.00"),
                "ebitda": Decimal("150.00"),
                "net_income": Decimal("90.00"),
                "total_assets": Decimal("2000.00"),
                "total_liabilities": Decimal("900.00"),
                "total_equity": Decimal("1100.00"),
                "long_term_debt": Decimal("300.00"),
                "cash_and_equivalents": Decimal("500.00"),
                "current_assets": Decimal("1000.00"),
                "current_liabilities": Decimal("400.00"),
                "accounts_receivable": Decimal("200.00"),
                "inventory": Decimal("150.00"),
                "debt_to_equity": Decimal("0.2727"),
                "current_ratio": Decimal("2.5000"),
                "debt_to_ebitda": Decimal("2.0000"),
                "interest_coverage_ratio": Decimal("4.0000"),
                "gross_margin": Decimal("0.4000"),
                "ebitda_margin": Decimal("0.1500"),
                "net_margin": Decimal("0.0900"),
            },
            {
                "fiscal_year": 2024,
                "total_revenue": Decimal("1200.00"),
                "gross_profit": Decimal("500.00"),
                "operating_income": Decimal("160.00"),
                "ebitda": Decimal("210.00"),
                "net_income": Decimal("110.00"),
                "total_assets": Decimal("2300.00"),
                "total_liabilities": Decimal("1000.00"),
                "total_equity": Decimal("1300.00"),
                "long_term_debt": Decimal("280.00"),
                "cash_and_equivalents": Decimal("540.00"),
                "current_assets": Decimal("1150.00"),
                "current_liabilities": Decimal("430.00"),
                "accounts_receivable": Decimal("220.00"),
                "inventory": Decimal("175.00"),
                "debt_to_equity": Decimal("0.2154"),
                "current_ratio": Decimal("2.6744"),
                "debt_to_ebitda": Decimal("1.3333"),
                "interest_coverage_ratio": Decimal("5.1000"),
                "gross_margin": Decimal("0.4167"),
                "ebitda_margin": Decimal("0.1750"),
                "net_margin": Decimal("0.0917"),
            },
        ]
        self.flags = [
            {
                "flag_type": "AML_WATCH",
                "severity": "MEDIUM",
                "is_active": True,
                "added_date": date(2024, 5, 1),
                "note": "monitor",
            },
            {
                "flag_type": "PEP_LINK",
                "severity": "LOW",
                "is_active": False,
                "added_date": date(2023, 3, 15),
                "note": "historical",
            },
        ]
        self.relationships = [
            {
                "loan_amount": Decimal("250000.00"),
                "loan_year": 2021,
                "was_repaid": True,
                "default_occurred": False,
                "note": "closed cleanly",
            }
        ]

    async def fetchrow(self, query: str, *args):
        if "FROM applicant_registry.companies" in query:
            if "WHERE company_id = $1" in query:
                company_id = args[0]
                return self.company if company_id == self.company["company_id"] else None
            if "WHERE jurisdiction = $1" in query:
                jurisdiction = args[0]
                return self.company if jurisdiction == self.company["jurisdiction"] else None
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *args):
        if "FROM applicant_registry.financial_history" in query:
            rows = list(self.financial_rows)
            if "ANY($2::int[])" in query:
                years = set(args[1])
                rows = [row for row in rows if row["fiscal_year"] in years]
            return rows
        if "FROM applicant_registry.compliance_flags" in query:
            rows = list(self.flags)
            if "is_active = TRUE" in query:
                rows = [row for row in rows if row["is_active"]]
            return rows
        if "FROM applicant_registry.loan_relationships" in query:
            return list(self.relationships)
        raise AssertionError(f"Unexpected fetch query: {query}")


@pytest.mark.asyncio
async def test_registry_client_returns_company_profile():
    client = ApplicantRegistryClient(FakePool())

    company = await client.get_company("COMP-001")

    assert company is not None
    assert company.company_id == "COMP-001"
    assert company.risk_segment == "LOW"


@pytest.mark.asyncio
async def test_registry_client_can_find_company_by_jurisdiction():
    client = ApplicantRegistryClient(FakePool())

    company = await client.find_company_by_jurisdiction("TX")

    assert company is not None
    assert company.company_id == "COMP-001"


@pytest.mark.asyncio
async def test_registry_client_filters_financial_history_by_years():
    client = ApplicantRegistryClient(FakePool())

    history = await client.get_financial_history("COMP-001", years=[2024])

    assert len(history) == 1
    assert history[0].fiscal_year == 2024
    assert history[0].current_ratio == pytest.approx(2.6744)


@pytest.mark.asyncio
async def test_registry_client_filters_active_compliance_flags():
    client = ApplicantRegistryClient(FakePool())

    flags = await client.get_compliance_flags("COMP-001", active_only=True)

    assert len(flags) == 1
    assert flags[0].flag_type == "AML_WATCH"
    assert flags[0].added_date == "2024-05-01"


@pytest.mark.asyncio
async def test_registry_client_returns_loan_relationships():
    client = ApplicantRegistryClient(FakePool())

    relationships = await client.get_loan_relationships("COMP-001")

    assert relationships == [
        {
            "loan_amount": 250000.0,
            "loan_year": 2021,
            "was_repaid": True,
            "default_occurred": False,
            "note": "closed cleanly",
        }
    ]
