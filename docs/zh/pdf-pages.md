# 财报 PDF 全文解析与证据页检索

`powercontext.sources.pdf_pages` 将完整 PDF 转成按物理页划分的 captured Source，并从指定作用域的 Memory 返回精确页级引用。解析和检索均可离线运行，不需要模型服务。

## 安装

```bash
uv sync --extra pdf --extra builtin
```

PDF extra 固定使用 `pypdf==6.19.0`。产品源码的公开入口为：

```python
from powercontext.sources.pdf_pages import (
    PDFDocumentInput,
    PDFPageInput,
    PDF_PAGE_SOURCE_DEFINITION,
    parse_pdf_document,
    search_pdf_pages,
)
```

## 解析和来源

`parse_pdf_document(PDFDocumentInput(document_id=..., content=...))` 接收完整原 PDF 字节，返回全部页面组成的 tuple。物理页从 1 开始，包含空白页，不使用财报印刷页码；每页携带原文件完整字节的 SHA-256。

文档 ID 必须包含非空白字符，其他字符原样保留。无需输入密码即可读取的权限加密 PDF 可以解析。空文件、非 PDF、损坏页、不可解码内容和需要用户密码的文件以 `ValueError` 失败，不返回部分页。本接口不进行 OCR。

正文提取遵循 PDF 标记内容的 `/ActualText` 替代文本，支持嵌套标记、命名属性和 Form XObject 的独立资源作用域，并将拉丁连字展开为普通字母。`TJ` 指令末尾的定位调整在被后续定位指令覆盖时不会产生多余空格；仍影响连续文本的词间距会保留。这些处理只作用于本次读取的内存对象。

Source Definition 名称为 `pdf-page`、版本为 `1`。身份由文档 ID、完整摘要和物理页码共同决定，重复解析身份稳定，修订后身份独立。`read` 返回原 `PDFPageInput`，标准文本投影内容为 `DOCUMENT_ID\nPAGE_TEXT`，metadata 仅包含 `document_id`、`document_sha256` 和 `page_number`。

## 完整写入和重启检索

下例使用保留每页全文的 pipeline。Memory 的通用文本规范化会去掉首尾空白；整页正文不会因普通记忆的 8 KiB 限制而被截断。

```python
import asyncio
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


class PageEvidencePipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
        )


async def main():
    config = BuiltinConfig(database=SQLiteConfig(url="sqlite+aiosqlite:///reports.db"))
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    pages = parse_pdf_document(
        PDFDocumentInput(document_id="ACME_2025_10K", content=Path("report.pdf").read_bytes())
    )
    async with open_builtin_runtime(config, candidate_pipeline=PageEvidencePipeline()) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(title="财报", summary="页级证据", idempotency_key="financial-reports")
        )
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for page in pages:
            source = await registry.resolve(page)
            await runtime.ingestion.submit(
                SubmitSourceObservation(
                    scope_id=scope.scope_id,
                    observation=project_source_for_transport(registry, source),
                )
            )
        memory = runtime.memory.for_scope(scope.scope_id)
        while (await memory.flush()).processed:
            pass

    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        citations = await search_pdf_pages(memory, "What is Acme's FY2025 EBITDA margin?", limit=20)
        for citation in citations:
            record = await memory.get(GetMemoryEntryRequest(citation=citation))
            print(record.entry.text, record.entry.sources)


asyncio.run(main())
```

检索只访问传入 Memory 的公开 `list`、`search` 接口，从活跃的单页来源条目返回已有 citation。它在当前作用域内计算 BM25，结合财务术语、报表标题和指标组成部分排序，并按 Source 去重；不会从文件路径、答案、标注或外部服务建立旁路索引。每次读取当前快照，因此新增、撤回和重启不会复用过期缓存。

文档 ID 若包含公司名与年份，会用完整公司名筛选候选文档，兼容名称中的空格、下划线和常见公司后缀。不同公司的共享单词不会单独触发筛选；没有识别出完整名称时保留当前作用域的所有候选文档。

`limit <= 0` 或空白问题返回空 tuple，正整数 limit 限制返回的不同页面数量。不同 PDF 修订的同一物理页拥有不同 Source 身份，历史 citation 继续指向原修订。检索计算量与作用域内页面总文本量相关，适合财报集合；超大语料应使用有界候选索引。

## 验证

```bash
uv run pytest -q tests/test_pdf_pages.py tests/e2e/test_pdf_page_memory.py
PYTHONPATH=src:competition uv run --no-sync python -m pytest -q -s \
  -c competition/pytest.ini --confcutdir=competition \
  competition/challenges/source-03-pdf-retrieval/acceptance
```

公开自测验证 503 页完整解析、真实持久化、重启和 5 个问题的引用可回查性。官方不分发质量 gold；公开自测通过不能证明公开及留出集合的词项质量门槛、Recall@20 或最终 50 分结果。

本地回归还覆盖替代文本、Unicode、重复调用的 Form、逐字定位与连续词间距，以及重启后多词公司名的检索隔离。真实财报的逐页文本可以用独立 PDF 提取器交叉检查词项数量和全文 F1；该对照不代替官方 gold。
