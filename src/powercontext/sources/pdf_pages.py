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
import zlib
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Annotated, NoReturn, Protocol
from urllib.parse import quote, unquote

from pydantic import AfterValidator, BaseModel, Field
from pypdf import PageObject, PdfReader, get_configuration
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NullObject,
    NumberObject,
    PdfObject,
    StreamObject,
)

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
_SPACED_GLYPHS = re.compile(r"(?<!\w)(?:[A-Za-z0-9][ \t]+){7,}[A-Za-z0-9](?!\w)")
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
_STATEMENT_HEADINGS = {
    "income": (
        r"(?:(?:consolidated|combined) )?(?:statements? of (?:income|earnings|operations|loss)"
        r"(?: \(loss\))?(?: and comprehensive (?:income|loss)(?: \(loss\))?)?|income statements)"
    ),
    "cash": r"(?:(?:consolidated|combined) )?(?:statements? of cash flows?|cash flows? statements?)",
    "balance": (r"(?:(?:consolidated|combined) )?(?:balance sheets?|statements? of financial (?:position|condition))"),
}
_HEADING_CUES = (
    (
        r"income statements?|statements? of (?:income|earnings|operations)|p\s*&\s*l|ebitda|margin",
        _STATEMENT_HEADINGS["income"],
    ),
    (
        r"cash flows?|ebitda|capex|depreciation|amortization",
        _STATEMENT_HEADINGS["cash"],
    ),
    (
        r"balance sheets?|assets|liabilities|working capital|equity|liquidity",
        _STATEMENT_HEADINGS["balance"],
    ),
    (
        r"geograph|regions?|countries",
        r"(?:our )?divisions?|(?:our )?segments?|geographic(?:al)? (?:information|areas|data)",
    ),
    (r"legal|lawsuits?|battles?|litigation", r"legal proceedings|litigation"),
)

# Retrieve each input of a financial metric independently. These are accounting
# relationships, not answers: only text from the caller's Memory is ranked.
_FINANCIAL_COMPONENTS = {
    "revenue": ("income", "revenue sales turnover"),
    "income": ("income", "net income earnings profit"),
    "operating": ("income", "operating income earnings profit"),
    "cost": ("income", "cost revenue sales goods sold"),
    "interest": ("income", "interest expense finance costs"),
    "tax": ("income", "income before taxes provision tax expense"),
    "assets": ("balance", "total assets"),
    "equity": ("balance", "total shareholders stockholders equity"),
    "receivables": ("balance", "trade accounts receivable receivables"),
    "inventory": ("balance", "inventories inventory"),
    "payables": ("balance", "trade accounts payable payables"),
    "current": ("balance", "current assets current liabilities"),
    "cash": ("balance", "cash equivalents short term investments"),
    "debt": ("balance", "debt borrowings obligations liabilities"),
    "operating_cash": ("cash", "net cash provided used operating activities operations"),
    "capex": ("cash", "capital expenditures spending additions purchases property plant equipment"),
    "depreciation": ("cash", "depreciation amortization"),
    "dividends": ("cash", "cash dividends paid distributions"),
}
_METRIC_COMPONENTS = (
    (r"\b(?:revenue|sales|turnover)\b", ("revenue",)),
    (r"\b(?:net income|earnings|profit|margin|income statements?|p\s*&\s*l)\b", ("income", "revenue")),
    (r"\b(?:operating income|operating profit|ebit)\b", ("operating",)),
    (r"\b(?:gross|cogs|cost of (?:goods|sales|revenue))\b", ("revenue", "cost")),
    (r"\b(?:roa|return on (?:total )?assets?)\b", ("income", "assets")),
    (r"\b(?:roe|return on (?:shareholders.? |stockholders.? )?equity)\b", ("income", "equity")),
    (r"\b(?:roic|return on invested capital)\b", ("operating", "tax", "debt", "equity", "cash")),
    (r"\b(?:dso|days sales outstanding|receivables? turnover)\b", ("receivables", "revenue")),
    (r"\b(?:dpo|days payables? outstanding|payables? turnover)\b", ("payables", "cost")),
    (r"\b(?:dio|days inventory outstanding|inventory turnover)\b", ("inventory", "cost")),
    (
        r"\b(?:cash conversion cycle|cash operating cycle)\b",
        ("receivables", "inventory", "payables", "revenue", "cost"),
    ),
    (r"\bassets? turnover\b", ("revenue", "assets")),
    (r"\b(?:working capital|liquidity|current ratio|quick ratio)\b", ("current", "cash", "inventory")),
    (r"\b(?:cash balance|cash ratio|cash equivalents)\b", ("cash", "current")),
    (r"\b(?:debt|borrowings|leverage)\b", ("debt", "equity")),
    (r"\binterest coverage\b", ("operating", "interest")),
    (r"\b(?:effective tax|tax rate)\b", ("tax",)),
    (r"\b(?:ebitda)\b", ("operating", "depreciation")),
    (r"\b(?:capex|capital (?:expenditures?|spending))\b", ("capex",)),
    (r"\b(?:free cash flow|fcf)\b", ("operating_cash", "capex")),
    (r"\b(?:cash flows?|operating cash|cash conversion ratio)\b", ("operating_cash",)),
    (r"\b(?:depreciation|amortization|d\s*&\s*a)\b", ("depreciation",)),
    (r"\bdividends?\b", ("dividends",)),
    (r"\bpayout ratio\b", ("dividends", "income")),
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
    reader = PdfReader(BytesIO(content), strict=True)
    if reader.is_encrypted and reader.decrypt("") == 0:
        _invalid("PDF user password is not empty")
    # pypdf's strict mode still repairs some streams and ignores child counts.
    # Validate before accepting text, including pages that have no font resources.
    page_count = _validate_page_tree(reader.root_object["/Pages"], set())
    pages: list[PDFPageInput] = []
    for page_number, page in enumerate(reader.pages, start=1):
        _validate_contents(
            page.get("/Contents", NullObject()),
            reader,
            page.get("/Resources", DictionaryObject()).get_object(),
            set(),
        )
        extracted = _extract_page_text(page)
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
    if page_count != len(pages):
        _invalid("PDF page tree does not match its declared page count")
    return tuple(pages)


def _extract_page_text(page: PageObject) -> str:
    text = page.extract_text() or ""
    if not _SPACED_GLYPHS.search(text):
        return text
    # Some producers place each glyph in a separate text object with a trailing
    # TJ displacement. Plain extraction mistakes that displacement for a space.
    # Use layout only to restore word boundaries of an exact character match;
    # retain plain text elsewhere (including rotated text omitted by layout).
    layout = page.extract_text(extraction_mode="layout") or ""
    offsets = [index for index, character in enumerate(layout) if not character.isspace()]
    compact = "".join(layout[index] for index in offsets)

    def restore(match: re.Match[str]) -> str:
        original = match.group()
        if not _SPACED_GLYPHS.search(original):
            return original
        characters = "".join(original.split())
        start = compact.find(characters)
        if start < 0 or compact.find(characters, start + 1) >= 0:
            return original
        replacement = " ".join(layout[offsets[start] : offsets[start + len(characters) - 1] + 1].split())
        if len(_WORDS.findall(replacement)) >= len(_WORDS.findall(original)):
            return original
        return replacement

    return re.sub(r"(?m)^[^\r\n]+", restore, text)


def _validate_page_tree(value: PdfObject, seen: set[int]) -> int:
    node = value.get_object()
    if not isinstance(node, DictionaryObject) or id(node) in seen:
        _invalid("PDF page tree contains an invalid or repeated node")
    seen.add(id(node))
    node_type = node.get("/Type", NullObject()).get_object()
    if node_type == "/Page":
        return 1
    if node_type != "/Pages":
        _invalid("PDF page tree contains an invalid node type")
    # Indexing resolves legal indirect references; dict.get does not.
    children, declared = node["/Kids"], node["/Count"]
    if not isinstance(children, ArrayObject) or not isinstance(declared, NumberObject):
        _invalid("PDF page tree has invalid children or count")
    actual = sum(_validate_page_tree(child, seen) for child in children)
    if actual != declared:
        _invalid("PDF page subtree does not match its declared page count")
    return actual


def _validate_contents(contents: PdfObject, reader: PdfReader, resources: PdfObject, forms: set[int]) -> None:
    resolved = contents.get_object()
    if isinstance(resolved, NullObject):
        return
    streams = resolved if isinstance(resolved, ArrayObject) else (resolved,)
    for reference in streams:
        if reference is None:
            _invalid("PDF contents reference cannot be resolved")
        stream = reference.get_object()
        if isinstance(stream, NullObject):
            continue
        if not isinstance(stream, StreamObject):
            _invalid("PDF contents must be a stream or an array of streams")
        _validate_stream_filters(stream)
    # Parse instructions even when extract_text would shortcut to an empty string.
    # Form extraction catches exceptions internally, so validate invoked forms too.
    for operands, operator in ContentStream(resolved, reader, "bytes").operations:
        if operator != b"Do":
            continue
        if len(operands) != 1 or not isinstance(resources, DictionaryObject):
            _invalid("PDF XObject invocation has invalid resources or operands")
        xobjects = resources["/XObject"]
        if not isinstance(xobjects, DictionaryObject):
            _invalid("PDF XObject resources must be a dictionary")
        _validate_form(xobjects[operands[0]], reader, resources, forms)


def _validate_form(form: PdfObject, reader: PdfReader, resources: PdfObject, forms: set[int]) -> None:
    if not isinstance(form, StreamObject):
        _invalid("PDF XObject must be a stream")
    if form.get("/Subtype", NullObject()).get_object() != "/Form":
        return  # Image data is not a PDF instruction stream; no OCR is required.
    if id(form) in forms:
        _invalid("PDF contains a recursive Form XObject")
    forms.add(id(form))
    try:
        _validate_contents(form, reader, form.get("/Resources", resources).get_object(), forms)
    finally:
        forms.remove(id(form))


def _validate_stream_filters(stream: StreamObject) -> None:
    filters = stream.get("/Filter", NullObject()).get_object()
    if isinstance(filters, NullObject):
        return
    filters = filters if isinstance(filters, ArrayObject) else ArrayObject([filters])
    params = stream.get("/DecodeParms", NullObject()).get_object()
    if isinstance(params, NullObject):
        params = ArrayObject([NullObject() for _ in filters])
    elif not isinstance(params, ArrayObject):
        params = ArrayObject([params])
    if len(params) != len(filters):
        _invalid("PDF stream filters and parameters do not match")
    for index, name in enumerate(filters):
        if name.get_object() not in ("/FlateDecode", "/Fl"):
            continue
        # The pinned pypdf decoder silently recovers corrupt/truncated Flate data.
        # Decode preceding filters on a copy, then require a complete zlib stream.
        prefix = EncodedStreamObject()
        prefix._data = stream._data
        prefix[NameObject("/Filter")] = ArrayObject(filters[:index])
        prefix[NameObject("/DecodeParms")] = ArrayObject(params[:index])
        _validate_flate_data(prefix.get_data())


def _validate_flate_data(pending: bytes) -> None:
    decoder = zlib.decompressobj()
    maximum_length = get_configuration().zlib_maximum_output_length
    decoded_length = 0
    while True:
        chunk = decoder.decompress(pending, 65536)
        decoded_length += len(chunk)
        if maximum_length and decoded_length > maximum_length:
            _invalid("PDF Flate stream exceeds the configured decoding limit")
        pending = decoder.unconsumed_tail
        if decoder.eof:
            return
        if not pending and len(chunk) < 65536:
            _invalid("PDF contains a truncated Flate stream")


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


def _heading_matches(text: str, patterns: tuple[str, ...]) -> int:
    # A section may start after a long table or paragraph, anywhere on the page.
    # Match whole lines: index page numbers and exhibit references are not headings.
    if not patterns:
        return 0
    lines = [" ".join(line.split()) for line in unicodedata.normalize("NFKC", text).casefold().splitlines()]
    headings: set[str] = set()
    for start in range(len(lines)):
        # Narrow columns can wrap a title; never join a heading to a long paragraph.
        for end in range(start + 1, min(start + 4, len(lines) + 1)):
            heading = " ".join(lines[start:end]).strip(" .:")
            if len(heading) > 160:
                break
            heading = re.sub(r"^(?:item\s+\d+[a-z]?[.:]?\s*|(?:\(?unaudited\)?|condensed)\s+)+", "", heading)
            heading = re.sub(
                r"(?:\s*\((?:continued|unaudited|in (?:thousands|millions|billions)[^()]*)\))+$", "", heading
            )
            headings.add(heading)
    return sum(any(re.fullmatch(pattern, heading) for heading in headings) for pattern in patterns)


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
    # A historical year alone is not evidence about a financial question.
    query = {term: weight for term, weight in query.items() if not re.fullmatch(r"(?:19|20)\d{2}", term)}
    headings = tuple(pattern for cue, pattern in _HEADING_CUES if re.search(cue, question, re.IGNORECASE))
    scored: list[tuple[float, str, _Page]] = []
    for page in pages:
        score = _bm25(page.terms, query, frequencies, len(pages), average_length)
        if score <= 0:
            continue
        score += 20.0 * _heading_matches(page.text, headings)
        entry = page.record.entry
        lexical_rank = lexical_ranks.get((entry.entry_id, entry.entry_version_id))
        if lexical_rank is not None:
            score += 2.0 / lexical_rank
        name_match, year_match = affinities[page.document_id]
        # Identity is a bounded prior, never a hard partition: company names can
        # overlap ordinary query words, and later reports contain comparative data.
        identity = set(_tokens(page.document_id))
        if any(page.terms[term] for term in query if term not in identity):
            score += 6.0 * name_match + 4.0 * year_match
        scored.append((score, page.source_id, page))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ranked = [page for _, _, page in scored]
    components = _rank_financial_components(question, pages, frequencies, average_length, affinities)
    return _merge_component_rankings(ranked, components)


def _merge_component_rankings(ranked: list[_Page], components: list[list[_Page]]) -> list[_Page]:
    if not components:
        return ranked
    # Preserve the best source for each required input before filling remaining
    # slots. Rank fusion alone can let one verbose topic crowd out another input.
    # Keep the overall intent first: e.g. lawsuits about revenue recognition are
    # primarily a legal question even though revenue is a financial component.
    selected = {ranked[0].source_id: ranked[0]} if ranked else {}
    for component in components:
        selected.setdefault(component[0].source_id, component[0])
    fused: dict[str, float] = {}
    candidates = {page.source_id: page for page in ranked}
    for ranking, weight in [(ranked, 1.0), *((component, 1.0 / len(components)) for component in components)]:
        for rank, page in enumerate(ranking, start=1):
            candidates[page.source_id] = page
            fused[page.source_id] = fused.get(page.source_id, 0.0) + weight / (20.0 + rank)
    for source_id in sorted(fused, key=lambda source_id: (-fused[source_id], source_id)):
        selected.setdefault(source_id, candidates[source_id])
    return list(selected.values())


def _rank_financial_components(
    question: str,
    pages: list[_Page],
    frequencies: Counter[str],
    average_length: float,
    affinities: dict[str, tuple[float, float]],
) -> list[list[_Page]]:
    component_names = dict.fromkeys(
        component
        for pattern, components in _METRIC_COMPONENTS
        if re.search(pattern, question, re.IGNORECASE)
        for component in components
    )
    rankings = []
    heading_matches: dict[tuple[str, str], int] = {}
    for name in component_names:
        kind, terms = _FINANCIAL_COMPONENTS[name]
        query = dict.fromkeys(_tokens(terms), 1.0)
        scored: list[tuple[float, str, _Page]] = []
        for page in pages:
            score = _bm25(page.terms, query, frequencies, len(pages), average_length)
            if score <= 0:
                continue
            key = (page.source_id, kind)
            if key not in heading_matches:
                heading_matches[key] = _heading_matches(page.text, (_STATEMENT_HEADINGS[kind],))
            name_match, year_match = affinities[page.document_id]
            score += 20.0 * heading_matches[key] + 6.0 * name_match + 4.0 * year_match
            scored.append((score, page.source_id, page))
        if scored:
            scored.sort(key=lambda item: (-item[0], item[1]))
            rankings.append([page for _, _, page in scored])
    return rankings


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
