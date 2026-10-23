"""Stage 3 driver: incremental analysis on a copy of the test repo.

Modifying files in-place would pollute the pristine index used for the
full-analysis report, so this script:
  1. copies the repo + its SQLite graph to a sibling directory
  2. mutates a deterministic set of files each round (seed=42)
  3. runs incremental index + dependency-closure summarisation
  4. records every round in SQLite

Round 0 (the baseline) is the existing full run already recorded in the copied
DB, so the "full vs incremental" comparison uses the *same* cache state.

Usage:
    python scripts/run_incremental.py [--rounds 5] [--files 3] [--model ...]
"""

from __future__ import annotations

import os

import argparse
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph.config import IndexConfig, LLMConfig  # noqa: E402
from codegraph.db import connect  # noqa: E402
from codegraph.graph import GraphBuilder  # noqa: E402
from codegraph.pipeline import Pipeline  # noqa: E402

SEED = 42
SRC = Path(os.environ.get("CODEGRAPH_REPO", Path(__file__).resolve().parents[1] / "repos" / "ripgrep"))
DST = SRC.with_name(SRC.name + "-incr")


def prepare_copy() -> Path:
    """Copy the repo and its graph DB so the incremental run starts cache-warm."""
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git"))
    # The copied DB still stores the OLD absolute paths. Without retargeting,
    # `ensure_repo` creates a second repo row and every cached summary is
    # invisible -> 0% cache hits. This is the single most important step.
    cfg = IndexConfig(db_path=DST / ".codegraph" / "graph.db")
    conn = connect(cfg.db_path)
    n = GraphBuilder.retarget_repo(conn, DST)
    conn.close()
    print(f"copied {SRC} -> {DST} (retargeted {n} file rows)")
    return DST


def pick_targets(repo: Path, n: int, rng: random.Random) -> list[str]:
    """Choose n source files that already have cached summaries."""
    db = repo / ".codegraph" / "graph.db"
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        """SELECT f.path FROM files f
           JOIN file_summaries s ON s.file_id = f.id
           WHERE f.language IN ('rust','python','typescript','javascript')
           ORDER BY f.path"""
    ).fetchall()
    conn.close()
    paths = [r[0] for r in rows]
    if not paths:
        return []
    return sorted(rng.sample(paths, min(n, len(paths))))


def mutate(path: Path, round_no: int, rng: random.Random) -> None:
    """Append a deterministic comment so the content hash changes but the AST does not."""
    tag = rng.randint(1000, 9999)
    if path.suffix == ".rs":
        marker = f"\n// codegraph incremental round {round_no} tag {tag}\n"
    elif path.suffix in (".ts", ".tsx", ".js", ".jsx"):
        marker = f"\n// codegraph incremental round {round_no} tag {tag}\n"
    else:
        marker = f"\n# codegraph incremental round {round_no} tag {tag}\n"
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(text + marker, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--files", type=int, default=3)
    ap.add_argument("--model", default="qwen2.5-coder:7b")
    ap.add_argument("--reuse", action="store_true", help="reuse the existing copy")
    a = ap.parse_args()

    repo = DST if a.reuse else prepare_copy()
    cfg = IndexConfig(db_path=repo / ".codegraph" / "graph.db")

    # baseline: what a full re-run would cost, from the copied DB
    conn = sqlite3.connect(str(cfg.db_path))
    conn.row_factory = sqlite3.Row
    base = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o
           FROM llm_calls WHERE phase='summary' AND cache_hit=0 AND error IS NULL"""
    ).fetchone()
    base_total = base["i"] + base["o"]
    n_summaries = conn.execute("SELECT COUNT(*) FROM file_summaries").fetchone()[0]
    conn.close()

    print(f"baseline: {n_summaries} cached summaries, "
          f"{base['n']} real calls, {base_total:,} tokens\n")

    rng = random.Random(SEED)
    rounds_data: list[dict] = []

    for r in range(1, a.rounds + 1):
        targets = pick_targets(repo, a.files, rng)
        if not targets:
            print("no summarised files to mutate; aborting")
            return
        for rel in targets:
            mutate(repo / rel, r, rng)

        gb = GraphBuilder(repo, cfg)
        st = gb.index(incremental=True)

        p = Pipeline(repo, cfg, LLMConfig(model=a.model))
        closure = p.qe.dependency_closure(targets)
        # Count cache state across the WHOLE repo, not just the closure: the
        # closure is by construction the changed files, so a "closure hit rate"
        # would always read 0% and understate the real saving.
        total_files = p.conn.execute(
            "SELECT COUNT(*) FROM files WHERE repo_id=?", (p.repo_id,)
        ).fetchone()[0]
        closure_hits = sum(1 for c in closure if p.get_cached_summary(c) is not None)
        run = p.summarize(closure, kind="incremental", verbose=False)
        repo_cache_hits = total_files - run.files_called

        rec = {
            "round": r,
            "changed": targets,
            "closure": len(closure),
            "closure_cached": closure_hits,
            "indexed": st.files_indexed,
            "tokens": run.total_tokens,
            "in": run.input_tokens,
            "out": run.output_tokens,
            "called": run.files_called,
            "cached": run.files_cached,
            "repo_files": total_files,
            "repo_cached": repo_cache_hits,
            "repo_hit_rate": repo_cache_hits / max(1, total_files),
            "elapsed": run.elapsed_s,
            "run_id": run.run_id,
        }
        rounds_data.append(rec)
        print(
            f"round {r}: changed={len(targets)} closure={len(closure)} "
            f"indexed={st.files_indexed} | "
            f"LLM calls={run.files_called} | "
            f"repo cache hits={repo_cache_hits}/{total_files} "
            f"({rec['repo_hit_rate'] * 100:.1f}%) | "
            f"tok={run.total_tokens:,} ({run.input_tokens:,} in / {run.output_tokens:,} out) "
            f"| {run.elapsed_s:.0f}s"
        )
        time.sleep(0.5)

    # summary table
    print("\n" + "=" * 78)
    print(f"baseline full analysis: {base_total:,} tokens")
    if rounds_data:
        totals = [x["tokens"] for x in rounds_data]
        avg = sum(totals) / len(totals)
        print(f"incremental rounds: {len(totals)}")
        print(f"  mean  = {avg:,.0f} tokens  ({(1 - avg / base_total) * 100:.1f}% saved vs full)")
        print(f"  min   = {min(totals):,}  ({(1 - min(totals) / base_total) * 100:.1f}% saved)")
        print(f"  max   = {max(totals):,}  ({(1 - max(totals) / base_total) * 100:.1f}% saved)")
        rates = [x["repo_hit_rate"] for x in rounds_data]
        print(f"  mean repo cache hit rate = {sum(rates) / len(rates) * 100:.1f}%")
        print(f"  total incremental tokens = {sum(totals):,}")

    out = Path(__file__).resolve().parents[1] / "reports" / "incremental_rounds.json"
    out.write_text(
        __import__("json").dumps(
            {"baseline_tokens": base_total, "rounds": rounds_data}, indent=2
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
