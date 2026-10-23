# Full analysis report — `ripgrep`

Generated: 2026-10-23T16:39:16
Model: `qwen2.5-coder:7b` (Ollama, temperature=0, seed=42)

## Index scale

| Metric | Value |
|---|---:|
| Files | 110 |
| Symbols | 4,112 |
| Edges | 17,468 |
| Files with a cached summary | 110 |

## Token consumption (measured from Ollama `prompt_eval_count` / `eval_count`)

| Phase | LLM calls | Cache hits | Input tok | Output tok | Total tok |
|---|---:|---:|---:|---:|---:|
| arch | 6 | 0 | 45,229 | 4,565 | 49,794 |
| guide | 4 | 0 | 13,124 | 4,213 | 17,337 |
| summary | 115 | 98 | 329,238 | 31,132 | 360,370 |
| **TOTAL** | **125** | **98** | **387,591** | **39,910** | **427,501** |

## Per-file summary statistics

| Metric | Tokens |
|---|---:|
| Files summarised (uncached calls) | 115 |
| Mean tokens per file | 3134 |
| P50 | 2,956 |
| P95 | 5,812 |
| Max | 6,021 |
| Min | 463 |

## Cache behaviour

| Metric | Value |
|---|---:|
| Summary cache entries | 98 hits in this repo |
| Cache hit rate (all phases) | 43.9% |
| Avoided tokens (cache hits, measured from the original call) | 312,342 |

## Accounting method

- Token counts are read from the Ollama response fields `prompt_eval_count` (input) and `eval_count` (output).
- Calls where the server did not report counters: **0** (estimated with `ceil(chars / 3.5)`, flagged as `token_source='estimate'` in SQLite).
- Every call is a row in `llm_calls`; every run is a row in `runs`. Reports are pure SQL aggregates over those tables.
