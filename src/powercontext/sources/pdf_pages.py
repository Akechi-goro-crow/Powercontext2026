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
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Annotated, NoReturn, Protocol
from urllib.parse import quote, unquote

from pydantic import AfterValidator, BaseModel, Field
from pypdf import PdfReader
from pypdf.generic import DictionaryObject

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.runtime.models import (
        MemoryEntriesPage,
        MemoryEntryRecord,
        MemorySearchPage,
        SearchMemoryRequest,
    )

PDF_PAGE_SOURCE_NAME = "pdf-page"
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "has",
    "have",
    "had",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "their",
    "there",
    "any",
    "all",
    "only",
    "not",
    "if",
    "then",
    "than",
    "into",
    "about",
    "give",
    "provide",
    "answer",
    "respond",
    "question",
    "assume",
    "assuming",
    "perspective",
    "investment",
    "analyst",
    "use",
    "using",
    "based",
    "shown",
    "details",
    "please",
    "calculate",
    "define",
    "report",
    "reported",
    "annual",
    "fiscal",
    "fy",
    "company",
    "corporation",
    "corp",
    "inc",
    "limited",
    "ltd",
    "plc",
    "s",
    "k",
])
# General accounting vocabulary, independent of any company, question or evidence page.
_CONCEPTS = (
    (
        "geography geographic geographies operates",
        "geographic regions segments divisions countries international operations north south america europe asia africa australia pacific middle east",
    ),
    ("legal lawsuit lawsuits battle battles litigation", "legal proceedings litigation contingencies"),
    ("revenue revenues turnover", "revenue revenues sales"),
    ("profit profits profitability margin", "income earnings profit revenue sales"),
    ("ebitda", "operating income depreciation amortization"),
    ("capex", "capital expenditures purchases property plant equipment"),
    ("debt borrowings", "debt borrowings borrowing obligations"),
    ("liquidity", "current assets liabilities cash"),
    ("inventory inventories", "inventory inventories"),
    ("buyback buybacks repurchase repurchases", "repurchase repurchases treasury shares stock"),
    ("dividend dividends", "dividend dividends distributions"),
    ("restructuring reorganization", "restructuring reorganization charges costs"),
    ("cogs", "cost goods sales sold"),
)
_HEADING_CUES = (
    (
        r"income statements?|statements? of (?:income|earnings|operations)|p\s*&\s*l|ebitda|margin",
        r"(?:consolidated|combined) statements? of (?:income|earnings|operations)|income statements?",
    ),
    (r"cash flows?|ebitda|capex|depreciation|amortization", r"(?:consolidated|combined) statements? of cash flows?"),
    (
        r"balance sheets?|assets|liabilities|working capital|equity|liquidity",
        r"(?:consolidated|combined) balance sheets?|statements? of financial (?:position|condition)",
    ),
    (
        r"geograph|regions?|countries",
        r"(?:our )?divisions?|(?:our )?segments?|geographic(?:al)? (?:information|areas|data)",
    ),
    (r"legal|lawsuits?|battles?|litigation", r"(?:item\s+\d+\.?\s*)?legal proceedings|litigation"),
)


def _document_id(value: str) -> str:
    if not value.strip():
        _invalid("document_id must not be blank")
    return value


class PDFDocumentInput(BaseModel):
    """One complete original PDF addressed by a caller-stable document id."""

    document_id: Annotated[str, AfterValidator(_document_id)]
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page, including blank pages, tied to the file digest."""

    document_id: Annotated[str, AfterValidator(_document_id)]
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(ge=1, strict=True)
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
    except Exception as error:
        detail = "PDF could not be parsed completely"
        raise ValueError(detail) from error


def _page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    source_id = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        # Preserve existing short identities, while accepting arbitrary document IDs.
        # A separate namespace prevents ambiguity with a literal user-supplied ID.
        identity = json.dumps([document_id, document_sha256, page_number], ensure_ascii=False, separators=(",", ":"))
        source_id = "pdf-sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return source_id


class PDFPageSourceAdapter:
    """Resolve one parsed page and read that same page back."""

    input_class: type[PDFPageInput] = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class: type[PDFPageSource] = PDFPageSource

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
    source_class: type[PDFPageSource] = PDFPageSource
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


class _ScopedMemory(Protocol):
    async def list(self) -> MemoryEntriesPage: ...

    async def search(self, request: SearchMemoryRequest, /) -> MemorySearchPage: ...


async def search_pdf_pages(memory: _ScopedMemory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Search a scoped Memory and return its exact, distinct PDF page citations.

    FTS supplies a lexical ranking. Public ``list`` supplies active page bodies for
    collection statistics and synonym recall, including pages omitted by the FTS
    admission threshold. No PDF, index file, model, or external service is read here.
    """

    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if limit < 1 or not question.strip():
        return ()
    terms = _query_terms(question)
    if not terms:
        return ()
    listed = await memory.list()
    pages = _stored_pages(listed)
    if not pages:
        return ()
    candidate_limit = min(len(pages), max(80, limit * 4))
    result = await memory.search(SearchMemoryRequest(query=" ".join(terms), mode="fts", limit=candidate_limit))
    lexical_ranks = {(hit.entry_id, hit.entry_version_id): rank for rank, hit in enumerate(result.hits, start=1)}
    ranked = _rank_pages(question, terms, pages, lexical_ranks)
    return tuple(page.record.citation for page in ranked[:limit])


def _pages(document_id: str, content: bytes) -> tuple[PDFPageInput, ...]:
    digest = hashlib.sha256(content).hexdigest()
    # Strict mode rejects damaged objects instead of silently repairing a report
    # into a shorter document or treating an unreadable page as an empty page.
    reader = PdfReader(BytesIO(content), strict=True)
    if reader.is_encrypted and reader.decrypt("") == 0:
        _invalid("PDF user password is not empty")
    pages: list[PDFPageInput] = []
    for page_number, page in enumerate(reader.pages, start=1):
        extracted = page.extract_text() or ""
        text = "" if extracted.strip() == "" else extracted
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
    page_tree = reader.root_object["/Pages"]
    if not isinstance(page_tree, DictionaryObject) or page_tree.get("/Count") != len(pages):
        _invalid("PDF page tree does not match its declared page count")
    return tuple(pages)


@dataclass(frozen=True)
class _Page:
    record: MemoryEntryRecord
    source_id: str
    document_id: str
    text: str
    terms: Counter[str]


def _stored_pages(listed: MemoryEntriesPage) -> list[_Page]:
    pages: list[_Page] = []
    seen: set[str] = set()
    for record in listed.entries:
        if record.state != "active":
            continue
        sources = {source.source_id for source in record.entry.sources if source.source_type == PDF_PAGE_SOURCE_NAME}
        # An aggregated entry cannot promise a citation to exactly one physical page.
        if len(sources) != 1:
            continue
        source_id = next(iter(sources))
        if source_id in seen:
            continue
        seen.add(source_id)
        document_id, _, text = record.entry.text.partition("\n")
        if source_id.startswith("pdf:"):
            document_id = unquote(source_id.split(":", 3)[1])
            # Memory strips outer whitespace, so blank pages contain only the ID.
            text = (
                ""
                if record.entry.text == document_id.strip()
                else record.entry.text.removeprefix(document_id.lstrip() + "\n")
            )
        pages.append(_Page(record, source_id, document_id, text, Counter(_tokens(text))))
    return pages


def _tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"\bfy\s*(\d{4})\b", r"\1", normalized)
    normalized = re.sub(r"\bd\s*&\s*a\b", "depreciation amortization", normalized)
    return tuple(_singular(word) for word in _WORDS.findall(normalized))


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _query_terms(question: str) -> dict[str, float]:
    terms = dict.fromkeys((term for term in _tokens(question) if term not in _STOP_WORDS), 1.0)
    original = set(terms)
    for cues, expansion in _CONCEPTS:
        if original.intersection(_tokens(cues)):
            for term in _tokens(expansion):
                terms.setdefault(term, 0.45)
    return terms


def _document_affinity(question: str, document_id: str) -> tuple[float, float]:
    query = set(_tokens(question))
    identifiers = set(_tokens(document_id)) - _STOP_WORDS - {"10k", "10q", "20f"}
    names = {term for term in identifiers if not term.isdigit()}
    name_match = len(names & query) / len(names) if names else 0.0
    # Document IDs sometimes join company names which the question writes as words.
    compact_name = "".join(term for term in _tokens(document_id) if term in names)
    if len(compact_name) >= 4 and compact_name in "".join(_tokens(question)):
        name_match = 1.0
    years = {term for term in identifiers if term.isdigit() and len(term) == 4}
    year_match = 1.0 if years & query else 0.0
    return name_match, year_match


def _rank_pages(
    question: str,
    query: dict[str, float],
    pages: list[_Page],
    lexical_ranks: dict[tuple[str, str], int],
) -> list[_Page]:
    frequencies: Counter[str] = Counter()
    for page in pages:
        frequencies.update(page.terms.keys())
    average_length = max(1.0, sum(page.terms.total() for page in pages) / len(pages))
    affinities = {page.document_id: _document_affinity(question, page.document_id) for page in pages}
    headings = tuple(pattern for cue, pattern in _HEADING_CUES if re.search(cue, question, re.IGNORECASE))
    scored: list[tuple[float, float, str, _Page]] = []
    for page in pages:
        score = _bm25(page.terms, query, frequencies, len(pages), average_length)
        if score <= 0:
            continue
        # Many reports put a "Table of Contents" navigation link on every page.
        # Match actual heading lines; that link must not suppress financial tables.
        lines = [" ".join(line.casefold().split()) for line in page.text.splitlines()[:12]]
        score += 20.0 * sum(any(re.match(pattern, line) for line in lines) for pattern in headings)
        entry = page.record.entry
        lexical_rank = lexical_ranks.get((entry.entry_id, entry.entry_version_id))
        if lexical_rank is not None:
            score += 2.0 / lexical_rank
        name_match, year_match = affinities[page.document_id]
        score += year_match
        # An explicitly named report outranks similar vocabulary in other companies.
        scored.append((name_match, score, page.source_id, page))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [page for _, _, _, page in scored]


def _bm25(
    terms: Counter[str],
    query: dict[str, float],
    frequencies: Counter[str],
    page_count: int,
    average_length: float,
) -> float:
    normalization = 1.2 * (0.45 + 0.55 * terms.total() / average_length)
    score = 0.0
    for term, weight in query.items():
        count = terms[term]
        if count:
            rarity = math.log1p((page_count - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += weight * rarity * count * 2.2 / (count + normalization)
    return score


def _invalid(detail: str) -> NoReturn:
    raise ValueError(detail)


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDFPageSourceAdapter(),
    projections=(PDFPageTextEvidenceProjection(),),
)
