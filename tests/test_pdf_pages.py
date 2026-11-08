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

from __future__ import annotations

import asyncio
import hashlib
import io
import zlib

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NumberObject,
)

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _font() -> DictionaryObject:
    return DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })


def _writer(*contents: bytes | None) -> PdfWriter:
    writer = PdfWriter()
    for content in contents:
        page = writer.add_blank_page(width=600, height=800)
        if content is not None:
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): _font()})
            })
            stream = DecodedStreamObject()
            stream.set_data(content)
            page[NameObject("/Contents")] = writer._add_object(stream)
    return writer


def _bytes(writer: PdfWriter) -> bytes:
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _parse(writer: PdfWriter, document_id: str = "financial-report") -> tuple[PDFPageInput, ...]:
    return parse_pdf_document(PDFDocumentInput(document_id=document_id, content=_bytes(writer)))


def test_all_physical_pages_numbers_and_tables_survive() -> None:
    writer = _writer(
        b"BT /F1 12 Tf 40 700 Td (Printed page ix) Tj 0 -20 Td (Revenue 1,234.50) Tj ET",
        None,
        b"BT /F1 12 Tf 40 700 Td (Printed page 1) Tj 0 -20 Td (Debt 9,876) Tj ET",
    )
    raw = _bytes(writer)
    pages = parse_pdf_document(PDFDocumentInput(document_id=" annual:report/2025 ", content=raw))
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert pages[1].text == ""
    assert "Revenue 1,234.50" in pages[0].text and "Debt 9,876" in pages[2].text
    assert all(page.document_id == " annual:report/2025 " for page in pages)
    assert all(page.document_sha256 == hashlib.sha256(raw).hexdigest() for page in pages)


@pytest.mark.parametrize("document_id", ["", " ", "\t\n"])
def test_blank_document_identity_is_rejected(document_id: str) -> None:
    with pytest.raises(ValueError):
        _parse(_writer(None), document_id)


def test_captured_identity_projection_and_revision_round_trip() -> None:
    async def check() -> None:
        writer = _writer(b"BT /F1 12 Tf 40 700 Td (Net income 321) Tj ET", None)
        original = _parse(writer, "财报:a/b:" * 100)
        writer.add_metadata({"/Title": "A revised document"})
        updated = _parse(writer, original[0].document_id)
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        sources = [await registry.resolve(page) for page in (*original, *updated)]
        assert len({source.name for source in sources}) == 4
        for page, source in zip((*original, *updated), sources, strict=True):
            assert len(source.name) <= 256
            assert source.materialization == SourceMaterialization.CAPTURED
            assert source.definition_version == "1"
            assert await registry.read(source) == page
            assert (await registry.resolve(page)).name == source.name
            projection = project_source_for_transport(registry, source).projection(TEXT_EVIDENCE_PROJECTION_KEY)
            assert projection == {
                "source_type": "pdf-page",
                "source_id": source.name,
                "content": f"{page.document_id}\n{page.text}",
                "metadata": {
                    "document_id": page.document_id,
                    "document_sha256": page.document_sha256,
                    "page_number": page.page_number,
                },
            }
        assert await registry.read(sources[0]) == original[0]

    asyncio.run(check())


@pytest.mark.parametrize("password", ["", "secret"])
def test_encrypted_document_password_contract(password: str) -> None:
    writer = _writer(b"BT /F1 12 Tf 40 700 Td (Readable revenue 123) Tj ET")
    writer.encrypt(user_password=password, owner_password="owner")  # noqa: S106
    if password:
        with pytest.raises(ValueError):
            _parse(writer)
    else:
        assert "Readable revenue 123" in _parse(writer)[0].text


@pytest.mark.parametrize("content", [b"", b"not a PDF", b"%PDF-1.7\ntruncated"])
def test_invalid_files_fail(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken", content=content))


@pytest.mark.parametrize("damage", ["contents", "flate", "syntax", "count", "form", "font"])
def test_corrupt_later_page_never_returns_partial_pages(damage: str) -> None:
    writer = _writer(b"BT /F1 12 Tf 40 700 Td (Valid first page) Tj ET", b"q Q")
    page = writer.pages[1]
    if damage == "contents":
        page[NameObject("/Contents")] = NumberObject(42)
    elif damage == "count":
        root = writer.root_object["/Pages"]
        assert isinstance(root, DictionaryObject)
        root[NameObject("/Count")] = NumberObject(3)
    elif damage == "font":
        page[NameObject("/Resources")] = DictionaryObject()
        text = DecodedStreamObject()
        text.set_data(b"BT /MissingFont 12 Tf (Unextractable text) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(text)
    else:
        stream = EncodedStreamObject() if damage in {"flate", "form"} else DecodedStreamObject()
        if damage in {"flate", "form"}:
            stream[NameObject("/Filter")] = NameObject("/FlateDecode")
            stream._data = zlib.compress(b"BT /F1 12 Tf (Missing text) Tj ET")[:-5]
        else:
            stream.set_data(b"BT /F1 12 Tf (Unterminated literal")
        if damage == "form":
            stream[NameObject("/Subtype")] = NameObject("/Form")
            stream[NameObject("/BBox")] = ArrayObject([NumberObject(n) for n in [0, 0, 600, 800]])
            resources = page["/Resources"]
            assert isinstance(resources, DictionaryObject)
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Fm"): writer._add_object(stream)})
            invocation = DecodedStreamObject()
            invocation.set_data(b"q /Fm Do Q")
            page[NameObject("/Contents")] = writer._add_object(invocation)
        else:
            page[NameObject("/Contents")] = writer._add_object(stream)
    with pytest.raises(ValueError):
        _parse(writer)


def test_form_xobject_text_is_not_dropped() -> None:
    writer = _writer(b"q /Fm Do Q")
    form = DecodedStreamObject()
    form.set_data(b"BT /F1 12 Tf 40 700 Td (Inside a form: 456) Tj ET")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/BBox")] = ArrayObject([NumberObject(n) for n in [0, 0, 600, 800]])
    form[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): _font()})
    })
    resources = writer.pages[0]["/Resources"]
    assert isinstance(resources, DictionaryObject)
    resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Fm"): writer._add_object(form)})
    assert "Inside a form: 456" in _parse(writer)[0].text


def test_page_operations_can_span_multiple_content_streams() -> None:
    writer = _writer(b"q Q")
    pieces = ArrayObject()
    for data in (b"BT /F1 12", b"Tf 40 700 Td (Split operations 123) Tj ET"):
        stream = DecodedStreamObject()
        stream.set_data(data)
        pieces.append(writer._add_object(stream))
    writer.pages[0][NameObject("/Contents")] = pieces
    assert "Split operations 123" in _parse(writer)[0].text
