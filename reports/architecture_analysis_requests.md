# Architecture analysis report — `requests`

Generated: 2026-10-23T16:39:19
Model: `qwen2.5-coder:7b`

## Token cost comparison

| Phase | Calls | Input tok | Output tok | Total tok | Tokens per call | vs single-file summary |
|---|---:|---:|---:|---:|---:|---:|
| summary (per file) | 37 | 80,460 | 8,537 | 88,997 | 2405 | 1.0× |

The architecture and guide prompts inject the call chain, module layout, cross-file dependency edges and up to 25 file summaries, so their single-call input token count is an order of magnitude above a per-file summary.

## Generated artifacts

| Kind | Entry | Created | Size (chars) |
|---|---|---|---:|
