"""HTTP reranking rejects ambiguous identities and preserves request failures."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
        entry_id=f"e{rank}",
        entry_version_id=f"v{rank}",
        text=f"evidence {rank}",
        score=1.0,
        matched_by=("fts",),
    )


@pytest.mark.parametrize("field,value", [("rank", True), ("rank", 1.0), ("score", True), ("score", "0.5")])
def test_rejects_coercible_response_values(field: str, value: object) -> None:
    async def scenario() -> None:
        row = {"rank": 1, "score": 0.5, field: value}
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"scores": [row]}))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://score.invalid", client=client).rerank("q", (_hit(1),), 1)

    asyncio.run(scenario())


def test_rejects_cross_batch_rank_after_a_valid_first_batch() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"scores": [{"rank": 1, "score": 1}]}))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://score.invalid", client=client, batch_size=1).rerank(
                    "q", (_hit(1), _hit(2)), 1
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 429, 500])
def test_propagates_non_success_status_from_later_batch(status: int) -> None:
    async def scenario() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            rank = json.loads(request.content)["candidates"][0]["rank"]
            if rank == 2:
                return httpx.Response(status)
            return httpx.Response(200, json={"scores": [{"rank": rank, "score": 1}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await HTTPMemoryReranker("https://score.invalid", client=client, batch_size=1).rerank(
                    "q", (_hit(1), _hit(2)), 1
                )
            assert error.value.response.status_code == status
            assert not client.is_closed

    asyncio.run(scenario())
