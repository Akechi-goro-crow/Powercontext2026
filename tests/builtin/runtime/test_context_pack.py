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

"""Context pack interoperability, invalid input, and native wire-cost regressions."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


def _wire(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("size", [0, 1, 9, 100, 600, 7000])
def test_full_wire_matches_independent_digest_and_count(size: int) -> None:
    pack = ContextPack.from_content("海洋🌊\n" * size)
    payload = json.loads(pack.to_bytes())
    unsigned = {"schema": "powercontext.context-pack.v1", "items": payload["items"]}
    assert payload["digest"] == "sha256:" + hashlib.sha256(_wire(unsigned)).hexdigest()
    assert payload["byte_count"] == len(pack.to_bytes()) == pack.byte_count
    assert pack.to_bytes() == _wire(payload)
    assert ContextPack.from_bytes(pack.to_bytes()) == pack


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 7, 511, 512])
def test_every_chunk_is_utf8_and_bounded(limit: int) -> None:
    for content in ("", "abc\n" * 100, "a海b洋c" * 100, "🌊é海a" * 100):
        if content and max(len(char.encode()) for char in content) > limit:
            with pytest.raises(ContextPackError):
                ContextPack.from_content(content, chunk_bytes=limit)
            continue
        pack = ContextPack.from_content(content, chunk_bytes=limit)
        assert b"".join(pack.items) == content.encode()
        assert all(0 < len(item) <= limit and item.decode() for item in pack.items)
        assert ContextPack.from_content(content, chunk_bytes=limit) == pack


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.5, "512", None])
def test_invalid_chunk_limit(limit) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("", chunk_bytes=limit)


@pytest.mark.parametrize("items", [[], ("text",), (b"\xff",), (b"\xe6", b"\xb5\xb7")])
def test_build_rejects_invalid_chunks(items) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.build(items)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "other"),
        ("extra", 0),
        ("items", None),
        ("items", [1]),
        ("items", ["Zg"]),
        ("items", ["Zg==\n"]),
        ("items", ["Zh=="]),
        ("items", ["_w=="]),
        ("items", ["/w=="]),
        ("items", ["海"]),
        ("digest", "sha256:" + "0" * 64),
        ("byte_count", True),
        ("byte_count", 1.5),
        ("byte_count", 0),
    ],
)
def test_pack_rejects_malformed_or_tampered_wire(field: str, value: object) -> None:
    payload = json.loads(ContextPack.build((b"f",)).to_bytes())
    payload[field] = value
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(_wire(payload))


@pytest.mark.parametrize("data", [b"[]", b"null", b"{", b"\xff", b"{}{}", b'{"schema":1,"schema":2}'])
def test_decoders_reject_invalid_json(data: bytes) -> None:
    for decoder in (ContextPack.from_bytes, ContextPackDelta.from_bytes):
        with pytest.raises(ContextPackError):
            decoder(data)


def test_missing_fields_and_duplicates_are_rejected() -> None:
    base = ContextPack.from_content("base")
    delta = ContextPackDelta.between(base, ContextPack.from_content("target"))
    for obj, decoder in ((base, ContextPack.from_bytes), (delta, ContextPackDelta.from_bytes)):
        payload = json.loads(obj.to_bytes())
        for key in payload:
            with pytest.raises(ContextPackError):
                decoder(_wire({k: v for k, v in payload.items() if k != key}))
        duplicated = b'{"schema":"ignored",' + obj.to_bytes()[1:]
        with pytest.raises(ContextPackError):
            decoder(duplicated)


@pytest.mark.parametrize("prefix", [True, False, 1.0, -1, "1", None])
def test_delta_prefix_has_strict_integer_type(prefix) -> None:
    base = ContextPack.build((b"a",))
    delta = ContextPackDelta.between(base, base)
    payload = json.loads(delta.to_bytes())
    payload["prefix_items"] = prefix
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(_wire(payload))
    with pytest.raises(ContextPackError):
        ContextPackDelta(base.digest, base.digest, prefix, ())


def test_any_valid_prefix_and_target_integrity() -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    target = ContextPack.build((b"a", b"b", b"new"))
    for prefix in range(3):
        delta = ContextPackDelta(base.digest, target.digest, prefix, target.items[prefix:])
        restored = ContextPackDelta.from_bytes(delta.to_bytes())
        assert apply_context_pack_delta(base, restored) == target
    for delta in (
        ContextPackDelta(base.digest, target.digest, 4, ()),
        ContextPackDelta(target.digest, target.digest, 0, target.items),
        ContextPackDelta(base.digest, target.digest, 2, (b"wrong",)),
    ):
        with pytest.raises(ContextPackError):
            apply_context_pack_delta(base, delta)


def test_reconstruction_in_an_independent_process() -> None:
    base = ContextPack.from_content("unchanged 海洋\n" * 100)
    target = ContextPack.from_content("unchanged 海洋\n" * 100 + "appended 🌊")
    delta = ContextPackDelta.between(base, target)
    receiver = """
import base64, json, sys
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
base, delta = json.loads(sys.stdin.buffer.read())
result = apply_context_pack_delta(ContextPack.from_bytes(base64.b64decode(base)),
                                  ContextPackDelta.from_bytes(base64.b64decode(delta)))
sys.stdout.buffer.write(b''.join(result.items))
"""
    result = subprocess.run(
        [sys.executable, "-c", receiver],
        input=_wire([base64.b64encode(obj.to_bytes()).decode() for obj in (base, delta)]),
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert result.stdout == b"".join(target.items)


class _CandidatePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="conversation-turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def test_native_empty_context_and_authorization() -> None:
    asyncio.run(_test_native_empty_context_and_authorization())


async def _test_native_empty_context_and_authorization() -> None:
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_CandidatePipeline()) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(ScopeDraft(title="Empty", summary="Empty context", idempotency_key="empty"))
        context = runtime.context.for_scope(scope.scope_id)
        calls: list[tuple[str, ...]] = []

        async def authorize(scope_ids: tuple[str, ...]) -> None:
            calls.append(scope_ids)

        request = PrepareContextRequest(query="unmatched", max_bytes=4096)
        pack = await context.prepare_incremental(request, authorize_scopes=authorize)
        assert isinstance(pack, ContextPack) and pack.items == ()
        assert calls == [(scope.scope_id,)]
        base = ContextPack.from_content("previous nonempty context")
        delta = await context.prepare_incremental(request, base_pack=base)
        assert isinstance(delta, ContextPackDelta)
        assert apply_context_pack_delta(base, ContextPackDelta.from_bytes(delta.to_bytes())).items == ()


def test_qmsum_40_round_wire_cost() -> None:
    asyncio.run(_test_qmsum_40_round_wire_cost())


async def _test_qmsum_40_round_wire_cost() -> None:
    sample = json.loads((Path(__file__).parent / "fixtures/qmsum-context-pack.json").read_text())
    raw_bytes = full_pack_bytes = incremental_bytes = gzip_bytes = 0
    contents: set[bytes] = set()
    base: ContextPack | None = None
    turns = sample["turns"]
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_CandidatePipeline()) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="QMSum", summary="40 round transmission", idempotency_key="qmsum-wire")
        )
        sources = runtime.sources.for_scope(scope.scope_id)
        memory = runtime.memory.for_scope(scope.scope_id)
        context = runtime.context.for_scope(scope.scope_id)

        async def capture(turn) -> None:
            await sources.capture(
                CaptureSource(source_id=f"turn-{turn['turn_index']}", content=turn["text"], metadata={})
            )

        for turn in turns[:8]:
            await capture(turn)
        while (await memory.flush()).processed:
            pass
        for round_index in range(40):
            if round_index in (8, 16, 24, 32):
                await capture(turns[8 + (round_index // 8) - 1])
                while (await memory.flush()).processed:
                    pass
            query = ("remote", "price", "market", "control")[round_index // 10]
            request = PrepareContextRequest(query=query, max_bytes=16384)
            ordinary = await context.prepare(request)
            assert ordinary.content
            raw = ordinary.content.encode()
            transmitted = await context.prepare_incremental(request, base_pack=base)
            wire = transmitted.to_bytes()
            if base is None:
                assert isinstance(transmitted, ContextPack)
                base = ContextPack.from_bytes(wire)
            else:
                assert isinstance(transmitted, ContextPackDelta)
                base = apply_context_pack_delta(base, ContextPackDelta.from_bytes(wire))
            assert b"".join(base.items) == raw
            contents.add(raw)
            raw_bytes += len(raw)
            full_pack_bytes += len(base.to_bytes())
            incremental_bytes += len(wire)
            gzip_bytes += len(gzip.compress(raw, compresslevel=9, mtime=0))
    metrics = {
        "rounds": 40,
        "distinct_contexts": len(contents),
        "raw_full_bytes": raw_bytes,
        "full_v1_pack_bytes": full_pack_bytes,
        "incremental_bytes": incremental_bytes,
        "gzip_bytes": gzip_bytes,
        "savings_vs_raw": 1 - incremental_bytes / raw_bytes,
        "savings_vs_full_pack": 1 - incremental_bytes / full_pack_bytes,
        "savings_vs_gzip": 1 - incremental_bytes / gzip_bytes,
    }
    print(json.dumps(metrics, sort_keys=True))
    assert len(contents) >= 4
    assert metrics["savings_vs_raw"] >= 0.45
