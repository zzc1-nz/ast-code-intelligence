"""Regression tests: incremental edges, visibility, TS kinds, decorators,
JSON repair, prompt budgets.

These pin down behaviour that was manually verified after bug-fix rounds in
src/codegraph/{graph,parser,prompts,pipeline}.py. No LLM / Ollama is used:
prompt *building* (not generation) is what these tests exercise.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph import parser as ts  # noqa: E402
from codegraph import prompts  # noqa: E402
from codegraph.config import IndexConfig, LLMConfig  # noqa: E402
from codegraph.graph import GraphBuilder  # noqa: E402
from codegraph.query import QueryEngine  # noqa: E402
from codegraph.pipeline import (  # noqa: E402
    ARCH_PROMPT_BUDGET_CHARS,
    Pipeline,
    _repair_truncated_json,
)
from codegraph.prompts import MAX_PROMPT_CHARS  # noqa: E402


class _NoLLM:
    """Client stub that fails loudly if a test ever tries to generate.

    Regression tests only *build* prompts; nothing here may talk to Ollama.
    """

    def generate(self, *a, **kw):  # noqa: D401 - test double
        raise AssertionError("regression tests must never call the LLM")

    def is_up(self):
        return False

    def has_model(self, name=None):
        return False

    def list_models(self):
        return []

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 1. Incremental indexing preserves cross-file call edges
# ---------------------------------------------------------------------------
INC_FILES = {
    "a.py": "def util():\n    return 1\n",
    "b.py": "from a import util\n\n\ndef core():\n    return util()\n",
    "c.py": "from b import core\n\n\ndef main():\n    return core()\n",
}


@pytest.fixture()
def inc_repo(tmp_path: Path) -> Path:
    root = tmp_path / "inc"
    for rel, content in INC_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _call_edges(qe: QueryEngine) -> set[tuple[str, str]]:
    rows = qe.conn.execute(
        """SELECT s.qualname, d.qualname FROM edges e
           JOIN symbols s ON s.id=e.src_id JOIN symbols d ON d.id=e.dst_id
           WHERE e.kind='call' AND e.resolved=1"""
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def test_incremental_keeps_b_to_util_after_util_edit(inc_repo: Path):
    cfg = IndexConfig(db_path=inc_repo / ".codegraph" / "graph.db")
    gb = GraphBuilder(inc_repo, cfg)
    gb.index(incremental=False)
    qe = QueryEngine(inc_repo, cfg)

    # edit only util.py -> the caller b.py (core -> util) edge must survive
    (inc_repo / "a.py").write_text("def util():\n    return 2\n", encoding="utf-8")
    st = gb.index(incremental=True)
    assert st.files_indexed == 1
    assert ("core", "util") in _call_edges(qe), "b->util call edge dropped after util edit"


def test_incremental_keeps_edges_after_core_edit(inc_repo: Path):
    cfg = IndexConfig(db_path=inc_repo / ".codegraph" / "graph.db")
    gb = GraphBuilder(inc_repo, cfg)
    gb.index(incremental=False)
    qe = QueryEngine(inc_repo, cfg)

    # edit core.py (b.py) -> edges MAIN->core AND core->util must both survive
    (inc_repo / "b.py").write_text(
        "from a import util\n\n\ndef core():\n    return util() + 1\n", encoding="utf-8"
    )
    st = gb.index(incremental=True)
    assert st.files_indexed == 1
    edges = _call_edges(qe)
    assert ("core", "util") in edges
    assert ("main", "core") in edges


# ---------------------------------------------------------------------------
# 2. Rust visibility  (pub / default-private / pub(crate))
# ---------------------------------------------------------------------------
RUST_VIS = b"""
pub fn exported(x: usize) -> usize { x }
fn private_one(x: usize) -> usize { x }
pub(crate) fn crate_visible(x: usize) -> usize { x }
pub(super) fn super_visible(x: usize) -> usize { x }
"""


def test_rust_visibility_pub_pubcrate_default_private():
    pf = ts.parse_source(RUST_VIS, "vis.rs", "rust")
    vis = {s.qualname: s.is_public for s in pf.symbols}
    assert vis["exported"] is True         # pub
    assert vis["private_one"] is False     # no modifier
    assert vis["crate_visible"] is True    # pub(crate) -> startswith("pub")
    assert vis["super_visible"] is True    # pub(super) -> startswith("pub")


# ---------------------------------------------------------------------------
# 3. Rust `impl Engine` container vs `struct Engine`
# ---------------------------------------------------------------------------
RUST_IMPL = b"""
pub struct Engine { name: String }
struct Hidden { x: usize }

impl Engine {
    pub fn new() -> Self { Engine { name: String::new() } }
    fn secret(&self) -> usize { 0 }
}
"""


def test_rust_impl_container_distinct_from_struct(tmp_path: Path):
    syms = {s.qualname: s for s in ts.parse_source(RUST_IMPL, "impl.rs", "rust").symbols}
    assert syms["Engine"].kind == "struct"
    assert syms["impl Engine"].kind == "impl"
    assert "Engine" in syms and "impl Engine" in syms, "struct and impl collided"

    # graph round-trip: both symbols survive indexing
    root = tmp_path / "rs"
    root.mkdir(parents=True)
    (root / "lib.rs").write_bytes(RUST_IMPL)
    cfg = IndexConfig(db_path=root / ".codegraph" / "graph.db")
    GraphBuilder(root, cfg).index(incremental=False)
    qe = QueryEngine(root, cfg)
    rows = {r.qualname: r for r in qe.file_symbols("lib.rs")}
    assert rows["Engine"].kind == "struct"
    assert rows["impl Engine"].kind == "impl"


# ---------------------------------------------------------------------------
# 4. TypeScript interface vs class -> distinct kinds
# ---------------------------------------------------------------------------
TS_KINDS = b"""
export interface Options { verbose: boolean }
export class Runner implements Options { verbose = true }
interface Local { x: number }
"""


def test_typescript_interface_vs_class_kinds():
    pf = ts.parse_source(TS_KINDS, "kinds.ts", "typescript")
    kinds = {s.qualname: s.kind for s in pf.symbols}
    assert kinds["Options"] == "interface"
    assert kinds["Runner"] == "class"
    assert kinds["Local"] == "interface"


# ---------------------------------------------------------------------------
# 5. Python decorator still yields the function with a sane line number
# ---------------------------------------------------------------------------
PY_DECORATED = b"""import functools


@functools.cache
def decorated(a):
    return a


@functools.cache
def multi(x):
    return x


class C:
    @property
    def prop(self):
        return 1
"""


def test_python_decorated_function_still_detected(tmp_path: Path):
    pf = ts.parse_source(PY_DECORATED, "dec.py", "python")
    by_name = {s.qualname: s for s in pf.symbols}
    # still recognised as a function, not dropped/absorbed by the decorator
    assert by_name["decorated"].kind == "function"
    assert by_name["multi"].kind == "function"
    assert by_name["C.prop"].kind == "method"
    # line numbers point at the decorated_definition node, whose start is the
    # first decorator line (module-level and in-class behave the same way).
    src_lines = PY_DECORATED.decode("utf-8").splitlines()
    for qual, first_decorator_line in (("decorated", 4), ("multi", 9), ("C.prop", 15)):
        sym = by_name[qual]
        assert sym.start_line == first_decorator_line
        assert "@" in src_lines[sym.start_line - 1], (qual, src_lines[sym.start_line - 1])
        # and the recorded span actually covers the function body
        assert "def " in src_lines[sym.start_line]
        assert sym.end_line > sym.start_line

    # and it must survive indexing + search
    root = tmp_path / "py"
    root.mkdir(parents=True)
    (root / "dec.py").write_bytes(PY_DECORATED)
    cfg = IndexConfig(db_path=root / ".codegraph" / "graph.db")
    GraphBuilder(root, cfg).index(incremental=False)
    qe = QueryEngine(root, cfg)
    hit = next(r for r in qe.search("decorated", limit=5) if r.name == "decorated")
    assert hit.kind == "function"
    assert hit.start_line == 4


# ---------------------------------------------------------------------------
# 6. FTS5 special characters must not raise
# ---------------------------------------------------------------------------
FTS_SRC = {
    "a.py": "def add(a, b):\n    return a + b\n\n\ndef alpha():\n    return add(1, 2)\n",
}


@pytest.fixture()
def fts_repo(tmp_path: Path) -> Path:
    root = tmp_path / "fts"
    for rel, content in FTS_SRC.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "query",
    ['"', "*", "(", "AND", "OR", "-", ":", "NEAR(", 'foo"bar', "a*b", "[", "{"],
)
def test_fts5_special_characters_no_raise(fts_repo: Path, query: str):
    cfg = IndexConfig(db_path=fts_repo / ".codegraph" / "graph.db")
    GraphBuilder(fts_repo, cfg).index(incremental=False)
    qe = QueryEngine(fts_repo, cfg)
    # must not raise (empty result is fine)
    rows = qe.search(query, limit=5)  # noqa: F841
    # sanity: a normal prefix search still works afterwards
    assert any(r.name == "add" for r in qe.search("add", limit=5))


# ---------------------------------------------------------------------------
# 7. _repair_truncated_json directly
# ---------------------------------------------------------------------------
def test_repair_truncated_json_object():
    assert _repair_truncated_json('{"a": "b"') == {"a": "b"}


def test_repair_truncated_json_array_element():
    assert _repair_truncated_json('{"a": [1, 2') == {"a": [1, 2]}


def test_repair_truncated_json_list():
    # trailing comma after the only element -> must still parse
    got = _repair_truncated_json('[{"x": 1},')
    assert got == [{"x": 1}] or (isinstance(got, list) and got and got[0] == {"x": 1})


def test_repair_truncated_json_edge_cases():
    assert _repair_truncated_json('{"a": 1, "b":') == {"a": 1}
    assert _repair_truncated_json('{"a": {"b": 1') == {"a": {"b": 1}}
    # already-valid fragments pass through
    assert _repair_truncated_json('{"a": "b"}') == {"a": "b"}
    assert _repair_truncated_json("[1, 2, 3") == [1, 2, 3]
    assert _repair_truncated_json("[]") == []
    # empty / garbage must return None rather than raise
    assert _repair_truncated_json("") is None
    assert _repair_truncated_json("   ") is None


# ---------------------------------------------------------------------------
# 8. Summary prompt always within MAX_PROMPT_CHARS
# ---------------------------------------------------------------------------
PROMPT_SRC = {
    "a.py": "def alpha():\n    return 1\n\n\ndef beta():\n    return alpha()\n",
    "b.py": "from a import beta\n\n\ndef gamma():\n    return beta()\n",
}


@pytest.fixture()
def prompt_repo(tmp_path: Path) -> Path:
    root = tmp_path / "pr"
    for rel, content in PROMPT_SRC.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def test_summary_prompt_within_budget(prompt_repo: Path):
    cfg = IndexConfig(db_path=prompt_repo / ".codegraph" / "graph.db")
    GraphBuilder(prompt_repo, cfg).index(incremental=False)
    p = Pipeline(prompt_repo, cfg, LLMConfig(model="fake"), client=_NoLLM())
    for rel in sorted(PROMPT_SRC):
        ctx = p.build_file_context(rel)
        prompt = prompts.build_summary_prompt(ctx)
        assert len(prompt) <= MAX_PROMPT_CHARS, f"summary prompt over budget for {rel}"


# ---------------------------------------------------------------------------
# 9. arch prompt is materially larger than a summary prompt (ripgrep DB)
# ---------------------------------------------------------------------------
_RIPGREP_SRC = Path(r"D:\codegraph\repos\ripgrep")
_RIPGREP_DB = _RIPGREP_SRC / ".codegraph" / "graph.db"
# The cached summaries in the fixture keyed by this model
_RIPGREP_MODEL = "qwen2.5-coder:7b"
_RIPGREP_MARKER = (r"D:\codegraph\repos\ripgrep\.codegraph\graph.db").replace("\\", "/")


def _copy_ripgrep_db(tmp_path: Path) -> Path:
    """Snapshot the read-only ripgrep graph DB into tmp so repos/ is never mutated.

    Uses SQLite's backup API over a `mode=ro` connection rather than copying the
    files: the live DB is in WAL mode and may be written by another process, so a
    raw file copy can be torn. The backup API yields a consistent snapshot.
    """
    dst = tmp_path / "graph.db"
    src = sqlite3.connect(f"file:{_RIPGREP_DB}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


@pytest.fixture()
def ripgrep_pipeline(tmp_path: Path) -> Pipeline:
    db = _copy_ripgrep_db(tmp_path)
    if not db.exists() or db.stat().st_size == 0:
        pytest.skip("ripgrep graph DB not available on this machine")
    cfg = IndexConfig(db_path=db)
    p = Pipeline(_RIPGREP_SRC, cfg, LLMConfig(model=_RIPGREP_MODEL), client=_NoLLM())
    if p.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] < 100:
        pytest.skip("ripgrep DB too small to be meaningful")
    return p


def test_arch_prompt_much_larger_than_summary(ripgrep_pipeline: Pipeline):
    p = ripgrep_pipeline
    entries = p.qe.entry_points(limit=5)
    assert entries, "no entry points in ripgrep DB"
    entry = entries[0].as_dict() if hasattr(entries[0], "as_dict") else dict(entries[0])
    chain = p.qe.call_chain(entry["id"], max_depth=4, max_nodes=150)

    # summaries: entry file first, then highest fan-in, mirroring analyze_architecture
    summaries: dict[str, dict] = {}
    order = [entry["path"]]
    for r in p.conn.execute(
        """SELECT f.path, COUNT(*) c FROM edges e
           JOIN symbols d ON d.id=e.dst_id JOIN files f ON f.id=d.file_id
           WHERE e.repo_id=? AND e.kind='call' GROUP BY f.path ORDER BY c DESC""",
        (p.repo_id,),
    ):
        order.append(r["path"])
    for path in order:
        if path in summaries:
            continue
        s = p.get_cached_summary(path)
        if s:
            summaries[path] = s

    module_stats: dict[str, int] = {}
    for r in p.conn.execute("SELECT path FROM files WHERE repo_id=?", (p.repo_id,)):
        top = r["path"].split("/")[0] if "/" in r["path"] else "(root)"
        module_stats[top] = module_stats.get(top, 0) + 1

    dep_edges: list[tuple[str, str]] = []
    for r in p.conn.execute(
        """SELECT DISTINCT f1.path a, f2.path b FROM edges e
           JOIN symbols s ON s.id=e.src_id JOIN files f1 ON f1.id=s.file_id
           JOIN symbols d ON d.id=e.dst_id JOIN files f2 ON f2.id=d.file_id
           WHERE e.repo_id=? AND e.kind='import' AND e.resolved=1 AND f1.path != f2.path""",
        (p.repo_id,),
    ):
        dep_edges.append((r["a"], r["b"]))

    arch = prompts.build_architecture_prompt(
        _RIPGREP_SRC.name, entry, chain, summaries, module_stats, dep_edges
    )

    ctx = p.build_file_context(entry["path"])
    summary = prompts.build_summary_prompt(ctx)

    assert len(summary) > 0
    assert len(summary) <= MAX_PROMPT_CHARS
    # the cross-file arch prompt must dwarf a single-file summary prompt
    assert len(arch) >= len(summary) * 2, (
        f"arch ({len(arch)}) vs summary ({len(summary)}); ratio must be >= 2"
    )
    # architectural property: a whole-repo prompt exceeds the per-file clamp
    assert len(arch) > MAX_PROMPT_CHARS
    assert len(arch) <= ARCH_PROMPT_BUDGET_CHARS


# ---------------------------------------------------------------------------
# 10. arch num_ctx must stay within the 16k ceiling (fixed: clamp + _fit_arch_prompt)
# ---------------------------------------------------------------------------
def test_arch_num_ctx_stays_within_ceiling_budget():
    from codegraph.pipeline import ARCH_MAX_CTX, ARCH_PROMPT_BUDGET_CHARS, _arch_num_ctx

    # The budget deliberately allows a prompt up to ARCH_PROMPT_BUDGET_CHARS...
    worst_case_prompt = "x" * ARCH_PROMPT_BUDGET_CHARS
    # ...so num_ctx must still not exceed the documented 16k window.
    assert _arch_num_ctx(worst_case_prompt) <= ARCH_MAX_CTX

    # And even an absurdly long prompt must not blow the ceiling.
    assert _arch_num_ctx("x" * 200_000) == ARCH_MAX_CTX


def test_fit_arch_prompt_trims_to_budget():
    """`_fit_arch_prompt` must shed summaries until the prompt fits the budget."""
    from codegraph.pipeline import ARCH_PROMPT_BUDGET_CHARS, _fit_arch_prompt

    # Fake builder: each summary contributes ~1000 chars of prompt.
    def build(items, **_kw):
        return "x" * (500 + 1000 * len(items))

    summaries = {f"f{i}.py": {"responsibility": "r"} for i in range(100)}
    out = _fit_arch_prompt(build, summaries)
    assert len(out) <= ARCH_PROMPT_BUDGET_CHARS
    # must keep at least the floor of 10 summaries
    assert (len(out) - 500) // 1000 >= 10