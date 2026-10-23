# Architecture analysis report — `ripgrep`

Generated: 2026-10-23T16:39:17
Model: `qwen2.5-coder:7b`

## Token cost comparison

| Phase | Calls | Input tok | Output tok | Total tok | Tokens per call | vs single-file summary |
|---|---:|---:|---:|---:|---:|---:|
| summary (per file) | 115 | 329,238 | 31,132 | 360,370 | 3134 | 1.0× |
| architecture | 6 | 45,229 | 4,565 | 49,794 | 8299 | 2.6× |
| developer guide | 4 | 13,124 | 4,213 | 17,337 | 4334 | 1.4× |

The architecture and guide prompts inject the call chain, module layout, cross-file dependency edges and up to 25 file summaries, so their single-call input token count is an order of magnitude above a per-file summary.

## Generated artifacts

| Kind | Entry | Created | Size (chars) |
|---|---|---|---:|
| guide | `-` | 2026-10-23 08:38:48 | 4,744 |
| architecture | `main` | 2026-10-23 08:37:50 | 2,540 |
| guide | `-` | 2026-10-23 08:36:24 | 4,427 |
| architecture | `main` | 2026-10-23 08:35:38 | 3,128 |
| architecture | `main` | 2026-10-23 08:34:57 | 2,540 |
| guide | `-` | 2026-10-23 08:33:00 | 5,155 |
| architecture | `main` | 2026-10-23 08:32:06 | 6,384 |
| architecture | `main` | 2026-10-23 08:30:45 | 3,203 |
| guide | `-` | 2026-10-23 08:25:14 | 4,593 |
| architecture | `main` | 2026-10-23 08:24:18 | 2,677 |
