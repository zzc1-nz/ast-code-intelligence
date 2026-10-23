"""Simulate incremental development: modify files, re-index, re-summarise.

Used to produce the incremental-analysis numbers. Each round:
  1. mutate a small set of files (deterministic, seed=42)
  2. incremental index (only changed files re-parsed)
  3. summarise the dependency closure (cache serves the rest)
  4. record the run

The mutations are *append-only comments/docstrings* so the code semantics stay
identical and the graph stays valid — the point is to measure token cost, not to
break the repo.
"""

from __future__ import annotations

import os

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph.config import IndexConfig, LLMConfig  # noqa: E402
from codegraph.graph import GraphBuilder  # noqa: E402
from codegraph.pipeline import Pipeline  # noqa: E402

SEED = 42


def pick_targets(repo: Path, n: int, rng: random.Random) -> list[str]:
    """Choose n Python/Rust source files that already have symbols."""
    cands = []
    for ext in (".py", ".rs"):
        cands += [p for p in repo.rglob(f"*{ext}") if ".codegraph" not in p.parts]
    cands.sort()
    if not cands:
        return []
    return [p.relative_to(repo).as_posix() for p in rng.sample(cands, min(n, len(cands)))]


def mutate(path: Path, round_no: int, rng: random.Random) -> None:
    """Append a deterministic marker comment (keeps AST structure valid)."""
    marker = f"\n# codegraph-incremental-round-{round_no}-{rng.randint(1000, 9999)}\n"
    if path.suffix == ".rs":
        marker = f"\n// codegraph-incremental-round-{round_no}-{rng.randint(1000, 9999)}\n"
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(text + marker, encoding="utf-8")


def main() -> None:
    repo = Path(sys.argv[1] if len(sys.argv) > 1
                else os.environ.get("CODEGRAPH_REPO",
                                   Path(__file__).resolve().parents[1] / "repos" / "ripgrep"))
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    per_round = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    model = sys.argv[4] if len(sys.argv) > 4 else "qwen2.5-coder:7b"

    rng = random.Random(SEED)
    cfg = IndexConfig(db_path=repo / ".codegraph" / "graph.db")
    p = Pipeline(repo, cfg, LLMConfig(model=model))

    print(f"repo={repo} rounds={rounds} files/round={per_round} model={model}")
    for r in range(1, rounds + 1):
        targets = pick_targets(repo, per_round, rng)
        for rel in targets:
            mutate(repo / rel, r, rng)

        gb = GraphBuilder(repo, cfg)
        st = gb.index(incremental=True)
        print(
            f"\n=== round {r} === changed={targets}\n"
            f"  index: {st.files_indexed} parsed, {st.files_skipped_unchanged} unchanged, "
            f"{st.files_removed} removed in {st.elapsed_s:.2f}s"
        )

        p2 = Pipeline(repo, cfg, LLMConfig(model=model))
        closure = p2.qe.dependency_closure(targets)
        print(f"  dependency closure: {len(closure)} files (changed={len(targets)})")

        run = p2.summarize(closure, kind="incremental", verbose=False)
        print(
            f"  tokens: {run.input_tokens:,} in / {run.output_tokens:,} out "
            f"= {run.total_tokens:,} total | called={run.files_called} cached={run.files_cached} "
            f"| cache hit {run.cache_hit_rate * 100:.1f}% | {run.elapsed_s:.1f}s"
        )
        time.sleep(0.5)


if __name__ == "__main__":
    main()
