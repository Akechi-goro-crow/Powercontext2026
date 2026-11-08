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

"""Identity-preserving Memory reranking through an injected HTTP scoring service."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class _Score(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)


class _Scores(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scores: list[_Score]


class HTTPMemoryReranker:
    """Score bounded batches, then select original ranks in one stable global order.

    The caller owns the injected client's authentication, timeout and lifecycle.
    HTTP and transport errors propagate without changing or accepting partial work.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Return original global ranks, reporting the actual number of requests."""

        if not candidates or type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("rerank requires candidates and a limit within their count")  # noqa: TRY003
        scores: list[_Score] = []
        request_count = 0
        for offset in range(0, len(candidates), self._batch_size):
            batch = candidates[offset : offset + self._batch_size]
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text} for rank, candidate in enumerate(batch, start=offset + 1)
                    ],
                },
            )
            request_count += 1
            response.raise_for_status()
            result = _Scores.model_validate(response.json())
            expected = set(range(offset + 1, offset + len(batch) + 1))
            if len(result.scores) != len(batch) or {item.rank for item in result.scores} != expected:
                raise ValueError("scores must cover each rank in this batch exactly once")  # noqa: TRY003
            scores.extend(result.scores)
        scores.sort(key=lambda item: (-item.score, item.rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(item.rank for item in scores[:limit]),
            usage=InferenceUsage(requests=request_count),
        )


__all__ = ["HTTPMemoryReranker"]
