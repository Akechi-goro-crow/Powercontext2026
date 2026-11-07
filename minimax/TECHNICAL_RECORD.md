# MiniMax SDD validation record

This sample demonstrates a complete task loop for the MiniMax challenge.

## Task and expected behavior

The task is recorded in `validation/task.json`. The checkout module must reject
remote regions while preserving the inclusive 99 CNY threshold and the public
function signature.

## Context selection

`context_agent.py ingest` stores the active specification, implementation, and
test file in seekdb. `context_agent.py prepare` queries that collection with the
task description and returns the selected paths and text used for the MiniMax
request. The collection uses pyseekdb's embedded seekdb runtime.

## MiniMax participation

Set `MINIMAX_API_KEY` locally, then run:

```bash
python minimax/context_agent.py ingest
python minimax/context_agent.py prepare "Fix free shipping eligibility for remote regions"
python minimax/minimax_client.py
```

The MiniMax call uses the OpenAI-compatible endpoint configured by
`MINIMAX_BASE_URL` and the model configured by `MINIMAX_MODEL`. The actual
call metadata is recorded in `validation/minimax_call.json`, and the generated
unified diff is recorded in `validation/minimax_patch.diff`. The response's
private reasoning content is not committed. Secrets are read from the
environment and never committed.

## PowerContext state reuse

After validation, save a task record containing the commit, selected sources,
test command, and results. Then restart the process and run `resume`.

```bash
python minimax/context_agent.py save-state minimax/validation/state.json
python minimax/context_agent.py resume
```

The second command reads the saved record through PowerContext's memory search,
which proves that the task state is available after the original process exits.

## Verification commands

```bash
python minimax/run_validation.py
python -m pytest minimax/sample_project/test_shipping.py -q
```

`run_validation.py` executes the same boundary cases against the before and
after snapshots. It must report a failed before snapshot and a passing current
implementation. The pytest suite must pass for the current implementation.

## Reproduction

1. Install `minimax/requirements.txt`.
2. Start a local PowerContext server and set `POWERCONTEXT_URL`.
3. Set `MINIMAX_API_KEY` for the MiniMax call.
4. Ingest and prepare the context with seekdb.
5. Record the MiniMax response and apply the reviewed change.
6. Run the before/after validation and pytest commands.
7. Save the state and restart the context process.

The sample does not commit credentials, model responses, or local database
files.
