# 财报 PDF 全文解析与页级检索

实现位于 `powercontext.sources.pdf_pages`，提供 `PDFDocumentInput`、`PDFPageInput`、`parse_pdf_document`、`PDF_PAGE_SOURCE_DEFINITION` 和 `search_pdf_pages`。

安装 PDF 可选依赖：

```bash
uv sync --locked --extra pdf
```

`parse_pdf_document` 使用锁定的 pypdf 6.19.0，接收完整 PDF 字节，严格解析所有物理页。空白页也保留，页码从 1 开始。摘要来自整个输入文件，正文不做检索分词。无需用户密码的权限加密文件可以读取；损坏文件、非空用户密码、缺失页面和空文档以 `ValueError` 原子失败。

正文默认使用 plain 提取。对连续逐字母拆分的异常行，额外使用 layout 的几何布局恢复单词间距：只有两者去掉空白后的字符序列唯一且完全相同时才替换该行空白，其他行保持原样。这避免逐字定位 PDF 中的末尾位移变成单词内部空格，也保留 plain 能提取的旋转文字和独立表格单元。

解析前逐层核对页树的实际页数，支持间接对象形式的计数。页面内容必须为合法内容流；Flate 压缩流须完整解码，内容数组拼接后校验指令语法。实际调用的 Form XObject 递归执行相同校验，损坏或截断的内部内容不能静默变为空白正文。合法空内容、绘图页、图片页及重复调用的 Form 均保留；不对图片执行 OCR。

`pdf-page` Definition 的版本为 `1`。Source 身份包含文档 ID、文件摘要和物理页码；超长 ID 使用独立命名空间中的 SHA-256 身份，保持名称不超过 256 字符。标准投影保留原始文档 ID、完整摘要、物理页码以及 `DOCUMENT_ID\nPAGE_TEXT`。修订文件产生不同身份，已有引用仍然指向原文件。

检索只使用传入的 scoped Memory 的公开 `list` 和 `search` API。它从真实活动条目中筛选 PDF 来源，以 FTS、BM25、通用财务词汇扩展、报表标题和文档身份进行离线排序，按 Source 去重后返回原条目的精确 citation。财务指标拆为独立的取数需求，例如 ROA 需要净利润和总资产，资本支出占销售额需要资本支出和收入；每项分别召回并保留最佳候选，再用倒数排名融合补充其余结果。未知指标仍使用一般词项检索。它不会读取文件、问题清单、答案、gold 或外部模型；每次查询重新读取当前 Memory，因此重启或新增修订不依赖进程缓存。调用方可用 `memory.get(GetMemoryEntryRequest(citation=...))` 回查原始条目。

标题识别覆盖整页，要求完整标题，支持短标题换行、单位和续表标记，以及 `Statements of Cash Flows` 和 `Cash Flows Statements` 等写法。带页码的目录条目、附带引用说明的报表清单和孤立的表格列标签不获得报表标题加分。页面上的导航文字不会屏蔽该页正文。公司名和年度只提供有限加分，不强制把其他文档排在后面；只匹配页眉公司名或年份的页面不获得身份加分，其他年度报告中的比较数据仍可召回。

检索需要读取作用域内全部活动条目以计算词频，时间和空间随正文规模线性增长。它面向题目中的千页级财报集合，不承诺大规模文档库的固定延迟。

验证命令：

```bash
# Linux x86_64、Python 3.12，使用组织方锁定的依赖和原始公开测试。
make -C competition install
make -C competition test-pdf

# 产品回归：构造真实 PDF，经过 Runtime 入库、重启、scoped Memory 检索及精确回查。
uv run --locked python -m pytest -q \
  tests/test_pdf_pages.py tests/test_pdf_form_content.py tests/e2e/test_pdf_retrieval.py \
  tests/test_pdf_text_spacing.py tests/e2e/test_pdf_retrieval_ranking.py \
  tests/e2e/test_pdf_financial_retrieval.py tests/e2e/test_pdf_document_routing.py \
  tests/test_sources.py tests/test_source_observations.py
```

GitHub Actions 的 `PDF retrieval acceptance` 工作流在 Linux、Python 3.12 上运行同一流程，并从官方固定 commit 单独取得公开验收文件。公开测试覆盖 503 页、5 个问题，产品回归另行覆盖解析、持久化、作用域隔离，以及目录抢占正文、历史年度叙述挤出目标报告、财务引用清单抢占实际报表等检索场景。

另用未标注的公司原始年报进行诊断：公开 PDF、Apple 2024 和 Walmart 2024 共 721 页中，空白恢复仅改变 Walmart 的一页，词数从 771 修复为 699，与两个独立解析器相符，其他 720 页逐字不变；该页非空白字符完全保留。Microsoft 2024 的 91 页真实入库并重启后，16 个自拟财务问题所需的报表组件均能进入前 20。上述自拟问题、检查结果和第三方解析器不进入候选运行环境，也不能代替官方质量或召回评分。

公开包不提供质量 gold，行为测试通过不能证明官方证据词项召回、整页 F1 或公开及隐藏两组 Recall@20 达标。全仓 `prek run -a` 还会检查其他题目的验收源码和依赖 Unix API 的模块；这些既有类型诊断不由 PDF 专项检查覆盖。
