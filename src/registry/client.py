"""Read-only Applicant Registry client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg


def _to_float(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _format_date(value: date | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class CompanyProfile:
    company_id: str
    name: str
    industry: str
    naics: str
    jurisdiction: str
    legal_type: str
    founded_year: int
    employee_count: int
    risk_segment: str
    trajectory: str
    submission_channel: str
    ip_region: str


@dataclass(frozen=True)
class FinancialYear:
    fiscal_year: int
    total_revenue: float
    gross_profit: float
    operating_income: float
    ebitda: float
    net_income: float
    total_assets: float
    total_liabilities: float
    total_equity: float
    long_term_debt: float
    cash_and_equivalents: float
    current_assets: float
    current_liabilities: float
    accounts_receivable: float
    inventory: float
    debt_to_equity: float | None
    current_ratio: float | None
    debt_to_ebitda: float | None
    interest_coverage_ratio: float | None
    gross_margin: float | None
    ebitda_margin: float | None
    net_margin: float | None


@dataclass(frozen=True)
class ComplianceFlag:
    flag_type: str
    severity: str
    is_active: bool
    added_date: str
    note: str | None


class ApplicantRegistryClient:
    """Read-only access to the seeded Applicant Registry schema."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def get_company(self, company_id: str) -> CompanyProfile | None:
        row = await self._pool.fetchrow(
            """
            SELECT company_id, name, industry, naics, jurisdiction, legal_type,
                   founded_year, employee_count, risk_segment, trajectory,
                   submission_channel, ip_region
            FROM applicant_registry.companies
            WHERE company_id = $1
            """,
            company_id,
        )
        if row is None:
            return None
        return CompanyProfile(
            company_id=row["company_id"],
            name=row["name"],
            industry=row["industry"],
            naics=row["naics"],
            jurisdiction=row["jurisdiction"],
            legal_type=row["legal_type"],
            founded_year=int(row["founded_year"]),
            employee_count=int(row["employee_count"]),
            risk_segment=row["risk_segment"],
            trajectory=row["trajectory"],
            submission_channel=row["submission_channel"],
            ip_region=row["ip_region"],
        )

    async def find_company_by_jurisdiction(self, jurisdiction: str) -> CompanyProfile | None:
        row = await self._pool.fetchrow(
            """
            SELECT company_id, name, industry, naics, jurisdiction, legal_type,
                   founded_year, employee_count, risk_segment, trajectory,
                   submission_channel, ip_region
            FROM applicant_registry.companies
            WHERE jurisdiction = $1
            ORDER BY company_id ASC
            LIMIT 1
            """,
            jurisdiction,
        )
        if row is None:
            return None
        return CompanyProfile(
            company_id=row["company_id"],
            name=row["name"],
            industry=row["industry"],
            naics=row["naics"],
            jurisdiction=row["jurisdiction"],
            legal_type=row["legal_type"],
            founded_year=int(row["founded_year"]),
            employee_count=int(row["employee_count"]),
            risk_segment=row["risk_segment"],
            trajectory=row["trajectory"],
            submission_channel=row["submission_channel"],
            ip_region=row["ip_region"],
        )

    async def get_financial_history(
        self,
        company_id: str,
        years: list[int] | None = None,
    ) -> list[FinancialYear]:
        if years:
            rows = await self._pool.fetch(
                """
                SELECT fiscal_year, total_revenue, gross_profit, operating_income, ebitda,
                       net_income, total_assets, total_liabilities, total_equity,
                       long_term_debt, cash_and_equivalents, current_assets,
                       current_liabilities, accounts_receivable, inventory,
                       debt_to_equity, current_ratio, debt_to_ebitda,
                       interest_coverage_ratio, gross_margin, ebitda_margin, net_margin
                FROM applicant_registry.financial_history
                WHERE company_id = $1 AND fiscal_year = ANY($2::int[])
                ORDER BY fiscal_year ASC
                """,
                company_id,
                years,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT fiscal_year, total_revenue, gross_profit, operating_income, ebitda,
                       net_income, total_assets, total_liabilities, total_equity,
                       long_term_debt, cash_and_equivalents, current_assets,
                       current_liabilities, accounts_receivable, inventory,
                       debt_to_equity, current_ratio, debt_to_ebitda,
                       interest_coverage_ratio, gross_margin, ebitda_margin, net_margin
                FROM applicant_registry.financial_history
                WHERE company_id = $1
                ORDER BY fiscal_year ASC
                """,
                company_id,
            )

        return [
            FinancialYear(
                fiscal_year=int(row["fiscal_year"]),
                total_revenue=float(row["total_revenue"]),
                gross_profit=float(row["gross_profit"]),
                operating_income=float(row["operating_income"]),
                ebitda=float(row["ebitda"]),
                net_income=float(row["net_income"]),
                total_assets=float(row["total_assets"]),
                total_liabilities=float(row["total_liabilities"]),
                total_equity=float(row["total_equity"]),
                long_term_debt=float(row["long_term_debt"]),
                cash_and_equivalents=float(row["cash_and_equivalents"]),
                current_assets=float(row["current_assets"]),
                current_liabilities=float(row["current_liabilities"]),
                accounts_receivable=float(row["accounts_receivable"]),
                inventory=float(row["inventory"]),
                debt_to_equity=_to_float(row["debt_to_equity"]),
                current_ratio=_to_float(row["current_ratio"]),
                debt_to_ebitda=_to_float(row["debt_to_ebitda"]),
                interest_coverage_ratio=_to_float(row["interest_coverage_ratio"]),
                gross_margin=_to_float(row["gross_margin"]),
                ebitda_margin=_to_float(row["ebitda_margin"]),
                net_margin=_to_float(row["net_margin"]),
            )
            for row in rows
        ]

    async def get_compliance_flags(
        self,
        company_id: str,
        active_only: bool = False,
    ) -> list[ComplianceFlag]:
        if active_only:
            rows = await self._pool.fetch(
                """
                SELECT flag_type, severity, is_active, added_date, note
                FROM applicant_registry.compliance_flags
                WHERE company_id = $1 AND is_active = TRUE
                ORDER BY added_date ASC, id ASC
                """,
                company_id,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT flag_type, severity, is_active, added_date, note
                FROM applicant_registry.compliance_flags
                WHERE company_id = $1
                ORDER BY added_date ASC, id ASC
                """,
                company_id,
            )

        return [
            ComplianceFlag(
                flag_type=row["flag_type"],
                severity=row["severity"],
                is_active=bool(row["is_active"]),
                added_date=_format_date(row["added_date"]),
                note=row["note"],
            )
            for row in rows
        ]

    async def get_loan_relationships(self, company_id: str) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            SELECT loan_amount, loan_year, was_repaid, default_occurred, note
            FROM applicant_registry.loan_relationships
            WHERE company_id = $1
            ORDER BY loan_year ASC, id ASC
            """,
            company_id,
        )
        return [
            {
                "loan_amount": float(row["loan_amount"]),
                "loan_year": int(row["loan_year"]),
                "was_repaid": bool(row["was_repaid"]),
                "default_occurred": bool(row["default_occurred"]),
                "note": row["note"],
            }
            for row in rows
        ]
