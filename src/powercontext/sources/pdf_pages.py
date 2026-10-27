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
    """Return distinct page citations, reranked from Memory's lexical candidates."""

    if limit < 1 or question.strip() == "":
        return ()
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    hits: list[Any] = []
    seen_queries: set[str] = set()
    for query, query_limit in _queries(question):
        if query in seen_queries:
            continue
        seen_queries.add(query)
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=query_limit))
        hits.extend(page.hits)
    selected: list[MemoryCitation] = []
    for hit in _rank_hits(question, hits):
        selected.append(
            MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
        )
        if len(selected) >= limit:
            break
    return tuple(selected)


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


def _queries(question: str) -> tuple[tuple[str, int], ...]:
    terms = list(dict.fromkeys(analyze_fts_query(question).split()))
    queries = [(question, _CANDIDATE_LIMIT)]
    anchors = [term for term in terms if any(character.isdigit() for character in term) or len(term) >= 7]
    queries.extend((term, _ANCHOR_LIMIT) for term in anchors[:12])
    for start in range(0, len(terms), 4):
        window = terms[start : start + 4]
        if len(window) >= 2:
            queries.append((" ".join(window), _ANCHOR_LIMIT))
    return tuple(queries)


def _rank_hits(question: str, hits: list[Any]) -> list[Any]:
    weights = _term_weights(question)
    ranked: list[tuple[float, float, Any]] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        key = (hit.entry_id, hit.entry_version_id)
        if key in seen:
            continue
        seen.add(key)
        page_terms = set(analyze_text(hit.text).split())
        overlap = sum(weight for term, weight in weights.items() if term in page_terms)
        coverage = 0.0 if not weights else sum(1 for term in weights if term in page_terms) / len(weights)
        ranked.append((overlap + coverage, hit.score, hit))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [hit for _overlap, _score, hit in ranked]


def _term_weights(question: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for term in analyze_fts_query(question).split():
        weight = 4.0 if any(character.isdigit() for character in term) else 1.0
        if len(term) >= 8:
            weight += 1.0
        weights[term] = weights.get(term, 0.0) + weight
    return weights


def _invalid(detail: str) -> NoReturn:
    raise ValueError(detail)


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDFPageSourceAdapter(),
    projections=(PDFPageTextEvidenceProjection(),),
)
