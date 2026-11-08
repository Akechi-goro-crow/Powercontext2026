# 验证样例与提交证据

## 功能验证

启动 seekdb、PowerContext 和 FastAPI 服务后，打开 `http://127.0.0.1:8790`。

使用 README 中的 Transformer 样例，记录以下截图：

1. 资料导入成功，页面显示”资料已加入资料库”。
2. 自然语言检索返回刚才导入的笔记内容，证明 seekdb 参与了核心流程。
3. 保存进度后点击”读取历史”，返回 PowerContext 记忆，证明状态被复用。
4. 在检索区域输入标签（例如”深度学习”）后点击检索，结果仅返回匹配该标签的资料，证明标签筛选生效。不填标签时行为与原来一致。

## 技术核对

- seekdb 核对点是 `/api/ingest` 写入和 `/api/search` 返回的资料正文与标题。
- `/api/search` 接受可选的 `tags` 参数（字符串数组）。传入 tags 时，seekdb 使用 `where` 子句按标签元数据过滤；未传入时保持原有全文语义检索行为。
- PowerContext 核对点是 `/api/memory/save` 的 `changed` 或 `saved` 结果，以及随后 `/api/memory/load` 返回的记忆记录。
- Qoder 核对点是 Qoder 对话中完成需求拆解、实现接口和页面、调试验证的记录截图。

## 接口变更

### POST `/api/search`

请求体新增可选字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 自然语言查询，长度 1–500 |
| `limit` | int | 否 | 返回条数，默认 5，范围 1–20 |
| `tags` | string[] | 否 | 标签筛选，传入时仅返回包含任一指定标签的资料；省略或为空数组时不做过滤 |

## Qoder Sites 发布

在 Qoder 中打开项目，使用 Sites 创建站点并发布。访问范围选择”任何人都可访问”。发布后用浏览器无痕窗口打开站点网址，确认未登录也能访问项目介绍和体验入口。

## 重置

删除 `qoder/data` 后重新初始化 seekdb collection。PowerContext 使用新的 Scope ID 进行干净验证。
