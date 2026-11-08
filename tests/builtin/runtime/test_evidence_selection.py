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

"""Literal evidence, identity isolation and final-wire budget regressions."""

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import ContextAssembly, PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
    PreparedMemoryFragment,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

REF = ArtifactRef(family="memory", artifact_id="memory", revision=3)


def _group(scope, text, *, entry="entry", version="version"):
    return PreparedMemoryCandidates(
        scope_id=scope,
        memory_ref=REF,
        hits=(
            MemoryHit(
                memory_ref=REF, entry_id=entry, entry_version_id=version, text=text, score=1, matched_by=("fts",)
            ),
        ),
    )


def _build(query, groups, *, budget=1800, **options):
    return PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query=query, max_bytes=budget, evidence_selection=True, **options),
        current_scope_id="left",
        memory_candidates=groups,
    )


def _check(result, groups, budget):
    originals = {(g.scope_id, h.entry_id, h.entry_version_id): h.text for g in groups for h in g.hits}
    content = result.context.content or ""
    assert result.context.content_bytes == len(content.encode("utf-8")) <= budget
    if not content:
        assert result.fragments == result.origins == ()
        return
    assert content.startswith(TRUST_POLICY)
    assert "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n" in content
    assert content.endswith("END_POWERCONTEXT_PREPARED_CONTEXT_V1")
    envelope = json.loads(content.splitlines()[-2])
    assert envelope["trust"] == "untrusted_history"
    assert len(envelope["items"]) == len(result.fragments) == len(result.origins)
    for fragment, origin, item in zip(result.fragments, result.origins, envelope["items"], strict=True):
        address = fragment.address
        original = originals[address.memory.scope_id, address.entry_id, address.entry_version_id]
        assert type(fragment.start) is int and type(fragment.end) is int
        assert 0 <= fragment.start < fragment.end <= len(original)
        assert fragment.text == original[fragment.start : fragment.end] == item["content"]
        assert origin == address
        assert (item["start"], item["end"]) == (fragment.start, fragment.end)
        assert item["citation"] == {
            "scope_id": address.memory.scope_id,
            "memory_ref": address.memory.artifact.model_dump(mode="json"),
            "entry_id": address.entry_id,
            "entry_version_id": address.entry_version_id,
        }


def test_selection_preserves_equal_text_from_distinct_scopes_and_versions():
    groups = (
        _group("left", "Cobalt sensor measures temperature."),
        _group("right", "Cobalt sensor measures temperature."),
    )
    result = _build("cobalt sensor", groups, budget=3000)
    _check(result, groups, 3000)
    assert {f.address.memory.scope_id for f in result.fragments} == {"left", "right"}
    assert _build("cobalt sensor", (groups[0], groups[0]), budget=3000).fragments == (result.fragments[0],)


def test_disjoint_relevant_passages_from_one_entry_keep_separate_coordinates():
    text = (
        "Cobalt sensor is calibrated to 37 degrees. "
        + "Unrelated administrative policy. " * 80
        + "Cobalt sensor tolerance is 0.4 degrees."
    )
    groups = (_group("left", text),)
    result = _build("cobalt sensor calibrated tolerance", groups, budget=1200)
    _check(result, groups, 1200)
    assert any("37" in f.text for f in result.fragments)
    assert any("0.4" in f.text for f in result.fragments)
    assert len(result.fragments) >= 2


def test_oversized_sentence_can_contribute_interior_literal_evidence():
    text = "unrelated details " * 120 + "cobalt sensor calibrated 37 degrees " + "additional notes " * 100
    groups = (_group("left", text),)
    result = _build("cobalt sensor calibrated", groups, budget=1200)
    _check(result, groups, 1200)
    assert any("cobalt sensor" in f.text and f.start > 1000 for f in result.fragments)


def test_document_defined_abbreviation_can_select_late_evidence():
    text = (
        "Interleukin (IL) is a signal. " + "Unrelated administrative notes. " * 50 + "IL activation increased 4-fold."
    )
    groups = (_group("left", text),)
    result = _build("interleukin activation", groups, budget=1000)
    _check(result, groups, 1000)
    assert any("4-fold" in fragment.text for fragment in result.fragments)


def test_contiguous_evidence_shares_citation_before_budget_admission():
    text = (
        "Cobalt sensor calibration was measured under controlled experimental conditions at the research station. "
        "The cobalt sensor experiment included a separate control group and recorded the calibration baseline. "
        "The calibrated cobalt sensor showed significantly higher precision than the experimental control group."
    )
    groups = (_group("left", text),)
    full = _build("cobalt sensor calibration precision", groups, budget=3000)
    result = _build("cobalt sensor calibration precision", groups, budget=full.context.content_bytes)
    _check(result, groups, full.context.content_bytes)
    assert len(result.fragments) == 1
    assert result.fragments[0].text == text


@pytest.mark.parametrize("budget", [512, 600, 800, 1200, 1800, 32768])
def test_entire_utf8_wire_cost_includes_escaping_and_delimiters(budget):
    text = '海洋 station 🌊 reports "temperature".\nStation data: \\sensor\t37 degrees. ' * 12
    groups = (_group("left", text),)
    _check(_build("海洋 station temperature", groups, budget=budget), groups, budget)


def test_exact_wire_budget_and_empty_result_are_valid():
    groups = (_group("left", "Cobalt sensor reports 37 degrees."),)
    full = _build("cobalt", groups)
    exact = _build("cobalt", groups, budget=full.context.content_bytes)
    assert exact.context == full.context
    smaller = _build("cobalt", groups, budget=full.context.content_bytes - 1)
    _check(smaller, groups, full.context.content_bytes - 1)
    assert _build("cobalt", ()).context.status == "empty"


def test_selection_is_opt_in_and_has_its_own_memory_envelope():
    assert PrepareContextRequest(query="sensor").evidence_selection is False
    with pytest.raises(ValidationError):
        PrepareContextRequest.model_validate({"query": "sensor", "evidence_selection": "true"})
    groups = (_group("left", "Sensor reports temperature."),)
    result = _build("sensor", groups, assembly=ContextAssembly(), include_code=True)
    _check(result, groups, 1800)


@pytest.mark.parametrize("start,end", [(False, 2), (0, 2.0), (-1, 2), (2, 2), (0, 1)])
def test_fragment_coordinates_are_strict_and_match_the_literal_length(start, end):
    address = MemoryEntryAddress(ArtifactAddress(scope_id="left", artifact=REF), "entry", "version")
    with pytest.raises(ValueError):
        PreparedMemoryFragment(address=address, start=start, end=end, text="ab")
