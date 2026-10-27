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

"""Lifecycle registration, scope isolation, and crash/retry regression tests."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput, MemoryService
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef


def _event(*, sequence: int = 1, available: bool = False) -> SourceLifecycleEvent:
    return SourceLifecycleEvent(
        node_id="root0", revision=3, sequence=sequence, available=available, reason="withdrawal"
    )


async def _build(runtime, key="one"):
    assert runtime.scopes is not None
    scope = await runtime.scopes.create(ScopeDraft(title=key, summary="Lifecycle regression", idempotency_key=key))
    scope_id = scope.scope_id
    lifecycle = runtime.lifecycle.for_scope(scope_id)
    roots = []
    for number in range(2):
        receipt = await runtime.sources.for_scope(scope_id).capture(
            CaptureSource(source_id=f"root{number}", content=f"root {number}", metadata={})
        )
        roots.append(receipt.source_ref)
        await lifecycle.register_source(f"root{number}", receipt.source_ref, revision=3)
    created = await runtime.memory.for_scope(scope_id).remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"leaf {key} evidence"),))
    )
    assert created.entry is not None
    citation = created.entry.citation
    await lifecycle.register_memory("leaf", citation, parents=("root0", "root1"), policy="all")
    artifact = await runtime.records.for_scope(scope_id).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": f"topic-{key}", "summary": "topic supported evidence", "detail": key}),
    )
    topic = ArtifactRef(family="topic-memory", artifact_id=artifact.artifact_id, revision=artifact.revision)
    await lifecycle.register_topic("topic", topic, parents=("leaf",), policy="all")
    return scope_id, lifecycle, tuple(roots), citation, topic


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node_id", ""),
        ("node_id", "  "),
        ("node_id", 1),
        ("node_id", True),
        ("reason", ""),
        ("reason", "\n"),
        ("reason", 1),
        ("reason", None),
        ("revision", True),
        ("revision", 0),
        ("revision", -1),
        ("revision", "3"),
        ("revision", 3.0),
        ("sequence", True),
        ("sequence", 0),
        ("sequence", -1),
        ("sequence", "1"),
        ("sequence", 1.0),
        ("available", 0),
        ("available", 1),
        ("available", "false"),
        ("extra", True),
    ],
)
def test_event_fields_are_strict(field: str, value: object) -> None:
    payload = {"node_id": "root0", "revision": 3, "sequence": 1, "available": False, "reason": "withdrawal"}
    payload[field] = value
    with pytest.raises(ValidationError):
        SourceLifecycleEvent.model_validate(payload)


def test_registration_is_idempotent_after_withdrawal_and_parent_order_is_semantic() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig()) as runtime:
            scope, lifecycle, roots, citation, topic = await _build(runtime)
            await lifecycle.apply(_event())
            before = (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref
            await lifecycle.register_source("root0", roots[0], revision=3)
            await lifecycle.register_memory("leaf", citation, parents=("root1", "root0"), policy="all")
            await lifecycle.register_topic("topic", topic, parents=("leaf",), policy="all")
            assert (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref == before
            assert await lifecycle.apply(_event()) == ["root1"]
            assert await lifecycle.resolve("leaf", purpose="audit") == citation
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("leaf", purpose="generation")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("root0", roots[1], revision=3)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("root0", roots[0], revision=4)

    asyncio.run(scenario())


def test_invalid_bindings_and_dependencies_do_not_leave_registered_nodes() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig()) as runtime:
            _, lifecycle, _, citation, topic = await _build(runtime)
            for parents in ((), ("missing",), ("bad",), ("root0", "root0")):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.register_memory("bad", citation, parents=parents, policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("bad", citation, parents=("root0",), policy="unknown")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source(
                    "bad", SourceRef(source_type="content", source_id="missing"), revision=3
                )
            missing = citation.model_copy(update={"entry_id": "missing"})
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("bad", missing, parents=("root0",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_topic("bad", topic.model_copy(update={"revision": 999}), parents=("root0",))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("bad", purpose="audit")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("root0", purpose="unknown")
            for field, value in (("node_id", "leaf"), ("node_id", "missing"), ("revision", 4)):
                payload = _event().model_dump()
                payload[field] = value
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.apply(SourceLifecycleEvent.model_validate(payload))

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_phase", ["before", "after"])
def test_interrupted_native_transition_retries_after_restart_without_duplicate_revision(
    tmp_path, monkeypatch, failure_phase: str
) -> None:
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}"))

    async def scenario() -> None:
        original = MemoryService.forget
        async with open_builtin_runtime(config) as runtime:
            scope, lifecycle, _, citation, topic = await _build(runtime)

            async def fail(self, memory, *, entries, reason=None):
                if failure_phase == "after":
                    await original(self, memory, entries=entries, reason=reason)
                raise RuntimeError("interrupted-native-write")

            with monkeypatch.context() as patch:
                patch.setattr(MemoryService, "forget", fail)
                with pytest.raises(RuntimeError, match="interrupted-native-write"):
                    await lifecycle.apply(_event())
            interrupted = (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref
            assert interrupted is not None
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope)
            assert await lifecycle.apply(_event()) == ["root1"]
            memory = runtime.memory.for_scope(scope)
            completed = await memory.list(include_inactive=True)
            assert completed.memory_ref is not None
            assert completed.memory_ref.revision == interrupted.revision + (failure_phase == "before")
            assert completed.entries[0].state == "inactive"
            assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text == "leaf one evidence"
            assert not (await memory.search(SearchMemoryRequest(query="leaf", mode="fts", limit=10))).hits
            assert not (
                await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="topic", limit=10))
            ).hits
            prepared = await runtime.context.for_scope(scope).prepare(
                PrepareContextRequest(query="leaf", max_bytes=4096)
            )
            assert "leaf one evidence" not in (prepared.content or "")
            await lifecycle.apply(_event())
            assert (await memory.list(include_inactive=True)).memory_ref == completed.memory_ref
            await lifecycle.apply(_event(sequence=2, available=True))
            assert (await memory.search(SearchMemoryRequest(query="leaf", mode="fts", limit=10))).hits
            assert (
                await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="topic", limit=10))
            ).hits
            assert await lifecycle.resolve("leaf", purpose="generation") == citation
            assert await lifecycle.resolve("topic", purpose="generation") == topic
            record = await runtime.records.for_scope(scope).get_artifact_revision(
                "topic-memory", topic.artifact_id, topic.revision
            )
            assert record.content["title"] == "topic-one"
            # Sequence identity remains strict after a restart and a successful restoration.
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event())
            changed = _event(sequence=2, available=True).model_copy(update={"reason": "conflicting-replay"})
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(changed)

    asyncio.run(scenario())


def test_scope_isolation_and_original_references_survive_restart(tmp_path) -> None:
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'isolation.db'}"))

    async def scenario() -> None:
        async with open_builtin_runtime(config) as runtime:
            left = await _build(runtime, "left")
            right = await _build(runtime, "right")
            await left[1].apply(_event())
        async with open_builtin_runtime(config) as runtime:
            for bound, active in ((left, False), (right, True)):
                scope, _, roots, citation, topic = bound
                lifecycle = runtime.lifecycle.for_scope(scope)
                memory = runtime.memory.for_scope(scope)
                assert (await memory.list(include_inactive=True)).entries[0].state == (
                    "active" if active else "inactive"
                )
                assert await lifecycle.resolve("root0", purpose="audit") == roots[0]
                assert await lifecycle.resolve("leaf", purpose="audit") == citation
                assert await lifecycle.resolve("topic", purpose="audit") == topic
                if active:
                    assert await lifecycle.resolve("leaf", purpose="generation") == citation
                else:
                    with pytest.raises(SourceLifecycleError):
                        await lifecycle.resolve("leaf", purpose="generation")
            # A later accepted event that leaves availability unchanged adds no native revision.
            service = runtime.lifecycle.for_scope(left[0])
            before = (await runtime.memory.for_scope(left[0]).list(include_inactive=True)).memory_ref
            await service.apply(_event(sequence=5))
            assert (await runtime.memory.for_scope(left[0]).list(include_inactive=True)).memory_ref == before

    asyncio.run(scenario())
