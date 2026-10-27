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

"""Query-directed, literal Memory evidence selection with exact wire-byte accounting."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_context import (
    _BEGIN_MARKER,
    _END_MARKER,
    MemoryEntryAddress,
    PreparedContextBuild,
    PreparedContextOmissions,
    PreparedMemoryCandidates,
    PreparedMemoryFragment,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+|\n+")  # noqa: RUF001 - Unicode sentence punctuation
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
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "might",
    "of",
    "on",
    "or",
    "our",
    "should",
    "than",
    "that",
    "the",
    "their",
    "them",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "with",
    "would",
    "we",
    "you",
    "your",
    "not",
    "no",
    "only",
    "also",
    "more",
    "most",
    "such",
    "using",
    "used",
    "use",
])
_ABBREVIATIONS = frozenset({"al.", "e.g.", "i.e.", "fig.", "dr.", "vs.", "etc."})
_BEAM_BUCKETS = 32
_STATES_PER_BUCKET = 3
_FINDINGS = frozenset([
    "found",
    "show",
    "showed",
    "demonstrate",
    "demonstrated",
    "observe",
    "observed",
    "reveal",
    "revealed",
    "suggest",
    "suggests",
    "indicate",
    "indicated",
    "identify",
    "identified",
    "conclude",
    "confirmed",
    "results",
    "associated",
    "reduced",
    "increased",
    "higher",
    "lower",
    "significantly",
])


@dataclass(frozen=True)
class _Document:
    address: MemoryEntryAddress
    text: str


@dataclass(frozen=True)
class _Sentence:
    document: int
    start: int
    end: int
    terms: Counter[str]


@dataclass(frozen=True)
class _Selection:
    mask: int
    score: float
    cost: int


def _terms(text: str) -> Counter[str]:
    words = _WORDS.findall(unicodedata.normalize("NFKC", text).casefold())
    result: Counter[str] = Counter()
    for word in words:
        if word in _STOP:
            continue
        # Character bigrams retain query specificity in unsegmented CJK text.
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            result.update(word[i : i + 2] for i in range(max(1, len(word) - 1)))
            continue
        if word.endswith("ies") and len(word) > 5:
            word = word[:-3] + "y"
        else:
            if word.endswith("s") and not word.endswith("ss") and len(word) > 4:
                word = word[:-1]
            for suffix in ("ization", "ation", "ment", "ing", "ed", "ion"):
                if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                    word = word[:-3] if suffix == "ation" else word[: -len(suffix)]
                    break
            if word.endswith("e") and len(word) > 4:
                word = word[:-1]
        result[word] += 1
    return result


def _sentences(document: int, text: str) -> list[_Sentence]:
    result: list[_Sentence] = []
    start = 0
    ends = []
    for boundary in _BOUNDARY.finditer(text):
        previous = text[start : boundary.start()].rsplit(maxsplit=1)
        if previous and previous[-1].lower() in _ABBREVIATIONS and "\n" not in boundary.group():
            continue
        ends.append(boundary.end())
    for end in (*ends, len(text)):
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            result.append(_Sentence(document, left, right, _terms(text[left:right])))
        start = end
    return result


def _item(fragment: PreparedMemoryFragment) -> dict[str, object]:
    address = fragment.address
    return {
        "citation": {
            "scope_id": address.memory.scope_id,
            "memory_ref": address.memory.artifact.model_dump(mode="json"),
            "entry_id": address.entry_id,
            "entry_version_id": address.entry_version_id,
        },
        "content": fragment.text,
        "start": fragment.start,
        "end": fragment.end,
    }


def _budgeted_sentences(index: int, document: _Document, max_bytes: int) -> list[_Sentence]:
    sentences: list[_Sentence] = []
    for sentence in _sentences(index, document.text):
        fragment = PreparedMemoryFragment(
            document.address, sentence.start, sentence.end, document.text[sentence.start : sentence.end]
        )
        if len(_render((fragment,)).encode("utf-8")) <= max_bytes:
            sentences.append(sentence)
            continue
        # Split only individually oversized sentences. This preserves complete
        # sentences when possible and still exposes a long sentence's interior.
        start = sentence.start
        while start < sentence.end:
            end = min(start + 160, sentence.end)
            boundary = document.text.rfind(" ", start + 80, end)
            if end < sentence.end and boundary > start:
                end = boundary
            left, right = start, end
            while left < right and document.text[left].isspace():
                left += 1
            while right > left and document.text[right - 1].isspace():
                right -= 1
            if left < right:
                sentences.append(_Sentence(index, left, right, _terms(document.text[left:right])))
            start = end
    return sentences


def _render(fragments: Sequence[PreparedMemoryFragment]) -> str:
    payload = {"trust": "untrusted_history", "items": [_item(fragment) for fragment in fragments]}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f"{TRUST_POLICY}\n\n{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"


def _fragments(
    mask: int, sentences: Sequence[_Sentence], documents: Sequence[_Document]
) -> tuple[PreparedMemoryFragment, ...]:
    spans: list[tuple[int, int, int]] = []
    for index, sentence in enumerate(sentences):
        if not mask & (1 << index):
            continue
        if (
            spans
            and spans[-1][0] == sentence.document
            and not documents[sentence.document].text[spans[-1][2] : sentence.start].strip()
        ):
            doc, start, _ = spans.pop()
            spans.append((doc, start, sentence.end))
        else:
            spans.append((sentence.document, sentence.start, sentence.end))
    return tuple(
        PreparedMemoryFragment(documents[doc].address, start, end, documents[doc].text[start:end])
        for doc, start, end in spans
    )


def _lexical_scores(query: str, sentences: Sequence[_Sentence]) -> list[float]:
    terms = _terms(query)
    if not terms:
        return [0.0] * len(sentences)
    frequency: Counter[str] = Counter()
    for sentence in sentences:
        frequency.update(sentence.terms.keys() & terms.keys())
    weights = {term: math.log1p((len(sentences) + 0.5) / (frequency[term] + 0.5)) for term in terms}
    average = sum(sum(sentence.terms.values()) for sentence in sentences) / max(1, len(sentences))
    scores = []
    document_terms: dict[int, set[str]] = {}
    document_positions: dict[int, list[int]] = {}
    for i, sentence in enumerate(sentences):
        document_terms.setdefault(sentence.document, set()).update(sentence.terms)
        document_positions.setdefault(sentence.document, []).append(i)
    document_coverage = {
        doc: sum(weights[term] for term in terms if term in words) / sum(weights.values())
        for doc, words in document_terms.items()
    }
    relative_positions = {
        index: position / max(1, len(positions) - 1)
        for positions in document_positions.values()
        for position, index in enumerate(positions)
    }
    finding_terms = _terms(" ".join(_FINDINGS)).keys()
    for sentence in sentences:
        length = sum(sentence.terms.values())
        score = sum(
            weights[term] * count * 2.2 / (count + 1.2 * (0.3 + 0.7 * length / max(1, average)))
            for term in terms
            if (count := sentence.terms[term])
        )
        coverage = sum(weights[term] for term in terms if sentence.terms[term]) / sum(weights.values())
        position = relative_positions[len(scores)]
        findings = bool(sentence.terms.keys() & finding_terms)
        score *= (0.5 + coverage) * (0.5 + document_coverage[sentence.document])
        score *= (0.65 + 0.85 * position) * (1.4 if findings else 1)
        scores.append(score)
    # Neighboring sentences can explain a result without repeating its entities.
    return [
        score
        + 0.12
        * max(
            (scores[j] for j in (i - 1, i + 1) if 0 <= j < len(scores) and sentences[j].document == sentence.document),
            default=0,
        )
        for i, (score, sentence) in enumerate(zip(scores, sentences, strict=True))
    ]


def _document_aliases(text: str) -> list[tuple[set[str], set[str]]]:
    """Infer only definitions explicitly written as 'long form (SHORT)' in this text."""

    aliases = []
    for match in re.finditer(r"\(([A-Za-z][A-Za-z0-9-]{1,14})\)", text):
        short = match.group(1)
        if not any(char.isupper() for char in short):
            continue
        letters = "".join(char.lower() for char in short if char.isalnum())
        words = text[max(0, match.start() - 256) : match.start()].split()[-min(len(letters) * 2, len(letters) + 5) :]
        long = " ".join(words)
        position = len(long) - 1
        for index in range(len(letters) - 1, -1, -1):
            while position >= 0 and (
                long[position].lower() != letters[index]
                or (index == 0 and position > 0 and long[position - 1].isalnum())
            ):
                position -= 1
            if position < 0:
                break
            if index:
                position -= 1
        if position >= 0:
            expanded, abbreviated = set(_terms(long[position:])), set(_terms(short))
            if expanded and abbreviated:
                aliases.append((expanded, abbreviated))
    return aliases


def _scores(query: str, sentences: Sequence[_Sentence], documents: Sequence[_Document]) -> list[float]:
    aliases = [_document_aliases(document.text) for document in documents]
    expanded = []
    for sentence in sentences:
        terms = sentence.terms.copy()
        for long, short in aliases[sentence.document]:
            if short <= terms.keys():
                for term in long - terms.keys():
                    terms[term] = 1
            elif long <= terms.keys():
                for term in short - terms.keys():
                    terms[term] = 1
        expanded.append(_Sentence(sentence.document, sentence.start, sentence.end, terms))
    return _lexical_scores(query, expanded)


def _window_masks(scores: Sequence[float], sentences: Sequence[_Sentence]) -> list[int]:
    """Offer contiguous evidence as a unit, before charging per-fragment citations."""

    anchors = sorted(
        (index for index, score in enumerate(scores) if score > 0),
        key=lambda index: (-scores[index], index),
    )[:128]
    windows: set[int] = set()
    for index in anchors:
        for start in range(max(0, index - 4), index + 1):
            mask = 0
            for end in range(start, min(start + 5, len(sentences))):
                if sentences[end].document != sentences[index].document:
                    break
                mask |= 1 << end
                if end >= index:
                    windows.add(mask)
    return sorted(windows, key=lambda mask: (-sum(score for i, score in enumerate(scores) if mask & (1 << i)), mask))


def _choose(
    query: str, sentences: Sequence[_Sentence], documents: Sequence[_Document], max_bytes: int
) -> tuple[PreparedMemoryFragment, ...]:
    scores = _scores(query, sentences, documents)
    candidates = _window_masks(scores, sentences)
    # Beam states are literal sentence sets. Merging adjacent sentences before
    # charging bytes lets several pieces share one exact citation.
    empty_cost = len(_render(()).encode("utf-8"))
    states = [_Selection(0, 0.0, empty_cost)]
    measured: dict[int, int] = {0: empty_cost}
    for window in candidates:
        choices = {state.mask: state for state in states}
        for state in states:
            added = window & ~state.mask
            if not added:
                continue
            mask = state.mask | window
            if mask not in measured:
                measured[mask] = len(_render(_fragments(mask, sentences, documents)).encode("utf-8"))
            cost = measured[mask]
            if cost <= max_bytes:
                gain = sum(score for i, score in enumerate(scores) if added & (1 << i))
                choices[mask] = _Selection(mask, state.score + gain, cost)
        buckets: dict[int, list[_Selection]] = {}
        for state in sorted(choices.values(), key=lambda state: (-state.score, state.cost, state.mask)):
            bucket = state.cost * _BEAM_BUCKETS // max_bytes
            kept = buckets.setdefault(bucket, [])
            if len(kept) < _STATES_PER_BUCKET:
                kept.append(state)
        states = [state for bucket in buckets.values() for state in bucket]
        if not any(state.mask == 0 for state in states):
            states.append(_Selection(0, 0, empty_cost))
    best = max(states, key=lambda state: (state.score, -state.cost))
    return _fragments(best.mask, sentences, documents)


def build_memory_evidence(
    request: PrepareContextRequest,
    candidates: Sequence[PreparedMemoryCandidates],
    *,
    current_scope_id: str | None,
) -> PreparedContextBuild:
    """Read no external state: query, exact candidates and the wire budget are sufficient."""

    documents: list[_Document] = []
    seen: set[tuple[str, str, str, int, str, str]] = set()
    for group in candidates:
        for hit in group.hits:
            if not hit.text.strip() or not hit.entry_id.strip() or not hit.entry_version_id.strip():
                continue
            scope = group.scope_id or current_scope_id
            if not scope:
                raise PreparedContextInvariantError("evidence-scope-missing")
            identity = (
                scope,
                hit.memory_ref.family,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            if identity in seen:
                continue
            seen.add(identity)
            documents.append(
                _Document(
                    MemoryEntryAddress(
                        ArtifactAddress(scope_id=scope, artifact=hit.memory_ref), hit.entry_id, hit.entry_version_id
                    ),
                    hit.text,
                )
            )
    sentences = [
        sentence
        for i, document in enumerate(documents)
        for sentence in _budgeted_sentences(i, document, request.max_bytes)
    ]
    fragments = _choose(request.query, sentences, documents, request.max_bytes)
    if not fragments:
        return PreparedContextBuild(context=PreparedContext(status="empty", content=None, content_bytes=0), origins=())
    content = _render(fragments)
    return PreparedContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=len(content.encode("utf-8"))),
        origins=tuple(fragment.address for fragment in fragments),
        fragments=fragments,
        omissions=PreparedContextOmissions(
            truncated_items=sum(
                fragment.start > 0 or fragment.end < len(document.text)
                for fragment in fragments
                for document in documents
                if fragment.address == document.address
            )
        ),
    )
