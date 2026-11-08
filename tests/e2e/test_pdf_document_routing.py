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

"""Report names and years must not override relevant financial evidence."""

import asyncio

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, GetMemoryEntryRequest, open_builtin_runtime
from powercontext.sources.pdf_pages import search_pdf_pages
from tests.e2e.test_pdf_retrieval_ranking import _store_reports


def test_pdf_search_keeps_relevant_page_when_another_company_name_matches_query_word(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'companies.db'}"))
        scope_id, sources = await _store_reports(
            config,
            {
                "ORION_HOLDINGS_2022_10K": (
                    "Consolidated Statements of Income\n2022 2021\nOrion global revenue 500 400\nNet earnings 90 60",
                ),
                "GLOBAL_2022_10K": tuple(
                    f"Revenue policy {index}\n2022 revenue recognition policies remain unchanged."
                    for index in range(21)
                ),
            },
        )
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            citations = await search_pdf_pages(memory, "What is Orion's global revenue for FY2022?", limit=20)
            records = [await memory.get(GetMemoryEntryRequest(citation=citation)) for citation in citations]

            assert sources[("ORION_HOLDINGS_2022_10K", 1)] in {
                source.source_id for record in records for source in record.entry.sources
            }

    asyncio.run(scenario())


def test_pdf_search_keeps_comparative_year_evidence_above_unrelated_report_year_matches(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'comparatives.db'}"))
        scope_id, sources = await _store_reports(
            config,
            {
                "ORION_2024_10K": (
                    "Consolidated Balance Sheets\n2024 2023\nCash and cash equivalents 300 200\n"
                    "Accounts receivable 50 40",
                ),
                "ORION_2023_10K": tuple(
                    f"Shareholder information {index}\n2023 executive appointment and shareholder annual meeting details."
                    for index in range(21)
                ),
            },
        )
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            citations = await search_pdf_pages(memory, "What is Orion's cash balance in FY2023?", limit=20)
            records = [await memory.get(GetMemoryEntryRequest(citation=citation)) for citation in citations]

            assert sources[("ORION_2024_10K", 1)] in {
                source.source_id for record in records for source in record.entry.sources
            }

    asyncio.run(scenario())


def test_pdf_search_report_headers_do_not_crowd_out_relevant_long_notes(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'headers.db'}"))
        note = "Retirement benefits\n2024 2023\nPension expense 300 200\n" + " ".join(
            f"Plan {index} provides employees defined benefits after retirement based on eligible service "
            "and final compensation."
            for index in range(12)
        )
        scope_id, sources = await _store_reports(
            config,
            {
                "ORION_2024_10K": (note,),
                "ORION_2023_10K": tuple(
                    f"Orion 2023 annual report\nShareholder information {index}\n"
                    "Executive appointment and annual meeting details."
                    for index in range(21)
                ),
            },
        )
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            citations = await search_pdf_pages(memory, "What was Orion's pension expense in FY2023?", limit=20)
            records = [await memory.get(GetMemoryEntryRequest(citation=citation)) for citation in citations]

            assert sources[("ORION_2024_10K", 1)] in {
                source.source_id for record in records for source in record.entry.sources
            }

    asyncio.run(scenario())


def test_pdf_search_preserves_legal_question_when_revenue_is_only_its_subject(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'legal.db'}"))
        scope_id, sources = await _store_reports(
            config,
            {
                "ORION_2024_10K": (
                    "Legal Proceedings\nOrion is defending pending lawsuits about revenue recognition. "
                    "A contract claim remains unresolved.",
                    "Consolidated Statements of Income\n2024 2023\nNet revenue 500 400\nOperating profit 100 80",
                ),
            },
        )
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            citations = await search_pdf_pages(
                memory,
                "Does Orion have pending lawsuits about revenue recognition?",
                limit=1,
            )

            assert len(citations) == 1
            record = await memory.get(GetMemoryEntryRequest(citation=citations[0]))
            assert record.entry.sources[0].source_id == sources[("ORION_2024_10K", 1)]

    asyncio.run(scenario())
