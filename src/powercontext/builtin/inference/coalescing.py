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

"""Share in-flight embedding work and cache successful vectors in one event loop."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile


@dataclass(frozen=True, slots=True, eq=False)
class _BatchUsage:
    # Identity belongs to the provider invocation, even if it reuses a usage object.
    usage: InferenceUsage


_PendingValue: TypeAlias = tuple[EmbeddingVector, _BatchUsage]


def _observe_exception(future: asyncio.Future[_PendingValue]) -> None:
    """Retrieve failures even when all callers have already been cancelled."""
    if not future.cancelled():
        future.exception()


def _sum_usage(batches: set[_BatchUsage]) -> InferenceUsage:
    input_tokens = [batch.usage.input_tokens for batch in batches if batch.usage.input_tokens is not None]
    output_tokens = [batch.usage.output_tokens for batch in batches if batch.usage.output_tokens is not None]
    return InferenceUsage(
        requests=sum(batch.usage.requests for batch in batches),
        input_tokens=sum(input_tokens) if input_tokens else None,
        output_tokens=sum(output_tokens) if output_tokens else None,
    )


class CoalescingEmbeddingModel:
    """A deployment-fixed embedding model with bounded batches and an LRU cache.

    Calls made in the same event loop share queued and executing texts. Caller
    cancellation only stops that caller's wait; successful shared work can still
    populate the cache. The wrapper can be constructed before the loop starts.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("invalid-max-batch-size")
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("invalid-cache-size")
        if (
            isinstance(coalesce_ms, bool)
            or not isinstance(coalesce_ms, (int, float))
            or not math.isfinite(coalesce_ms)
            or coalesce_ms < 0
        ):
            raise ValueError("invalid-coalesce-window")
        self.profile: EmbeddingProfile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[_PendingValue]] = {}
        self._queued: dict[str, asyncio.Future[_PendingValue]] = {}
        self._flush_handle: asyncio.Handle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return original order and charge each awaited provider batch once."""
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        loop = asyncio.get_running_loop()
        resolved: dict[str, EmbeddingVector] = {}
        waiting: dict[str, asyncio.Future[_PendingValue]] = {}
        # No suspension until every text has registered its shared future.
        for text in texts:
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                resolved[text] = cached
                continue
            future = self._inflight.get(text)
            if future is None:
                future = loop.create_future()
                future.add_done_callback(_observe_exception)
                self._inflight[text] = future
                self._queued[text] = future
            waiting[text] = future
        if self._queued and self._flush_handle is None:
            self._flush_handle = loop.call_later(self._coalesce_seconds, self._dispatch)
        # Shield each shared future, not the caller itself. Cancelling gather
        # must never propagate to a provider batch or another caller's result.
        values = await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
        batches: set[_BatchUsage] = set()
        for text, (vector, batch) in zip(waiting, values, strict=True):
            resolved[text] = vector
            batches.add(batch)
        return EmbeddingResult(vectors=tuple(resolved[text] for text in texts), usage=_sum_usage(batches))

    def _dispatch(self) -> None:
        self._flush_handle = None
        queued = tuple(self._queued.items())
        self._queued.clear()
        loop = asyncio.get_running_loop()
        for start in range(0, len(queued), self._max_batch_size):
            batch = queued[start : start + self._max_batch_size]
            task = loop.create_task(self._embed_batch(batch))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _embed_batch(self, batch: tuple[tuple[str, asyncio.Future[_PendingValue]], ...]) -> None:
        try:
            result = await self._model.embed(tuple(text for text, _ in batch))
            paired = tuple(zip(batch, result.vectors, strict=True))
            usage = _BatchUsage(result.usage.model_copy())
        except (Exception, asyncio.CancelledError) as error:
            for text, future in batch:
                self._inflight.pop(text)
                if isinstance(error, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(error)
            return
        for (text, future), vector in paired:
            self._inflight.pop(text)
            if self._cache_size:
                self._cache[text] = vector
                self._cache.move_to_end(text)
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            future.set_result((vector, usage))
