from __future__ import annotations

import json
import os
from typing import Protocol
from urllib import error, request

from src.document_processing.models import DocumentPackageAnalysis, DocumentPartResult


def _json_default(value):
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    return str(value)


class BaseSummarizer(Protocol):
    def summarize_document(self, document: DocumentPartResult) -> str:
        ...

    def summarize_package(self, analysis: DocumentPackageAnalysis) -> str:
        ...


class OllamaSummarizer:
    """Summarize extracted document parts with a local Ollama instance."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        part_model: str = "qwen3-coder:480b-cloud",
        package_model: str | None = "deepseek-v3.1:671b-cloud",
        timeout_seconds: int = 120,
        max_input_chars: int = 6000,
    ):
        self.base_url = base_url.rstrip("/")
        self.part_model = part_model
        self.package_model = package_model or part_model
        self.timeout_seconds = timeout_seconds
        self.max_input_chars = max_input_chars

    @classmethod
    def from_env(cls) -> "OllamaSummarizer":
        return cls(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            part_model=os.environ.get("OLLAMA_PART_MODEL", "qwen3-coder:480b-cloud"),
            package_model=os.environ.get("OLLAMA_PACKAGE_MODEL", "deepseek-v3.1:671b-cloud"),
            timeout_seconds=int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120")),
            max_input_chars=int(os.environ.get("OLLAMA_MAX_INPUT_CHARS", "6000")),
        )

    def summarize_document(self, document: DocumentPartResult) -> str:
        content = {
            "document_type": document.document_type.value,
            "path": document.path,
            "extraction_notes": document.extraction_notes,
            "structured_data": document.structured_data,
            "financial_facts": document.financial_facts.model_dump(mode="json", exclude_none=True),
            "text_excerpt": document.extracted_text[: self.max_input_chars],
        }
        prompt = (
            "Summarize this commercial lending document for both technical and non-technical readers. "
            "Explain what it contains, the most important numbers or facts, and any extraction caveats. "
            "Be concise, concrete, and avoid making a loan decision.\n\n"
            f"{json.dumps(content, indent=2, default=_json_default)}"
        )
        return self._generate(self.part_model, prompt)

    def summarize_package(self, analysis: DocumentPackageAnalysis) -> str:
        documents = []
        for document in analysis.documents:
            documents.append(
                {
                    "document_type": document.document_type.value,
                    "summary": document.summary,
                    "notes": document.extraction_notes,
                }
            )
        prompt = (
            "Summarize this applicant document package for a Week 5 Apex Ledger review. "
            "Write 2 short paragraphs. First explain the package in plain language. "
            "Then explain the technical extraction quality, main financial signals, and any caveats. "
            "Do not approve or decline the application.\n\n"
            f"{json.dumps({'company_id': analysis.company_id, 'documents': documents, 'consistency_notes': analysis.consistency_notes, 'merged_financial_facts': analysis.merged_financial_facts.model_dump(mode='json', exclude_none=True)}, indent=2, default=_json_default)}"
        )
        return self._generate(self.package_model, prompt)

    def _generate(self, model: str, prompt: str) -> str:
        prompts = [prompt, prompt[: max(1200, self.max_input_chars // 2)]]

        last_error: Exception | None = None
        for candidate_prompt in prompts:
            payload = json.dumps(
                {
                    "model": model,
                    "prompt": candidate_prompt,
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": 220},
                }
            ).encode("utf-8")
            req = request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with request.urlopen(req, timeout=self.timeout_seconds) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except error.URLError as exc:
                last_error = exc
                continue

            text = (body.get("response") or "").strip()
            if text:
                return text
            last_error = RuntimeError(f"Ollama returned an empty response for model '{model}'")

        raise RuntimeError(f"Ollama request failed for model '{model}': {last_error}")
