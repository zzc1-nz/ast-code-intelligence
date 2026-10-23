# Incremental analysis report — `requests`

Generated: 2026-10-23T16:39:18
Model: `qwen2.5-coder:7b`

## Run-by-run token consumption

| # | Run ID | Kind | Files total | Called | Cached | In tok | Out tok | Total tok | Cache hit | Note |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | `full-20261023-160555-15bd33` | full | 37 | 37 | 0 | 80,460 | 8,537 | 88,997 | 0.0% | paths=all |

## Savings versus a full re-run

_No incremental runs recorded yet._

## Method

1. A file edit changes its content hash.
2. `codegraph index` re-parses only files whose hash changed.
3. The dependency closure (changed files + direct importers + direct callees) is computed from the SQLite graph.
4. Only closure members whose content hash changed lose their cache entry; everything else is served from `file_summaries` without an LLM call.
