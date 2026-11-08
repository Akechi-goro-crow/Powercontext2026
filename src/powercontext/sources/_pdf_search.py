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

"""Offline financial-page ranking over an explicitly scoped Memory snapshot."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime.models import MemoryEntryRecord, SearchMemoryRequest

_WORDS = re.compile(r"[a-z]+|\d+")
_YEARS = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_STOP = frozenset([
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "more",
    "most",
    "not",
    "of",
    "on",
    "or",
    "our",
    "per",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
    "any",
    "all",
    "only",
    "also",
    "such",
    "using",
    "use",
    "used",
    "based",
    "shown",
    "give",
    "answer",
    "respond",
    "response",
    "question",
    "assume",
    "assuming",
    "perspective",
    "analyst",
    "investment",
    "details",
    "within",
    "please",
    "calculate",
    "define",
    "defined",
    "state",
    "explicitly",
    "directly",
    "outlined",
    "reported",
    "report",
    "annual",
    "fiscal",
    "fy",
    "year",
    "years",
    "million",
    "millions",
    "billion",
    "billions",
    "usd",
    "dollar",
    "dollars",
    "unadjusted",
    "adjusted",
    "quantity",
    "amount",
    "important",
    "materially",
    "ongoing",
    "primarily",
    "respectively",
    "end",
    "ended",
    "according",
    "financial",
    "company",
    "corporation",
    "inc",
])


@dataclass(frozen=True)
class _Concept:
    aliases: tuple[str, ...]
    terms: str
    headings: tuple[str, ...] = ()
    components: tuple[str, ...] = ()


# Accounting vocabulary and metric components, independent of issuers, years,
# question wording, report locations, or any benchmark annotations.
_CONCEPTS = {
    "geography": _Concept(
        ("geographies", "geographic", "geographical", "regions", "countries", "operates in"),
        "geographic geography countries region international domestic segment division operation",
        ("business overview", "our divisions", "geographic", "segment information"),
    ),
    "legal": _Concept(
        ("legal", "litigation", "lawsuit", "lawsuits", "legal battles"),
        "legal litigation lawsuit proceeding contingency claim settlement",
        ("legal proceedings", "commitments and contingencies", "litigation"),
    ),
    "restructuring": _Concept(
        ("restructuring", "reorganization", "severance"),
        "restructuring reorganization severance charge expense cost",
        ("restructuring", "reorganization"),
    ),
    "income": _Concept(
        (
            "income statement",
            "statement of income",
            "statement of operations",
            "statement of earnings",
            "p&l",
            "profit and loss",
        ),
        "income earning revenue sale expense operating profit loss cost",
        ("statements of income", "statements of operations", "statements of earnings", "income statement"),
    ),
    "cashflow": _Concept(
        ("cash flow", "cash flows", "operating cash", "cash generated"),
        "cash flow operating investing financing activity",
        ("statements of cash flows", "statement of cash flows", "cash flow statement"),
    ),
    "balance": _Concept(
        ("balance sheet", "financial position"),
        "asset liability equity balance",
        ("balance sheet", "financial position"),
    ),
    "revenue": _Concept(("revenue", "sales", "turnover"), "revenue sale net", components=("income",)),
    "netincome": _Concept(
        ("net income", "net earnings", "net profit"), "net income earning profit", components=("income",)
    ),
    "operating": _Concept(
        ("operating income", "operating profit", "ebit"), "operating income profit", components=("income",)
    ),
    "depreciation": _Concept(
        ("depreciation", "amortization", "d&a"), "depreciation amortization", components=("cashflow",)
    ),
    "capex": _Concept(
        ("capex", "capital expenditure", "capital spending"),
        "capital spending expenditure additions purchase property plant equipment",
        components=("cashflow",),
    ),
    "ebitda": _Concept(
        ("ebitda",), "operating income depreciation amortization", components=("operating", "depreciation")
    ),
    "margin": _Concept(("margin",), "revenue sale income profit", components=("income",)),
    "freecash": _Concept(("free cash flow",), "cash operating capital expenditure", components=("cashflow", "capex")),
    "assets": _Concept(("assets",), "asset current total", components=("balance",)),
    "liabilities": _Concept(("liabilities",), "liability current total", components=("balance",)),
    "equity": _Concept(
        ("equity", "book value", "net worth"), "equity stockholder shareholder", components=("balance",)
    ),
    "working": _Concept(
        ("working capital", "current ratio", "quick ratio"), "current asset liability", components=("balance",)
    ),
    "roa": _Concept(("return on assets", "roa"), "net income total asset", components=("netincome", "assets")),
    "roe": _Concept(("return on equity", "roe"), "net income equity", components=("netincome", "equity")),
    "debt": _Concept(("debt", "borrowing", "leverage"), "debt borrowing maturity obligation", components=("balance",)),
    "dividend": _Concept(("dividend",), "dividend declared paid cash", components=("cashflow",)),
    "repurchase": _Concept(("repurchase", "buyback"), "repurchase stock share treasury", components=("cashflow",)),
    "tax": _Concept(("tax", "taxation"), "income tax provision effective rate", ("income taxes",)),
    "interest": _Concept(("interest expense", "interest coverage"), "interest expense income", components=("income",)),
    "inventory": _Concept(("inventory", "inventories"), "inventory inventories", components=("balance",)),
    "receivable": _Concept(("receivable",), "account receivable net", components=("balance",)),
    "segment": _Concept(
        ("segment", "division"), "segment division revenue operating", ("segment information", "business segments")
    ),
    "risk": _Concept(("risk",), "risk uncertainty adverse", ("risk factors",)),
    "auditor": _Concept(
        ("auditor", "audit opinion"),
        "independent registered public accounting firm opinion",
        ("report of independent",),
    ),
}


def _terms(text: str) -> list[str]:
    words = _WORDS.findall(unicodedata.normalize("NFKC", text).casefold())
    return [_singular(word) for word in words]


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _contains(text: str, phrase: str) -> bool:
    return " " + " ".join(_terms(phrase)) + " " in text


@dataclass
class _Page:
    record: MemoryEntryRecord
    source_id: str
    document: str
    text: str
    counts: Counter[str]
    heading: str


def _pages(records: tuple[MemoryEntryRecord, ...]) -> list[_Page]:
    pages = []
    seen = set()
    for record in records:
        sources = record.entry.sources
        if record.state != "active" or len(sources) != 1 or sources[0].source_type != "pdf-page":
            continue
        source_id = sources[0].source_id
        if source_id in seen:
            continue
        seen.add(source_id)
        document, _, body = record.entry.text.partition("\n")
        pages.append(
            _Page(record, source_id, document, body, Counter(_terms(body)), " " + " ".join(_terms(body[:350])) + " ")
        )
    return pages


def _select_documents(pages: list[_Page], question: str) -> list[_Page]:
    query = " " + " ".join(_terms(question)) + " "
    names = {}
    for document in {page.document for page in pages}:
        # Document names often encode an issuer followed by the filing year/type.
        # An unknown/opaque name leaves the full scoped corpus eligible.
        name = _YEARS.split(document)[0]
        words = [word for word in _terms(name) if word not in _STOP and word not in {"k", "q", "plc", "ltd", "corp"}]
        names[document] = sum(len(word) for word in words if _contains(query, word))
    matched = {document for document, score in names.items() if score > 0}
    if matched:
        pages = [page for page in pages if page.document in matched]
    return pages


def _query(question: str) -> tuple[dict[str, float], tuple[_Concept, ...]]:
    normalized = " " + " ".join(_terms(question)) + " "
    active = [
        name for name, concept in _CONCEPTS.items() if any(_contains(normalized, alias) for alias in concept.aliases)
    ]
    for name in active:
        for component in _CONCEPTS[name].components:
            if component not in active:
                active.append(component)
    concepts = tuple(_CONCEPTS[name] for name in active)
    weights = {term: (0.15 if term.isdigit() else 1.0) for term in _terms(question) if term not in _STOP}
    for concept in concepts:
        for term in _terms(concept.terms):
            weights[term] = max(weights.get(term, 0), 0.35)
    return weights, concepts


class _BM25:
    def __init__(self, pages: list[_Page]) -> None:
        frequencies: Counter[str] = Counter()
        for page in pages:
            frequencies.update(page.counts.keys())
        self.idf = {term: math.log1p((len(pages) - count + 0.5) / (count + 0.5)) for term, count in frequencies.items()}
        lengths = [sum(page.counts.values()) for page in pages]
        average = sum(lengths) / max(1, len(pages)) or 1
        self.norms = [1.2 * (0.65 + 0.35 * length / average) for length in lengths]

    def score(self, page: _Page, index: int, weights: dict[str, float]) -> float:
        return sum(
            weight * self.idf.get(term, 0) * count * 2.2 / (count + self.norms[index])
            for term, weight in weights.items()
            if (count := page.counts.get(term, 0))
        )


def _rank(pages: list[_Page], question: str, fts_ids: set[str], limit: int) -> tuple[MemoryCitation, ...]:
    weights, concepts = _query(question)
    ranker = _BM25(pages)
    years = set(_YEARS.findall(question))
    scores, facets = [], [[] for _ in concepts]
    for index, page in enumerate(pages):
        score = ranker.score(page, index, weights)
        for facet, concept in zip(facets, concepts, strict=True):
            heading = sum(_contains(page.heading, title) for title in concept.headings)
            facet.append(ranker.score(page, index, dict.fromkeys(_terms(concept.terms), 1.0)) + 30.0 * heading)
            score += 4.0 * heading
        if years.intersection(_YEARS.findall(page.document)):
            score *= 1.15
        if page.record.entry.entry_id in fts_ids:
            score *= 1.05
        scores.append(score)
    ranking = sorted(range(len(pages)), key=lambda index: (-scores[index], pages[index].source_id))
    selected = _diversify(ranking, scores, facets, limit)
    return tuple(pages[index].record.citation for index in selected)


def _diversify(ranking: list[int], scores: list[float], facets: list[list[float]], limit: int) -> list[int]:
    selected = [index for index in ranking[: max(1, limit // 2)] if scores[index] > 0]
    # Multi-statement questions need each component, even when one subject has
    # many more mentions. Reserve at most half the result budget for facets.
    facet_rankings = [sorted(ranking, key=lambda index: (-facet[index], -scores[index])) for facet in facets]
    for position in range(2):
        for facet, ordering in zip(facets, facet_rankings, strict=True):
            if len(ordering) > position:
                index = ordering[position]
                if index not in selected and facet[index] > 0 and scores[index] > 0 and len(selected) < limit:
                    selected.append(index)
    for index in ranking:
        if len(selected) >= limit:
            break
        if index not in selected and scores[index] > 0:
            selected.append(index)
    return selected[:limit]


async def search_pages(memory, question: str, limit: int) -> tuple[MemoryCitation, ...]:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise TypeError("limit must be an integer")  # noqa: TRY003
    if limit <= 0 or not question.strip():
        return ()
    weights, _ = _query(question)
    if not weights:
        return ()
    # A current public snapshot supplies authoritative sources and citations.
    # At financial-report scale, ranking this scope avoids candidate truncation
    # on rare synonyms. No state survives a call, revision, or scope change.
    snapshot = await memory.list()
    pages = _select_documents(_pages(snapshot.entries), question)
    if not pages:
        return ()
    probe = " ".join(sorted(weights, key=lambda term: -weights[term])[:24])
    hits = await memory.search(SearchMemoryRequest(query=probe, mode="fts", limit=min(400, max(80, limit * 4))))
    return _rank(pages, question, {hit.entry_id for hit in hits.hits}, limit)
