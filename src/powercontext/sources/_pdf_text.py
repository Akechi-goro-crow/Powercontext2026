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

"""Honor PDF replacement text and text positioning during plain extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pypdf.generic import ContentStream, DictionaryObject, NameObject, NullObject, create_string_object

if TYPE_CHECKING:
    from pypdf import PageObject, PdfReader
    from pypdf.generic import PdfObject

_LIGATURES = str.maketrans(dict(zip("ﬀﬁﬂﬃﬄﬅﬆ", ("ff", "fi", "fl", "ffi", "ffl", "st", "st"), strict=True)))
_SHOW_TEXT = {b"Tj", b"TJ", b"'", b'"'}
_POSITION_TEXT = {b"Tm", b"Td", b"TD", b"T*", b"BT", b"ET", b"'", b'"'}


def _prepare(
    value: PdfObject,
    reader: PdfReader,
    resources: DictionaryObject,
    prepared: dict[tuple[int, int], ContentStream],
) -> ContentStream:
    original = value.get_object()
    if isinstance(original, DictionaryObject) and "/Resources" in original:
        resources = cast(DictionaryObject, original["/Resources"])
    key = (id(original), id(resources))
    if key in prepared:
        return prepared[key]
    content = ContentStream(original, reader, "bytes")
    prepared[key] = content
    if isinstance(original, DictionaryObject):
        content.update(original)
    local_resources = DictionaryObject(resources)
    content[NameObject("/Resources")] = local_resources
    objects = resources.get("/XObject", DictionaryObject()).get_object()
    local_objects = DictionaryObject(objects)
    local_resources[NameObject("/XObject")] = local_objects
    for name, reference in objects.items():
        form = reference.get_object()
        if form.get("/Subtype") == "/Form":
            local_objects[name] = _prepare(form, reader, resources, prepared)
    _trim_position_adjustments(content)
    return content


def _trim_position_adjustments(content: ContentStream) -> None:
    # TJ numbers move the text cursor; they are not characters. A trailing
    # movement cannot create whitespace when the next text position replaces
    # that cursor. Keep it when a following text-show operation uses the cursor.
    repositioned = True
    for operands, operator in reversed(content.operations):
        if operator == b"TJ" and repositioned and operands:
            items = operands[0]
            while items and isinstance(items[-1], (int, float)):
                items.pop()
        if operator in _SHOW_TEXT:
            repositioned = False
        if operator in _POSITION_TEXT:
            repositioned = True


def _replacement(properties: Any, resources: DictionaryObject) -> str | None:
    if isinstance(properties, NameObject):
        properties = resources.get("/Properties", DictionaryObject()).get_object()[properties]
    if not isinstance(properties, DictionaryObject) or "/ActualText" not in properties:
        return None
    replacement = properties["/ActualText"]
    if isinstance(replacement, bytes):
        replacement = create_string_object(replacement)
    if not isinstance(replacement, str):
        raise TypeError("PDF replacement text cannot be decoded")  # noqa: TRY003
    return replacement


class _ActualText:
    def __init__(self, resources: DictionaryObject) -> None:
        self.resources = [resources]
        self.spans: list[str | None] = []
        self.saved: list[Any] | None = None

    def before(self, operator: bytes, operands: list[Any], _cm: list[float], _tm: list[float]) -> None:
        if operator == b"Do":
            objects = cast(DictionaryObject, self.resources[-1]["/XObject"])
            form = cast(DictionaryObject, objects[operands[0]])
            self.resources.append(form.get("/Resources", self.resources[-1]).get_object())
        elif operator in {b"BMC", b"BDC"}:
            replacement = None
            if operator == b"BDC" and len(operands) == 2:
                replacement = _replacement(operands[1], self.resources[-1])
            self.spans.append(replacement)
        elif operator == b"EMC":
            self.spans.pop()
        elif operator in _SHOW_TEXT:
            for index, replacement in enumerate(self.spans):
                if replacement is not None:
                    self.saved = operands[:]
                    # A Python str bypasses font decoding in pypdf. ActualText
                    # is a PDF text string, not a glyph encoded by the font.
                    if operator == b"TJ":
                        operands[:] = [[replacement]]
                    else:
                        operands[-1] = replacement
                    self.spans[index] = ""
                    break

    def after(self, operator: bytes, operands: list[Any], _cm: list[float], _tm: list[float]) -> None:
        if operator == b"Do":
            self.resources.pop()
        elif self.saved is not None:
            operands[:] = self.saved
            self.saved = None


def extract_text(page: PageObject, reader: PdfReader, resources: DictionaryObject) -> str:
    value = page.get("/Contents")
    if value is None or isinstance(value.get_object(), NullObject):
        return ""
    # These objects belong to this in-memory reader only; original PDF bytes
    # and the document digest never change. Forms retain their resource scope.
    content = _prepare(value, reader, resources, {})
    page[NameObject("/Contents")] = content
    resources = cast(DictionaryObject, content["/Resources"])
    page[NameObject("/Resources")] = resources
    visitor = _ActualText(resources)
    return page.extract_text(visitor_operand_before=visitor.before, visitor_operand_after=visitor.after).translate(
        _LIGATURES
    )
