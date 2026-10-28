# seekdb 单 SQL 混合候选检索

seekdb 配置了 embedding model 时，Memory 的 hybrid 候选检索使用
`SeekDBMemoryIndex`。FTS-only、vector-only 和 OceanBase 路径保留原有实现。

## 查询与引用

一条参数化 SQL 通过 `UNION ALL` 返回两路候选：

- 全文分支从 `pc_memory_entry_heads` 使用 `MATCH ... AGAINST` 检索，按相关性及身份排序。
- 向量分支从 `pc_memory_vector_entries` 使用 `l2_distance ... APPROXIMATE` 检索。
- 两个分支分别在数据库内应用 scope、Memory artifact、标签过滤和 candidate limit。
- 外层排序恢复各通道顺序，并按身份处理相同分数或距离的候选。

带标签的向量分支先按 scope、artifact 和标签匹配方式汇总合格条目身份，
再通过嵌套循环连接驱动原生向量索引读取，最后排序并限制候选数。
这避免了相关标签子查询在 ANN 自身的 Top-K 截断后执行而漏掉合格条目。
标签身份分组也避免了多个匹配标签导致同一条目重复进入候选。

两路候选保留独立的通道成员资格，沿用共享的 admission 与 RRF 融合流程。
因此同一条目同时命中两路时可以保留 `matched_by=("fts", "vector")`，
融合结果按身份去重。全文和距离均由数据库计算。

条目正文通过 scope、Memory artifact 和 entry version 联接到权威版本表。
搜索仍由后端在候选查询前后校验请求 head；embedding 完整性校验也沿用原实现。
完整性校验、head 校验和后续精确正文读取属于产品原有流程，
单 statement 限制仅适用于取得两路候选的 SQL。

投影中未变化的条目可以保留旧 `head_revision`，查询不要求它等于当前 Memory revision。
返回引用使用请求中已校验的当前 head，而 entry version 来自当前活跃投影。
索引维护继续复用原有事务内的 upsert/delete/replace 操作。

## 验证

在 Linux x86_64、Python 3.12 和 `competition/requirements.txt` 固定依赖环境运行：

```bash
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
PYTHONPATH=src:competition python -m pytest -q -p no:cacheprovider \
  -c competition/pytest.ini --confcutdir=competition \
  competition/challenges/retrieval-02-hybrid-search/acceptance

PYTHONPATH=src python -m pytest -q -p no:cacheprovider -p pytest_asyncio.plugin \
  tests/builtin/persistence/test_seekdb_memory_index.py

PYTHONPATH=src python -m pytest -q -p no:cacheprovider -p pytest_asyncio.plugin \
  tests/e2e/test_native_memory_seekdb.py
```

公开验收包含原生 SQL 的双索引 EXPLAIN、scope 隔离、标签过滤、旧投影、
正文修订、停用恢复、当前引用、重复查询及单通道行为。
单元回归检查一次 hybrid 候选读取的 statement 预算和双通道引用处理。
另一个原生回归使用 384 维单位向量，将标签仅赋给未过滤 Top-1 之外的条目，
检查 all/any 标签过滤、候选上限、增量 head、与双查询路径的一致性和双索引计划。

本地验证使用 Python 3.12 和 `pylibseekdb==1.3.0.post4`：公开原生验收
`1 passed`；新增单元测试及 seekdb profile、Memory、SQLite vector 相关回归
共 `17 passed`。仓库锁定版本的 Ruff、格式和 ty 检查通过。
384 维标签截断原生回归 `1 passed`；公开 SciFact 正文、确定性 384 维单位向量、
三个 scope、标签筛选、增量 head 和候选上限 1/10 的 24 次同库候选对照全部一致。

公开样例不包含真实验收集的 qrels，无法据此确认 60 次查询的逐查询 Recall/MRR、
平均召回或完整搜索延迟门槛；这些指标需要组织方固定语料、向量及评分环境测量。
