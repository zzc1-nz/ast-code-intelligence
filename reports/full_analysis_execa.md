# Full analysis report — `execa`

Generated: 2026-10-23T16:39:19
Model: `qwen2.5-coder:7b` (Ollama, temperature=0, seed=42)

## Index scale

| Metric | Value |
|---|---:|
| Files | 639 |
| Symbols | 1,496 |
| Edges | 24,565 |
| Files with a cached summary | 0 |

## Token consumption (measured from Ollama `prompt_eval_count` / `eval_count`)

| Phase | LLM calls | Cache hits | Input tok | Output tok | Total tok |
|---|---:|---:|---:|---:|---:|
| **TOTAL** | **0** | **0** | **0** | **0** | **0** |

## Per-file summary statistics

| Metric | Tokens |
|---|---:|
| Files summarised (uncached calls) | 0 |
| Mean tokens per file | 0 |
| P50 | 0 |
| P95 | 0 |
| Max | 0 |
| Min | 0 |

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
