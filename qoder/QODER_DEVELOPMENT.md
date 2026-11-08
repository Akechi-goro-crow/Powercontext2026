# Qoder 开发指南

## 项目结构

```
qoder/
  backend/
    __init__.py
    app.py              # FastAPI 主应用，包含全部 API 端点
  static/
    index.html          # 单页前端（深色主题）
  data/                 # seekdb 嵌入式数据库文件（已 gitignore）
  requirements.txt      # Python 依赖
  validation.md         # 验证样例与提交证据
  README.md             # 项目概览与运行说明
```

## 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 后端框架 | FastAPI | 提供 RESTful API 和静态文件服务 |
| 向量检索 | seekdb (pyseekdb) | 支持嵌入式（本地 SQLite）和远程（Server）两种模式 |
| 用户记忆 | PowerContext | 保存和加载用户研究上下文 |
| 前端 | 原生 HTML/CSS/JS | 无构建工具，直接由 FastAPI 提供服务 |
| 运行时 | Python 3.11+ | 需要 uvicorn ASGI 服务器 |

## API 端点

### GET `/`
返回前端页面 `static/index.html`。

### GET `/health`
返回服务状态，包括 seekdb 模式（remote/embedded）和 PowerContext 是否配置。

### POST `/api/ingest`
导入研究资料到 seekdb。

请求体：
```json
{
  "title": "string (1-200字)",
  "content": "string (1-100000字)",
  "tags": ["string"],
  "source": "string"
}
```

### POST `/api/search`
语义检索研究资料。

请求体：
```json
{
  "query": "string (1-500字)",
  "limit": 5,
  "tags": ["string"]
}
```

`tags` 为可选参数。传入时按标签过滤（匹配任一指定标签），省略或为空数组时返回纯语义检索结果。

### POST `/api/memory/save`
保存用户研究进度到 PowerContext。

### POST `/api/memory/load`
从 PowerContext 加载用户历史上下文。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SEEKDB_PATH` | `qoder/data/notes.db` | 嵌入式模式数据库路径 |
| `SEEKDB_HOST` | `""` (空=嵌入式) | 远程 seekdb Server 地址 |
| `SEEKDB_PORT` | `2881` | 远程 seekdb 端口 |
| `SEEKDB_TENANT` | `sys` | seekdb 租户 |
| `SEEKDB_USER` | `root` | seekdb 用户 |
| `SEEKDB_PASSWORD` | `""` | seekdb 密码 |
| `POWERCONTEXT_URL` | `http://127.0.0.1:8000` | PowerContext 服务地址 |
| `POWERCONTEXT_SCOPE_ID` | `""` | PowerContext Scope ID |
| `POWERCONTEXT_TOKEN` | `""` | PowerContext 认证 Token |

## 本地开发

### 1. 环境准备

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows PowerShell
pip install -r qoder/requirements.txt
```

### 2. 启动服务（嵌入式模式，无需 seekdb Server）

```bash
python -m uvicorn qoder.backend.app:app --host 0.0.0.0 --port 8790 --reload
```

`--reload` 参数启用热重载，修改代码后自动重启。

### 3. 代码风格

- 后端使用类型注解（`dict[str, Any]`、`list[str]` 等）
- Pydantic 模型负责请求体验证
- 前端为单文件 HTML，CSS 和 JS 内联

### 4. 运行语法检查

```bash
python -m py_compile qoder/backend/app.py
```

无输出即表示语法正确。

## 标签筛选功能

`/api/search` 支持可选的 `tags` 参数：

- 前端搜索区域包含一个"按标签筛选"输入框
- 用户输入逗号分隔的标签后，请求体会携带 `tags` 数组
- 后端将 `tags` 转换为 seekdb 的 `where` 过滤条件 `{"tags": {"$in": [...]}}`
- 不填写标签时，请求体不包含 `tags` 字段，后端执行纯语义检索

## 常见问题

**seekdb 连接失败**：检查 `SEEKDB_HOST` 和端口是否正确。留空 `SEEKDB_HOST` 可切换到嵌入式模式。

**PowerContext 返回 503**：`POWERCONTEXT_SCOPE_ID` 未配置。设置环境变量后重启服务。

**前端页面不更新**：浏览器可能缓存了旧页面。硬刷新（Ctrl+Shift+R）或清除缓存。

**重置数据**：删除 `qoder/data/` 目录，重启服务后 seekdb 会重新创建空数据库。
