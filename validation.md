# Dify Fun Debate 验证记录

## 初始条件

- seekdb 中已运行 `python dify/backend/seed_seekdb.py`
- Dify 导入 `dify/absurd-debate.yml`
- bridge `/health` 返回 `ok=true`
- Dify HTTP 节点指向 bridge 地址
- PowerContext Scope 已创建，`POWERCONTEXT_SCOPE_ID` 已配置

## 验证步骤

1. 使用同一个 Dify 会话输入“月球快递员和会唱歌的火锅谁更适合当城市市长？”
2. 检查回答包含双方立场、至少两轮论证和裁判结论。
3. 检查 bridge 日志出现 `/retrieve` 和 `/memory/save`。
4. 在同一会话输入“继续上次辩论，引入图书馆自动门作为第三方证人”。
5. 检查回答能延续上一轮状态，并能使用新增的 seekdb 素材。
6. 在 PowerContext API 或 Dashboard 查询该 Scope，确认存在本轮保存的记忆。

## 异常样例

- 停止 bridge 后再次请求，Dify 应说明外部素材服务不可用，不编造 seekdb 结果。
- 清空 seekdb 后请求，Dify 可以继续主持，但应标记“世界观素材未命中”。
- 清除 PowerContext Scope 后请求，Dify 从新会话开始，不应声称记得旧状态。
