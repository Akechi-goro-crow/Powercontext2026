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

"""Do not silently lose text from invoked PDF Form XObjects."""

import zlib

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    IndirectObject,
    NameObject,
    NumberObject,
    StreamObject,
)

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document
from tests.pdf_helpers import pdf_bytes, pdf_writer


def _form(
    writer: PdfWriter, resources: DictionaryObject, payload: bytes, *, compressed: bool = False
) -> IndirectObject:
    stream: StreamObject
    if compressed:
        stream = EncodedStreamObject()
        stream._data = payload
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    else:
        stream = DecodedStreamObject()
        stream.set_data(payload)
    stream[NameObject("/Type")] = NameObject("/XObject")
    stream[NameObject("/Subtype")] = NameObject("/Form")
    stream[NameObject("/BBox")] = ArrayObject([NumberObject(n) for n in (0, 0, 100, 100)])
    stream[NameObject("/Resources")] = resources
    return writer._add_object(stream)


def _form_pdf(payload: bytes, *, compressed: bool, nested: bool = False, repeat: bool = False) -> bytes:
    writer = pdf_writer("Readable outside Form")
    page = writer.pages[0]
    resources = page["/Resources"]
    assert isinstance(resources, DictionaryObject)
    form_resources = DictionaryObject({NameObject("/Font"): resources["/Font"]})
    form = _form(writer, form_resources, payload, compressed=compressed)
    if nested:
        outer_resources = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Inner"): form})})
        form = _form(writer, outer_resources, b"/Inner Do")
    resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Evidence"): form})
    content = page["/Contents"]
    assert isinstance(content, StreamObject)
    content.set_data(content.get_data() + b"\n/Evidence Do" * (2 if repeat else 1))
    return pdf_bytes(writer)


@pytest.mark.parametrize(("nested", "repeat"), [(False, False), (True, False), (False, True)])
def test_valid_invoked_forms_preserve_text(nested: bool, repeat: bool) -> None:
    raw = _form_pdf(
        zlib.compress(b"BT /F1 12 Tf (Form revenue 100) Tj ET"),
        compressed=True,
        nested=nested,
        repeat=repeat,
    )
    pages = parse_pdf_document(PDFDocumentInput(document_id="forms", content=raw))

    assert len(pages) == 1
    assert "Readable outside Form" in pages[0].text
    assert pages[0].text.count("Form revenue 100") == (2 if repeat else 1)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    ("payload", "compressed"),
    [(b"not-a-zlib-stream", True), (b"BT (unterminated", False)],
    ids=["invalid-flate", "unterminated-string"],
)
def test_corrupt_invoked_forms_fail_atomically(payload: bytes, compressed: bool, nested: bool) -> None:
    raw = _form_pdf(payload, compressed=compressed, nested=nested)

    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="forms", content=raw))
