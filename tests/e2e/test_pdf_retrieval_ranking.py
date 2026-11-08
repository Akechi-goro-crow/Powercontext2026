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

"""Retrieve actual report sections across indexes, page layouts, and years."""

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY
from tests.pdf_helpers import pdf_bytes, pdf_writer


class _PageEvidencePipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
        )


async def _store_reports(config, documents):
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    source_ids = {}
    async with open_builtin_runtime(config, candidate_pipeline=_PageEvidencePipeline()) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Reports", summary="Original synthetic reports", idempotency_key="reports")
        )
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for document_id, texts in documents.items():
            pages = parse_pdf_document(PDFDocumentInput(document_id=document_id, content=pdf_bytes(pdf_writer(*texts))))
            for page in pages:
                source = project_source_for_transport(registry, await registry.resolve(page))
                source_ids[(document_id, page.page_number)] = source.name
                await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=source))
        memory = runtime.memory.for_scope(scope.scope_id)
        for _ in range(100):
            if not (await memory.flush()).processed:
                return scope.scope_id, source_ids
    message = "Report ingestion did not finish"
    raise AssertionError(message)


def test_pdf_search_prefers_actual_section_to_table_of_contents(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sections.db'}"))
        legal_page = "\n".join([
            "Item 2. Properties",
            *(f"Office location {index} with leased warehouse capacity" for index in range(15)),
            "Item 3. Legal Proceedings",
            "We are defending a pending claim related to product liability. No other matters are material.",
        ])
        scope_id, sources = await _store_reports(
            config,
            {
                "ORION_2024_10K": (
                    "Table of Contents\nItem 3. Legal proceedings 12\nItem 4. Mine safety disclosures 13",
                    legal_page,
                    "Executive officers\nOur board appointed a new director in September.",
                )
            },
        )
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            citations = await search_pdf_pages(memory, "Does Orion have ongoing legal battles?", limit=1)
            assert len(citations) == 1
            record = await memory.get(GetMemoryEntryRequest(citation=citations[0]))
            assert record.entry.sources[0].source_id == sources[("ORION_2024_10K", 2)]
            assert "We are defending a pending claim" in record.entry.text

    asyncio.run(scenario())


def test_pdf_search_keeps_explicit_report_year_in_top_twenty(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'years.db'}"))
        scope_id, sources = await _store_reports(
            config,
            {
                "ORION_2023_10K": (
                    "Consolidated statements of income\n2023 2022\nNet sales 320 300\nOperating profit 40 35",
                ),
                "ORION_2024_10K": (
                    *(
                        f"Historical review {index}\nIn 2023 total revenue from continuing operations increased. "
                        "Net sales contributed to revenue growth. Revenue from continuing operations includes "
                        "revenue earned from customers."
                        for index in range(21)
                    ),
                    *(
                        f"General corporate information {index}\nBoard directors appointments oversight."
                        for index in range(5)
                    ),
                ),
            },
        )
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            citations = await search_pdf_pages(
                memory,
                "What is the FY2023 total revenue from continuing operations for Orion?",
                limit=20,
            )
            records = [await memory.get(GetMemoryEntryRequest(citation=citation)) for citation in citations]
            assert sources[("ORION_2023_10K", 1)] in {
                source.source_id for record in records for source in record.entry.sources
            }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("income_heading", "cash_heading"),
    [
        ("Consolidated Statement of Income", "Consolidated Statement of Cash Flows"),
        (
            "Unaudited Condensed Consolidated Statements of Operations and\nComprehensive Loss",
            "Consolidated Statements of\nCash Flows (in millions)",
        ),
        (
            "Consolidated Statements of Income (Loss) (Unaudited).",
            "Consolidated Statements of Cash Flows (continued).",
        ),
    ],
)
def test_pdf_search_prefers_financial_data_to_statement_reference_list(tmp_path, income_heading, cash_heading):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'statements.db'}"))
        scope_id, sources = await _store_reports(
            config,
            {
                "ORION_2024_10K": (
                    f"{income_heading}\n2024 2023\nNet revenue 320 300\nOperating income 40 35",
                    f"{cash_heading}\n2024 2023\nDepreciation and amortization 15 12",
                    "Item 15. Exhibits and Financial Statement Schedules\n"
                    "The following statements are included by reference to the pages indicated in the index:\n"
                    "Consolidated Statement of Income - Fiscal years ended December 31, 2024 and 2023\n"
                    "Consolidated Statement of Cash Flows - Fiscal years ended December 31, 2024 and 2023",
                    "Gains on hedging instruments\nLosses recognized in\nIncome Statement\n2024 2023\n12 10",
                )
            },
        )
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            citations = await search_pdf_pages(
                memory,
                "What is Orion's FY2024 EBITDA margin using operating income and depreciation and amortization?",
                limit=2,
            )
            records = [await memory.get(GetMemoryEntryRequest(citation=citation)) for citation in citations]
            assert {source.source_id for record in records for source in record.entry.sources} == {
                sources[("ORION_2024_10K", 1)],
                sources[("ORION_2024_10K", 2)],
            }

    asyncio.run(scenario())
