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
import hashlib

import pytest
from pypdf.generic import DictionaryObject, IndirectObject, NameObject, NumberObject

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


@pytest.mark.parametrize("damage", ["late-stream", "page-count", "truncated", "no-pages"])
def test_damage_after_readable_content_never_returns_partial_pages(damage):
    writer = pdf_writer("First page is readable", "Second page must not be lost")
    if damage == "late-stream":
        writer.pages[1][NameObject("/Contents")] = IndirectObject(99999, 0, writer)
    elif damage == "page-count":
        page_tree = writer.root_object["/Pages"]
        assert isinstance(page_tree, DictionaryObject)
        page_tree[NameObject("/Count")] = NumberObject(3)
    elif damage == "no-pages":
        writer = pdf_writer()
    raw = pdf_bytes(writer)
    if damage == "truncated":
        raw = raw[: raw.rindex(b"%%EOF")]
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken", content=raw))
