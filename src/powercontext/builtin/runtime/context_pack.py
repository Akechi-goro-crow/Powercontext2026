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

"""Portable, checked UTF-8 context packs and prefix-reuse deltas."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ContextPackError(ValueError):
    """A context pack, delta, or UTF-8 chunk violates the wire contract."""


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError("duplicate-field")
        result[key] = value
    return result


def _decode(data: bytes, schema: str, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise ContextPackError("invalid-wire-bytes")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ContextPackError("invalid-wire-json") from error
    if not isinstance(payload, dict) or payload.keys() != fields or payload["schema"] != schema:
        raise ContextPackError("invalid-wire-fields")
    return payload


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple):
        raise ContextPackError("invalid-chunks")
    for item in items:
        if not isinstance(item, bytes):
            raise ContextPackError("invalid-chunk-bytes")
        try:
            item.decode("utf-8")
        except UnicodeError as error:
            raise ContextPackError("invalid-chunk-utf8") from error


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("invalid-chunk-array")
    items: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("invalid-base64-string")
        try:
            item = base64.b64decode(value.encode("ascii"), validate=True)
        except ValueError as error:
            raise ContextPackError("invalid-base64") from error
        if base64.b64encode(item).decode("ascii") != value:
            raise ContextPackError("noncanonical-base64")
        items.append(item)
    result = tuple(items)
    _validate_items(result)
    return result


def _validate_digest(value: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContextPackError("invalid-digest")


@dataclass(frozen=True, slots=True)
class ContextPack:
    """An immutable full context whose digest authenticates its chunk boundaries."""

    items: tuple[bytes, ...]
    digest: str = field(init=False)
    byte_count: int = field(init=False)
    _wire: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_items(self.items)
        payload: dict[str, Any] = {"schema": _PACK_SCHEMA, "items": _encode_items(self.items)}
        digest = "sha256:" + hashlib.sha256(_encode(payload)).hexdigest()
        payload.update(digest=digest, byte_count=0)
        wire = _encode(payload)
        # The count includes its own decimal digits; solve that small fixed point.
        while payload["byte_count"] != len(wire):
            payload["byte_count"] = len(wire)
            wire = _encode(payload)
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "byte_count", len(wire))
        object.__setattr__(self, "_wire", wire)

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Build a full pack from independently valid UTF-8 chunks."""
        return cls(items)

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = 512) -> ContextPack:
        """Split text deterministically without breaking a UTF-8 code point."""
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("invalid-chunk-limit")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ContextPackError("invalid-content")
        try:
            data = content.encode("utf-8")
        except UnicodeError as error:
            raise ContextPackError("invalid-content-utf8") from error
        items: list[bytes] = []
        start = 0
        while start < len(data):
            end = min(start + chunk_bytes, len(data))
            while end < len(data) and data[end] & 0xC0 == 0x80:
                end -= 1
            if end == start:
                raise ContextPackError("chunk-limit-too-small")
            items.append(data[start:end])
            start = end
        return cls.build(tuple(items))

    def to_bytes(self) -> bytes:
        """Return canonical v1 JSON, including its exact serialized byte count."""
        return self._wire

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode a standalone pack and validate both its digest and byte count."""
        payload = _decode(data, _PACK_SCHEMA, _PACK_FIELDS)
        pack = cls.build(_decode_items(payload["items"]))
        if payload["digest"] != pack.digest:
            raise ContextPackError("pack-digest-mismatch")
        if type(payload["byte_count"]) is not int or payload["byte_count"] != pack.byte_count:
            raise ContextPackError("pack-byte-count-mismatch")
        return pack


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A checked reference to a base prefix followed by replacement chunks."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]
    _wire: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("invalid-prefix")
        _validate_items(self.appended)
        object.__setattr__(
            self,
            "_wire",
            _encode({
                "schema": _DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }),
        )

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Reuse the longest common chunk prefix, without retaining either pack."""
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("invalid-pack")
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        """Count every wire byte, without adding a field to the delta protocol."""
        return len(self._wire)

    def to_bytes(self) -> bytes:
        return self._wire

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _decode(data, _DELTA_SCHEMA, _DELTA_FIELDS)
        return cls(
            payload["base_digest"],
            payload["target_digest"],
            payload["prefix_items"],
            _decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Reconstruct and authenticate a target using only its base and delta."""
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("invalid-pack-or-delta")
    if delta.base_digest != base.digest:
        raise ContextPackError("base-digest-mismatch")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("prefix-out-of-bounds")
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("target-digest-mismatch")
    return target
