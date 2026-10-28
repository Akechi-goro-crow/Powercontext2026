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


"""The hybrid candidate statement budget and channel/reference contract."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemorySearchRequest
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryIndex
from powercontext.builtin.tags import TagFilter


@pytest.mark.asyncio
@pytest.mark.parametrize("tag_filter", [None, TagFilter(tags=("selected",))])
async def test_hybrid_uses_one_candidate_statement_and_preserves_both_channels(tag_filter, monkeypatch) -> None:
    profile = EmbeddingProfile(profile_id="test", model="test", dimension=3, distance="l2", normalization="unit")
    index = SeekDBMemoryIndex(profile)
    # Completeness and head validation are separate from candidate retrieval.
    monkeypatch.setattr(index, "vector_complete", AsyncMock(return_value=True))
    current = ArtifactRef(family="memory", artifact_id="memory", revision=7)
    identity = {
        "memory_artifact_id": "memory",
        "head_revision": 2,
        "entry_id": "entry",
        "entry_version_id": "version",
        "text": "Exact current version body.",
    }
    connection = AsyncMock(spec=AsyncConnection)
    connection.execute.return_value.mappings = MagicMock(
        return_value=MagicMock(
            all=MagicMock(
                return_value=[
                    dict(identity, channel="fts", distance=None),
                    dict(identity, channel="vector", distance=0.0),
                ]
            )
        )
    )
    result = await index.search(
        cast(AsyncConnection, connection),
        "scope",
        MemorySearchRequest(
            query="exact",
            analyzed_query="exact",
            memories=(current,),
            candidate_limit=10,
            mode="hybrid",
            query_vector=(1.0, 0.0, 0.0),
            embedding_profile=profile,
            tag_filter=tag_filter,
        ),
    )
    connection.execute.assert_awaited_once()
    assert len(result.fts) == len(result.vector) == 1
    assert result.fts[0].memory_ref == result.vector[0].memory_ref == current
    assert result.fts[0].entry_version_id == result.vector[0].entry_version_id == "version"
    assert result.fts[0].text == result.vector[0].text == identity["text"]
    assert result.vector[0].distance == 0.0
