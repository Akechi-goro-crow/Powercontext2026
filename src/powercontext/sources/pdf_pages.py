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

"""Complete, offline PDF ingestion with immutable physical-page provenance."""

from __future__ import annotations

import hashlib
import io
import json
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from powercontext.artifacts import MemoryCitation
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _document_id(value: str) -> str:
    if not value.strip():
        raise ValueError("document_id must not be blank")  # noqa: TRY003
    return value


class PDFDocumentInput(BaseModel):
    """The complete original PDF bytes and a caller-assigned document identity."""

    model_config = ConfigDict(frozen=True)

    document_id: Annotated[str, AfterValidator(_document_id)]
    content: bytes


class PDFPageInput(BaseModel):
    """Extracted text from one physical page of an exact PDF revision."""

    model_config = ConfigDict(frozen=True)

    document_id: Annotated[str, AfterValidator(_document_id)]
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(ge=1, strict=True)
    text: str


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page, or raise ``ValueError`` without partial output.

    Requires the ``pdf`` extra. Empty-password encryption is readable; PDFs
    requiring a supplied password are not. Whitespace and reading order follow
    pypdf's text extraction, including text inside Form XObjects.
    """

    from powercontext.sources._pdf_extract import extract_pages

    _document_id(value.document_id)
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        texts = extract_pages(io.BytesIO(value.content))
    except Exception as error:
        raise ValueError("PDF cannot be completely parsed") from error  # noqa: TRY003
    return tuple(
        PDFPageInput(document_id=value.document_id, document_sha256=digest, page_number=number, text=text)
        for number, text in enumerate(texts, 1)
    )


class _PDFPageSource(Source):
    page: PDFPageInput


class _PDFPageDefinition:
    name = "pdf-page"
    version = "1"
    input_class: type[PDFPageInput] = PDFPageInput
    source_class: type[_PDFPageSource] = _PDFPageSource

    def __init__(self) -> None:
        self.projections: tuple[SourceProjection[_PDFPageSource], ...] = (_PDFPageTextProjection(),)

    async def resolve(self, value: PDFPageInput, /) -> _PDFPageSource:
        identity = json.dumps(
            [value.document_id, value.document_sha256, value.page_number],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        # Hash the full, unambiguous tuple: no truncation or delimiter collisions,
        # even for very long document ids. The complete fields stay in the Source.
        return _PDFPageSource(
            name=f"pdf-page:{hashlib.sha256(identity).hexdigest()}",
            materialization=SourceMaterialization.CAPTURED,
            page=value,
        )

    async def read(self, source: _PDFPageSource, /) -> PDFPageInput:
        return source.page


class _PDFPageTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[_PDFPageSource] = _PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: _PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata={
                "document_id": page.document_id,
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, _PDFPageSource, PDFPageInput] = _PDFPageDefinition()


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Search the supplied scoped Memory offline and return exact, distinct pages.

    Uses only public Memory methods and stored entries; no original PDFs,
    external corpus, models, or filesystem caches are needed after ingestion.
    """

    from powercontext.sources._pdf_search import search_pages

    return await search_pages(memory, question, limit)


__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
