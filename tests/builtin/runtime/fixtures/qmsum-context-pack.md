# QMSum context-pack regression fixture

Source: https://github.com/Yale-LILY/QMSum/blob/83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e/data/ALL/test/ES2004a.json

Upstream commit: `83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e`.
Original file SHA-256: `911e56db0d8482f1e730bae0a8fcf0ab0095c94abb000ba0516e975fa6e48b31`.
The upstream MIT license is included as `QMSum-LICENSE`.

Select the first 12 original turns at or after zero-based index 131 whose
speaker-prefixed text is 120 to 900 UTF-8 bytes. Preserve their original order,
speaker, punctuation, and full turn text. This includes the two turns in the
competition public sample and adds independent meeting material.

The offline regression starts with eight turns and appends the remaining four
at zero-based rounds 8, 16, 24, and 32. Across 40 rounds, query `remote`, `price`,
`market`, and `control` for ten rounds each, with a 16,384-byte prepare budget.
Every result must be nonempty, at least four contexts must differ, and a receiver
reconstructs every result using only deserialized full-pack/delta bytes.

The reported denominator is ordinary prepared UTF-8 content. The incremental
numerator includes the first full pack and every delta. Full v1 pack and
independent per-round gzip level 9 (`mtime=0`) totals are also reported. These
are a reproducible local regression configuration, not undisclosed grader data.
