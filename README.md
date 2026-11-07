# Dify Fun Debate

这是一个“离谱辩论主持人”应用。用户输入两个看似不相关的立场或一个
脑洞题目，Dify Chatflow 会检索 seekdb 中的世界观素材，读取玩家的
PowerContext 状态，生成带有双方立场、意外论据和裁判结论的短辩论。

## 技术分工

- Dify Chatflow 负责输入、HTTP 节点、模型调用、提示词编排和最终回答。
- seekdb 保存离谱世界观和规则，并通过 `/retrieve` 返回检索结果。
- PowerContext 保存 Dify 会话 ID 对应的玩家状态和上一轮辩论结果，下一轮通过
  `/memory/load` 读取。

## 运行

```bash
python -m pip install -r dify/requirements.txt
python dify/backend/seed_seekdb.py

export SEEKDB_PATH="$PWD/dify/data/fun.db"
export POWERCONTEXT_URL="http://127.0.0.1:8000"
export POWERCONTEXT_SCOPE_ID="replace-with-your-scope-id"
uvicorn dify.backend.app:app --host 0.0.0.0 --port 8787
```

在 Dify 导入 `dify/absurd-debate.yml`，将两个 HTTP Request 节点的地址改为
部署后的 `/retrieve`、`/memory/load` 和 `/memory/save`。LLM 节点选择你
工作区中可用的模型。Dify 的 HTTP Request 节点允许工作流连接外部服务。

## 体验样例

输入：

```text
月球快递员和会唱歌的火锅谁更适合当城市市长？请举办一场三轮辩论，最后给出离谱但有逻辑的裁判结论。
```

预期表现：

1. seekdb 检索月尘、火锅和主持规则素材。
2. Dify 生成三轮辩论，并分别呈现双方观点。
3. 结论引用检索到的设定，不声称真实事实。
4. PowerContext 保存本轮 Dify 会话状态。下一轮输入“继续上次辩论”时，
   `/memory/load` 返回上一轮状态，Dify 继续同一场辩论。

重置方式是删除 `dify/data/fun.db`，并在 PowerContext 中使用新的 Scope。

## DSL

`absurd-debate.yml` 是 Chatflow 导入文件。导入后需要绑定当前工作区的
LLM，并把 HTTP 节点 URL 改为实际后端地址。DSL 不包含密钥、数据库文件
或 PowerContext Token。
