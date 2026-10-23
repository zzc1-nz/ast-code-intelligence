"""Git-diff driven incremental analysis.

Reads changed files straight from git (so this works on a real developer
workflow, not just simulated edits), re-indexes them, and re-summarises only the
dependency closure.

Usage:
    python scripts/incremental_from_git.py <repo> [--rev HEAD~1] [--model ...]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph.config import IndexConfig, LLMConfig  # noqa: E402
from codegraph.graph import GraphBuilder  # noqa: E402
from codegraph.pipeline import Pipeline  # noqa: E402

SOURCE_EXTS = {".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


def git_changed_files(repo: Path, rev: str) -> list[str]:
    """Repo-relative paths changed between `rev` and the working tree."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", rev],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"git diff failed: {exc}", file=sys.stderr)
        return []
    files = []
    for line in out.splitlines():
        rel = line.strip()
        if rel and Path(rel).suffix.lower() in SOURCE_EXTS:
            files.append(rel)
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--rev", default="HEAD~1", help="git revision to diff against")
    ap.add_argument("--model", default="qwen2.5-coder:7b")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    cfg = IndexConfig(db_path=repo / ".codegraph" / "graph.db")

    changed = git_changed_files(repo, a.rev)
    print(f"changed files vs {a.rev}: {len(changed)}")
    for c in changed[:20]:
        print(f"  {c}")
    if not changed:
        print("nothing to do")
        return

    gb = GraphBuilder(repo, cfg)
    st = gb.index(incremental=True)
    print(f"index: {st.files_indexed} parsed, {st.files_skipped_unchanged} unchanged")

    p = Pipeline(repo, cfg, LLMConfig(model=a.model))
    closure = p.qe.dependency_closure(changed)
    print(f"dependency closure: {len(closure)} files")
    for c in closure:
        print(f"  {c}")

    run = p.summarize(closure, kind="incremental", verbose=True)
    print(
        f"\ntokens: {run.input_tokens:,} in / {run.output_tokens:,} out = {run.total_tokens:,}\n"
        f"called={run.files_called} cached={run.files_cached} "
        f"cache_hit={run.cache_hit_rate * 100:.1f}% elapsed={run.elapsed_s:.1f}s"
    )


if __name__ == "__main__":
    main()
