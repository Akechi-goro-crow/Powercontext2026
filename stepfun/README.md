# 端侧知识库

这是阶跃星辰赛题的可运行 Web 应用。浏览器使用 Transformers.js 在本地运行 `Xenova/all-MiniLM-L6-v2`，以 q8 量化配置执行 embedding。应用优先选择 WebGPU，浏览器不支持时自动使用 WASM。Node 服务通过 seekdb-js 把浏览器生成的向量、原文和元数据写入 seekdb，并调用 seekdb 的 `hybridSearch()` 完成向量与全文混合检索。

## 启动

需要 Node.js 22、Chrome 或 Edge 最新版，以及运行中的 seekdb Server。

```powershell
cd C:\path\to\Powercontext2026\stepfun
npm install --omit=optional --legacy-peer-deps

$env:SEEKDB_HOST = "127.0.0.1"
$env:SEEKDB_PORT = "2881"
$env:SEEKDB_USER = "root"
$env:SEEKDB_PASSWORD = Read-Host "seekdb root password"
$env:SEEKDB_DATABASE = "stepfun_kb"

npm start
```

打开 `http://127.0.0.1:8791`。首次使用会在浏览器下载模型文件并缓存，推理计算发生在当前设备。

## 验证样例

1. 在“导入文本”中输入标题 `Transformer 笔记`。
2. 粘贴 `自注意力根据 token 之间的关系动态聚合信息，位置编码用于补充序列顺序。`
3. 点击“本地向量化并写入 seekdb”。页面应显示当前推理后端和本地向量化耗时。
4. 输入 `位置编码解决什么问题？`，点击“生成查询向量并检索”。
5. 页面显示查询向量化耗时、seekdb 混合检索耗时和匹配原文。
6. 关闭浏览器后重新打开，确认模型缓存和 seekdb 中的资料仍可查询。

## 量化和性能记录

模型配置使用 Transformers.js 的 `dtype: "q8"`。页面记录首次模型加载、文档向量化、查询向量化、seekdb 检索和端到端耗时。测试报告应在同一设备和语料上记录 q8 与非量化基线的实际模型文件体积、压缩比和 Recall@K。`TECHNICAL.md` 提供记录模板和混合检索说明。
