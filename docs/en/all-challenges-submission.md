# Unified PowerContext competition submission

Repository: `https://github.com/Reverie-t/powercontext67`

Branch: `all-challenges-submit`

This branch includes the frozen PowerContext baseline and the implementations for
all ten published code challenges. Use one full commit SHA from this branch for
every selected PowerContext challenge, including repository preparation. Select
the applicable challenges on the evaluation platform; combining their code does
not select platform tasks automatically.

| Challenge | Implementation |
| --- | --- |
| 2 | CSV row evidence capture |
| 3 | Pi session history capture |
| 4 | PDF page evidence retrieval |
| 5 | HTTP Memory reranking with strict response validation and global stable ordering |
| 6 | Incremental context packs |
| 7 | Source-count processing triggers |
| 8 | Embedding batching and caching |
| 9 | Persistent Source lifecycle propagation |
| 10 | Query-driven original evidence selection within the full UTF-8 budget |
| 11 | Single-statement SeekDB hybrid retrieval |

Run the published checks from the product checkout with the fixed competition
Python environment installed:

```bash
make -C competition check-baseline
make -C competition test
```

These are the public checks. Official private quality thresholds, repository
permissions and platform task selection are evaluated separately.
