"""FastAPI service exposing the codegraph engine.

Endpoints mirror the CLI:
    GET  /api/health
    GET  /api/stats?repo=...
    GET  /api/search?q=...&repo=...
    GET  /api/trace?symbol=...&dir=out|in|both
    GET  /api/files
    GET  /api/summary/{path}
    POST /api/index
    POST /api/summarize
    POST /api/analyze
    POST /api/guide
    GET  /api/tokens
    GET  /api/runs
    GET  /api/resolution   -> edge resolution strategy histogram (edges.via)
    GET  /static/*         -> dashboard assets (offline, no CDN)
    GET  /                 -> dashboard (static/index.html)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import DEFAULT_OUTPUT, DEFAULT_REPORTS, IndexConfig, LLMConfig
from .graph import GraphBuilder
from .pipeline import Pipeline
from .query import QueryEngine

app = FastAPI(
    title="codegraph",
    version=__version__,
    description="AST-driven local code intelligence engine",
)

DEFAULT_REPO = Path(os.environ.get("CODEGRAPH_REPO", str(Path.cwd() / "repos" / "ripgrep")))
DEFAULT_MODEL = os.environ.get("CODEGRAPH_MODEL", "qwen2.5-coder:7b")

# Dashboard assets live next to this module; served verbatim so the page works
# fully offline (no CDN references anywhere in index.html / app.css / app.js).
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


def _cfg(repo: Path) -> IndexConfig:
    return IndexConfig(db_path=Path(repo) / ".codegraph" / "graph.db")


def _qe(repo: Path) -> QueryEngine:
    repo = Path(repo).resolve()
    try:
        return QueryEngine(repo, _cfg(repo))
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _pipeline(repo: Path, model: str = DEFAULT_MODEL) -> Pipeline:
    repo = Path(repo).resolve()
    try:
        return Pipeline(repo, _cfg(repo), LLMConfig(model=model))
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


class IndexRequest(BaseModel):
    repo: str = Field(default=str(DEFAULT_REPO))
    incremental: bool = True


class SummarizeRequest(BaseModel):
    repo: str = Field(default=str(DEFAULT_REPO))
    files: Optional[list[str]] = None
    force: bool = False
    limit: Optional[int] = None
    model: str = DEFAULT_MODEL


class AnalyzeRequest(BaseModel):
    repo: str = Field(default=str(DEFAULT_REPO))
    entry: Optional[str] = None
    model: str = DEFAULT_MODEL


class GuideRequest(BaseModel):
    repo: str = Field(default=str(DEFAULT_REPO))
    entry: Optional[str] = None
    model: str = DEFAULT_MODEL


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------


@app.get("/api/health")
def health(repo: str = str(DEFAULT_REPO)):
    from .llm import OllamaClient

    client = OllamaClient(LLMConfig(model=DEFAULT_MODEL))
    up = client.is_up()
    return {
        "status": "ok",
        "version": __version__,
        "repo": str(repo),
        "ollama_up": up,
        "model": DEFAULT_MODEL,
        "model_present": client.has_model() if up else False,
    }


@app.get("/api/stats")
def stats(repo: str = str(DEFAULT_REPO)):
    qe = _qe(Path(repo))
    p = Pipeline(Path(repo), _cfg(Path(repo)), LLMConfig(model=DEFAULT_MODEL))
    return {"stats": qe.stats(), "tokens": p.token_report()}


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), repo: str = str(DEFAULT_REPO), limit: int = 20):
    qe = _qe(Path(repo))
    return {"query": q, "results": [r.as_dict() for r in qe.search(q, limit=limit)]}


@app.get("/api/trace")
def trace(symbol: str, repo: str = str(DEFAULT_REPO), direction: str = "out", depth: int = 3):
    qe = _qe(Path(repo))
    cands = qe.search(symbol, limit=5)
    if not cands:
        raise HTTPException(status_code=404, detail=f"symbol not found: {symbol}")
    target = cands[0]
    out: dict = {"symbol": target.as_dict()}
    if direction in ("out", "both"):
        out["callees"] = qe.call_chain(target.id, max_depth=depth)
    if direction in ("in", "both"):
        out["callers"] = qe.reverse_chain(target.id, max_depth=depth)
    out["unresolved_calls"] = qe.unresolved_calls(target.id)
    return out


@app.get("/api/files")
def files(repo: str = str(DEFAULT_REPO), language: Optional[str] = None):
    qe = _qe(Path(repo))
    return {
        "files": [
            {
                "path": r["path"],
                "language": r["language"],
                "line_count": r["line_count"],
                "size_bytes": r["size_bytes"],
                "parse_ok": bool(r["parse_ok"]),
            }
            for r in qe.files(language)
        ]
    }


@app.get("/api/summary/{path:path}")
def get_summary(path: str, repo: str = str(DEFAULT_REPO)):
    p = _pipeline(Path(repo))
    s = p.get_cached_summary(path)
    if s is None:
        raise HTTPException(status_code=404, detail=f"no cached summary for {path}")
    return {"path": path, "summary": s}


@app.post("/api/index")
def do_index(req: IndexRequest):
    repo = Path(req.repo).resolve()
    gb = GraphBuilder(repo, _cfg(repo))
    st = gb.index(incremental=req.incremental)
    return {
        "repo": str(repo),
        "stats": gb.stats(),
        "run": {
            "files_indexed": st.files_indexed,
            "files_unchanged": st.files_skipped_unchanged,
            "files_removed": st.files_removed,
            "elapsed_s": round(st.elapsed_s, 3),
            "parse_errors": st.parse_errors,
        },
    }


@app.post("/api/summarize")
def do_summarize(req: SummarizeRequest):
    p = _pipeline(Path(req.repo), req.model)
    run = p.summarize(req.files, force=req.force, limit=req.limit)
    return {
        "run_id": run.run_id,
        "files_total": run.files_total,
        "files_called": run.files_called,
        "files_cached": run.files_cached,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "avoided_tokens": run.avoided_input_tokens + run.avoided_output_tokens,
        "cache_hit_rate": run.cache_hit_rate,
        "elapsed_s": round(run.elapsed_s, 2),
        "errors": run.errors,
    }


@app.post("/api/analyze")
def do_analyze(req: AnalyzeRequest):
    p = _pipeline(Path(req.repo), req.model)
    run, md = p.analyze_architecture(req.entry)
    dest = DEFAULT_OUTPUT / "architecture.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md, encoding="utf-8")
    return {
        "run_id": run.run_id,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "elapsed_s": round(run.elapsed_s, 2),
        "output_path": str(dest),
        "markdown": md,
    }


@app.post("/api/guide")
def do_guide(req: GuideRequest):
    p = _pipeline(Path(req.repo), req.model)
    arch_run, arch_md = p.analyze_architecture(req.entry, verbose=False)
    run, md = p.generate_guide(arch_md)
    dest = DEFAULT_OUTPUT / "developer_guide.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md, encoding="utf-8")
    return {
        "architecture_run_id": arch_run.run_id,
        "guide_run_id": run.run_id,
        "architecture_tokens": arch_run.total_tokens,
        "guide_tokens": run.total_tokens,
        "total_tokens": arch_run.total_tokens + run.total_tokens,
        "output_path": str(dest),
        "markdown": md,
    }


@app.get("/api/tokens")
def tokens(repo: str = str(DEFAULT_REPO)):
    p = _pipeline(Path(repo))
    return p.token_report()


@app.get("/api/runs")
def runs(repo: str = str(DEFAULT_REPO)):
    qe = _qe(Path(repo))
    rows = qe.conn.execute(
        "SELECT * FROM runs WHERE repo_id=? ORDER BY started_at DESC LIMIT 50", (qe.repo_id,)
    ).fetchall()
    return {"runs": [dict(r) for r in rows]}


@app.get("/api/resolution")
def resolution(repo: str = str(DEFAULT_REPO)):
    """Histogram of edge resolution strategies (the ``edges.via`` column).

    ``via`` is written by the graph builder when an edge is resolved:
    scope | receiver | import | unique | global-unique | file-unique |
    callable-unique | dir-unique | module-map | heuristic.
    Unresolved edges carry ``via IS NULL`` and are reported separately, so the
    chart can show the resolvable/unresolvable split of the whole graph.
    """
    qe = _qe(Path(repo))
    via: dict[str, int] = {
        r["strategy"]: int(r["n"])
        for r in qe.conn.execute(
            """SELECT COALESCE(via, 'unspecified') AS strategy, COUNT(*) AS n
               FROM edges WHERE repo_id=? AND resolved=1
               GROUP BY strategy ORDER BY n DESC""",
            (qe.repo_id,),
        )
    }
    resolved = sum(via.values())
    unresolved = int(
        qe.conn.execute(
            "SELECT COUNT(*) FROM edges WHERE repo_id=? AND resolved=0", (qe.repo_id,)
        ).fetchone()[0]
    )
    by_kind: dict[str, dict[str, int]] = {}
    for r in qe.conn.execute(
        """SELECT kind, COALESCE(via, 'unspecified') AS strategy, COUNT(*) AS n
           FROM edges WHERE repo_id=? AND resolved=1
           GROUP BY kind, strategy""",
        (qe.repo_id,),
    ):
        by_kind.setdefault(r["kind"], {})[r["strategy"]] = int(r["n"])
    return {
        "repo": str(qe.root),
        "via": via,
        "resolved": resolved,
        "unresolved": unresolved,
        "total": resolved + unresolved,
        "by_kind": by_kind,
    }


# --------------------------------------------------------------------------
# dashboard (static assets, fully offline — no CDN references)
# --------------------------------------------------------------------------

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    if INDEX_HTML.is_file():
        return FileResponse(str(INDEX_HTML), media_type="text/html; charset=utf-8")
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><body style='background:#0d1117;"
        "color:#c9d1d9;font-family:monospace;padding:24px'>"
        "<h1>codegraph</h1><p>dashboard assets not found at "
        f"<code>{INDEX_HTML}</code></p></body>"
    )


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
