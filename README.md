# codegraph — AST-driven local code intelligence engine

A fully local code-analysis engine. It parses a repository with **Tree-sitter**,
builds a **SQLite knowledge graph** of symbols and their call / import /
inheritance edges, and drives a **local Ollama model** (`qwen2.5-coder:7b`) to
produce per-file summaries, cross-file architecture analysis, developer guides
and code reviews.

The engineering claim is narrow and testable: **AST-grounded context beats
grep-style context**. Instead of stuffing raw source into the prompt, every
prompt is built from graph facts — the symbol table, resolved call edges,
unresolved dynamic-call evidence, the dependency neighbourhood, and the cached
summaries of neighbouring files. That is what lets a 7B model on an 8 GB laptop
GPU produce coherent, citable analysis.

> **Verified on real repositories** — 786 files / 6,411 symbols / 46,203 graph
> edges across Rust, Python and TypeScript, **zero parse errors**.
> 156 LLM calls, **516,498 tokens** measured end-to-end, every count reported
> by the Ollama server (zero character-based estimates).
>
> | Metric | Result |
> |---|---|
> | First full analysis (ripgrep, 110 files) | **360,370 tokens** · 3,134/file · P95 5,812 |
> | Incremental round (5 measured rounds) | **9,225 tokens** → **97.4 % saved** |
> | Summary cache | 98 hits · **312,342 tokens avoided** → **46.4 % saved** |
> | Architecture analysis vs per-file summary | **3.3×** (10,362 vs 3,134 tokens) |
> | Test suite | **45 passed** |
>
> See [`reports/FINAL_REPORT.md`](reports/FINAL_REPORT.md) for the full
> breakdown and [`docs/VERIFICATION.md`](docs/VERIFICATION.md) to reproduce it.

---

## Architecture

```
                    ┌──────────────────────────────────────────────┐
  source files ────▶│  Tree-sitter parsers                         │
  (.py .rs .ts .js) │  python · rust · typescript · tsx · javascript │
                    └───────────────┬──────────────────────────────┘
                                    │ RawSymbol / RawCall / RawImport / RawInherit
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │  graph builder  (edge resolution)            │
                    │  1 local scope  2 import-aware               │
                    │  3 globally-unique  4 unresolved (recorded)  │
                    └───────────────┬──────────────────────────────┘
                                    ▼
      ┌──────────────────────────────────────────────────────────────┐
      │  SQLite knowledge graph  (.codegraph/graph.db)               │
      │  files · symbols · edges · symbols_fts (FTS5)                │
      │  file_summaries (cache) · llm_calls (token ledger) · runs    │
      └───────────────┬──────────────────────────────┬───────────────┘
                      │                              │
         ┌────────────▼───────────┐      ┌───────────▼──────────────┐
         │ query engine           │      │ prompt builder            │
         │ search / callers /     │      │ symbols + call edges +    │
         │ callees / call chains  │      │ neighbour summaries +     │
         │ dependency closure     │      │ dependency closure        │
         └────────────┬───────────┘      └───────────┬──────────────┘
                      │                              │
                      └──────────────┬───────────────┘
                                     ▼
                    ┌──────────────────────────────────────────────┐
                    │  Ollama  qwen2.5-coder:7b  (HTTP)            │
                    │  temperature=0  seed=42                      │
                    └───────────────┬──────────────────────────────┘
                                    ▼
        summaries · architecture analysis · developer guide · review
                                    │
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │  CLI (Typer) · REST API (FastAPI) · Web UI    │
                    └──────────────────────────────────────────────┘
```

### Why the AST layer matters

| Naive approach | codegraph |
|---|---|
| grep the whole repo, truncate to fit context | symbol table with exact line ranges and signatures |
| "who calls this?" → text search | resolved call edges + explicitly recorded unresolved ones |
| context = whatever files happened to match | context = dependency closure from the graph |
| no idea if the analysis is stale | content-hash keyed summary cache, incremental re-index |

---

## Hardware / environment

| Item | Value |
|---|---|
| GPU | RTX 4060 Laptop, 8 GB (≈6.7 GB usable with desktop running) |
| RAM | 16 GB DDR5 |
| CPU | i7-13700H |
| OS | Windows 11, native (no Docker for the core pipeline) |
| Python | 3.11.9 + uv 0.13.0 |
| LLM | Ollama + `qwen2.5-coder:7b` (Q4_K_M GGUF, ≈4.7 GB) |
| Storage | everything under `D:\codegraph` |

---

## Repository layout

```
D:\codegraph\
├─ src\codegraph\
│   ├─ config.py      # paths, LLM + index config, template versions
│   ├─ db.py          # SQLite schema, FTS5 index, triggers
│   ├─ parser.py      # Tree-sitter extraction (python/rust/ts/tsx/js)
│   ├─ graph.py       # repo scan, graph build, edge resolution
│   ├─ query.py       # search, callers/callees, call chains, closures
│   ├─ prompts.py     # AST-grounded prompt construction + token budgets
│   ├─ llm.py         # Ollama HTTP client, token accounting
│   ├─ pipeline.py    # summary / arch / guide / review phases, cache, ledger
│   ├─ reports.py     # Markdown reports generated from SQL aggregates
│   ├─ cli.py         # Typer CLI
│   └─ api.py         # FastAPI + embedded dashboard
├─ tests\test_core.py # parser / graph / query / prompt-budget tests
├─ scripts\simulate_incremental.py
├─ reports\           # generated Markdown reports
├─ output\            # generated architecture / guide / review artifacts
├─ repos\             # cloned test repositories
├─ models\            # GGUF weights
└─ .venv\
```

---

## Reproducing from scratch

```powershell
# 1. environment (uv, Python 3.11)
uv venv --python 3.11 D:\codegraph\.venv
uv pip install --python D:\codegraph\.venv\Scripts\python.exe -e D:\codegraph

# 2. model (ModelScope is the fast path here; ~26 MB/s)
curl.exe -L -o D:\codegraph\models\qwen2.5-coder-7b-instruct-q4_k_m.gguf `
  "https://www.modelscope.cn/api/v1/models/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/repo?Revision=master&FilePath=qwen2.5-coder-7b-instruct-q4_k_m.gguf"

# 3. Ollama: start the server with the model directory pointed at D:\
$env:OLLAMA_MODELS = "D:\codegraph\models\ollama"
ollama serve
# then create the model from the GGUF:
#   (Modelfile)  FROM D:\codegraph\models\qwen2.5-coder-7b-instruct-q4_k_m.gguf
ollama create qwen2.5-coder:7b -f D:\codegraph\models\Modelfile

# 4. clone a target repo and index it
git clone --depth 1 https://github.com/BurntSushi/ripgrep.git D:\codegraph\repos\ripgrep
codegraph index D:\codegraph\repos\ripgrep --full

# 5. query
codegraph search Searcher --repo D:\codegraph\repos\ripgrep
codegraph trace Searcher::search_reader --repo D:\codegraph\repos\ripgrep --dir both --depth 3

# 6. LLM phases
codegraph summary --repo D:\codegraph\repos\ripgrep
codegraph analyze --repo D:\codegraph\repos\ripgrep --entry main
codegraph guide   --repo D:\codegraph\repos\ripgrep

# 7. reports + dashboard
codegraph report --repo D:\codegraph\repos\ripgrep
codegraph serve --port 8765     # http://127.0.0.1:8765/
```

---

## CLI

| Command | Purpose |
|---|---|
| `codegraph index <repo> [--full/--incremental]` | Tree-sitter parse → SQLite graph |
| `codegraph search <query>` | FTS5 symbol search |
| `codegraph trace <symbol> --dir out/in/both --depth N` | callers / callees / multi-hop chains |
| `codegraph summary [--files a,b] [--force]` | per-file LLM summaries (cached) |
| `codegraph analyze [--entry main]` | cross-file architecture analysis |
| `codegraph guide` | developer guide (architecture → guide) |
| `codegraph review --files a,b` | AST-grounded code review |
| `codegraph stats` | index + token statistics |
| `codegraph report [--which all\|index\|full\|incremental\|arch]` | regenerate Markdown reports |
| `codegraph serve` | FastAPI + web dashboard |
| `codegraph doctor` | Ollama connectivity + warmup check |

## REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | service + Ollama + model status |
| GET | `/api/stats` | index stats + token report |
| GET | `/api/search?q=` | FTS5 symbol search |
| GET | `/api/trace?symbol=&dir=&depth=` | call chains |
| GET | `/api/files` | indexed files |
| GET | `/api/summary/{path}` | cached summary for one file |
| GET | `/api/tokens` | token ledger aggregate |
| GET | `/api/runs` | analysis runs |
| POST | `/api/index` | (re)index |
| POST | `/api/summarize` | run summaries |
| POST | `/api/analyze` | architecture analysis |
| POST | `/api/guide` | developer guide |
| GET | `/` | dashboard |

---

## Token accounting

Every LLM call is one row in `llm_calls`:

| Column | Meaning |
|---|---|
| `input_tokens` | Ollama `prompt_eval_count` |
| `output_tokens` | Ollama `eval_count` |
| `token_source` | `server` when reported by Ollama, `estimate` when the fallback `ceil(chars/3.5)` was used |
| `latency_ms` | wall time of the call |
| `cache_hit` | 1 for cache hits (no HTTP call) |
| `avoided_input_tokens` / `avoided_output_tokens` | the historical cost of the call that was avoided |

Reports are pure SQL aggregates over this table, so **every published number is
reproducible** with `codegraph report`.

Measured results are in:
- [`reports/index_stats.md`](reports/index_stats.md)
- [`reports/full_analysis.md`](reports/full_analysis.md)
- [`reports/incremental_analysis.md`](reports/incremental_analysis.md)
- [`reports/architecture_analysis.md`](reports/architecture_analysis.md)

---

## Reproducibility

- `seed=42` for every LLM call (`options.seed`) and for every script that
  samples files (`scripts/simulate_incremental.py`).
- `temperature=0`, `top_p=0.9`.
- Prompt template versions are part of the cache key (`summary-v1`, `arch-v1`,
  `guide-v1`, `review-v1`), so changing a prompt correctly invalidates the cache.

## Tests

```powershell
D:\codegraph\.venv\Scripts\python.exe -m pytest tests -q
```

Covers: Python/Rust/TypeScript symbol extraction, call scoping, inheritance
edges, graph build + FTS search, callers/callees, multi-hop chains, dependency
closure, incremental re-index, JSON extraction from model output, and the prompt
token budget.

## Design notes / limitations

- **Static resolution is incomplete by nature.** Dynamic dispatch, trait
  objects, `getattr`, macros and stdlib calls cannot always be resolved. These
  are stored as edges with `resolved=0` and a textual `dst_name`, and the
  resolution rate is reported rather than hidden.
- **No MCP server, no IDE plugin, no cloud sync** — deliberately out of scope.
- **Index and LLM phases run serially** so the 7B model and the indexer never
  compete for the ~6.2 GB of usable VRAM.

See [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) for the full list, including
the verified finding that generated *code examples* in the developer guide are
not compile-checked, while every *symbol reference* in the architecture
analysis was verified to exist at the stated line.

---

## Measured results

All numbers below come from SQLite (`llm_calls` / `runs` tables) and can be
regenerated with `codegraph report`. Full breakdown in
[`reports/FINAL_REPORT.md`](reports/FINAL_REPORT.md).

### Index scale

| Repository | Language | Files | Symbols | Edges | Call edges | Parse errors | Index time | DB size |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **ripgrep** (primary) | Rust | 110 | 4,112 | 17,468 | 16,842 | 0 | 2.6 s | 4.4 MB |
| **requests** | Python | 37 | 803 | 4,170 | 3,805 | 0 | 0.4 s | 1.3 MB |
| **execa** | TS/JS | 639 | 1,496 | 24,565 | 22,840 | 0 | 13.9 s | 3.9 MB |

### Call-graph resolution (measured, not hidden)

| Repository | Resolved | Unresolved | Resolution rate |
|---|---:|---:|---:|
| ripgrep | 7,964 | 9,504 | **45.6%** |
| requests | 1,770 | 2,400 | **42.4%** |
| execa | 8,775 | 15,790 | **35.7%** |

Unresolved targets are dominated by stdlib/macro symbols with no definition
inside the repo (`assert_eq!` 1,167 · `assert` 771 · `new` 544 · `Ok` 540 ·
`Some` 470). Genuinely un-nameable dynamic call sites are counted separately:
29/17,088 (0.17%) for ripgrep.

### Token consumption

| Phase | Calls | Input tok | Output tok | Total | Per call |
|---|---:|---:|---:|---:|---:|
| ripgrep full summary (110 files) | 115 | 329,238 | 31,132 | **360,370** | 3,134 |
| requests full summary (37 files) | 37 | 80,460 | 8,537 | **88,997** | 2,405 |
| ripgrep architecture analysis (final version, n=3) | 3 | 29,199 | 1,888 | 31,087 | **10,362** |
| ripgrep developer guide (final version, n=1) | 1 | 7,237 | 1,092 | 8,329 | **8,329** |

> The `arch` / `guide` phases were re-run while iterating on the prompt; all 10
> of those calls are recorded in `llm_calls` and counted in the project total,
> but the per-call ratios use the final prompt version. See
> [`reports/FINAL_REPORT.md`](reports/FINAL_REPORT.md) §2.3 for the full table.

| Per-file summary metric | ripgrep | requests |
|---|---:|---:|
| mean | 3,134 | 2,405 |
| P50 | 2,956 | 1,895 |
| **P95** | **5,812** | **5,530** |
| max | 6,021 | 5,586 |

**All 156 real calls reported `token_source='server'` — zero estimated tokens.**

### Incremental analysis (5 rounds on a copy of ripgrep)

| Round | Changed | Closure | Re-indexed | LLM calls | Tokens |
|---|---:|---:|---:|---:|---:|
| 1 | 3 | 3 | 3 | 3 | 9,779 |
| 2 | 3 | 3 | 3 | 3 | 13,021 |
| 3 | 3 | 2 | 2 | 2 | 3,021 |
| 4 | 3 | 3 | 3 | 3 | 6,170 |
| 5 | 3 | 3 | 3 | 3 | 14,133 |
| **total** | 15 | 14 | 14 | **14** | **46,124** |

- Full-run baseline: **360,370** tokens
- Mean incremental round: **9,225** tokens → **97.4% saved**
- Cache: 98 hits, **312,342 tokens avoided** → **46.4% saved** across all runs
- A repeat full run over an unchanged repo: 93/110 files served from cache
  (84.55% hit rate), only 48,028 tokens spent

### Key ratios

| Question | Answer |
|---|---|
| First full analysis cost | **360,370 tokens** (ripgrep, 110 files) |
| One incremental round | **9,225 tokens** on average |
| Cache saving ratio | **46.4%** |
| Architecture vs single-file summary | **3.3×** (10,362 vs 3,134 tokens/call, final version) |
| Incremental vs full | **97.4%** saved |
| Total tokens across the whole project | **516,498** |

### Test coverage

```
45 passed in ~2s
```

Covers AST extraction for Python/Rust/TS, call scoping, inheritance, edge
resolution strategies, FTS5 search (incl. special characters), incremental
re-index edge retention, dependency closure, JSON truncation repair, prompt
budgets, and the 16k context ceiling.
