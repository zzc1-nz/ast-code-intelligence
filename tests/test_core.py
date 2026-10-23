"""Unit tests for the parser / graph / query layers (no LLM required).

Run: .venv\\Scripts\\python.exe -m pytest tests -q
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph import parser as ts  # noqa: E402
from codegraph.config import IndexConfig  # noqa: E402
from codegraph.graph import GraphBuilder  # noqa: E402
from codegraph.query import QueryEngine  # noqa: E402


# --------------------------------------------------------------------------
# parser unit tests
# --------------------------------------------------------------------------

PY_SRC = b'''
"""Module docstring."""
import os
from collections import OrderedDict

CONST = 1


def helper(x):
    """Doc for helper."""
    if x > 1:
        return x * 2
    return 0


class Base:
    """Base class."""

    def run(self):
        return helper(3)


class Child(Base):
    def run(self):
        return super().run() + 1


def main():
    c = Child()
    return c.run()
'''


def test_python_symbols_and_calls():
    pf = ts.parse_source(PY_SRC, "demo.py", "python")
    names = {s.qualname for s in pf.symbols}
    assert "helper" in names
    assert "Base" in names
    assert "Base.run" in names
    assert "Child.run" in names
    assert "main" in names

    kinds = {s.qualname: s.kind for s in pf.symbols}
    assert kinds["Base"] == "class"
    assert kinds["Base.run"] == "method"
    assert kinds["helper"] == "function"

    # inheritance edge
    assert any(i.child == "Child" and i.base == "Base" for i in pf.inherits)

    # imports
    mods = {i.module for i in pf.imports}
    assert "os" in mods
    assert "collections" in mods

    # call scoping: helper() called from Base.run, main calls Child.run
    calls = {(c.caller, c.callee) for c in pf.calls}
    assert ("Base.run", "helper") in calls
    assert ("main", "Child") in calls

    # docstrings
    helper = next(s for s in pf.symbols if s.qualname == "helper")
    assert "Doc for helper" in (helper.docstring or "")


RUST_SRC = b"""
use std::collections::HashMap;

pub struct Engine {
    name: String,
}

impl Engine {
    pub fn new(name: &str) -> Self {
        Engine { name: name.to_string() }
    }

    pub fn run(&self) -> usize {
        self.compute(2)
    }

    fn compute(&self, n: usize) -> usize {
        if n > 1 { n * 2 } else { n }
    }
}

fn helper(x: usize) -> usize {
    x + 1
}

pub fn main_entry() {
    let e = Engine::new("x");
    let _ = e.run();
    let _ = helper(3);
}
"""


def test_rust_symbols_and_calls():
    pf = ts.parse_source(RUST_SRC, "demo.rs", "rust")
    names = {s.qualname for s in pf.symbols}
    assert "Engine" in names
    assert "impl Engine" in names
    assert "Engine::new" in names
    assert "Engine::run" in names
    assert "Engine::compute" in names
    assert "helper" in names
    assert "main_entry" in names

    kinds = {s.qualname: s.kind for s in pf.symbols}
    assert kinds["Engine"] == "struct"
    assert kinds["Engine::run"] == "method"

    calls = {(c.caller, c.callee) for c in pf.calls}
    assert ("Engine::run", "compute") in calls
    assert ("main_entry", "new") in calls
    assert ("main_entry", "helper") in calls

    # pub vs private
    priv = next(s for s in pf.symbols if s.qualname == "Engine::compute")
    assert priv.is_public is False


TS_SRC = b"""
import { readFile } from 'node:fs';

export interface Options { verbose: boolean }

export class Runner {
  constructor(private opts: Options) {}

  async run(path: string): Promise<string> {
    return this.load(path);
  }

  private load(path: string): string {
    return path;
  }
}

export function bootstrap(opts: Options): Runner {
  const r = new Runner(opts);
  return r;
}
"""


def test_typescript_symbols():
    pf = ts.parse_source(TS_SRC, "demo.ts", "typescript")
    names = {s.qualname for s in pf.symbols}
    assert "Options" in names
    assert "Runner" in names
    assert "Runner.run" in names
    assert "Runner.load" in names
    assert "bootstrap" in names

    calls = {(c.caller, c.callee) for c in pf.calls}
    assert ("Runner.run", "load") in calls
    assert ("bootstrap", "Runner") in calls


def test_parse_never_raises_on_garbage():
    pf = ts.parse_source(b"def (::: not valid python at all", "bad.py", "python")
    assert pf.parse_ok in (True, False)  # total function


# --------------------------------------------------------------------------
# graph + query integration
# --------------------------------------------------------------------------

SAMPLE_FILES = {
    "pkg/__init__.py": "",
    "pkg/util.py": '''
def add(a, b):
    return a + b


def mul(a, b):
    return a * b
''',
    "pkg/core.py": '''
from pkg.util import add


def compute(x):
    total = add(x, 1)
    return total * 2


def run():
    return compute(10)
''',
    "main.py": '''
from pkg.core import run


def main():
    return run()


if __name__ == "__main__":
    main()
''',
}


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    for rel, content in SAMPLE_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def test_graph_build_and_queries(sample_repo: Path):
    cfg = IndexConfig(db_path=sample_repo / ".codegraph" / "graph.db")
    gb = GraphBuilder(sample_repo, cfg)
    st = gb.index(incremental=False)
    assert st.files_indexed == 4
    assert st.symbols >= 5

    qe = QueryEngine(sample_repo, cfg)
    rows = qe.search("add", limit=5)
    assert any(r.name == "add" for r in rows)

    add_sym = next(r for r in rows if r.name == "add")
    callers = qe.callers(add_sym.id)
    assert any(c["name"] == "compute" for c in callers), callers

    compute_sym = next(r for r in qe.search("compute", limit=5) if r.name == "compute")
    callees = qe.callees(compute_sym.id)
    assert any(c["name"] == "add" for c in callees), callees

    # multi-hop chain from run() reaches add()
    run_sym = next(r for r in qe.search("run", limit=5) if r.name == "run")
    chain = qe.call_chain(run_sym.id, max_depth=3)
    reached = {step["symbol"]["name"] for step in chain}
    assert "compute" in reached
    assert "add" in reached

    # file dependencies
    deps = qe.file_dependencies("pkg/core.py")
    assert "pkg/util.py" in deps["imports"]
    assert "pkg/util.py" in deps["dependencies"]
    assert "main.py" in deps["dependents"]


def test_incremental_index_reuses_unchanged(sample_repo: Path):
    cfg = IndexConfig(db_path=sample_repo / ".codegraph" / "graph.db")
    gb = GraphBuilder(sample_repo, cfg)
    gb.index(incremental=False)
    before = gb.stats()

    # no changes -> nothing reparsed
    st2 = gb.index(incremental=True)
    assert st2.files_indexed == 0
    assert st2.files_skipped_unchanged == 4
    after = gb.stats()
    assert after["symbols"] == before["symbols"]
    assert after["edges"] == before["edges"]

    # touch one file -> only it reparsed
    (sample_repo / "pkg" / "util.py").write_text(
        "def add(a, b):\n    return a + b + 0\n\n\ndef mul(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    st3 = gb.index(incremental=True)
    assert st3.files_indexed == 1
    assert st3.files_skipped_unchanged == 3


def test_dependency_closure(sample_repo: Path):
    cfg = IndexConfig(db_path=sample_repo / ".codegraph" / "graph.db")
    gb = GraphBuilder(sample_repo, cfg)
    gb.index(incremental=False)
    qe = QueryEngine(sample_repo, cfg)
    closure = qe.dependency_closure(["pkg/util.py"])
    assert "pkg/util.py" in closure
    assert "pkg/core.py" in closure  # direct dependent


# --------------------------------------------------------------------------
# token accounting helpers
# --------------------------------------------------------------------------


def test_estimate_tokens_monotonic():
    from codegraph.llm import estimate_tokens

    assert estimate_tokens("") == 0
    a = estimate_tokens("x" * 100)
    b = estimate_tokens("x" * 1000)
    assert 0 < a < b
    assert b == pytest.approx(1000 / 3.5, rel=0.01)


def test_extract_json_handles_fences():
    from codegraph.pipeline import _extract_json, _normalize_summary

    txt = '```json\n{"responsibility": "does things", "key_functions": ["a: b"]}\n```'
    got = _extract_json(txt)
    assert got["responsibility"] == "does things"
    norm = _normalize_summary(got)
    assert norm["key_functions"] == ["a: b"]

    # prose around json
    txt2 = 'Sure! Here you go: {"responsibility": "x"} hope that helps'
    assert _extract_json(txt2)["responsibility"] == "x"

    # broken json -> normalized fallback
    assert _normalize_summary(None, "fallback")["responsibility"] == "fallback"


def test_prompt_budget_respected(sample_repo: Path):
    from codegraph.pipeline import Pipeline
    from codegraph.prompts import MAX_PROMPT_CHARS

    cfg = IndexConfig(db_path=sample_repo / ".codegraph" / "graph.db")
    GraphBuilder(sample_repo, cfg).index(incremental=False)
    p = Pipeline(sample_repo, cfg)
    ctx = p.build_file_context("pkg/core.py")
    from codegraph.prompts import build_summary_prompt

    prompt = build_summary_prompt(ctx)
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert "pkg/core.py" in prompt
    assert "compute" in prompt
