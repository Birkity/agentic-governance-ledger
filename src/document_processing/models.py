from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.models.events import DocumentFormat, DocumentType, FinancialFacts


class DocumentPartResult(BaseModel):
    company_id: str
    document_type: DocumentType
    document_format: DocumentFormat
    path: str
    parser_used: str
    extracted_text: str = ""
    structured_data: dict[str, Any] = Field(default_factory=dict)
    financial_facts: FinancialFacts = Field(default_factory=FinancialFacts)
    extraction_notes: list[str] = Field(default_factory=list)
    summary: str | None = None


class DocumentPackageAnalysis(BaseModel):
    company_id: str
    documents: list[DocumentPartResult] = Field(default_factory=list)
    merged_financial_facts: FinancialFacts = Field(default_factory=FinancialFacts)
    field_sources: dict[str, str] = Field(default_factory=dict)
    consistency_notes: list[str] = Field(default_factory=list)
    package_summary: str | None = None

    def get_document(self, document_type: DocumentType) -> DocumentPartResult | None:
        for document in self.documents:
            if document.document_type == document_type:
                return document
        return None
