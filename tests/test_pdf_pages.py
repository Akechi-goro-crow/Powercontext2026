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

"""Physical page boundaries, immutable document identity, and atomic PDF failures."""

import asyncio
import base64
import hashlib
import zlib

import pytest
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    IndirectObject,
    NameObject,
    NullObject,
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
from tests.pdf_helpers import pdf_bytes, pdf_writer


def test_all_pages_keep_text_numbers_and_physical_boundaries():
    raw = pdf_bytes(pdf_writer("Printed page ix\nNet revenue 1,234.56", "", "Printed page 1\nDebt (123) 2024"))
    pages = parse_pdf_document(PDFDocumentInput(document_id="report", content=raw))
    assert isinstance(pages, tuple)
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [page.text.strip() for page in pages] == [
        "Printed page ix\nNet revenue 1,234.56",
        "",
        "Printed page 1\nDebt (123) 2024",
    ]
    assert {page.document_sha256 for page in pages} == {hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize("document_id", ["", " ", "\t\n"])
def test_blank_document_ids_are_rejected(document_id):
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id=document_id, content=pdf_bytes(pdf_writer("test"))))
    with pytest.raises(ValueError):
        PDFPageInput(document_id=document_id, document_sha256="a" * 64, page_number=1, text="test")


@pytest.mark.parametrize(
    "field,value", [("document_sha256", "A" * 64), ("document_sha256", "abc"), ("page_number", 0), ("page_number", -1)]
)
def test_page_input_rejects_invalid_provenance(field, value):
    data = {"document_id": "report", "document_sha256": "a" * 64, "page_number": 1, "text": "test", field: value}
    with pytest.raises(ValueError):
        PDFPageInput.model_validate(data)


def test_long_unicode_ids_and_revisions_have_bounded_unambiguous_sources():
    async def scenario():
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        writer = pdf_writer("Revenue 123", "Revenue 456")
        raw = pdf_bytes(writer)
        ids = ["a:b", "a%3Ab", " 财报 /:#% ", "财报" * 300, "财报" * 300 + "修订"]
        sources = []
        for document_id in ids:
            for page in parse_pdf_document(PDFDocumentInput(document_id=document_id, content=raw)):
                source = await registry.resolve(page)
                assert source.materialization is SourceMaterialization.CAPTURED
                assert source.definition_version == "1"
                assert 0 < len(source.name) <= 256
                assert (await registry.resolve(page)).name == source.name
                assert await registry.read(source) == page
                projection = project_source_for_transport(registry, source).projection(TEXT_EVIDENCE_PROJECTION_KEY)
                assert projection == {
                    "source_type": "pdf-page",
                    "source_id": source.name,
                    "content": f"{document_id}\n{page.text}",
                    "metadata": {
                        "document_id": document_id,
                        "document_sha256": hashlib.sha256(raw).hexdigest(),
                        "page_number": page.page_number,
                    },
                }
                sources.append(source)
        assert len({source.name for source in sources}) == len(sources)
        writer.add_metadata({"/Title": "A revised original PDF"})
        changed = parse_pdf_document(PDFDocumentInput(document_id=ids[0], content=pdf_bytes(writer)))
        assert (await registry.resolve(changed[0])).name != sources[0].name
        original_page = await registry.read(sources[0])
        assert isinstance(original_page, PDFPageInput)
        assert original_page.document_sha256 == hashlib.sha256(raw).hexdigest()

    asyncio.run(scenario())


@pytest.mark.parametrize("password", ["", "secret"])
def test_permission_encryption_needs_no_user_password(password):
    writer = pdf_writer("Permission protected revenue 123")
    writer.encrypt(user_password=password, owner_password="owner")  # noqa: S106
    value = PDFDocumentInput(document_id="permissions", content=pdf_bytes(writer))
    if password:
        with pytest.raises(ValueError):
            parse_pdf_document(value)
    else:
        assert "revenue 123" in parse_pdf_document(value)[0].text


@pytest.mark.parametrize("raw", [b"", b"not a PDF", b"%PDF-1.7\ntruncated"])
def test_invalid_pdf_bytes_raise_value_error(raw):
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken", content=raw))


@pytest.mark.parametrize(
    "damage",
    ["late-stream", "broken-flate", "invalid-contents", "page-count", "nested-page-count", "truncated", "no-pages"],
)
def test_damage_after_readable_content_never_returns_partial_pages(damage):
    writer = pdf_writer("First page is readable", "Second page must not be lost")
    if damage == "late-stream":
        writer.pages[1][NameObject("/Contents")] = IndirectObject(99999, 0, writer)
    elif damage == "broken-flate":
        stream = EncodedStreamObject()
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        stream._data = b"not a zlib stream"
        writer.pages[1][NameObject("/Contents")] = writer._add_object(stream)
    elif damage == "invalid-contents":
        writer.pages[1][NameObject("/Contents")] = NumberObject(42)
    elif damage == "page-count":
        page_tree = writer.root_object["/Pages"]
        assert isinstance(page_tree, DictionaryObject)
        page_tree[NameObject("/Count")] = NumberObject(3)
    elif damage == "nested-page-count":
        page_tree = writer.root_object["/Pages"]
        assert isinstance(page_tree, DictionaryObject)
        kids = page_tree["/Kids"]
        assert isinstance(kids, ArrayObject)
        nested = DictionaryObject({
            NameObject("/Type"): NameObject("/Pages"),
            NameObject("/Kids"): ArrayObject(list(kids)),
            NameObject("/Count"): NumberObject(3),
            NameObject("/Parent"): page_tree.indirect_reference,
        })
        nested_reference = writer._add_object(nested)
        page_tree[NameObject("/Kids")] = ArrayObject([nested_reference])
        for kid in kids:
            child_page = kid.get_object()
            assert isinstance(child_page, DictionaryObject)
            child_page[NameObject("/Parent")] = nested_reference
    elif damage == "no-pages":
        writer = pdf_writer()
    raw = pdf_bytes(writer)
    if damage == "truncated":
        raw = raw[: raw.rindex(b"%%EOF")]
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken", content=raw))


def test_indirect_page_count_is_a_legal_pdf_object():
    writer = pdf_writer("First physical page", "Second physical page")
    page_tree = writer.root_object["/Pages"]
    assert isinstance(page_tree, DictionaryObject)
    page_tree[NameObject("/Count")] = writer._add_object(NumberObject(2))
    pages = parse_pdf_document(PDFDocumentInput(document_id="indirect-count", content=pdf_bytes(writer)))
    assert [page.text.strip() for page in pages] == ["First physical page", "Second physical page"]


@pytest.mark.parametrize("contents", ["null", "empty-stream", "empty-array", "graphics", "image"])
def test_legal_pages_without_extractable_text_remain_empty(contents):
    writer = pdf_writer("Before blank page", "", "After blank page")
    page = writer.pages[1]
    if contents == "null":
        page[NameObject("/Contents")] = NullObject()
    elif contents == "empty-array":
        page[NameObject("/Contents")] = ArrayObject()
    else:
        stream = DecodedStreamObject()
        data = b""
        if contents == "graphics":
            data = b"0 0 0 rg 20 20 40 40 re f"
        elif contents == "image":
            image = DecodedStreamObject()
            image.update({
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/BitsPerComponent"): NumberObject(8),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            })
            image.set_data(b"\xff\xff\xff")
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): writer._add_object(image)}),
            })
            data = b"q 1 0 0 1 20 20 cm /Im1 Do Q"
        stream.set_data(data)
        page[NameObject("/Contents")] = writer._add_object(stream)
    pages = parse_pdf_document(PDFDocumentInput(document_id="blank", content=pdf_bytes(writer)))
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [page.text.strip() for page in pages] == ["Before blank page", "", "After blank page"]


@pytest.mark.parametrize("ascii85", [False, True], ids=["flate", "ascii85-flate"])
@pytest.mark.parametrize("truncated", [False, True], ids=["complete", "truncated"])
def test_compressed_contents_require_complete_streams(ascii85, truncated):
    writer = pdf_writer("First page is readable", "Second page placeholder")
    data = zlib.compress(b"BT /F1 12 Tf 50 750 Td (Complete compressed content 123) Tj ET")
    if truncated:
        # Removing the checksum leaves recoverable text but no complete zlib stream.
        data = data[:-4]
    stream = EncodedStreamObject()
    if ascii85:
        data = base64.a85encode(data) + b"~>"
        stream[NameObject("/Filter")] = ArrayObject([NameObject("/ASCII85Decode"), NameObject("/FlateDecode")])
    else:
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    stream._data = data
    writer.pages[1][NameObject("/Contents")] = writer._add_object(stream)
    value = PDFDocumentInput(document_id="compressed", content=pdf_bytes(writer))
    if truncated:
        with pytest.raises(ValueError):
            parse_pdf_document(value)
    else:
        pages = parse_pdf_document(value)
        assert [page.text.strip() for page in pages] == ["First page is readable", "Complete compressed content 123"]


def test_contents_array_is_parsed_as_one_combined_instruction_stream():
    writer = pdf_writer("Placeholder")
    streams = ArrayObject()
    # A text-showing array starts in one stream and finishes in the next.
    for data in (b"BT /F1 12 Tf 50 750 Td [(Joined )", b"0 (contents 456)] TJ ET"):
        stream = DecodedStreamObject()
        stream.set_data(data)
        streams.append(writer._add_object(stream))
    writer.pages[0][NameObject("/Contents")] = streams
    pages = parse_pdf_document(PDFDocumentInput(document_id="joined", content=pdf_bytes(writer)))
    assert len(pages) == 1
    assert pages[0].text.strip() == "Joined contents 456"
