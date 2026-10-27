# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capture every physical PDF page and retrieve evidence pages through Memory."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from io import BytesIO
from typing import Any, NoReturn
from urllib.parse import quote

from pydantic import BaseModel
from pypdf import PdfReader

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
_CANDIDATE_LIMIT = 80
_ANCHOR_LIMIT = 20
_INCOME_WEIGHT = 2.6
_CASH_WEIGHT = 1.2
_LEGAL_WEIGHT = 2.0
_GEO_WEIGHT = 1.8
_INCOME_TERMS = frozenset({"revenue", "revenues", "sales", "operating", "income", "profit", "earnings", "expenses"})
_CASH_TERMS = frozenset({"depreciation", "amortization", "expenditures", "cash", "flows", "purchases"})
_LEGAL_TERMS = frozenset({"proceedings", "litigation", "legal"})
_GEO_TERMS = frozenset({"segments", "countries", "america", "europe", "latin", "asia", "africa", "operations"})
_FINANCIAL_CUES = frozenset({"ebitda", "capex", "margin"})
_LEGAL_CUES = frozenset({"legal", "lawsuit", "lawsuits", "battles", "battle"})
_GEO_CUES = frozenset({"geographies", "geography", "geographic"})
_QUERY_EXPANSIONS = {
    "geographies": ("segments", "countries", "america", "europe", "latin", "operations"),
    "geography": ("segments", "countries", "america", "europe", "latin", "operations"),
    "geographic": ("segments", "countries", "america", "europe", "latin", "operations"),
    "operates": ("operations", "segments"),
    "battles": ("proceedings", "litigation"),
    "battle": ("proceedings", "litigation"),
    "lawsuit": ("proceedings", "litigation"),
    "lawsuits": ("proceedings", "litigation"),
    "legal": ("proceedings", "litigation"),
    "ebitda": ("depreciation", "amortization"),
    "capex": ("expenditures", "purchases"),
    "margin": ("revenue", "sales", "profit"),
}


class PDFDocumentInput(BaseModel):
    """One complete original PDF addressed by a caller-stable document id."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page, including blank pages, tied to the file digest."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """Captured page whose identity changes when the PDF bytes change."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page before returning any of them."""

    if value.document_id.strip() == "":
        _invalid("document_id must not be blank")
    try:
        return _pages(value.document_id, value.content)
    except ValueError:
        raise
    except Exception:
        _invalid("PDF could not be parsed completely")


def _page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    source_id = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        _invalid("source id must not exceed 256 characters")
    return source_id


class PDFPageSourceAdapter:
    """Resolve one parsed page and read that same page back."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = _page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one page as document-tagged text addressed by its physical page number."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return distinct page citations after ranking every stored page for the question."""

    if limit < 1 or question.strip() == "":
        return ()
    records = await _stored_pages(memory, question)
    return tuple(record.citation for record in _rank_pages(question, records)[:limit])


def _pages(document_id: str, content: bytes) -> tuple[PDFPageInput, ...]:
    digest = hashlib.sha256(content).hexdigest()
    reader = PdfReader(BytesIO(content))
    if reader.is_encrypted and reader.decrypt("") == 0:
        _invalid("PDF user password is not empty")
    pages: list[PDFPageInput] = []
    for page_number, page in enumerate(reader.pages, start=1):
        extracted = page.extract_text() or ""
        text = "" if extracted.strip() == "" else extracted
        _page_source_id(document_id, digest, page_number)
        pages.append(
            PDFPageInput(
                document_id=document_id,
                document_sha256=digest,
                page_number=page_number,
                text=text,
            )
        )
    if not pages:
        _invalid("PDF contains no pages")
    return tuple(pages)


async def _stored_pages(memory: Any, question: str) -> list[Any]:
    if hasattr(memory, "list"):
        listed = await memory.list()
        if listed.entries:
            return list(listed.entries)
    return await _search_pages(memory, question)


async def _search_pages(memory: Any, question: str) -> list[Any]:
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    hits: list[Any] = []
    seen_queries: set[str] = set()
    for query, query_limit in _queries(question):
        if query in seen_queries:
            continue
        seen_queries.add(query)
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=query_limit))
        hits.extend(_SearchPage(hit) for hit in page.hits)
    return hits


def _queries(question: str) -> tuple[tuple[str, int], ...]:
    terms = _question_terms(question)
    queries = [(question, _CANDIDATE_LIMIT)]
    anchors = [term for term in terms if any(character.isdigit() for character in term) or len(term) >= 7]
    queries.extend((term, _ANCHOR_LIMIT) for term in anchors[:12])
    for start in range(0, len(terms), 4):
        window = terms[start : start + 4]
        if len(window) >= 2:
            queries.append((" ".join(window), _ANCHOR_LIMIT))
    return tuple(queries)


def _rank_pages(question: str, records: list[Any]) -> list[Any]:
    unique = _unique_pages(records)
    term_sets = [set(analyze_text(_page_text(record)).split()) for record in unique]
    document_frequency: Counter[str] = Counter()
    for terms in term_sets:
        document_frequency.update(terms)
    cues = set(_question_terms(question))
    anchors = _expanded_terms(cues)
    scores = [_page_score(terms, anchors, cues, document_frequency, len(term_sets)) for terms in term_sets]
    order = sorted(range(len(unique)), key=lambda index: scores[index], reverse=True)
    return [unique[index] for index in order]


def _page_text(record: Any) -> str:
    entry = getattr(record, "entry", None)
    if entry is not None:
        return entry.text
    return record.text


def _unique_pages(records: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.citation.entry_id, record.citation.entry_version_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _question_terms(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(analyze_fts_query(question).split()))


def _expanded_terms(terms: set[str]) -> tuple[str, ...]:
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        expanded.extend(_QUERY_EXPANSIONS.get(term, ()))
    return tuple(dict.fromkeys(expanded))


def _page_score(
    terms: set[str],
    anchors: tuple[str, ...],
    cues: set[str],
    document_frequency: Counter[str],
    page_count: int,
) -> float:
    score = sum(math.log((page_count + 1) / (document_frequency[anchor] + 1)) for anchor in anchors if anchor in terms)
    if cues & _FINANCIAL_CUES:
        score += _INCOME_WEIGHT * len(_INCOME_TERMS & terms) + _CASH_WEIGHT * len(_CASH_TERMS & terms)
    if cues & _LEGAL_CUES:
        score += _LEGAL_WEIGHT * len(_LEGAL_TERMS & terms)
    if cues & _GEO_CUES:
        score += _GEO_WEIGHT * len(_GEO_TERMS & terms)
    return score


class _SearchPage:
    """Search hit shaped like a listed Memory entry for ranking."""

    def __init__(self, hit: Any) -> None:
        self.text = hit.text
        self.citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )


def _invalid(detail: str) -> NoReturn:
    raise ValueError(detail)


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDFPageSourceAdapter(),
    projections=(PDFPageTextEvidenceProjection(),),
)
