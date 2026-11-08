# 财报 PDF 全文解析与页级检索

实现位于 `powercontext.sources.pdf_pages`，提供 `PDFDocumentInput`、`PDFPageInput`、`parse_pdf_document`、`PDF_PAGE_SOURCE_DEFINITION` 和 `search_pdf_pages`。

安装 PDF 可选依赖：

```bash
uv sync --locked --extra pdf
```

`parse_pdf_document` 使用锁定的 pypdf 6.19.0，接收完整 PDF 字节，严格解析所有物理页。空白页也保留，页码从 1 开始。摘要来自整个输入文件，正文不做检索分词或布局重写。无需用户密码的权限加密文件可以读取；损坏文件、非空用户密码、缺失页面和空文档以 `ValueError` 原子失败。

`pdf-page` Definition 的版本为 `1`。Source 身份包含文档 ID、文件摘要和物理页码；超长 ID 使用独立命名空间中的 SHA-256 身份，保持名称不超过 256 字符。标准投影保留原始文档 ID、完整摘要、物理页码以及 `DOCUMENT_ID\nPAGE_TEXT`。修订文件产生不同身份，已有引用仍然指向原文件。

检索只使用传入的 scoped Memory 的公开 `list` 和 `search` API。它从真实活动条目中筛选 PDF 来源，以 FTS、BM25、通用财务词汇扩展、报表标题和文档身份进行离线排序，按 Source 去重后返回原条目的精确 citation。它不会读取文件、问题清单、答案、gold 或外部模型；每次查询重新读取当前 Memory，因此重启或新增修订不依赖进程缓存。调用方可用 `memory.get(GetMemoryEntryRequest(citation=...))` 回查原始条目。

标题识别覆盖整页，要求完整标题，支持短标题换行、单位和续表标记；带页码的目录条目、附带引用说明的报表清单和孤立的表格列标签不获得报表标题加分。页面上的导航文字不会屏蔽该页正文。问题中明确出现的年度与文档 ID 匹配时获得排序加分，同时保留其他年度报告中的比较数据候选。

检索需要读取作用域内全部活动条目以计算词频，时间和空间随正文规模线性增长。它面向题目中的千页级财报集合，不承诺大规模文档库的固定延迟。

验证命令：

```bash
# Linux x86_64、Python 3.12，使用组织方锁定的依赖和原始公开测试。
make -C competition install
make -C competition test-pdf

# 产品回归：构造真实 PDF，经过 Runtime 入库、重启、scoped Memory 检索及精确回查。
uv run --locked python -m pytest -q \
  tests/test_pdf_pages.py tests/e2e/test_pdf_retrieval.py \
  tests/e2e/test_pdf_retrieval_ranking.py \
  tests/test_sources.py tests/test_source_observations.py
```

GitHub Actions 的 `PDF retrieval acceptance` 工作流在 Linux、Python 3.12 上运行同一流程，并从官方固定 commit 单独取得公开验收文件。公开测试覆盖 503 页、5 个问题，产品回归另行覆盖解析、持久化、作用域隔离，以及目录抢占正文、历史年度叙述挤出目标报告、财务引用清单抢占实际报表等检索场景。

公开包不提供质量 gold，行为测试通过不能证明官方证据词项召回、整页 F1 或公开及隐藏两组 Recall@20 达标。全仓 `prek run -a` 还会检查其他题目的验收源码和依赖 Unix API 的模块；这些既有类型诊断不由 PDF 专项检查覆盖。
