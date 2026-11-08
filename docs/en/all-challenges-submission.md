# PowerContext submission with a fresh PDF challenge

Repository: `https://github.com/Akechi-goro-crow/Powercontext2026`

Branch: `codex/pdf-fresh-start-2`

This branch starts from the published competition baseline and preserves the
existing implementations for the nine code challenges other than challenge 4,
along with repository preparation. Challenge 4 is unimplemented: its candidate
module, custom tests, workflow, documentation and optional dependency are absent.
The official challenge instructions and public evaluation assets remain available.

Select the applicable completed challenges on the evaluation platform; combining
their code does not select platform tasks automatically.

| Challenge | Implementation |
| --- | --- |
| 2 | CSV row evidence capture |
| 3 | Pi session history capture |
| 4 | Not implemented; ready for a fresh start |
| 5 | HTTP Memory reranking with strict response validation and global stable ordering |
| 6 | Incremental context packs |
| 7 | Source-count processing triggers |
| 8 | Embedding batching and caching |
| 9 | Persistent Source lifecycle propagation |
| 10 | Query-driven original evidence selection within the full UTF-8 budget |
| 11 | Single-statement SeekDB hybrid retrieval |

Run the baseline check from the product checkout with the fixed competition
Python environment installed, then the relevant completed challenge checks:

```bash
make -C competition check-baseline
make -C competition test-csv test-pi test-rerank test-incremental test-count-trigger test-embedding test-lifecycle test-evidence test-hybrid
```

These are the public checks. Official private quality thresholds, repository
permissions and platform task selection are evaluated separately.

