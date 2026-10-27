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

"""Durable evidence dependency graphs and retryable native Memory propagation."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.source_lifecycle import SourceLifecycleRepository
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.records import BaseAccessError
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import PowerContextError
from powercontext.sources import SourceMaterialization, SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Invalid lifecycle registration, event, dependency, or generation use."""


class SourceLifecycleEvent(BaseModel):
    """An immutable, strictly typed event for one registered Source version."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: StrictStr
    revision: StrictInt = Field(gt=0)
    sequence: StrictInt = Field(gt=0)
    available: StrictBool
    reason: StrictStr

    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank-lifecycle-field")
        return value


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["source", "memory", "topic"]
    reference: SourceRef | MemoryCitation | ArtifactRef
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"
    revision: int = 1
    available: bool = True
    last_event: SourceLifecycleEvent | None = None

    def binding(self) -> tuple[object, ...]:
        return self.kind, self.reference, self.parents, self.policy, self.revision


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("invalid-node-id")
    return value


def _propagate(nodes: dict[str, _Node]) -> dict[str, _Node]:
    graph = {node_id: node.parents for node_id, node in nodes.items()}
    try:
        order = tuple(TopologicalSorter(graph).static_order())
    except CycleError as error:
        raise SourceLifecycleError("dependency-cycle") from error
    updated = dict(nodes)
    for node_id in order:
        if node_id not in updated:
            raise SourceLifecycleError("missing-parent")
        node = updated[node_id]
        if node.kind == "source":
            continue
        support = (updated[parent].available for parent in node.parents)
        available = all(support) if node.policy == "all" else any(support)
        updated[node_id] = node.model_copy(update={"available": available})
    return updated


class SourceLifecycleApplication:
    """Select lifecycle services backed by the Runtime's own database."""

    def __init__(
        self, runtime: BuiltinRuntime, database: AsyncDatabase | None, sources: SourceRepository | None
    ) -> None:
        self._runtime = runtime
        self._database = database
        self._sources = sources

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        if self._database is None or self._sources is None:
            raise SourceLifecycleError("lifecycle-storage-unavailable")
        return ScopedSourceLifecycle(self._runtime, self._database, self._sources, validate_scope_id(scope_id))


class ScopedSourceLifecycle:
    """Register a fixed DAG and reconcile its intended state with native storage.

    Persist the source event and all desired states before native state writes.
    Retrying a partially applied event compares the current native manifest, so
    committed transitions are not repeated and unfinished transitions resume.
    """

    def __init__(
        self, runtime: BuiltinRuntime, database: AsyncDatabase, sources: SourceRepository, scope_id: str
    ) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = scope_id
        self._sources = sources
        self._repository = SourceLifecycleRepository()

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int = 1) -> None:
        if not isinstance(source_ref, SourceRef) or type(revision) is not int or revision <= 0:
            raise SourceLifecycleError("invalid-source-registration")
        await self._register(_name(node_id), _Node(kind="source", reference=source_ref, revision=revision))

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: str = "all"
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("invalid-memory-citation")
        node = self._derivative("memory", citation, parents, policy)
        await self._register(_name(node_id), node)

    async def register_topic(
        self, node_id: str, artifact_ref: ArtifactRef, *, parents: tuple[str, ...], policy: str = "all"
    ) -> None:
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("invalid-topic-reference")
        node = self._derivative("topic", artifact_ref, parents, policy)
        await self._register(_name(node_id), node)

    @staticmethod
    def _derivative(
        kind: Literal["memory", "topic"], reference: MemoryCitation | ArtifactRef, parents: tuple[str, ...], policy: str
    ) -> _Node:
        if (
            policy not in ("all", "any")
            or not isinstance(parents, (tuple, list))
            or not parents
            or any(not isinstance(parent, str) or not parent.strip() for parent in parents)
            or len(set(parents)) != len(parents)
        ):
            raise SourceLifecycleError("invalid-dependencies")
        return _Node(
            kind=kind, reference=reference, parents=tuple(sorted(parents)), policy="all" if policy == "all" else "any"
        )

    async def _load(self) -> dict[str, _Node]:
        async with self._database.transaction() as connection:
            stored = await self._repository.load(connection, self.scope_id)
        return {key: _Node.model_validate_json(payload, strict=True) for key, payload in stored.items()}

    async def _save(self, previous: dict[str, _Node], nodes: dict[str, _Node]) -> None:
        async with self._database.transaction() as connection:
            for node_id, node in nodes.items():
                if previous.get(node_id) == node:
                    continue
                topic = node.reference if node.kind == "topic" and isinstance(node.reference, ArtifactRef) else None
                await self._repository.save(
                    connection,
                    self.scope_id,
                    node_id,
                    exists=node_id in previous,
                    kind=node.kind,
                    available=node.available,
                    artifact_id=None if topic is None else topic.artifact_id,
                    reference_revision=None if topic is None else topic.revision,
                    payload=node.model_dump_json().encode(),
                )

    async def _validate_reference(self, node: _Node) -> None:
        reference = node.reference
        records = self._runtime.records.for_scope(self.scope_id)
        try:
            if isinstance(reference, SourceRef):
                async with self._database.transaction() as connection:
                    stored = await self._sources.get(connection, self.scope_id, reference)
                if stored.value.materialization != SourceMaterialization.CAPTURED:
                    raise SourceLifecycleError("source-not-captured")
                async with self._runtime._context(self.scope_id) as context:
                    await context.sources.catalog.read(stored.value)
            elif isinstance(reference, MemoryCitation):
                await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=reference))
            else:
                await records.get_artifact_revision(reference.family, reference.artifact_id, reference.revision)
        except (BaseAccessError, PowerContextError, LookupError, ValueError) as error:
            raise SourceLifecycleError("unreadable-reference") from error

    async def _register(self, node_id: str, node: _Node) -> None:
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            previous = await self._load()
            existing = previous.get(node_id)
            if existing is not None and existing.binding() != node.binding():
                raise SourceLifecycleError("registration-conflict")
            if node_id in node.parents or any(parent not in previous for parent in node.parents):
                raise SourceLifecycleError("missing-or-self-parent")
            await self._validate_reference(node)
            nodes = _propagate({**previous, node_id: node if existing is None else existing})
            await self._save(previous, nodes)
            await self._reconcile_memory(nodes)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("invalid-lifecycle-event")
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            previous = await self._load()
            source = previous.get(event.node_id)
            if source is None or source.kind != "source" or source.revision != event.revision:
                raise SourceLifecycleError("unknown-source-or-version")
            last = source.last_event
            if last is not None and (
                event.sequence < last.sequence or (event.sequence == last.sequence and event != last)
            ):
                raise SourceLifecycleError("event-sequence-conflict")
            nodes = dict(previous)
            nodes[event.node_id] = source.model_copy(update={"available": event.available, "last_event": event})
            nodes = _propagate(nodes)
            await self._save(previous, nodes)
            await self._reconcile_memory(nodes)
            return sorted(node_id for node_id, node in nodes.items() if node.available)

    async def _reconcile_memory(self, nodes: dict[str, _Node]) -> None:
        bindings: dict[str, tuple[str, bool]] = {}
        for node in nodes.values():
            if node.kind != "memory" or not isinstance(node.reference, MemoryCitation):
                continue
            citation = node.reference
            previous = bindings.get(citation.entry_id)
            bindings[citation.entry_id] = (
                citation.entry_version_id,
                node.available or (previous is not None and previous[1]),
            )
        if not bindings:
            return
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            memory = await service.head(context.artifacts.memory_artifact_id)
            versions = {entry.entry_id: entry for entry in await service.entries(memory)}
            states = {entry.entry_id: entry.state for entry in memory.content.manifest.entries}
            for node_id, (version_id, _) in bindings.items():
                entry = versions.get(node_id)
                if entry is None or entry.entry_version_id != version_id:
                    raise SourceLifecycleError("registered-memory-version-changed")
            for available in (False, True):
                target = "active" if available else "inactive"
                entries = tuple(
                    versions[node_id]
                    for node_id, (_, desired) in bindings.items()
                    if desired == available and states[node_id] != target
                )
                if entries:
                    transition = service.reactivate if available else service.forget
                    memory = await transition(memory, entries=entries, reason="source-lifecycle")

    async def resolve(self, node_id: str, *, purpose: str = "generation") -> SourceRef | MemoryCitation | ArtifactRef:
        _name(node_id)
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("unknown-purpose")
        async with self._runtime._scope_operation(self.scope_id):
            node = (await self._load()).get(node_id)
            if node is None or (purpose == "generation" and not node.available):
                raise SourceLifecycleError("unknown-or-unavailable-node")
            return node.reference
