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
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RetireMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFPageInput, search_pdf_pages
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


class _PagePipeline:
    async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
        )


def test_restarted_scoped_memory_retrieves_metric_components_and_exact_revisions(tmp_path: Path) -> None:
    async def check() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pages.db'}"))
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        pages = [
            PDFPageInput(document_id="ACME_2024_10K", document_sha256="a" * 64, page_number=number, text=text)
            for number, text in enumerate(
                [
                    f"Investor discussion {number}: EBITDA margin EBITDA margin targets and guidance."
                    for number in range(30)
                ]
                + [
                    "CONSOLIDATED STATEMENTS OF INCOME\nNet revenue 1000. Operating income 200. Net earnings 150.",
                    "CONSOLIDATED STATEMENTS OF CASH FLOWS\nDepreciation and amortization 50. Capital expenditure 30.",
                    "Legal proceedings\nA pending antitrust lawsuit is being contested. Possible settlement 15.",
                    "Business overview\nOur geographic segments include domestic and international operations.",
                    "Accounting policies\n" + "Detailed accounting policy disclosure 123. " * 500,
                ],
                1,
            )
        ]
        pages += [
            PDFPageInput(
                document_id="RIVAL_2024_10K",
                document_sha256="b" * 64,
                page_number=1,
                text="EBITDA margin. Income statement. Operating income. Depreciation amortization. " * 10,
            ),
            PDFPageInput(
                document_id="ACME_2024_10K",
                document_sha256="c" * 64,
                page_number=33,
                text="Legal proceedings\nA pending antitrust lawsuit was settled. Settlement 20.",
            ),
        ]
        observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in pages]
        stored = dict(zip((observation.name for observation in observations), pages, strict=True))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="PDF evidence", idempotency_key="reports")
            )
            other = await runtime.scopes.create(
                ScopeDraft(title="Private", summary="Other scope", idempotency_key="private")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            await runtime.ingestion.submit(
                SubmitSourceObservation(scope_id=scope.scope_id, observation=observations[0])
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            while (await memory.flush()).processed:
                pass
            assert len((await memory.list()).entries) == len(observations)

        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            assert await search_pdf_pages(runtime.memory.for_scope(other.scope_id), "Acme EBITDA margin") == ()
            assert await search_pdf_pages(memory, "", 20) == ()
            assert await search_pdf_pages(memory, "cash flow", 0) == ()
            assert await search_pdf_pages(memory, "cash flow", -1) == ()
            assert await search_pdf_pages(memory, "zzzyxx") == ()
            citations = await search_pdf_pages(memory, "What is Acme's FY2024 EBITDA margin?", 4)
            found = []
            for citation in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                page = stored[entry.sources[0].source_id]
                assert entry.text == f"{page.document_id}\n{page.text}"
                assert page.document_id == "ACME_2024_10K"
                found.append(page.page_number)
            assert len(set(found)) == len(found) <= 4
            assert {31, 32} <= set(found)
            assert len(await search_pdf_pages(memory, "Acme EBITDA", 1)) == 1
            dense = await search_pdf_pages(memory, "Acme accounting policies", 1)
            dense_entry = (await memory.get(GetMemoryEntryRequest(citation=dense[0]))).entry
            assert dense_entry.text == f"{pages[34].document_id}\n{pages[34].text}".strip()
            assert len(dense_entry.text.encode("utf-8")) > 8192

            legal = await search_pdf_pages(memory, "Has Acme been involved in legal battles?", 2)
            assert len(legal) == 2
            revisions = set()
            for citation in legal:
                record = await memory.get(GetMemoryEntryRequest(citation=citation))
                page = stored[record.entry.sources[0].source_id]
                assert page.page_number == 33
                revisions.add(page.document_sha256)
            assert revisions == {"a" * 64, "c" * 64}
            await memory.retire(RetireMemoryEntryRequest(citation=legal[0], reason="Withdrawn evidence"))
            current = await search_pdf_pages(memory, "Acme legal battles", 2)
            assert legal[0].entry_id not in {citation.entry_id for citation in current}
            assert (await memory.get(GetMemoryEntryRequest(citation=legal[0]))).state == "active"

    asyncio.run(check())
