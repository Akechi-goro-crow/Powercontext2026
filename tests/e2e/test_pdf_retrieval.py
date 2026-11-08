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

"""Exercise PDF retrieval through persistent Runtime and scoped Memory APIs."""

import asyncio

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
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


class _EvidencePipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind=kind,
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
            for kind in ("source-evidence", "duplicate-evidence")
        )


async def _observations(document_id, *texts):
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    pages = parse_pdf_document(PDFDocumentInput(document_id=document_id, content=pdf_bytes(pdf_writer(*texts))))
    observations = []
    for page in pages:
        observations.append(project_source_for_transport(registry, await registry.resolve(page)))
    return tuple(observations)


async def _drain(memory):
    for _ in range(100):
        if not (await memory.flush()).processed:
            return
    detail = "PDF source processing did not drain"
    raise AssertionError(detail)


def test_pdf_search_is_scoped_distinct_and_revision_exact_after_restart(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}"))
        observations = await _observations(
            "ORION_2024_10K",
            "Table of Contents\nConsolidated statements of income\nNet revenue 320\nOperating income 70\nNet income 40",
            "",
            "Table of Contents\nConsolidated statements of cash flows\nDepreciation 12\nAmortization 3\nPurchases of property plant equipment 20",
            "Legal proceedings\nLitigation provision 13\nManagement reviews contingencies",
            "Geographic information\nOur regions include Europe and Asia\nInternational operations across countries",
            *(
                f"Market risk discussion {index}\nUnadjusted EBITDA capex income revenue profit margin cash flow statements"
                for index in range(25)
            ),
        )
        distractors = await _observations(
            "LYRA_2024_10K",
            *(
                f"Unadjusted EBITDA income revenue capex cash flow statement profit margin {index}"
                for index in range(25)
            ),
        )
        outside = await _observations("ORION_2024_10K", "Operating income 999999 outside the requested scope")
        all_observations = observations + distractors
        stored = {source.name: source for source in all_observations}
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Local PDFs", idempotency_key="reports")
            )
            other = await runtime.scopes.create(
                ScopeDraft(title="Private", summary="Other scope", idempotency_key="private")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for source in (*all_observations, observations[0]):
                await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=source))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=other.scope_id, observation=outside[0]))
        async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            await _drain(memory)
            await _drain(runtime.memory.for_scope(other.scope_id))
            # The pipeline created two kinds of entries per page. A separate note has no PDF source.
            await memory.remember(
                RememberMemoryRequest(
                    entries=(
                        MemoryEntryInput(
                            kind="unrelated-note", text="Orion unadjusted EBITDA capex operating income " * 10
                        ),
                    )
                )
            )
        async with open_builtin_contexts(config) as contexts:
            journal = await (await contexts.get(scope.scope_id)).sources.journal.entries()
            assert len(journal) == len(all_observations)
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            assert await search_pdf_pages(memory, "   ", limit=20) == ()
            assert await search_pdf_pages(memory, "Orion revenue", limit=0) == ()
            assert await search_pdf_pages(memory, "Orion revenue", limit=-1) == ()
            assert await search_pdf_pages(memory, "zzzzunknownterm", limit=20) == ()
            queries = (
                ("What is Orion's FY2024 unadjusted EBITDA less capex? Use income and cash flow statements.", {1, 3}),
                ("Which geographies does Orion primarily operate in?", {5}),
                ("Does Orion have any ongoing legal battles?", {4}),
            )
            for query, required in queries:
                citations = await search_pdf_pages(memory, query, limit=20)
                assert 0 < len(citations) <= 20
                locations = set()
                leading_pages = set()
                for index, citation in enumerate(citations):
                    record = await memory.get(GetMemoryEntryRequest(citation=citation))
                    source_id = record.entry.sources[0].source_id
                    assert source_id in stored
                    projection = stored[source_id].projection(TEXT_EVIDENCE_PROJECTION_KEY)
                    assert record.entry.text == projection["content"].strip()
                    metadata = projection["metadata"]
                    locations.add((metadata["document_id"], metadata["document_sha256"], metadata["page_number"]))
                    if index < 5 and metadata["document_id"] == "ORION_2024_10K":
                        leading_pages.add(metadata["page_number"])
                assert len(locations) == len(citations)
                assert required <= leading_pages
            old_citation = (await search_pdf_pages(memory, "Orion net revenue", limit=1))[0]
            assert "320" in (await memory.get(GetMemoryEntryRequest(citation=old_citation))).entry.text
        revised = await _observations(
            "ORION_2024_10K", "Consolidated statements of income\nNet revenue 330\nOperating income 80"
        )
        async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=revised[0]))
            await _drain(runtime.memory.for_scope(scope.scope_id))
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            assert "320" in (await memory.get(GetMemoryEntryRequest(citation=old_citation))).entry.text
            citations = await search_pdf_pages(memory, "Orion net revenue", limit=2)
            entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
            assert any("330" in entry.text for entry in entries)
            assert len({entry.sources[0].source_id for entry in entries}) == 2

    asyncio.run(scenario())
