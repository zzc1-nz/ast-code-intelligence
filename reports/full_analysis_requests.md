# Full analysis report — `requests`

Generated: 2026-10-23T16:39:18
Model: `qwen2.5-coder:7b` (Ollama, temperature=0, seed=42)

## Index scale

| Metric | Value |
|---|---:|
| Files | 37 |
| Symbols | 803 |
| Edges | 4,170 |
| Files with a cached summary | 37 |

## Token consumption (measured from Ollama `prompt_eval_count` / `eval_count`)

| Phase | LLM calls | Cache hits | Input tok | Output tok | Total tok |
|---|---:|---:|---:|---:|---:|
| summary | 37 | 0 | 80,460 | 8,537 | 88,997 |
| **TOTAL** | **37** | **0** | **80,460** | **8,537** | **88,997** |

## Per-file summary statistics

| Metric | Tokens |
|---|---:|
| Files summarised (uncached calls) | 37 |
| Mean tokens per file | 2405 |
| P50 | 1,895 |
| P95 | 5,530 |
| Max | 5,586 |
| Min | 385 |

## Cache behaviour

| Metric | Value |
|---|---:|
| Summary cache entries | 0 hits in this repo |
| Cache hit rate (all phases) | 0.0% |
| Avoided tokens (cache hits, measured from the original call) | 0 |

## Accounting method

- Token counts are read from the Ollama response fields `prompt_eval_count` (input) and `eval_count` (output).
- Calls where the server did not report counters: **0** (estimated with `ceil(chars / 3.5)`, flagged as `token_source='estimate'` in SQLite).
- Every call is a row in `llm_calls`; every run is a row in `runs`. Reports are pure SQL aggregates over those tables.
