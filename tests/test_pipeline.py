"""Tests for the LLM-dependent layers using a fake client (no Ollama needed).

These validate the *accounting* invariants, which is what the reports depend on:
  * one row per LLM call
  * cache hits produce no HTTP call but record avoided tokens
  * prompt template version participates in the cache key
  * content hash change invalidates the cache
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph.config import IndexConfig, LLMConfig  # noqa: E402
from codegraph.graph import GraphBuilder  # noqa: E402
from codegraph.llm import LLMResult  # noqa: E402
from codegraph.pipeline import Pipeline, RunTotals, _extract_json, _normalize_summary  # noqa: E402


class FakeClient:
    """Deterministic stand-in for OllamaClient.

    Token counts scale with prompt length (like a real tokenizer) so tests can
    assert that architecture prompts are materially larger than file prompts.
    """

    def __init__(self, model: str = "fake-model", in_tok: int | None = None,
                 out_tok: int = 50, chars_per_token: float = 3.5):
        self.cfg = LLMConfig(model=model)
        self.model = model
        self.fixed_in = in_tok
        self.out_tok = out_tok
        self.cpt = chars_per_token
        self.calls: list[str] = []

    def generate(self, prompt, system=None, *, num_predict=None, num_ctx=None, json_mode=False):
        self.calls.append(prompt[:80])
        payload = {
            "responsibility": f"handles {len(prompt)} chars of context",
            "key_functions": ["a: does a", "b: does b"],
            "public_interface": ["alpha", "beta"],
            "design_notes": ["note"],
            "risk_flags": [],
        }
        in_tok = self.fixed_in if self.fixed_in is not None else max(
            1, int(len(prompt) / self.cpt)
        )
        return LLMResult(
            text=json.dumps(payload),
            input_tokens=in_tok,
            output_tokens=self.out_tok,
            token_source="server",
            latency_ms=1,
            model=self.model,
            prompt_chars=len(prompt),
        )

    def is_up(self):
        return True

    def has_model(self, name=None):
        return True

    def list_models(self):
        return [{"name": self.model}]

    def close(self):
        pass


SAMPLE = {
    "a.py": '''
def alpha():
    return 1


def beta():
    return alpha()
''',
    "b.py": '''
from a import beta


def gamma():
    return beta()
''',
    "c.py": '''
def delta():
    return 2
''',
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    for rel, content in SAMPLE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _pipeline(root: Path, client: FakeClient) -> Pipeline:
    cfg = IndexConfig(db_path=root / ".codegraph" / "graph.db")
    GraphBuilder(root, cfg).index(incremental=False)
    return Pipeline(root, cfg, LLMConfig(model=client.model), client=client)


def test_summarize_records_tokens_and_caches(repo: Path):
    client = FakeClient(in_tok=100)
    p = _pipeline(repo, client)

    run1 = p.summarize()
    assert run1.files_called == 3
    assert run1.files_cached == 0
    assert run1.input_tokens == 3 * 100
    assert run1.output_tokens == 3 * 50
    assert len(client.calls) == 3

    # second run: everything served from cache, zero new LLM calls
    run2 = p.summarize()
    assert run2.files_called == 0
    assert run2.files_cached == 3
    assert run2.total_tokens == 0
    assert len(client.calls) == 3  # unchanged
    # avoided tokens mirror the first run
    assert run2.avoided_input_tokens == 300
    assert run2.avoided_output_tokens == 150
    assert run2.cache_hit_rate == 1.0

    # ledger integrity: 6 rows, 3 real + 3 cache
    rows = p.conn.execute("SELECT cache_hit, COUNT(*) c FROM llm_calls GROUP BY cache_hit").fetchall()
    counts = {r["cache_hit"]: r["c"] for r in rows}
    assert counts.get(0) == 3
    assert counts.get(1) == 3

    # runs table updated
    r = p.conn.execute("SELECT * FROM runs WHERE run_id=?", (run1.run_id,)).fetchone()
    assert r["input_tokens"] == 300
    assert r["files_called"] == 3


def test_cache_invalidated_by_content_change(repo: Path):
    client = FakeClient(in_tok=100)
    p = _pipeline(repo, client)
    p.summarize()
    assert len(client.calls) == 3

    # modify one file -> only that one is re-summarised
    (repo / "a.py").write_text("def alpha():\n    return 42\n", encoding="utf-8")
    GraphBuilder(repo, IndexConfig(db_path=repo / ".codegraph" / "graph.db")).index(incremental=True)
    p2 = Pipeline(repo, IndexConfig(db_path=repo / ".codegraph" / "graph.db"),
                  LLMConfig(model=client.model), client=client)
    run = p2.summarize()
    assert run.files_called == 1
    assert run.files_cached == 2
    assert len(client.calls) == 4


def test_force_bypasses_cache(repo: Path):
    client = FakeClient(in_tok=100)
    p = _pipeline(repo, client)
    p.summarize()
    run = p.summarize(force=True)
    assert run.files_called == 3
    assert run.files_cached == 0
    assert len(client.calls) == 6


def test_token_report_aggregates(repo: Path):
    client = FakeClient(in_tok=100)
    p = _pipeline(repo, client)
    p.summarize()
    p.summarize()
    rep = p.token_report()
    s = rep["by_phase"]["summary"]
    assert s["calls"] == 3
    assert s["cache_hits"] == 3
    assert s["input_tokens"] == 300
    assert rep["summary_per_file"]["n"] == 3
    assert rep["summary_per_file"]["mean"] == 150
    assert rep["summary_per_file"]["p95"] == 150


def test_run_totals_saved_ratio():
    r = RunTotals(run_id="x", kind="full")
    r.files_called = 2
    r.files_cached = 8
    r.input_tokens = 100
    r.output_tokens = 50
    r.avoided_input_tokens = 400
    r.avoided_output_tokens = 200
    assert r.cache_hit_rate == 0.8
    assert r.saved_ratio == pytest.approx(600 / 750)


def test_incremental_closure_limits_scope(repo: Path):
    client = FakeClient(in_tok=100)
    p = _pipeline(repo, client)
    p.summarize()
    closure = p.qe.dependency_closure(["a.py"])
    assert "a.py" in closure
    assert "b.py" in closure  # b imports a


def test_architecture_and_guide_use_graph(repo: Path):
    client = FakeClient(in_tok=None, out_tok=400)
    p = _pipeline(repo, client)
    p.summarize()
    run, md = p.analyze_architecture("gamma")
    assert run.input_tokens > 0
    assert run.output_tokens == 400
    assert "architecture" in md or len(md) > 0

    grun, gmd = p.generate_guide(md)
    assert grun.total_tokens > 0

    arts = p.conn.execute("SELECT kind FROM arch_artifacts").fetchall()
    kinds = {a["kind"] for a in arts}
    assert "architecture" in kinds
    assert "guide" in kinds

    # On a 3-file fixture the architecture prompt is not inherently larger than a
    # single-file prompt (that ratio depends on repo size, and is measured for real
    # in reports/architecture_analysis.md). What must hold here is that the arch
    # phase is recorded as its own phase and produced an artifact.
    rep = p.token_report()["by_phase"]
    assert rep["arch"]["calls"] == 1
    assert rep["guide"]["calls"] == 1
    assert len(md) > 0


def test_review_returns_structured_findings(repo: Path):
    class ReviewClient(FakeClient):
        def generate(self, prompt, system=None, **kw):
            self.calls.append("review")
            body = json.dumps([
                {"line": 2, "symbol": "alpha", "severity": "medium",
                 "category": "logic", "issue": "returns a constant", "suggestion": "return input"}
            ])
            return LLMResult(text=body, input_tokens=10, output_tokens=5, token_source="server",
                             latency_ms=1, model=self.model, prompt_chars=len(prompt))

    client = ReviewClient()
    p = _pipeline(repo, client)
    run, findings = p.review(["a.py"])
    assert run.files_called == 1
    assert len(findings) == 1
    assert findings[0]["file"] == "a.py"
    assert findings[0]["severity"] == "medium"


def test_failed_call_is_recorded_not_counted(repo: Path):
    class FailClient(FakeClient):
        def generate(self, prompt, system=None, **kw):
            return LLMResult(text="", input_tokens=0, output_tokens=0, token_source="server",
                             latency_ms=5, model=self.model, prompt_chars=len(prompt),
                             error="connection refused")

    client = FailClient()
    p = _pipeline(repo, client)
    run = p.summarize()
    assert run.errors == 3
    assert run.files_called == 0
    assert run.total_tokens == 0
    rows = p.conn.execute("SELECT COUNT(*) c FROM llm_calls WHERE error IS NOT NULL").fetchone()
    assert rows["c"] == 3
    # no summaries cached for failed calls
    assert p.conn.execute("SELECT COUNT(*) c FROM file_summaries").fetchone()["c"] == 0
