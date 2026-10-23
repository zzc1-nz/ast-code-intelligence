"""Symbol / call-graph query layer on top of SQLite."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import IndexConfig
from .db import connect, init_db


@dataclass
class SymbolRow:
    id: int
    name: str
    qualname: str
    kind: str
    language: str
    path: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    is_public: bool
    complexity: int
    body_lines: int

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "qualname": self.qualname,
            "kind": self.kind,
            "language": self.language,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "docstring": self.docstring,
            "is_public": self.is_public,
            "complexity": self.complexity,
            "body_lines": self.body_lines,
        }


_SYMBOL_SELECT = """
SELECT s.id, s.name, s.qualname, s.kind, s.language, f.path,
       s.start_line, s.end_line, s.signature, s.docstring, s.is_public,
       s.complexity, s.body_lines
FROM symbols s JOIN files f ON f.id = s.file_id
"""


class QueryEngine:
    def __init__(self, root: Path, cfg: IndexConfig | None = None):
        self.root = Path(root).resolve()
        self.cfg = cfg or IndexConfig()
        self.conn = connect(self.cfg.db_path)
        init_db(self.conn)
        row = self.conn.execute(
            "SELECT id FROM repos WHERE root_path=?", (str(self.root),)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"repo not indexed yet: {self.root}")
        self.repo_id = int(row["id"])

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> SymbolRow:
        return SymbolRow(
            id=r["id"],
            name=r["name"],
            qualname=r["qualname"],
            kind=r["kind"],
            language=r["language"],
            path=r["path"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            signature=r["signature"],
            docstring=r["docstring"],
            is_public=bool(r["is_public"]),
            complexity=r["complexity"],
            body_lines=r["body_lines"],
        )

    def symbol_by_id(self, sid: int) -> SymbolRow | None:
        r = self.conn.execute(_SYMBOL_SELECT + " WHERE s.id=?", (sid,)).fetchone()
        return self._row(r) if r else None

    # -- search ------------------------------------------------------------
    def search(self, query: str, limit: int = 20, kind: str | None = None) -> list[SymbolRow]:
        """FTS5 prefix search, falling back to LIKE for odd inputs."""
        q = (query or "").strip()
        if not q:
            return []
        rows: list[sqlite3.Row] = []
        fts_query = " OR ".join(f'"{tok}"*' for tok in q.replace("::", " ").replace(".", " ").split() if tok)
        if fts_query:
            try:
                sql = (
                    _SYMBOL_SELECT
                    + " JOIN symbols_fts fts ON fts.rowid = s.id"
                    + " WHERE symbols_fts MATCH ? AND s.repo_id=?"
                )
                params: list = [fts_query, self.repo_id]
                if kind:
                    sql += " AND s.kind=?"
                    params.append(kind)
                sql += " ORDER BY rank LIMIT ?"
                params.append(limit)
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            sql = _SYMBOL_SELECT + " WHERE s.repo_id=? AND s.name LIKE ?"
            params = [self.repo_id, f"%{q}%"]
            if kind:
                sql += " AND s.kind=?"
                params.append(kind)
            sql += " ORDER BY s.is_public DESC, s.name LIMIT ?"
            params.append(limit)
            rows = self.conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    # -- call graph --------------------------------------------------------
    def callers(self, symbol_id: int, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            _SYMBOL_SELECT.replace("FROM symbols s JOIN files f ON f.id = s.file_id", "")
            + """ FROM symbols s JOIN files f ON f.id = s.file_id
                  JOIN edges e ON e.src_id = s.id
                  WHERE e.dst_id = ? AND e.kind='call'
                  ORDER BY f.path, s.start_line LIMIT ?""",
            (symbol_id, limit),
        ).fetchall()
        return [self._row(r).as_dict() for r in rows]

    def callees(self, symbol_id: int, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.id, s.name, s.qualname, s.kind, s.language, f.path,
                      s.start_line, s.end_line, s.signature, s.docstring, s.is_public,
                      s.complexity, s.body_lines, e.line AS call_line
               FROM edges e
               JOIN symbols s ON s.id = e.dst_id
               JOIN files f ON f.id = s.file_id
               WHERE e.src_id = ? AND e.kind='call'
               ORDER BY e.line LIMIT ?""",
            (symbol_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = self._row(r).as_dict()
            d["call_line"] = r["call_line"]
            out.append(d)
        return out

    def unresolved_calls(self, symbol_id: int) -> list[dict]:
        rows = self.conn.execute(
            """SELECT dst_name, line FROM edges
               WHERE src_id=? AND kind='call' AND resolved=0 ORDER BY line""",
            (symbol_id,),
        ).fetchall()
        return [{"name": r["dst_name"], "line": r["line"]} for r in rows]

    def call_chain(self, symbol_id: int, max_depth: int = 4, max_nodes: int = 200) -> list[dict]:
        """BFS over outgoing call edges; returns a flat list of (depth, symbol, via)."""
        seen: set[int] = set()
        out: list[dict] = []
        frontier = [(symbol_id, 0, None)]
        root = self.symbol_by_id(symbol_id)
        if root is None:
            return []
        seen.add(symbol_id)
        out.append({"depth": 0, "symbol": root.as_dict(), "via": None})
        while frontier and len(out) < max_nodes:
            sid, depth, via = frontier.pop(0)
            if depth >= max_depth:
                continue
            for d in self.callees(sid, limit=50):
                if d["id"] in seen:
                    continue
                seen.add(d["id"])
                out.append({"depth": depth + 1, "symbol": d, "via": via or self.symbol_by_id(sid).name})
                frontier.append((d["id"], depth + 1, d["name"]))
        return out

    def reverse_chain(self, symbol_id: int, max_depth: int = 3, max_nodes: int = 200) -> list[dict]:
        seen: set[int] = set()
        out: list[dict] = []
        root = self.symbol_by_id(symbol_id)
        if root is None:
            return []
        seen.add(symbol_id)
        out.append({"depth": 0, "symbol": root.as_dict(), "via": None})
        frontier = [(symbol_id, 0)]
        while frontier and len(out) < max_nodes:
            sid, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            for c in self.callers(sid, limit=50):
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                out.append({"depth": depth + 1, "symbol": c, "via": self.symbol_by_id(sid).name})
                frontier.append((c["id"], depth + 1))
        return out

    # -- file-level --------------------------------------------------------
    def file_row(self, rel_path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM files WHERE repo_id=? AND path=?", (self.repo_id, rel_path)
        ).fetchone()

    def file_symbols(self, rel_path: str) -> list[SymbolRow]:
        rows = self.conn.execute(
            _SYMBOL_SELECT + " WHERE f.path=? AND s.repo_id=? ORDER BY s.start_line",
            (rel_path, self.repo_id),
        ).fetchall()
        return [self._row(r) for r in rows]

    def files(self, language: str | None = None) -> list[sqlite3.Row]:
        if language:
            return self.conn.execute(
                "SELECT * FROM files WHERE repo_id=? AND language=? ORDER BY path",
                (self.repo_id, language),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM files WHERE repo_id=? ORDER BY path", (self.repo_id,)
        ).fetchall()

    # Entry-point heuristics: prefer real program entry points over test helpers
    _ENTRY_NAME_HINTS = ("main", "run", "start", "execute", "cli", "app", "serve", "init")
    _TEST_MARKERS = ("test", "tests", "spec", "bench", "fuzz", "examples")

    def entry_points(self, limit: int = 20, include_tests: bool = False) -> list[dict]:
        """Heuristic entry points.

        Scoring favours symbols that sit at the top of a call chain (high fan-out,
        low fan-in), are public, live in non-test code, and have an entry-ish name.
        """
        rows = self.conn.execute(
            """SELECT s.id, s.name, s.qualname, s.kind, f.path, s.start_line, s.end_line,
                      s.signature, s.is_public, s.complexity,
                      (SELECT COUNT(*) FROM edges e WHERE e.src_id=s.id AND e.kind='call') AS out_deg,
                      (SELECT COUNT(*) FROM edges e WHERE e.dst_id=s.id AND e.kind='call') AS in_deg
               FROM symbols s JOIN files f ON f.id=s.file_id
               WHERE s.repo_id=? AND s.kind IN ('function','method')""",
            (self.repo_id,),
        ).fetchall()

        scored: list[tuple[float, dict]] = []
        for r in rows:
            d = dict(r)
            path = d["path"].lower()
            name = (d["name"] or "").lower()

            is_test_path = any(m in path for m in self._TEST_MARKERS)
            if is_test_path and not include_tests:
                continue
            if name.startswith("test_") or name in ("tests", "setup", "teardown"):
                if not include_tests:
                    continue

            score = 0.0
            score += min(d["out_deg"], 60) * 1.0          # fan-out is the main signal
            score -= min(d["in_deg"], 40) * 1.5           # entries are rarely called
            score += 12.0 if d["is_public"] else 0.0
            if any(h == name or name.startswith(h) for h in self._ENTRY_NAME_HINTS):
                score += 60.0
            if d["kind"] == "function":
                score += 5.0
            # very large functions are usually dispatchers/entry glue
            score += min(d["complexity"], 40) * 0.5
            scored.append((score, d))

        scored.sort(key=lambda x: -x[0])
        return [d for _s, d in scored[:limit]]

    # -- file dependency closure ------------------------------------------
    def file_dependencies(self, rel_path: str) -> dict[str, list[str]]:
        """Direct dependency neighbourhood of a file.

        imports    : files this file imports (import edges, resolved)
        dependencies: files whose symbols this file calls (call edges out)
        dependents : files that call into this file (call edges in) or import it
        """
        imports: set[str] = set()
        for r in self.conn.execute(
            """SELECT DISTINCT f2.path AS path FROM edges e
               JOIN symbols s ON s.id=e.src_id
               JOIN files f ON f.id=s.file_id
               JOIN files f2 ON f2.id=e.dst_file_id
               WHERE f.path=? AND f.repo_id=? AND e.kind='import' AND e.resolved=1""",
            (rel_path, self.repo_id),
        ):
            if r["path"] and r["path"] != rel_path:
                imports.add(r["path"])

        deps: set[str] = set()
        for r in self.conn.execute(
            """SELECT DISTINCT f2.path AS path FROM edges e
               JOIN symbols s ON s.id=e.src_id
               JOIN files f ON f.id=s.file_id
               JOIN symbols d ON d.id=e.dst_id
               JOIN files f2 ON f2.id=d.file_id
               WHERE f.path=? AND f.repo_id=? AND e.kind='call'""",
            (rel_path, self.repo_id),
        ):
            if r["path"] and r["path"] != rel_path:
                deps.add(r["path"])

        dependents: set[str] = set()
        for r in self.conn.execute(
            """SELECT DISTINCT f2.path AS path FROM edges e
               JOIN symbols d ON d.id=e.dst_id
               JOIN files f ON f.id=d.file_id
               JOIN symbols s ON s.id=e.src_id
               JOIN files f2 ON f2.id=s.file_id
               WHERE f.path=? AND f.repo_id=? AND e.kind='call'""",
            (rel_path, self.repo_id),
        ):
            if r["path"] and r["path"] != rel_path:
                dependents.add(r["path"])
        # importers also count as dependents
        for r in self.conn.execute(
            """SELECT DISTINCT f2.path AS path FROM edges e
               JOIN files f ON f.id=e.dst_file_id
               JOIN symbols s ON s.id=e.src_id
               JOIN files f2 ON f2.id=s.file_id
               WHERE f.path=? AND f.repo_id=? AND e.kind='import'""",
            (rel_path, self.repo_id),
        ):
            if r["path"] and r["path"] != rel_path:
                dependents.add(r["path"])

        return {
            "imports": sorted(imports),
            "dependencies": sorted(deps),
            "dependents": sorted(dependents),
        }

    def dependency_closure(self, changed: list[str], max_nodes: int = 200) -> list[str]:
        """Changed files + their direct dependents + direct imports (1 hop each)."""
        result: set[str] = set(changed)
        for rel in changed:
            deps = self.file_dependencies(rel)
            result.update(deps["dependents"])
            result.update(deps["imports"])
        return sorted(result)[:max_nodes]

    # -- stats -------------------------------------------------------------
    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql, (self.repo_id,)).fetchone()[0]  # noqa: E731
        return {
            "repo": str(self.root),
            "files": q("SELECT COUNT(*) FROM files WHERE repo_id=?"),
            "symbols": q("SELECT COUNT(*) FROM symbols WHERE repo_id=?"),
            "edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=?"),
            "call_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND kind='call'"),
            "import_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND kind='import'"),
            "inherit_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND kind IN ('inherit','implements','extends')"),
            "resolved_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND resolved=1"),
            "unresolved_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND resolved=0"),
            "summaries": q("SELECT COUNT(*) FROM file_summaries WHERE repo_id=?"),
            "parse_errors": q("SELECT COUNT(*) FROM files WHERE repo_id=? AND parse_ok=0"),
            "by_language": {
                r[0]: r[1]
                for r in self.conn.execute(
                    "SELECT language, COUNT(*) FROM files WHERE repo_id=? GROUP BY language",
                    (self.repo_id,),
                )
            },
            "by_kind": {
                r[0]: r[1]
                for r in self.conn.execute(
                    "SELECT kind, COUNT(*) FROM symbols WHERE repo_id=? GROUP BY kind",
                    (self.repo_id,),
                )
            },
            "db_bytes": sum(
                Path(str(self.cfg.db_path) + sfx).stat().st_size
                for sfx in ("", "-wal", "-shm")
                if Path(str(self.cfg.db_path) + sfx).exists()
            ),
        }
