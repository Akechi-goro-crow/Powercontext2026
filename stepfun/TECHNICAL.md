# 技术说明

## 端侧推理

模型来源是 Hugging Face 的 `Xenova/all-MiniLM-L6-v2`。Transformers.js 在浏览器内运行特征提取，`dtype: "q8"` 使用量化配置。支持 WebGPU 时将 `device` 设为 `webgpu`，否则设置为 `wasm`，页面徽标会显示当前路径。文档和查询都经过同一个本地 embedding 函数。

## seekdb 数据层

Node 服务使用 `seekdb` SDK 的 `SeekdbClient` 连接 `stepfun_kb` 数据库和 `browser_documents` collection。collection 保存文档 ID、原文、标题元数据和 384 维向量。导入使用 `upsert`，完成后调用 seekdb-js 的 `refresh_index()`，确保新增内容可检索。查询通过 `hybridSearch()` 一次请求完成全文条件和 KNN 向量排序，客户端不分别拉取两路结果再拼接。

## 性能记录模板

在同一台设备、同一浏览器、同一批文档和同一组查询上，分别记录以下配置的中位数。

| 配置 | 模型文件体积 | 压缩比 | 文档向量化 | 查询向量化 | seekdb 检索 | 端到端 | Recall@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 非量化基线 | 待实测 | 1.00 | 待实测 | 待实测 | 待实测 | 待实测 | 待实测 |
| q8 + WebGPU | 待实测 | 待实测 | 待实测 | 待实测 | 待实测 | 待实测 | 待实测 |
| q8 + WASM | 待实测 | 待实测 | 待实测 | 待实测 | 待实测 | 待实测 | 待实测 |

## 限制

模型首次加载需要网络下载模型文件。推理本身在浏览器设备完成。Node 服务只接收已经由浏览器生成的向量并负责 seekdb 存取与混合检索。
