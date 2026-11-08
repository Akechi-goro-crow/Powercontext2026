# 验证样例与提交证据

## 功能验证

启动 seekdb、PowerContext 和 FastAPI 服务后，打开 `http://127.0.0.1:8790`。

使用 README 中的 Transformer 样例，记录以下三张截图：

1. 资料导入成功，页面显示“资料已加入资料库”。
2. 自然语言检索返回刚才导入的笔记内容，证明 seekdb 参与了核心流程。
3. 保存进度后点击“读取历史”，返回 PowerContext 记忆，证明状态被复用。

## 技术核对

- seekdb 核对点是 `/api/ingest` 写入和 `/api/search` 返回的资料正文与标题。
- PowerContext 核对点是 `/api/memory/save` 的 `changed` 或 `saved` 结果，以及随后 `/api/memory/load` 返回的记忆记录。
- Qoder 核对点是 Qoder 对话中完成需求拆解、实现接口和页面、调试验证的记录截图。

## Qoder Sites 发布

在 Qoder 中打开项目，使用 Sites 创建站点并发布。访问范围选择“任何人都可访问”。发布后用浏览器无痕窗口打开站点网址，确认未登录也能访问项目介绍和体验入口。

## 重置

删除 `qoder/data` 后重新初始化 seekdb collection。PowerContext 使用新的 Scope ID 进行干净验证。
