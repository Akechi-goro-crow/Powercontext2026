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

"""Regression coverage for shared embedding work, accounting, and cancellation."""

from __future__ import annotations

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


def _vector(text: str) -> tuple[float, ...]:
    return float(len(text)), float(sum(map(ord, text)))


class _Provider:
    profile = EmbeddingProfile(profile_id="coalescing-regression", model="offline", dimension=2)

    def __init__(self, *, blocked: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.started: asyncio.Queue[tuple[str, ...]] = asyncio.Queue()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        if not blocked:
            self.release.set()
        self.failures: set[str] = set()
        self.error = RuntimeError("temporary-provider-failure")
        self.vector_adjustment = 0
        # Deliberately reuse one object: accounting must identify invocations.
        self.usage = InferenceUsage(requests=2, input_tokens=7, output_tokens=3)

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        self.started.put_nowait(texts)
        await self.release.wait()
        self.finished.set()
        if self.failures.intersection(texts):
            raise self.error
        vectors = tuple(_vector(text) for text in texts)
        if self.vector_adjustment < 0:
            vectors = vectors[:-1]
        elif self.vector_adjustment > 0:
            vectors += ((99.0, 99.0),)
        return EmbeddingResult(vectors=vectors, usage=self.usage)


@pytest.mark.parametrize("window", [0, 10])
def test_overlapping_calls_charge_distinct_batches_and_keep_order(window: float) -> None:
    async def scenario() -> None:
        provider = _Provider()
        model = CoalescingEmbeddingModel(provider, max_batch_size=2, coalesce_ms=window)
        inputs = (("a", "bb", "a", "海"), ("海", "bb", "ccc"), ("", "a", ""))
        results = await asyncio.gather(*(model.embed(texts) for texts in inputs))
        assert model.profile is provider.profile
        assert sorted(text for batch in provider.calls for text in batch) == ["", "a", "bb", "ccc", "海"]
        assert all(0 < len(batch) <= 2 for batch in provider.calls)
        for texts, result in zip(inputs, results, strict=True):
            batches = sum(bool(set(texts).intersection(batch)) for batch in provider.calls)
            assert result.vectors == tuple(_vector(text) for text in texts)
            assert result.usage == InferenceUsage(
                requests=2 * batches, input_tokens=7 * batches, output_tokens=3 * batches
            )
        cached = await model.embed(("海", "bb", "a", ""))
        assert cached.usage.requests == 0
        assert len(provider.calls) == 3

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("cache_size", [0, 4])
def test_late_call_reuses_executing_text_and_only_submits_new_text(cache_size: int) -> None:
    async def scenario() -> None:
        provider = _Provider(blocked=True)
        model = CoalescingEmbeddingModel(provider, max_batch_size=2, coalesce_ms=0, cache_size=cache_size)
        first = asyncio.create_task(model.embed(("a", "bb")))
        assert await provider.started.get() == ("a", "bb")
        late = asyncio.create_task(model.embed(("bb", "ccc", "bb")))
        assert await provider.started.get() == ("ccc",)
        provider.release.set()
        one, two = await asyncio.gather(first, late)
        assert one.vectors == (_vector("a"), _vector("bb"))
        assert two.vectors == (_vector("bb"), _vector("ccc"), _vector("bb"))
        assert one.usage == InferenceUsage(requests=2, input_tokens=7, output_tokens=3)
        assert two.usage == InferenceUsage(requests=4, input_tokens=14, output_tokens=6)
        again = await model.embed(("bb",))
        assert len(provider.calls) == (2 if cache_size else 3)
        assert again.usage.requests == (0 if cache_size else 2)

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_failure_does_not_cancel_independent_batches_and_retry_is_not_cached() -> None:
    async def scenario() -> None:
        provider = _Provider(blocked=True)
        provider.failures.add("bad")
        model = CoalescingEmbeddingModel(provider, max_batch_size=1, coalesce_ms=0)
        failed = asyncio.create_task(model.embed(("bad", "good")))
        good = asyncio.create_task(model.embed(("good",)))
        other = asyncio.create_task(model.embed(("other",)))
        assert {await provider.started.get() for _ in range(3)} == {("bad",), ("good",), ("other",)}
        provider.release.set()
        results = await asyncio.gather(failed, good, other, return_exceptions=True)
        assert results[0] is provider.error
        successful = await good
        independent = await other
        assert successful.vectors == (_vector("good"),)
        assert independent.vectors == (_vector("other"),)
        assert (await model.embed(("good", "other"))).usage.requests == 0
        provider.failures.clear()
        retry = await model.embed(("bad",))
        assert retry.vectors == (_vector("bad"),)
        assert provider.calls.count(("bad",)) == 2

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("adjustment", [-1, 1])
def test_malformed_batch_is_rejected_atomically_for_all_waiters(adjustment: int) -> None:
    async def scenario() -> None:
        provider = _Provider()
        provider.vector_adjustment = adjustment
        model = CoalescingEmbeddingModel(provider, coalesce_ms=0)
        results = await asyncio.gather(model.embed(("a", "bb")), model.embed(("a",)), return_exceptions=True)
        assert all(isinstance(result, ValueError) for result in results)
        assert len(provider.calls) == 1
        provider.vector_adjustment = 0
        retry = await model.embed(("a", "bb"))
        assert retry.vectors == (_vector("a"), _vector("bb"))
        assert provider.calls == [("a", "bb"), ("a", "bb")]

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("cache_size", [0, 2])
def test_cancelled_waiter_does_not_cancel_shared_provider(cache_size: int) -> None:
    async def scenario() -> None:
        provider = _Provider(blocked=True)
        model = CoalescingEmbeddingModel(provider, coalesce_ms=0, cache_size=cache_size)
        first = asyncio.create_task(model.embed(("same",)))
        assert await provider.started.get() == ("same",)
        survivor = asyncio.create_task(model.embed(("same", "same")))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        provider.release.set()
        result = await survivor
        assert result.vectors == (_vector("same"), _vector("same"))
        assert result.usage.requests == 2
        assert provider.calls == [("same",)]

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("fail", [False, True])
def test_all_callers_can_cancel_without_poisoning_future_calls(fail: bool) -> None:
    async def scenario() -> None:
        provider = _Provider(blocked=True)
        if fail:
            provider.failures.add("same")
        model = CoalescingEmbeddingModel(provider, coalesce_ms=0)
        errors: list[object] = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: errors.append(context))
        callers = [asyncio.create_task(model.embed(("same",))) for _ in range(2)]
        await provider.started.get()
        for caller in callers:
            caller.cancel()
        assert all(
            isinstance(value, asyncio.CancelledError)
            for value in await asyncio.gather(*callers, return_exceptions=True)
        )
        provider.release.set()
        await provider.finished.wait()
        await asyncio.sleep(0)
        provider.failures.clear()
        result = await model.embed(("same",))
        assert result.vectors == (_vector("same"),)
        assert len(provider.calls) == (2 if fail else 1)
        assert (await model.embed(("same",))).usage.requests == 0
        await asyncio.sleep(0)
        assert errors == []

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_lru_access_refreshes_only_successful_vectors() -> None:
    async def scenario() -> None:
        provider = _Provider()
        model = CoalescingEmbeddingModel(provider, coalesce_ms=0, cache_size=2)
        await model.embed(("a", "b"))
        assert (await model.embed(("a",))).usage.requests == 0
        await model.embed(("c",))
        assert (await model.embed(("a",))).usage.requests == 0
        provider.failures.add("bad")
        with pytest.raises(RuntimeError):
            await model.embed(("bad",))
        assert (await model.embed(("a", "c"))).usage.requests == 0
        await model.embed(("b",))
        assert provider.calls == [("a", "b"), ("c",), ("bad",), ("b",)]

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_unknown_token_usage_stays_unknown_and_empty_call_issues_no_request() -> None:
    provider = _Provider()
    provider.usage = InferenceUsage(requests=3)
    model = CoalescingEmbeddingModel(provider, coalesce_ms=0)

    async def scenario() -> None:
        empty = await model.embed(())
        assert empty.vectors == () and empty.usage.requests == 0 and provider.calls == []
        result = await model.embed(("a", "a"))
        assert result.usage == InferenceUsage(requests=3)
        assert (await model.embed(("a",))).usage.requests == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_batch_size", 0),
        ("max_batch_size", -1),
        ("max_batch_size", True),
        ("max_batch_size", 1.5),
        ("cache_size", -1),
        ("cache_size", True),
        ("cache_size", 1.5),
        ("coalesce_ms", -1),
        ("coalesce_ms", True),
        ("coalesce_ms", float("nan")),
        ("coalesce_ms", float("inf")),
    ],
)
def test_invalid_configuration(name: str, value) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(_Provider(), **{name: value})
