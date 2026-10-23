# Incremental analysis report — `ripgrep`

Generated: 2026-10-23T16:39:17
Model: `qwen2.5-coder:7b`

## Run-by-run token consumption

| # | Run ID | Kind | Files total | Called | Cached | In tok | Out tok | Total tok | Cache hit | Note |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | `full-20261023-151849-265a89` | full | 5 | 5 | 0 | 11,802 | 1,385 | 13,187 | 0.0% | paths=all |
| 2 | `full-20261023-152006-4fbf2d` | full | 5 | 0 | 5 | 0 | 0 | 0 | 100.0% | paths=all |
| 3 | `full-20261023-152032-b012d4` | full | 0 | 0 | 0 | 0 | 0 | 0 | 0.0% | paths=all |
| 4 | `full-20261023-160005-be6c11` | full | 110 | 17 | 93 | 44,464 | 3,564 | 48,028 | 84.5% | paths=all |

## Savings versus a full re-run

_No incremental runs recorded yet._

## Method

1. A file edit changes its content hash.
2. `codegraph index` re-parses only files whose hash changed.
3. The dependency closure (changed files + direct importers + direct callees) is computed from the SQLite graph.
4. Only closure members whose content hash changed lose their cache entry; everything else is served from `file_summaries` without an LLM call.
