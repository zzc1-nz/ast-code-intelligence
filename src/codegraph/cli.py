"""Typer CLI for codegraph."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    DEFAULT_OUTPUT,
    DEFAULT_REPORTS,
    IndexConfig,
    LLMConfig,
    ensure_dirs,
)
from .graph import GraphBuilder
from .pipeline import Pipeline
from .query import QueryEngine

app = typer.Typer(
    add_completion=False,
    help="AST-driven local code intelligence engine (Tree-sitter + SQLite + Ollama).",
    no_args_is_help=True,
)
console = Console()


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------
# index
# --------------------------------------------------------------------------


@app.command()
def index(
    repo: Path = typer.Argument(..., help="Path to the repository to index"),
    incremental: bool = typer.Option(True, "--incremental/--full", help="Incremental or full reindex"),
    db: Optional[Path] = typer.Option(None, help="SQLite database path"),
    json_out: bool = typer.Option(False, "--json", help="Print stats as JSON"),
):
    """Parse a repository with Tree-sitter and build the SQLite knowledge graph."""
    ensure_dirs()
    repo = repo.resolve()
    cfg = IndexConfig(db_path=db or (repo / ".codegraph" / "graph.db"))
    console.print(f"[bold]Indexing[/bold] {repo}  (incremental={incremental})")
    t0 = time.perf_counter()
    gb = GraphBuilder(repo, cfg)
    stats = gb.index(incremental=incremental)
    gb_stats = gb.stats()

    if json_out:
        console.print_json(json.dumps(gb_stats, indent=2, default=str))
        return

    tbl = Table(title="Index statistics", show_header=False)
    tbl.add_row("Repo", str(repo))
    tbl.add_row("DB", f"{cfg.db_path} ({_human_bytes(gb_stats['db_bytes'])})")
    tbl.add_row("Files", f"{gb_stats['files']:,}")
    tbl.add_row("Symbols", f"{gb_stats['symbols']:,}")
    tbl.add_row("Edges (total)", f"{gb_stats['edges']:,}")
    tbl.add_row("  call", f"{gb_stats['call_edges']:,}")
    tbl.add_row("  import", f"{gb_stats['import_edges']:,}")
    tbl.add_row("  inherit/implements", f"{gb_stats['inherit_edges']:,}")
    resolved = gb_stats["resolved_edges"]
    total = max(1, gb_stats["edges"])
    tbl.add_row("Resolved edges", f"{resolved:,} ({resolved / total * 100:.1f}%)")
    tbl.add_row("Unresolved edges", f"{gb_stats['unresolved_edges']:,}")
    tbl.add_row("Parse errors", f"{gb_stats['parse_errors']}")
    tbl.add_row("Languages", ", ".join(f"{k}:{v}" for k, v in gb_stats["by_language"].items()))
    tbl.add_row("Symbol kinds", ", ".join(f"{k}:{v}" for k, v in sorted(gb_stats["by_kind"].items(), key=lambda x: -x[1])))
    tbl.add_row("This run", f"{stats.files_indexed} parsed, {stats.files_skipped_unchanged} unchanged, "
                              f"{stats.files_removed} removed in {stats.elapsed_s:.2f}s")
    console.print(tbl)


# --------------------------------------------------------------------------
# search / trace
# --------------------------------------------------------------------------


def _qe(repo: Path, db: Optional[Path]) -> QueryEngine:
    repo = repo.resolve()
    cfg = IndexConfig(db_path=db or (repo / ".codegraph" / "graph.db"))
    return QueryEngine(repo, cfg)


@app.command()
def search(
    query: str = typer.Argument(..., help="Symbol name / FTS query"),
    repo: Path = typer.Option(Path("."), help="Repository root"),
    limit: int = typer.Option(15, help="Max results"),
    kind: Optional[str] = typer.Option(None, help="Filter by symbol kind"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Full-text symbol search (SQLite FTS5)."""
    qe = _qe(repo, None)
    rows = qe.search(query, limit=limit, kind=kind)
    if json_out:
        console.print_json(json.dumps([r.as_dict() for r in rows], indent=2))
        return
    if not rows:
        console.print("[yellow]no matches[/yellow]")
        return
    tbl = Table(title=f"Symbols matching {query!r}")
    tbl.add_column("Kind")
    tbl.add_column("Qualname")
    tbl.add_column("Location")
    tbl.add_column("Signature", overflow="fold")
    for r in rows:
        tbl.add_row(r.kind, r.qualname, f"{r.path}:{r.start_line}", (r.signature or "")[:90])
    console.print(tbl)


@app.command()
def trace(
    symbol: str = typer.Argument(..., help="Symbol name to trace"),
    repo: Path = typer.Option(Path("."), help="Repository root"),
    direction: str = typer.Option("out", "--dir", help="out|in|both"),
    depth: int = typer.Option(3, help="Max hops"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Show call chain / callers / callees for a symbol."""
    qe = _qe(repo, None)
    cands = qe.search(symbol, limit=5)
    if not cands:
        console.print(f"[red]symbol not found: {symbol}[/red]")
        raise typer.Exit(1)
    target = cands[0]
    console.print(f"[bold]{target.qualname}[/bold]  {target.kind}  {target.path}:{target.start_line}-{target.end_line}")
    if target.signature:
        console.print(f"  [dim]{target.signature}[/dim]")

    if direction in ("out", "both"):
        chain = qe.call_chain(target.id, max_depth=depth)
        tbl = Table(title="Callees (transitive)")
        tbl.add_column("Depth")
        tbl.add_column("Symbol")
        tbl.add_column("Location")
        for step in chain[1:]:
            s = step["symbol"]
            tbl.add_row(str(step["depth"]), s["qualname"], f"{s['path']}:{s['start_line']}")
        console.print(tbl)

    if direction in ("in", "both"):
        rc = qe.reverse_chain(target.id, max_depth=depth)
        tbl = Table(title="Callers (transitive)")
        tbl.add_column("Depth")
        tbl.add_column("Symbol")
        tbl.add_column("Location")
        for step in rc[1:]:
            s = step["symbol"]
            tbl.add_row(str(step["depth"]), s["qualname"], f"{s['path']}:{s['start_line']}")
        console.print(tbl)

    unresolved = qe.unresolved_calls(target.id)
    if unresolved:
        console.print(f"[dim]unresolved dynamic calls: {', '.join(u['name'] for u in unresolved[:10])}[/dim]")


# --------------------------------------------------------------------------
# summary / analyze / guide / review
# --------------------------------------------------------------------------


@app.command()
def summary(
    repo: Path = typer.Option(Path("."), help="Repository root"),
    files: Optional[str] = typer.Option(None, help="Comma-separated repo-relative paths"),
    force: bool = typer.Option(False, help="Ignore cache"),
    limit: Optional[int] = typer.Option(None, help="Max files to process"),
    model: str = typer.Option("qwen2.5-coder:7b", help="Ollama model"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Generate per-file LLM summaries (cached by content hash + prompt version)."""
    p = _pipeline(repo, model)
    paths = [f.strip() for f in files.split(",")] if files else None
    run = p.summarize(paths, kind="full", force=force, limit=limit)
    _print_run(run)


@app.command()
def analyze(
    repo: Path = typer.Option(Path("."), help="Repository root"),
    entry: Optional[str] = typer.Option(None, help="Entry symbol name"),
    model: str = typer.Option("qwen2.5-coder:7b", help="Ollama model"),
    out: Optional[Path] = typer.Option(None, help="Write Markdown to this path"),
):
    """Cross-file architecture analysis from the call graph."""
    p = _pipeline(repo, model)
    run, md = p.analyze_architecture(entry)
    _print_run(run)
    dest = out or (DEFAULT_OUTPUT / "architecture.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md, encoding="utf-8")
    console.print(f"[green]wrote[/green] {dest}")


@app.command()
def guide(
    repo: Path = typer.Option(Path("."), help="Repository root"),
    entry: Optional[str] = typer.Option(None, help="Entry symbol name"),
    model: str = typer.Option("qwen2.5-coder:7b", help="Ollama model"),
    out: Optional[Path] = typer.Option(None, help="Write Markdown to this path"),
):
    """Generate a Markdown developer guide (architecture -> guide)."""
    p = _pipeline(repo, model)
    arch_run, arch_md = p.analyze_architecture(entry, verbose=False)
    run, md = p.generate_guide(arch_md)
    _print_run(arch_run, label="architecture")
    _print_run(run, label="guide")
    dest = out or (DEFAULT_OUTPUT / "developer_guide.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md, encoding="utf-8")
    console.print(f"[green]wrote[/green] {dest}")


@app.command()
def review(
    repo: Path = typer.Option(Path("."), help="Repository root"),
    files: str = typer.Option(..., help="Comma-separated repo-relative paths"),
    model: str = typer.Option("qwen2.5-coder:7b", help="Ollama model"),
    out: Optional[Path] = typer.Option(None, help="Write JSON findings to this path"),
):
    """AST-grounded code review for selected files."""
    p = _pipeline(repo, model)
    paths = [f.strip() for f in files.split(",") if f.strip()]
    run, findings = p.review(paths)
    _print_run(run)
    dest = out or (DEFAULT_OUTPUT / "review.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"[green]wrote[/green] {dest} ({len(findings)} findings)")


# --------------------------------------------------------------------------
# stats / token report
# --------------------------------------------------------------------------


@app.command()
def stats(
    repo: Path = typer.Option(Path("."), help="Repository root"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Index + token statistics for a repository."""
    p = _pipeline(repo, "qwen2.5-coder:7b")
    s = p.qe.stats()
    rep = p.token_report()
    if json_out:
        console.print_json(json.dumps({"stats": s, "tokens": rep}, indent=2, default=str))
        return
    tbl = Table(title="Repository", show_header=False)
    for k in ("files", "symbols", "edges", "call_edges", "import_edges",
              "inherit_edges", "resolved_edges", "unresolved_edges", "summaries", "parse_errors"):
        tbl.add_row(k.replace("_", " "), f"{s.get(k, 0):,}")
    tbl.add_row("DB size", _human_bytes(s.get("db_bytes", 0)))
    console.print(tbl)

    t2 = Table(title="Token consumption (from llm_calls)")
    t2.add_column("Phase")
    t2.add_column("Calls", justify="right")
    t2.add_column("Cache hits", justify="right")
    t2.add_column("Input tok", justify="right")
    t2.add_column("Output tok", justify="right")
    t2.add_column("Avoided tok", justify="right")
    for phase, d in rep["by_phase"].items():
        t2.add_row(
            phase,
            f"{d['calls']:,}",
            f"{d['cache_hits']:,}",
            f"{d['input_tokens']:,}",
            f"{d['output_tokens']:,}",
            f"{d['avoided_input_tokens'] + d['avoided_output_tokens']:,}",
        )
    console.print(t2)

    sp = rep["summary_per_file"]
    if sp["n"]:
        console.print(
            f"per-file summary tokens: n={sp['n']} mean={sp['mean']:.0f} "
            f"p50={sp['p50']} p95={sp['p95']} max={sp['max']}"
        )


@app.command()
def doctor(
    model: str = typer.Option("qwen2.5-coder:7b", help="Ollama model to check"),
):
    """Check Ollama connectivity, model availability and run a warmup call."""
    from .llm import OllamaClient

    cfg = LLMConfig(model=model)
    c = OllamaClient(cfg)
    console.print(f"Ollama base: {cfg.normalized_base()}")
    if not c.is_up():
        console.print("[red]Ollama server not reachable[/red]")
        raise typer.Exit(1)
    console.print("[green]server up[/green]")
    models = c.list_models()
    console.print(f"models: {[m.get('name') for m in models]}")
    ok, msg = c.warmup()
    console.print(("[green]warmup ok: " + msg + "[/green]") if ok else f"[red]warmup failed: {msg}[/red]")
    if not ok:
        raise typer.Exit(1)


@app.command()
def version():
    """Print version."""
    console.print(f"codegraph {__version__}")


@app.command()
def report(
    repo: Path = typer.Option(Path("."), help="Repository root"),
    which: str = typer.Option("all", help="all|index|full|incremental|arch"),
    model: str = typer.Option("qwen2.5-coder:7b", help="Ollama model"),
):
    """Regenerate Markdown reports from live SQLite data (no LLM calls)."""
    from . import reports as R

    repo = repo.resolve()
    cfg = IndexConfig(db_path=repo / ".codegraph" / "graph.db")
    written = []
    if which in ("all", "index"):
        written.append(R.write_index_stats(repo, cfg))
    if which in ("all", "full"):
        written.append(R.write_full_analysis(repo, cfg, model))
    if which in ("all", "incremental"):
        written.append(R.write_incremental_analysis(repo, cfg, model))
    if which in ("all", "arch"):
        written.append(R.write_architecture_analysis(repo, cfg, model))
    for p in written:
        console.print(f"[green]wrote[/green] {p}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8765, help="Bind port"),
):
    """Start the FastAPI server + web dashboard."""
    from .api import serve as _serve

    console.print(f"[bold]codegraph[/bold] dashboard: http://{host}:{port}/")
    _serve(host, port)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _pipeline(repo: Path, model: str) -> Pipeline:
    ensure_dirs()
    repo = repo.resolve()
    cfg = IndexConfig(db_path=repo / ".codegraph" / "graph.db")
    return Pipeline(repo, cfg, LLMConfig(model=model))


def _print_run(run, label: str = "summary") -> None:
    tbl = Table(title=f"{label} run {run.run_id}", show_header=False)
    tbl.add_row("Files", f"{run.files_total} total / {run.files_called} called / {run.files_cached} cached")
    tbl.add_row("Input tokens", f"{run.input_tokens:,}")
    tbl.add_row("Output tokens", f"{run.output_tokens:,}")
    tbl.add_row("Total tokens", f"{run.total_tokens:,}")
    tbl.add_row("Cache hit rate", f"{run.cache_hit_rate * 100:.1f}%")
    tbl.add_row("Avoided tokens", f"{run.avoided_input_tokens + run.avoided_output_tokens:,}")
    tbl.add_row("Errors", str(run.errors))
    tbl.add_row("Elapsed", f"{run.elapsed_s:.1f}s")
    console.print(tbl)


if __name__ == "__main__":
    app()
