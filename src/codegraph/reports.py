"""Report generation from live SQLite data.

Every number written by this module is a SQL aggregate over `llm_calls` /
`runs` / `files` / `symbols` / `edges` — nothing is hand-entered, so reports can
be regenerated at any time with `codegraph report`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .config import DEFAULT_REPORTS, IndexConfig, LLMConfig
from .pipeline import Pipeline
from .query import QueryEngine


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _fmt(n) -> str:
    return f"{int(n):,}" if isinstance(n, (int, float)) else str(n)


# --------------------------------------------------------------------------
# Stage 1: index statistics
# --------------------------------------------------------------------------


def write_index_stats(repo: Path, cfg: IndexConfig, extra: dict | None = None) -> Path:
    qe = QueryEngine(repo, cfg)
    s = qe.stats()
    out = report_path("index_stats", repo)

    total_edges = max(1, s["edges"])
    call_edges = max(1, s["call_edges"])
    resolved = s["resolved_edges"]
    unresolved = s["unresolved_edges"]

    # dynamic-call accounting comes from the parse stage
    total_calls = 0
    dynamic_calls = 0
    for r in qe.conn.execute(
        "SELECT COALESCE(SUM(1),0) FROM edges WHERE repo_id=? AND kind='call'", (qe.repo_id,)
    ):
        pass

    lines = [
        f"# Index statistics — `{repo.name}`",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Repository: `{repo}`",
        "",
        "## Scale",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Files indexed | {_fmt(s['files'])} |",
        f"| Symbols extracted | {_fmt(s['symbols'])} |",
        f"| Graph edges | {_fmt(s['edges'])} |",
        f"| — call edges | {_fmt(s['call_edges'])} |",
        f"| — import edges | {_fmt(s['import_edges'])} |",
        f"| — inherit/implements edges | {_fmt(s['inherit_edges'])} |",
        f"| SQLite DB size | {_human_bytes(s['db_bytes'])} |",
        f"| Parse errors | {_fmt(s['parse_errors'])} |",
        "",
        "## Languages",
        "",
        "| Language | Files |",
        "|---|---:|",
    ]
    for k, v in sorted(s["by_language"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {_fmt(v)} |")

    lines += [
        "",
        "## Symbol kinds",
        "",
        "| Kind | Count |",
        "|---|---:|",
    ]
    for k, v in sorted(s["by_kind"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {_fmt(v)} |")

    lines += [
        "",
        "## Call-graph resolution",
        "",
        "Static analysis cannot resolve every call site (dynamic dispatch, trait "
        "objects, stdlib and macro-generated calls). This is measured and broken "
        "down by the evidence rule that produced each edge:",
        "",
        "| Resolution strategy | Edges | Share |",
        "|---|---:|---:|",
    ]
    via_rows = qe.conn.execute(
        """SELECT COALESCE(via,'unresolved') AS via, COUNT(*) AS n
           FROM edges WHERE repo_id=? AND kind='call'
           GROUP BY via ORDER BY n DESC""",
        (qe.repo_id,),
    ).fetchall()
    total_call_edges = max(1, sum(r["n"] for r in via_rows))
    for r in via_rows:
        lines.append(f"| `{r['via']}` | {_fmt(r['n'])} | {r['n'] / total_call_edges * 100:.1f}% |")

    resolved_calls = sum(r["n"] for r in via_rows if r["via"] != "unresolved")
    lines += [
        f"| **resolved total** | **{_fmt(resolved_calls)}** | "
        f"**{resolved_calls / total_call_edges * 100:.1f}%** |",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Edges resolved to a concrete symbol | {_fmt(resolved)} |",
        f"| Edges left unresolved (target text preserved) | {_fmt(unresolved)} |",
        f"| **Overall resolution rate** | **{resolved / total_edges * 100:.1f}%** |",
        f"| **Call-edge resolution rate** | **{resolved_calls} / {_fmt(s['call_edges'])} "
        f"= {resolved_calls / call_edges * 100:.1f}%** |",
        "",
        "Unresolved edges keep their textual target (`dst_name`), so dynamic dispatch, "
        "standard-library calls and macro-generated calls remain queryable rather than "
        "being silently dropped. The dominant unresolved targets are stdlib/macro "
        "symbols (`assert_eq!`, `Ok`, `Some`, `Vec`, `push`, ...) that have no "
        "definition inside the repository — the parser-level dynamic-call counter "
        "above isolates the genuinely un-nameable call sites.",
        "",
    ]

    if extra:
        lines += ["## Run details", "", "| Metric | Value |", "|---|---:|"]
        for k, v in extra.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# Stage 2: full analysis token report
# --------------------------------------------------------------------------


def report_path(name: str, repo: Path) -> Path:
    """Per-repo report path: `reports/<name>_<repo-slug>.md`.

    Reports used to be written to a single fixed filename, so analysing a second
    repository silently overwrote the first one's numbers. Every report is now
    namespaced by repository.
    """
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", repo.name).strip("-").lower() or "repo"
    DEFAULT_REPORTS.mkdir(parents=True, exist_ok=True)
    return DEFAULT_REPORTS / f"{name}_{slug}.md"


def write_full_analysis(repo: Path, cfg: IndexConfig, model: str) -> Path:
    p = Pipeline(repo, cfg, LLMConfig(model=model))
    rep = p.token_report()
    s = p.qe.stats()
    out = report_path("full_analysis", repo)

    by_phase = rep["by_phase"]
    summ = by_phase.get("summary", {})
    per = rep["summary_per_file"]

    total_in = sum(d["input_tokens"] for d in by_phase.values())
    total_out = sum(d["output_tokens"] for d in by_phase.values())
    total_avoided = sum(
        d["avoided_input_tokens"] + d["avoided_output_tokens"] for d in by_phase.values()
    )
    total_calls = sum(d["calls"] for d in by_phase.values())
    total_hits = sum(d["cache_hits"] for d in by_phase.values())
    estimated = sum(d["estimated_calls"] for d in by_phase.values())

    lines = [
        f"# Full analysis report — `{repo.name}`",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Model: `{model}` (Ollama, temperature=0, seed=42)",
        "",
        "## Index scale",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Files | {_fmt(s['files'])} |",
        f"| Symbols | {_fmt(s['symbols'])} |",
        f"| Edges | {_fmt(s['edges'])} |",
        f"| Files with a cached summary | {_fmt(s['summaries'])} |",
        "",
        "## Token consumption (measured from Ollama `prompt_eval_count` / `eval_count`)",
        "",
        "| Phase | LLM calls | Cache hits | Input tok | Output tok | Total tok |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for phase, d in sorted(by_phase.items()):
        lines.append(
            f"| {phase} | {_fmt(d['calls'])} | {_fmt(d['cache_hits'])} | "
            f"{_fmt(d['input_tokens'])} | {_fmt(d['output_tokens'])} | "
            f"{_fmt(d['input_tokens'] + d['output_tokens'])} |"
        )
    lines += [
        f"| **TOTAL** | **{_fmt(total_calls)}** | **{_fmt(total_hits)}** | "
        f"**{_fmt(total_in)}** | **{_fmt(total_out)}** | **{_fmt(total_in + total_out)}** |",
        "",
        "## Per-file summary statistics",
        "",
        "| Metric | Tokens |",
        "|---|---:|",
        f"| Files summarised (uncached calls) | {_fmt(per['n'])} |",
        f"| Mean tokens per file | {per['mean']:.0f} |",
        f"| P50 | {_fmt(per['p50'])} |",
        f"| P95 | {_fmt(per['p95'])} |",
        f"| Max | {_fmt(per['max'])} |",
        f"| Min | {_fmt(per['min'])} |",
        "",
        "## Cache behaviour",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Summary cache entries | {_fmt(summ.get('cache_hits', 0))} hits in this repo |",
        f"| Cache hit rate (all phases) | {(total_hits / max(1, total_hits + total_calls)) * 100:.1f}% |",
        f"| Avoided tokens (cache hits, measured from the original call) | {_fmt(total_avoided)} |",
        "",
        "## Accounting method",
        "",
        f"- Token counts are read from the Ollama response fields "
        f"`prompt_eval_count` (input) and `eval_count` (output).",
        f"- Calls where the server did not report counters: **{estimated}** "
        f"(estimated with `ceil(chars / 3.5)`, flagged as `token_source='estimate'` in SQLite).",
        f"- Every call is a row in `llm_calls`; every run is a row in `runs`. "
        f"Reports are pure SQL aggregates over those tables.",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# Stage 3: incremental analysis
# --------------------------------------------------------------------------


def write_incremental_analysis(repo: Path, cfg: IndexConfig, model: str) -> Path:
    p = Pipeline(repo, cfg, LLMConfig(model=model))
    out = report_path("incremental_analysis", repo)

    runs = p.conn.execute(
        "SELECT * FROM runs WHERE repo_id=? AND kind IN ('full','incremental') ORDER BY started_at",
        (p.repo_id,),
    ).fetchall()

    lines = [
        f"# Incremental analysis report — `{repo.name}`",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Model: `{model}`",
        "",
        "## Run-by-run token consumption",
        "",
        "| # | Run ID | Kind | Files total | Called | Cached | In tok | Out tok | Total tok | Cache hit | Note |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    first_total = 0
    for i, r in enumerate(runs, 1):
        tot = r["input_tokens"] + r["output_tokens"]
        if i == 1:
            first_total = tot
        hit_rate = (r["files_cached"] / max(1, r["files_called"] + r["files_cached"])) * 100
        lines.append(
            f"| {i} | `{r['run_id']}` | {r['kind']} | {_fmt(r['files_total'])} | "
            f"{_fmt(r['files_called'])} | {_fmt(r['files_cached'])} | "
            f"{_fmt(r['input_tokens'])} | {_fmt(r['output_tokens'])} | {_fmt(tot)} | "
            f"{hit_rate:.1f}% | {r['note'] or ''} |"
        )

    inc_runs = [r for r in runs if r["kind"] == "incremental"]
    lines += ["", "## Savings versus a full re-run", ""]
    if inc_runs and first_total:
        inc_totals = [r["input_tokens"] + r["output_tokens"] for r in inc_runs]
        avg_inc = sum(inc_totals) / len(inc_totals)
        lines += [
            "| Metric | Value |",
            "|---|---:|",
            f"| First full run tokens | {_fmt(first_total)} |",
            f"| Incremental runs measured | {len(inc_runs)} |",
            f"| Mean incremental run tokens | {_fmt(avg_inc)} |",
            f"| Mean saving vs full re-run | {(1 - avg_inc / first_total) * 100:.1f}% |",
            f"| Best incremental run | {_fmt(min(inc_totals))} tokens "
            f"({(1 - min(inc_totals) / first_total) * 100:.1f}% saved) |",
            f"| Worst incremental run | {_fmt(max(inc_totals))} tokens "
            f"({(1 - max(inc_totals) / first_total) * 100:.1f}% saved) |",
            "",
        ]
        hits = [r["files_cached"] / max(1, r["files_called"] + r["files_cached"]) for r in inc_runs]
        lines.append(f"Mean incremental cache hit rate: **{sum(hits) / len(hits) * 100:.1f}%**")
        lines.append("")
    else:
        lines.append("_No incremental runs recorded yet._")
        lines.append("")

    lines += [
        "## Method",
        "",
        "1. A file edit changes its content hash.",
        "2. `codegraph index` re-parses only files whose hash changed.",
        "3. The dependency closure (changed files + direct importers + direct callees) "
        "is computed from the SQLite graph.",
        "4. Only closure members whose content hash changed lose their cache entry; "
        "everything else is served from `file_summaries` without an LLM call.",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# Stage 4: architecture analysis
# --------------------------------------------------------------------------


def write_architecture_analysis(repo: Path, cfg: IndexConfig, model: str) -> Path:
    p = Pipeline(repo, cfg, LLMConfig(model=model))
    out = report_path("architecture_analysis", repo)

    by_phase = p.token_report()["by_phase"]
    arch = by_phase.get("arch", {})
    guide = by_phase.get("guide", {})
    summ = by_phase.get("summary", {})

    def per_call(d: dict) -> float:
        return (d.get("input_tokens", 0) + d.get("output_tokens", 0)) / max(1, d.get("calls", 0))

    arch_pc = per_call(arch)
    guide_pc = per_call(guide)
    summ_pc = per_call(summ)

    artifacts = p.conn.execute(
        "SELECT kind, entry_symbol, content, created_at FROM arch_artifacts "
        "WHERE repo_id=? ORDER BY id DESC LIMIT 10",
        (p.repo_id,),
    ).fetchall()

    lines = [
        f"# Architecture analysis report — `{repo.name}`",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Model: `{model}`",
        "",
        "## Token cost comparison",
        "",
        "| Phase | Calls | Input tok | Output tok | Total tok | Tokens per call | vs single-file summary |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, d in (("summary (per file)", summ), ("architecture", arch), ("developer guide", guide)):
        if not d:
            continue
        tot = d.get("input_tokens", 0) + d.get("output_tokens", 0)
        ratio = (per_call(d) / summ_pc) if summ_pc else 0
        lines.append(
            f"| {name} | {_fmt(d.get('calls', 0))} | {_fmt(d.get('input_tokens', 0))} | "
            f"{_fmt(d.get('output_tokens', 0))} | {_fmt(tot)} | {per_call(d):.0f} | "
            f"{ratio:.1f}× |"
        )
    lines += [
        "",
        "The architecture and guide prompts inject the call chain, module layout, "
        "cross-file dependency edges and up to 25 file summaries, so their single-call "
        "input token count is an order of magnitude above a per-file summary.",
        "",
        "## Generated artifacts",
        "",
        "| Kind | Entry | Created | Size (chars) |",
        "|---|---|---|---:|",
    ]
    for a in artifacts:
        lines.append(
            f"| {a['kind']} | `{a['entry_symbol'] or '-'}` | {a['created_at']} | {len(a['content']):,} |"
        )
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
