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

"""Strict extraction boundary around pypdf's otherwise forgiving content reader."""

from __future__ import annotations

import io
import logging
import threading
import zlib
from contextlib import contextmanager
from typing import TYPE_CHECKING

from pypdf import PdfReader
from pypdf.generic import ArrayObject, ContentStream, DictionaryObject, NullObject, PdfObject, StreamObject
from typing_extensions import override

from powercontext.sources._pdf_text import extract_text

if TYPE_CHECKING:
    from collections.abc import Generator


class _ParseWarnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.thread = threading.get_ident()
        self.messages: list[str] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread:
            self.messages.append(record.getMessage())


@contextmanager
def _complete_extraction() -> Generator[None]:
    # pypdf catches errors in nested Form XObjects and logs instead of raising.
    # Capture only this parsing thread; do not change global levels or methods.
    logger = logging.getLogger("pypdf")
    handler = _ParseWarnings()
    logger.addHandler(handler)
    try:
        yield
        if handler.messages:
            raise ValueError("PDF extraction was incomplete: " + handler.messages[0])
    finally:
        logger.removeHandler(handler)


def extract_pages(stream: io.BytesIO) -> tuple[str, ...]:
    with _complete_extraction():
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password")  # noqa: TRY003
        pages = tuple(reader.pages)
        if not pages:
            raise ValueError("PDF contains no physical pages")  # noqa: TRY003
        root_pages = reader.root_object["/Pages"]
        if not isinstance(root_pages, DictionaryObject) or root_pages.get("/Count") != len(pages):
            raise ValueError("PDF page tree is incomplete")  # noqa: TRY003
        texts = []
        validated: set[tuple[int, int]] = set()
        for page in pages:
            resources = _resources(page.get("/Resources"))
            _validate_content(page.get("/Contents"), reader, validated, resources)
            _validate_forms(resources, reader, validated)
            texts.append(extract_text(page, reader, resources))
        return tuple(texts)


def _validate_content(
    value: PdfObject | None, reader: PdfReader, validated: set[tuple[int, int]], resources: DictionaryObject
) -> None:
    if value is None:
        return
    content = value.get_object()
    if isinstance(content, NullObject):
        return
    if not isinstance(content, (StreamObject, ArrayObject)):
        raise TypeError("PDF page content is not a stream")  # noqa: TRY003
    if isinstance(content, StreamObject) and "/Resources" in content:
        resources = _resources(content.get("/Resources"))
    key = (id(content), id(resources))
    if key in validated:
        return
    validated.add(key)
    for part in content if isinstance(content, ArrayObject) else (content,):
        _validate_compression(part.get_object())
    # A /Contents array is one logical program. Operands and text state may
    # legitimately continue across stream boundaries.
    _validate_operations(ContentStream(content, reader), resources)
    _validate_forms(resources, reader, validated)


def _validate_compression(content: PdfObject | None) -> None:
    if isinstance(content, NullObject):
        return
    if not isinstance(content, StreamObject):
        raise TypeError("PDF page content is not a stream")  # noqa: TRY003
    filters = content.get("/Filter")
    if filters in ("/FlateDecode", "/Fl", ["/FlateDecode"], ["/Fl"]):
        # get_data() deliberately repairs even truncated zlib streams. Validate
        # the original compressed bytes first so missing text cannot look blank.
        decoder = zlib.decompressobj()
        remaining = content._data
        while remaining:
            decoder.decompress(remaining, max_length=1024 * 1024)
            remaining = decoder.unconsumed_tail
        if not decoder.eof:
            raise ValueError("PDF content stream is truncated")  # noqa: TRY003


def _validate_operations(content: ContentStream, resources: DictionaryObject) -> None:
    fonts = resources.get("/Font", DictionaryObject()).get_object()
    for operands, operator in content.operations:
        if operator in {b"Tj", b"TJ", b"'"} and len(operands) != 1:
            raise ValueError("PDF text operation is malformed")  # noqa: TRY003
        if operator == b"Tf" and (
            len(operands) != 2 or not isinstance(fonts, DictionaryObject) or operands[0] not in fonts
        ):
            raise ValueError("PDF font operation cannot be resolved")  # noqa: TRY003
        if operator in {b"Tj", b"TJ", b"'", b'"'} and operands and not fonts:
            raise ValueError("PDF text has no font resources")  # noqa: TRY003


def _resources(value: PdfObject | None) -> DictionaryObject:
    if value is None:
        return DictionaryObject()
    resources = value.get_object()
    if not isinstance(resources, DictionaryObject):
        raise TypeError("PDF resources must be a dictionary")  # noqa: TRY003
    return resources


def _validate_forms(resources: DictionaryObject, reader: PdfReader, validated: set[tuple[int, int]]) -> None:
    objects = resources.get("/XObject", DictionaryObject()).get_object()
    for obj in objects.values():
        resolved = obj.get_object()
        if resolved.get("/Subtype") == "/Form":
            _validate_content(resolved, reader, validated, resources)
