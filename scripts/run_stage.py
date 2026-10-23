"""Stage driver: run the full pipeline and write every report.

Usage:
    python scripts/run_stage.py index   <repo>
    python scripts/run_stage.py summary <repo> [--limit N]
    python scripts/run_stage.py arch    <repo> [--entry main]
    python scripts/run_stage.py guide   <repo> [--entry main]
    python scripts/run_stage.py review  <repo> --files a.py,b.py
    python scripts/run_stage.py reports <repo>

All numbers printed here come from SQLite, never from the script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph import reports as R  # noqa: E402
from codegraph.config import IndexConfig, LLMConfig  # noqa: E402
from codegraph.graph import GraphBuilder  # noqa: E402
from codegraph.pipeline import Pipeline  # noqa: E402


def cfg_for(repo: Path) -> IndexConfig:
    return IndexConfig(db_path=repo / ".codegraph" / "graph.db")


def cmd_index(repo: Path, full: bool, model: str) -> None:
    gb = GraphBuilder(repo, cfg_for(repo))
    t0 = time.perf_counter()
    st = gb.index(incremental=not full)
    s = gb.stats()
    print(json.dumps(s, indent=2, default=str))
    print(
        f"\nrun: {st.files_indexed} parsed, {st.files_skipped_unchanged} unchanged, "
        f"{st.files_removed} removed in {st.elapsed_s:.2f}s"
    )
    print(f"parse errors: {st.parse_errors}  dynamic calls: {st.dynamic_calls}/{st.total_calls}")
    out = R.write_index_stats(repo, cfg_for(repo), extra={
        "Parse time (s)": f"{st.elapsed_s:.2f}",
        "Files parsed this run": st.files_indexed,
        "Files unchanged": st.files_skipped_unchanged,
        "Statically unresolvable call sites (parser level)": f"{st.dynamic_calls} / {st.total_calls}",
    })
    print(f"wrote {out}")


def cmd_summary(repo: Path, limit: int | None, model: str, force: bool, files: list[str] | None = None) -> None:
    p = Pipeline(repo, cfg_for(repo), LLMConfig(model=model))
    t0 = time.perf_counter()
    run = p.summarize(files, limit=limit, force=force, kind="full")
    print(json.dumps({
        "run_id": run.run_id,
        "files_total": run.files_total,
        "files_called": run.files_called,
        "files_cached": run.files_cached,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "avoided_tokens": run.avoided_input_tokens + run.avoided_output_tokens,
        "cache_hit_rate": round(run.cache_hit_rate, 4),
        "errors": run.errors,
        "elapsed_s": round(run.elapsed_s, 1),
    }, indent=2))
    out = R.write_full_analysis(repo, cfg_for(repo), model)
    print(f"wrote {out}  (wall {time.perf_counter() - t0:.0f}s)")


def cmd_arch(repo: Path, entry: str | None, model: str) -> None:
    p = Pipeline(repo, cfg_for(repo), LLMConfig(model=model))
    run, md = p.analyze_architecture(entry)
    from codegraph.config import DEFAULT_OUTPUT

    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    dest = DEFAULT_OUTPUT / "architecture.md"
    dest.write_text(md, encoding="utf-8")
    print(json.dumps({
        "run_id": run.run_id,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "elapsed_s": round(run.elapsed_s, 1),
        "chars": len(md),
    }, indent=2))
    print(f"wrote {dest}")
    out = R.write_architecture_analysis(repo, cfg_for(repo), model)
    print(f"wrote {out}")


def cmd_guide(repo: Path, entry: str | None, model: str) -> None:
    p = Pipeline(repo, cfg_for(repo), LLMConfig(model=model))
    arch_run, arch_md = p.analyze_architecture(entry, verbose=False)
    run, md = p.generate_guide(arch_md)
    from codegraph.config import DEFAULT_OUTPUT

    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    (DEFAULT_OUTPUT / "architecture.md").write_text(arch_md, encoding="utf-8")
    dest = DEFAULT_OUTPUT / "developer_guide.md"
    dest.write_text(md, encoding="utf-8")
    print(json.dumps({
        "arch_run_id": arch_run.run_id, "arch_tokens": arch_run.total_tokens,
        "guide_run_id": run.run_id, "guide_tokens": run.total_tokens,
        "chars": len(md),
    }, indent=2))
    print(f"wrote {dest}")


def cmd_review(repo: Path, files: list[str], model: str) -> None:
    p = Pipeline(repo, cfg_for(repo), LLMConfig(model=model))
    run, findings = p.review(files)
    from codegraph.config import DEFAULT_OUTPUT

    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    dest = DEFAULT_OUTPUT / "review.json"
    dest.write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "run_id": run.run_id, "files": len(files),
        "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
        "findings": len(findings),
    }, indent=2))
    print(f"wrote {dest}")


def cmd_reports(repo: Path, model: str) -> None:
    cfg = cfg_for(repo)
    for out in (
        R.write_index_stats(repo, cfg),
        R.write_full_analysis(repo, cfg, model),
        R.write_incremental_analysis(repo, cfg, model),
        R.write_architecture_analysis(repo, cfg, model),
    ):
        print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["index", "summary", "arch", "guide", "review", "reports"])
    ap.add_argument("repo")
    ap.add_argument("--model", default="qwen2.5-coder:7b")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--entry", default=None)
    ap.add_argument("--files", default=None)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    if a.stage == "index":
        cmd_index(repo, a.full, a.model)
    elif a.stage == "summary":
        files = [f.strip() for f in (a.files or "").split(",") if f.strip()] or None
        cmd_summary(repo, a.limit, a.model, a.force, files)
    elif a.stage == "arch":
        cmd_arch(repo, a.entry, a.model)
    elif a.stage == "guide":
        cmd_guide(repo, a.entry, a.model)
    elif a.stage == "review":
        files = [f.strip() for f in (a.files or "").split(",") if f.strip()]
        cmd_review(repo, files, a.model)
    else:
        cmd_reports(repo, a.model)


if __name__ == "__main__":
    main()
