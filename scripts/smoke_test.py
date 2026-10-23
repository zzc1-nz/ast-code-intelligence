"""Smoke-test the whole engine without a real LLM (uses a scripted fake client).

Verifies: index -> prompt build -> summary -> cache -> arch -> guide -> reports.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph import reports as R  # noqa: E402
from codegraph.config import IndexConfig, LLMConfig  # noqa: E402
from codegraph.graph import GraphBuilder  # noqa: E402
from codegraph.llm import LLMResult  # noqa: E402
from codegraph.pipeline import Pipeline  # noqa: E402


class ScriptedClient:
    def __init__(self):
        self.cfg = LLMConfig(model="scripted")
        self.model = "scripted"
        self.n = 0

    def generate(self, prompt, system=None, *, num_predict=None, num_ctx=None, json_mode=False):
        self.n += 1
        in_tok = max(1, int(len(prompt) / 3.5))
        if json_mode:
            text = json.dumps({
                "responsibility": "scripted responsibility",
                "key_functions": ["f: does f"],
                "public_interface": ["f"],
                "design_notes": [],
                "risk_flags": [],
            })
        else:
            text = "# Architecture\n\nscripted analysis\n"
        return LLMResult(
            text=text, input_tokens=in_tok, output_tokens=60,
            token_source="server", latency_ms=1, model="scripted",
            prompt_chars=len(prompt),
        )


def main() -> None:
    repo = Path(tempfile.mkdtemp(prefix="cg_smoke_"))
    try:
        src = Path(__file__).resolve().parents[1] / "src" / "codegraph"
        for f in src.glob("*.py"):
            shutil.copy(f, repo / f.name)

        cfg = IndexConfig(db_path=repo / ".codegraph" / "graph.db")
        gb = GraphBuilder(repo, cfg)
        st = gb.index(incremental=False)
        s = gb.stats()
        print(f"index: {s['files']} files, {s['symbols']} symbols, {s['edges']} edges "
              f"({st.elapsed_s:.2f}s)")

        client = ScriptedClient()
        p = Pipeline(repo, cfg, LLMConfig(model="scripted"), client=client)

        run = p.summarize()
        print(f"summary run 1: called={run.files_called} cached={run.files_cached} "
              f"tok={run.total_tokens:,}")

        run2 = p.summarize()
        print(f"summary run 2: called={run2.files_called} cached={run2.files_cached} "
              f"tok={run2.total_tokens:,} avoided={run2.avoided_input_tokens + run2.avoided_output_tokens:,}")

        arch_run, arch_md = p.analyze_architecture()
        print(f"arch: {arch_run.total_tokens:,} tok, {len(arch_md)} chars")
        guide_run, guide_md = p.generate_guide(arch_md)
        print(f"guide: {guide_run.total_tokens:,} tok, {len(guide_md)} chars")

        rep = p.token_report()
        for phase, d in rep["by_phase"].items():
            print(f"  {phase:12s} calls={d['calls']:3d} cache={d['cache_hits']:3d} "
                  f"in={d['input_tokens']:7,} out={d['output_tokens']:6,}")

        for out in (
            R.write_index_stats(repo, cfg),
            R.write_full_analysis(repo, cfg, "scripted"),
            R.write_incremental_analysis(repo, cfg, "scripted"),
            R.write_architecture_analysis(repo, cfg, "scripted"),
        ):
            print(f"  wrote {out.name} ({out.stat().st_size:,} bytes)")

        print("SMOKE OK")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    main()
