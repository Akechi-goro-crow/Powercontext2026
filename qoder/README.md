# 研究资料伴读台

这是 Qoder 赛题的可运行样例。它把论文摘要、课程笔记和实验记录导入 seekdb，按自然语言检索相关资料，并用 PowerContext 保存与读取用户的研究方向和复习进度。

## 技术分工

- Qoder 负责主要开发工作、调试和通过 Qoder Sites 发布展示网站。
- seekdb 保存研究资料正文和标签，并提供语义检索。
- PowerContext 保存用户研究进度和薄弱点，在后续会话中复用。

## 运行环境

- Python 3.11+
- seekdb Server，默认连接 `127.0.0.1:2881`
- PowerContext Server，默认连接 `127.0.0.1:8000`

```powershell
cd C:\path\to\Powercontext2026
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r qoder\requirements.txt

$env:SEEKDB_HOST = "127.0.0.1"
$env:SEEKDB_PORT = "2881"
$env:SEEKDB_TENANT = "sys"
$env:SEEKDB_USER = "root"
$env:SEEKDB_PASSWORD = Read-Host "seekdb root password"
$env:POWERCONTEXT_URL = "http://127.0.0.1:8000"
$env:POWERCONTEXT_SCOPE_ID = "replace-with-your-scope-id"

python -m uvicorn qoder.backend.app:app --host 0.0.0.0 --port 8790
```

打开 `http://127.0.0.1:8790`。

## 验证样例

1. 在“导入一份资料”中输入标题 `Transformer 复习笔记`。
2. 内容填写 `自注意力让模型能够根据当前 token 与其他 token 的关系动态聚合信息。位置编码补充序列顺序。`
3. 点击“加入资料库”。
4. 在检索框输入 `位置编码解决什么问题？`，点击“检索”。结果应来自刚才导入的 seekdb 资料。
5. 在“我的研究上下文”输入 `我正在复习 Transformer，薄弱点是位置编码`，点击“保存进度”。
6. 点击“读取历史”，应返回 PowerContext 中保存的记录。

## 可复现记录

`validation.md` 记录完整的初始化、验证、截图和 Qoder Sites 发布步骤。配置示例不包含密码、Token 或 API Key。
