# Query-directed literal Memory evidence

Set `PrepareContextRequest(evidence_selection=True)` to select query-relevant slices from recalled Memory entries. The default is `False` and retains ordinary preparation behavior.

Evidence mode processes Memory candidates only and emits the existing prepared-context JSON envelope, including its trust policy. This also applies when `assembly` or `include_code` is supplied. It performs no network access, model inference, annotation lookup, persistence, or source-tree writes.

`PreparedContextBuilder.build_scopes_result(...).fragments` contains immutable fragments. Each fragment has a scope-qualified `MemoryEntryAddress`, half-open Python-character `start` and `end` offsets, and literal `entry.text[start:end]`. The corresponding `origins` entry and JSON citation identify the same Memory revision and entry version. Equal text in different scopes retains separate identities.

The selector uses query term specificity, document coverage, explicit abbreviation definitions, sentence context and contiguous windows. A bounded search measures each complete rendering after merging adjacent slices. The budget includes UTF-8 text, JSON escaping, exact citations, the trust policy and envelope markers. Oversized sentences expose interior slices; a request may validly produce an empty context when no selected fragment fits.

## Validation

```bash
make -C competition test-evidence
uv run pytest tests/builtin/runtime/test_evidence_selection.py tests/builtin/runtime/test_prepared_context.py
make contract-test
```

Independent development-set measurement used 120 publicly licensed SciFact claims, three abstracts per request and both 1400-byte and 1800-byte budgets (240 requests). Mean evidence-token recall was 0.670784 and complete-evidence coverage was 0.604167. Public development annotations were used only by the separate scorer, never passed into the selector or included in product code. These results do not establish the private platform score.

SciFact source and attribution: https://github.com/allenai/scifact (Wadden et al., 2020). Claims/annotations use CC BY 4.0; abstracts follow S2ORC ODC-By 1.0.
