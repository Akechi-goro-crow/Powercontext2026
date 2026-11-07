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

Configure `POWERCONTEXT_URL`, `POWERCONTEXT_SCOPE_ID`, and `MINIMAX_API_KEY`
through the environment. Never place credentials in this directory.
