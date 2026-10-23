# Architecture analysis report — `execa`

Generated: 2026-10-23T16:39:20
Model: `qwen2.5-coder:7b`

## Token cost comparison

| Phase | Calls | Input tok | Output tok | Total tok | Tokens per call | vs single-file summary |
|---|---:|---:|---:|---:|---:|---:|

The architecture and guide prompts inject the call chain, module layout, cross-file dependency edges and up to 25 file summaries, so their single-call input token count is an order of magnitude above a per-file summary.

## Generated artifacts

| Kind | Entry | Created | Size (chars) |
|---|---|---|---:|
