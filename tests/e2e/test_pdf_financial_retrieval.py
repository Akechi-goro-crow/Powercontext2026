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

"""Financial metrics need all component pages, even without their literal names."""

import asyncio

import pytest

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, GetMemoryEntryRequest, open_builtin_runtime
from powercontext.sources.pdf_pages import search_pdf_pages
from tests.e2e.test_pdf_retrieval_ranking import _store_reports

_REPORT_ID = "ORION_2024_10K"
_STATEMENTS = (
    "Income Statements\nYear ended December 31\n2024 2023\n"
    "Net sales 800 700\nCost of goods sold 500 450\nNet earnings 180 160",
    "Balance Sheets\nDecember 31\n2024 2023\n"
    "Trade receivables 100 90\nInventories 70 60\nAccounts payable 80 75\n"
    "Current assets 230 200\nCurrent liabilities 120 110\n"
    "Total assets 1200 1000\nStockholders equity 600 450",
    "Cash Flows Statements\nYear ended December 31\n2024 2023\n"
    "Net cash from operations 220 200\nAdditions to property and equipment 60 50",
)


@pytest.mark.parametrize(
    ("question", "narrative", "required_pages"),
    [
        (
            "What is Orion's FY2024 return on assets?",
            "Asset return policy\nThe return of leased assets is subject to specified terms in 2024.",
            (1, 2),
        ),
        (
            "What is Orion's FY2024 days sales outstanding?",
            "Outstanding obligations\nThe sales program requires 30 days notice in 2024. "
            "All sales obligations remain outstanding.",
            (1, 2),
        ),
        (
            "What is Orion's FY2024 cash conversion cycle?",
            "Cash conversion policy\nCash conversion can occur at any time during the subscription cycle in 2024.",
            (1, 2),
        ),
        (
            "What is Orion's FY2024 capital expenditure as a percentage of sales?",
            "Capital expenditure policy\nThe capital expenditure program considers sales forecasts "
            "and the percentage of sales allocated to planned expansion in 2024.",
            (1, 3),
        ),
    ],
    ids=["return-on-assets", "days-sales-outstanding", "cash-conversion-cycle", "capital-expenditure-to-sales"],
)
def test_financial_metric_retrieves_all_required_statement_pages(tmp_path, question, narrative, required_pages):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'financial.db'}"))
        scope_id, sources = await _store_reports(
            config,
            {
                _REPORT_ID: (
                    *_STATEMENTS,
                    *(f"{narrative}\nBusiness unit {index}" for index in range(24)),
                ),
            },
        )
        # Reopening exercises persisted content and exact citations, with no source-PDF access at query time.
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            citations = await search_pdf_pages(memory, question, limit=20)
            assert 0 < len(citations) <= 20
            records = [await memory.get(GetMemoryEntryRequest(citation=citation)) for citation in citations]
            retrieved = {record.entry.sources[0].source_id for record in records}
            assert len(retrieved) == len(citations)
            assert {sources[(_REPORT_ID, page)] for page in required_pages} <= retrieved
            for record in records:
                assert record.entry.text.startswith(_REPORT_ID + "\n")

    asyncio.run(scenario())
