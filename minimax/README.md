# MiniMax SDD context sample

This directory is the complete validation sample for the MiniMax challenge.
It contains a small shipping-rule project, an active specification, a seekdb
context index, a PowerContext state adapter, a MiniMax API client, before/after
snapshots, and reproducible validation commands.

The main entry points are:

```bash
python minimax/context_agent.py ingest
python minimax/context_agent.py prepare "Fix free shipping eligibility for remote regions"
python minimax/run_validation.py
python -m pytest minimax/sample_project/test_shipping.py -q
```

Configure `POWERCONTEXT_URL`, `POWERCONTEXT_SCOPE_ID`, `MINIMAX_API_KEY`,
`MINIMAX_BASE_URL`, and `MINIMAX_MODEL` through the environment. Never place
credentials in this directory.

## Windows setup

The context module supports a remote seekdb server through these variables.
This is the recommended mode on Windows because the embedded native seekdb
runtime is platform-dependent.

```powershell
cd C:\path\to\Powercontext2026
.\.venv\Scripts\Activate.ps1
python -m pip install -r minimax\requirements.txt

$env:SEEKDB_HOST = "127.0.0.1"
$env:SEEKDB_PORT = "2881"
$env:SEEKDB_TENANT = "sys"
$env:SEEKDB_USER = "root"
$env:SEEKDB_PASSWORD = Read-Host "seekdb root password"
$env:POWERCONTEXT_URL = "http://127.0.0.1:8000"
$env:POWERCONTEXT_SCOPE_ID = "replace-with-your-scope-id"
```

Start PowerContext in a separate terminal. The server uses SQLite by default,
which is sufficient for this task's state-reuse requirement:

```powershell
uv tool install --force "powercontext[cli,server]==1.3.0"
powercontext server run
```

If the Windows `uv` launcher reports a child-process permission error, install
the same package into the project virtual environment and run its local
`powercontext` command instead.

For a China-region MiniMax account, set the compatible endpoint and model
shown by the MiniMax console. A current OpenAI-compatible configuration is:

```powershell
$env:MINIMAX_BASE_URL = "https://api.minimax.cn/v1/chat/completions"
$env:MINIMAX_MODEL = "MiniMax-M3.1-Flash"
$env:MINIMAX_REASONING_EFFORT = "low"
$env:MINIMAX_API_KEY = Read-Host "MiniMax API key"
```

For the global endpoint, use `https://api.minimax.io/v1/chat/completions`.
Subscription keys and ordinary pay-as-you-go API keys are different products.
Use the key shown by the Token Plan or M Plan console for the selected endpoint.

## Validation sequence

Run the before/after checks first:

```powershell
python minimax\run_validation.py
python -m pytest minimax\sample_project\test_shipping.py -q
```

Ingest and retrieve the real engineering sources from seekdb:

```powershell
python minimax\context_agent.py ingest
python minimax\context_agent.py prepare "Fix free shipping eligibility for remote regions" |
  Out-File -FilePath minimax\validation\context.json -Encoding utf8
```

Give the generated context to MiniMax for the actual code change:

```powershell
python minimax\minimax_client.py `
  --context-file minimax\validation\context.json `
  --task "Fix free shipping eligibility for remote regions" `
  --output minimax\artifacts\minimax\response.json
```

Review the returned unified diff before applying it. The committed sample
already contains the reviewed after-state in `sample_project/shipping.py`.

Save a fresh state record, then start a new process and read it back:

```powershell
python minimax\context_agent.py save-state minimax\validation\state.json
python minimax\context_agent.py resume
```
