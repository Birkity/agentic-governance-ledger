import os
from decimal import Decimal

import pytest

from src.document_processing import (
    DocumentPackageProcessor,
    OllamaSummarizer,
    persist_document_package,
)
from src.document_processing.models import DocumentPackageAnalysis, DocumentPartResult
from src.document_processing.pipeline import _extract_pdf_text
from src.event_store import InMemoryEventStore
from src.models.events import DocumentType


class FakeSummarizer:
    def summarize_document(self, document: DocumentPartResult) -> str:
        return f"{document.document_type.value} summary"

    def summarize_package(self, analysis: DocumentPackageAnalysis) -> str:
        return f"package summary for {analysis.company_id}"


def test_document_processor_extracts_pdf_excel_and_csv_sources():
    processor = DocumentPackageProcessor(documents_root="documents")
    analysis = processor.process_company("COMP-024", include_summaries=False)

    income = analysis.get_document(DocumentType.INCOME_STATEMENT)
    balance = analysis.get_document(DocumentType.BALANCE_SHEET)
    workbook = analysis.get_document(DocumentType.FINANCIAL_WORKBOOK)
    csv_summary = analysis.get_document(DocumentType.FINANCIAL_SUMMARY)

    assert income is not None
    assert balance is not None
    assert workbook is not None
    assert csv_summary is not None

    assert income.financial_facts.total_revenue == Decimal("12352904")
    assert income.financial_facts.operating_expenses == Decimal("3695659")
    assert balance.financial_facts.total_assets == Decimal("33114352")
    assert workbook.financial_facts.current_ratio == pytest.approx(2.44)
    assert csv_summary.financial_facts.total_revenue == Decimal("12352904.44")
    assert analysis.merged_financial_facts.interest_coverage > 0


def test_application_proposal_extracts_registry_style_fields(monkeypatch):
    import src.document_processing.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "_try_docling_extract", lambda path: None)

    processor = DocumentPackageProcessor(documents_root="documents")
    analysis = processor.process_company("COMP-068", include_summaries=False)
    proposal = analysis.get_document(DocumentType.APPLICATION_PROPOSAL)

    assert proposal is not None
    assert proposal.structured_data["naics"] == "621111"
    assert proposal.structured_data["ein"] == "42-5196677"
    assert proposal.structured_data["founded_year"] == 2002
    assert proposal.structured_data["employee_count"] == 494


def test_missing_ebitda_from_pdf_is_preserved_and_backfilled():
    processor = DocumentPackageProcessor(documents_root="documents")
    analysis = processor.process_company("COMP-044", include_summaries=False)
    income = analysis.get_document(DocumentType.INCOME_STATEMENT)

    assert income is not None
    assert income.financial_facts.ebitda is None
    assert any("ebitda" in note.lower() for note in income.extraction_notes)
    assert analysis.merged_financial_facts.ebitda is not None
    assert analysis.field_sources["ebitda"] in {
        DocumentType.FINANCIAL_WORKBOOK.value,
        DocumentType.FINANCIAL_SUMMARY.value,
    }


def test_balance_sheet_discrepancy_becomes_consistency_note():
    processor = DocumentPackageProcessor(documents_root="documents")
    analysis = processor.process_company("COMP-044", include_summaries=False)

    assert any("balance sheet" in note.lower() for note in analysis.consistency_notes)


def test_fake_summarizer_adds_document_and_package_summaries():
    processor = DocumentPackageProcessor(
        documents_root="documents",
        summarizer=FakeSummarizer(),
    )
    analysis = processor.process_company("COMP-024", include_summaries=True)

    assert analysis.package_summary == "package summary for COMP-024"
    assert all(document.summary for document in analysis.documents)


def test_docling_is_preferred_when_available(monkeypatch):
    import src.document_processing.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "_try_docling_extract", lambda path: "docling text")
    text, parser_used = _extract_pdf_text(os.path.join("documents", "COMP-024", "income_statement_2024.pdf"))

    assert parser_used == "docling"
    assert text == "docling text"


@pytest.mark.asyncio
async def test_persist_document_package_writes_docpkg_events():
    processor = DocumentPackageProcessor(
        documents_root="documents",
        summarizer=FakeSummarizer(),
    )
    analysis = processor.process_company("COMP-024", include_summaries=True)
    store = InMemoryEventStore()

    positions = await persist_document_package(store, analysis, application_id="APEX-DOC-024")
    events = await store.load_stream("docpkg-APEX-DOC-024")
    event_types = [event["event_type"] for event in events]

    assert positions
    assert event_types[0] == "PackageCreated"
    assert event_types[-1] == "PackageReadyForAnalysis"
    assert event_types.count("DocumentAdded") == 5
    assert event_types.count("ExtractionCompleted") == 5
    assert event_types.count("QualityAssessmentCompleted") == 5

    extraction_event = next(
        event for event in events
        if event["event_type"] == "ExtractionCompleted"
        and event["payload"]["document_type"] == DocumentType.INCOME_STATEMENT.value
    )
    assert extraction_event["payload"]["facts"]["total_revenue"] == "12352904"
    assert extraction_event["metadata"]["parser_used"] in {"docling", "pdfplumber", "pypdf"}


@pytest.mark.skipif(
    os.environ.get("RUN_OLLAMA_TESTS") != "1",
    reason="Set RUN_OLLAMA_TESTS=1 to run live Ollama summary tests",
)
def test_ollama_summarizer_smoke():
    processor = DocumentPackageProcessor(
        documents_root="documents",
        summarizer=OllamaSummarizer.from_env(),
    )
    analysis = processor.process_company("COMP-024", include_summaries=True)
    income = analysis.get_document(DocumentType.INCOME_STATEMENT)

    assert income is not None
    assert income.summary
    assert analysis.package_summary
