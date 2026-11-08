# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

"""Native regression for selective tags outside the unfiltered nearest neighbor."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput, MemorySearchRequest
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.oceanbase.memory_index import OceanBaseMemoryFTSIndex, OceanBaseMemoryVectorIndex
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryIndex
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter

pytest.importorskip("pylibseekdb")

_PROFILE = EmbeddingProfile(
    profile_id="tagged-native-384", model="deterministic", dimension=384, distance="l2", normalization="unit"
)


class _Embedding:
    profile = _PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple(
                ((1.0, 0.0) if "alpha" in text.lower() else (0.8, 0.6) if "selected" in text.lower() else (0.0, 1.0))
                + (0.0,) * 382
                for text in texts
            )
        )


@pytest.mark.asyncio
async def test_tag_filter_precedes_native_vector_candidate_limit(short_tmp_path: Path) -> None:
    baseline = CompositeMemoryIndex(OceanBaseMemoryFTSIndex(), OceanBaseMemoryVectorIndex(_PROFILE))
    candidate = SeekDBMemoryIndex(_PROFILE)
    async with open_builtin_contexts(
        BuiltinConfig(database=SeekDBConfig(path=short_tmp_path / "db")), embedding_model=_Embedding()
    ) as contexts:
        memory = (await contexts.get("project")).artifacts.memory
        first = await memory.remember(
            memory=None,
            entries=(
                MemoryEntryInput(kind="fact", text="Alpha nearest untagged result."),
                MemoryEntryInput(kind="fact", text="Selected farther result."),
            ),
            mode="append",
        )
        assert first is not None
        current = await memory.remember(
            memory=first, entries=(MemoryEntryInput(kind="fact", text="Another entry."),), mode="append"
        )
        assert current is not None
        selected = next(entry for entry in await memory.entries(current) if entry.text == "Selected farther result.")
        target = MemoryEntryTagTarget(artifact_id=current.artifact_id, entry_id=selected.entry_id)
        empty = await contexts.records.get_tags("project", target)
        await contexts.records.replace_tags("project", target, ("selected", "reviewed"), expected_etag=empty.etag)
        for tag_filter in (
            TagFilter(tags=("selected", "reviewed"), match="all"),
            TagFilter(tags=("reviewed", "missing"), match="any"),
        ):
            request = MemorySearchRequest(
                query="alpha",
                analyzed_query="alpha",
                memories=(current.as_ref(),),
                candidate_limit=1,
                mode="hybrid",
                query_vector=(1.0,) + (0.0,) * 383,
                embedding_profile=_PROFILE,
                tag_filter=tag_filter,
            )
            statements: list[tuple[str, Any]] = []

            def record(_connection, _cursor, statement, parameters, _context, _executemany, _statements=statements):
                if "AGAINST" in statement.upper() and "l2_distance" in statement:
                    _statements.append((statement, parameters))

            async with contexts.database.transaction() as connection:
                expected = await baseline.search(connection, "project", request)
                event.listen(Engine, "before_cursor_execute", record)
                try:
                    actual = await candidate.search(connection, "project", request)
                finally:
                    event.remove(Engine, "before_cursor_execute", record)
                assert actual == expected
                assert [hit.entry_id for hit in actual.vector] == [selected.entry_id]
                assert actual.vector[0].memory_ref == current.as_ref()
                assert len(statements) == 1
                statement, parameters = statements[0]
                plan = "\n".join(
                    str(row[0]) for row in (await connection.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
                )
                assert "TEXT RETRIEVAL SCAN" in plan
                assert "VECTOR INDEX" in plan
