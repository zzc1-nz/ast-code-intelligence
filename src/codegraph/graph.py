"""Repository scanner + graph builder (Tree-sitter AST -> SQLite).

Edge resolution strategy
------------------------
1. Local scope first: a call whose text matches a symbol in the same file
   (methods before free functions, then enclosing class methods).
2. Import-aware: if the file imports `name` from module M, and a symbol named
   `name` exists in a file that M maps to, resolve to it.
3. Global unique-name: if exactly one symbol in the repo has that name.
4. Otherwise the edge is stored with `resolved=0` and `dst_name` set — this is
   the measurable "statically unresolvable call" bucket we report on.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import parser as ts
from .config import IndexConfig
from .db import connect, init_db

CREATE_EDGE_SQL = """INSERT OR IGNORE INTO edges(repo_id, src_id, dst_id, dst_file_id,
                     kind, dst_name, line, resolved, via)
                     VALUES (?,?,?,?,?,?,?,?,?)"""


@dataclass
class ScanResult:
    files: list[Path]
    skipped: int = 0
    total_bytes: int = 0


@dataclass
class IndexStats:
    files_indexed: int = 0
    files_skipped_unchanged: int = 0
    files_removed: int = 0
    # files re-parsed (not re-written) because a symbol they point at was
    # replaced/deleted and their edges had to be rebuilt
    files_reparsed_for_edges: int = 0
    symbols: int = 0
    edges: int = 0
    resolved_edges: int = 0
    unresolved_edges: int = 0
    dynamic_calls: int = 0
    total_calls: int = 0
    parse_errors: int = 0
    elapsed_s: float = 0.0
    db_bytes: int = 0
    by_language: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def scan_repo(root: Path, cfg: IndexConfig) -> ScanResult:
    root = root.resolve()
    files: list[Path] = []
    skipped = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in cfg.exclude_dirs and not d.startswith(".git")
        ]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in cfg.include_exts:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > cfg.max_file_bytes:
                skipped += 1
                continue
            files.append(p)
            total += size
    files.sort()
    return ScanResult(files=files, skipped=skipped, total_bytes=total)


# --------------------------------------------------------------------------
# Graph builder
# --------------------------------------------------------------------------


class GraphBuilder:
    def __init__(self, root: Path, cfg: IndexConfig | None = None):
        self.root = Path(root).resolve()
        self.cfg = cfg or IndexConfig()
        self.conn = connect(self.cfg.db_path)
        init_db(self.conn)
        self.repo_id: int | None = None
        # Raw facts for the resolution pass, keyed by repo-relative path.
        # Instance attribute on purpose: a class-level dict is shared by every
        # GraphBuilder in the process (and leaks across repositories).
        self._pending: dict[str, dict] = {}

    # -- repo registration -------------------------------------------------
    def ensure_repo(self) -> int:
        row = self.conn.execute(
            "SELECT id FROM repos WHERE root_path=?", (str(self.root),)
        ).fetchone()
        if row:
            self.repo_id = row["id"]
            return self.repo_id
        cur = self.conn.execute(
            "INSERT INTO repos(root_path, name) VALUES (?, ?)",
            (str(self.root), self.root.name),
        )
        self.repo_id = int(cur.lastrowid)
        self.conn.commit()
        return self.repo_id

    @staticmethod
    def retarget_repo(conn, new_root: Path) -> int:
        """Point an existing graph DB at a moved/copied repository directory.

        Used when a repo is copied together with its `.codegraph` directory: the
        DB still records the old absolute paths, which would make `ensure_repo`
        create a second repo row and silently lose every cached summary.
        Rewrites `repos.root_path` and every `files.abs_path`.

        Returns the number of file rows retargeted.
        """
        new_root = Path(new_root).resolve()
        rows = conn.execute("SELECT id, root_path FROM repos").fetchall()
        if not rows:
            return 0
        # Pick the repo whose old root looks like the source of the copy
        target_id = rows[0]["id"]
        old_root = Path(rows[0]["root_path"])
        for r in rows:
            if Path(r["root_path"]).name == new_root.name.replace("-incr", ""):
                target_id = r["id"]
                old_root = Path(r["root_path"])
                break

        files = conn.execute(
            "SELECT id, path, abs_path FROM files WHERE repo_id=?", (target_id,)
        ).fetchall()
        for f in files:
            conn.execute(
                "UPDATE files SET abs_path=? WHERE id=?",
                (str(new_root / f["path"]), f["id"]),
            )
        conn.execute(
            "UPDATE repos SET root_path=?, name=? WHERE id=?",
            (str(new_root), new_root.name, target_id),
        )
        conn.commit()
        return len(files)

    # -- main entry --------------------------------------------------------
    def index(self, incremental: bool = True, verbose: bool = True) -> IndexStats:
        t0 = time.perf_counter()
        repo_id = self.ensure_repo()
        scan = scan_repo(self.root, self.cfg)

        # current on-disk state
        disk: dict[str, tuple[Path, str, int]] = {}
        for p in scan.files:
            try:
                data = p.read_bytes()
            except OSError:
                continue
            rel = p.relative_to(self.root).as_posix()
            disk[rel] = (p, file_hash(data), len(data))

        known = {
            r["path"]: r
            for r in self.conn.execute(
                "SELECT id, path, content_hash FROM files WHERE repo_id=?", (repo_id,)
            )
        }

        stats = IndexStats()

        # deletions
        for rel, row in known.items():
            if rel not in disk:
                self._delete_file(repo_id, row["id"], rel)
                stats.files_removed += 1

        # which files need (re)parsing
        to_parse: list[tuple[str, Path, str, int]] = []
        for rel, (p, h, size) in disk.items():
            row = known.get(rel)
            if incremental and row is not None and row["content_hash"] == h:
                stats.files_skipped_unchanged += 1
                continue
            to_parse.append((rel, p, h, size))

        for rel, p, h, size in to_parse:
            data = p.read_bytes()
            lang = ts.detect_language(p, self.cfg.include_exts)
            if lang is None:
                continue
            pf = ts.parse_source(data, rel, lang)
            if not pf.parse_ok:
                stats.parse_errors += 1
            n_symbols = self._write_file(repo_id, rel, p, h, size, lang, data, pf)
            stats.files_indexed += 1
            stats.symbols += n_symbols
            stats.dynamic_calls += pf.dynamic_calls
            stats.total_calls += pf.total_calls
            stats.by_language[lang] = stats.by_language.get(lang, 0) + 1

        # Files whose *symbols were replaced* also invalidate edges owned by other
        # files: `_write_file` deletes the old symbol rows, and edges referencing
        # them cascade away. Those edges must be rebuilt, so every file that had an
        # edge pointing into a rewritten file has to be re-resolved too — otherwise
        # a one-file edit silently drops A->B call edges until A is re-parsed.
        rewritten = [rel for rel, *_ in to_parse]
        touched = list(rewritten)
        if incremental and rewritten:
            extra = self._files_with_edges_into(repo_id, rewritten)
            extra -= set(rewritten)
            for rel in sorted(extra):
                p = self.root / rel
                if not p.exists():
                    continue
                data = p.read_bytes()
                lang = ts.detect_language(p, self.cfg.include_exts)
                if lang is None:
                    continue
                pf = ts.parse_source(data, rel, lang)
                self._write_file(repo_id, rel, p, file_hash(data), len(data), lang, data, pf)
                stats.files_reparsed_for_edges += 1
                touched.append(rel)
        if not incremental:
            touched = list(disk.keys())
        self._resolve_edges(repo_id, touched, stats)

        self.conn.execute(
            "UPDATE repos SET indexed_at=datetime('now') WHERE id=?", (repo_id,)
        )
        self.conn.commit()

        # final counts
        stats.edges = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE repo_id=?", (repo_id,)
        ).fetchone()["c"]
        stats.resolved_edges = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE repo_id=? AND resolved=1", (repo_id,)
        ).fetchone()["c"]
        stats.unresolved_edges = stats.edges - stats.resolved_edges
        if stats.symbols == 0:
            stats.symbols = self.conn.execute(
                "SELECT COUNT(*) c FROM symbols WHERE repo_id=?", (repo_id,)
            ).fetchone()["c"]
        for r in self.conn.execute(
            "SELECT kind, COUNT(*) c FROM symbols WHERE repo_id=? GROUP BY kind", (repo_id,)
        ):
            stats.by_kind[r["kind"]] = r["c"]

        stats.elapsed_s = time.perf_counter() - t0
        try:
            stats.db_bytes = self.db_size_bytes()
        except OSError:
            stats.db_bytes = 0
        return stats

    # -- per file ----------------------------------------------------------
    def _delete_file(self, repo_id: int, file_id: int, rel: str) -> None:
        """Remove a file and everything that pointed at it.

        `ON DELETE CASCADE` covers symbols/edges owned by the file, but edges
        *from other files* that resolve into it are only removed if
        `PRAGMA foreign_keys` is on — it is, but import edges reference
        `dst_file_id` and symbol edges `dst_id`; both cascade. Still, we delete
        the dependents' edges explicitly so the graph can never keep a dangling
        edge even if a connection was opened without the pragma.
        """
        self.conn.execute(
            """DELETE FROM edges WHERE repo_id=? AND dst_id IN
               (SELECT id FROM symbols WHERE file_id=?)""",
            (repo_id, file_id),
        )
        self.conn.execute(
            "DELETE FROM edges WHERE repo_id=? AND dst_file_id=?", (repo_id, file_id)
        )
        self.conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
        self.conn.execute("DELETE FROM files WHERE id=?", (file_id,))

    def _files_with_edges_into(self, repo_id: int, rel_paths: list[str]) -> set[str]:
        """Files that hold an edge whose target lives in one of `rel_paths`."""
        if not rel_paths:
            return set()
        qmarks = ",".join("?" * len(rel_paths))
        out: set[str] = set()
        for r in self.conn.execute(
            f"""SELECT DISTINCT f.path AS path FROM edges e
                JOIN symbols d ON d.id=e.dst_id
                JOIN files f ON f.id=d.file_id
                WHERE e.repo_id=? AND f.path IN ({qmarks})""",
            (repo_id, *rel_paths),
        ):
            out.add(r["path"])
        for r in self.conn.execute(
            f"""SELECT DISTINCT f.path AS path FROM edges e
                JOIN symbols s ON s.id=e.src_id
                JOIN files f ON f.id=s.file_id
                JOIN files df ON df.id=e.dst_file_id
                WHERE e.repo_id=? AND df.path IN ({qmarks})""",
            (repo_id, *rel_paths),
        ):
            out.add(r["path"])
        return out

    def _write_file(
        self,
        repo_id: int,
        rel: str,
        path: Path,
        h: str,
        size: int,
        lang: str,
        data: bytes,
        pf: ts.ParsedFile,
    ) -> int:
        line_count = ts.count_lines(data)
        row = self.conn.execute(
            "SELECT id FROM files WHERE repo_id=? AND path=?", (repo_id, rel)
        ).fetchone()
        if row:
            file_id = row["id"]
            self.conn.execute(
                """UPDATE files SET abs_path=?, language=?, content_hash=?, size_bytes=?,
                   line_count=?, parse_ok=?, parse_error=?, indexed_at=datetime('now')
                   WHERE id=?""",
                (
                    str(path),
                    lang,
                    h,
                    size,
                    line_count,
                    1 if pf.parse_ok else 0,
                    pf.parse_error,
                    file_id,
                ),
            )
            # cascade removes old symbols/edges
            self.conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
        else:
            cur = self.conn.execute(
                """INSERT INTO files(repo_id, path, abs_path, language, content_hash,
                   size_bytes, line_count, parse_ok, parse_error)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (repo_id, rel, str(path), lang, h, size, line_count, 1 if pf.parse_ok else 0, pf.parse_error),
            )
            file_id = int(cur.lastrowid)

        # symbols (two passes so parent_id can be linked)
        id_by_qual: dict[str, int] = {}
        for s in pf.symbols:
            parent_id = id_by_qual.get(s.parent_qualname) if s.parent_qualname else None
            cur = self.conn.execute(
                """INSERT INTO symbols(repo_id, file_id, name, qualname, kind, language,
                   start_line, end_line, start_byte, end_byte, signature, docstring,
                   is_public, parent_id, complexity, body_lines)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    repo_id,
                    file_id,
                    s.name,
                    s.qualname,
                    s.kind,
                    lang,
                    s.start_line,
                    s.end_line,
                    s.start_byte,
                    s.end_byte,
                    s.signature,
                    s.docstring,
                    1 if s.is_public else 0,
                    parent_id,
                    s.complexity,
                    s.body_lines,
                ),
            )
            id_by_qual[s.qualname] = int(cur.lastrowid)

        # stash raw facts for the resolution pass
        self._pending[rel] = {
            "file_id": file_id,
            "language": lang,
            "calls": pf.calls,
            "imports": pf.imports,
            "inherits": pf.inherits,
            "symbols": {s.qualname: s for s in pf.symbols},
            "ids": dict(id_by_qual),
        }
        return len(pf.symbols)

    # -- edge resolution ---------------------------------------------------

    def _resolve_edges(self, repo_id: int, touched: list[str], stats: IndexStats) -> None:
        if not touched:
            return

        # clear edges originating from touched files
        qmarks = ",".join("?" * len(touched))
        self.conn.execute(
            f"""DELETE FROM edges WHERE repo_id=? AND src_id IN (
                SELECT s.id FROM symbols s JOIN files f ON f.id=s.file_id
                WHERE f.repo_id=? AND f.path IN ({qmarks}))""",
            (repo_id, repo_id, *touched),
        )

        # global name index (built once per run)
        name_index: dict[str, list[tuple[int, str, str]]] = {}
        qual_index: dict[str, list[tuple[int, str, str]]] = {}
        for r in self.conn.execute(
            """SELECT s.id, s.name, s.kind, s.qualname, f.path FROM symbols s
               JOIN files f ON f.id=s.file_id WHERE s.repo_id=?""",
            (repo_id,),
        ):
            name_index.setdefault(r["name"], []).append((r["id"], r["kind"], r["path"]))
            qual_index.setdefault(r["qualname"], []).append((r["id"], r["kind"], r["path"]))

        # module -> file mapping for import-aware resolution
        path_to_id: dict[str, int] = {
            r["path"]: r["id"]
            for r in self.conn.execute("SELECT id, path FROM files WHERE repo_id=?", (repo_id,))
        }
        module_map = _build_module_map(path_to_id)

        for rel in touched:
            pend = self._pending.get(rel)
            if not pend:
                continue
            ids = pend["ids"]
            local_syms = pend["symbols"]
            local_name_index: dict[str, list[str]] = {}
            for q, s in local_syms.items():
                local_name_index.setdefault(s.name, []).append(q)
            language = pend.get("language", "python")

            # -- calls
            for c in pend["calls"]:
                src_id = ids.get(c.caller) if c.caller else _module_symbol_id(ids)
                if src_id is None:
                    continue
                dst_id, resolved, via = _resolve_call(
                    c, rel, local_syms, ids, name_index, module_map, pend["imports"],
                    path_to_id, language, local_name_index, qual_index,
                )
                self.conn.execute(
                    CREATE_EDGE_SQL,
                    (repo_id, src_id, dst_id, None, "call",
                     None if resolved else c.callee, c.line, 1 if resolved else 0, via),
                )

            # -- imports (file-level edge: src = first symbol of the file, dst = target FILE)
            src_id = _module_symbol_id(ids) or (next(iter(ids.values())) if ids else None)
            if src_id is not None:
                for imp in pend["imports"]:
                    target_file = _match_module(imp.module, module_map, path_to_id)
                    resolved = target_file is not None
                    self.conn.execute(
                        CREATE_EDGE_SQL,
                        (repo_id, src_id, None, target_file, "import", imp.module,
                         imp.line, 1 if resolved else 0, "module-map" if resolved else None),
                    )

            # -- inheritance
            for inh in pend["inherits"]:
                src_id = ids.get(inh.child)
                if src_id is None:
                    continue
                cands = name_index.get(inh.base, [])
                cands = [c for c in cands if c[1] in ("class", "interface", "trait", "struct")]
                dst_id = cands[0][0] if len(cands) == 1 else None
                self.conn.execute(
                    CREATE_EDGE_SQL,
                    (repo_id, src_id, dst_id, None, inh.kind, inh.base, inh.line,
                     1 if dst_id else 0, "unique" if dst_id else None),
                )

        stats.edges = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE repo_id=?", (repo_id,)
        ).fetchone()["c"]

    def db_size_bytes(self) -> int:
        """SQLite file + WAL sidecar (WAL is where fresh writes land until checkpoint)."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.cfg.db_path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    # -- stats -------------------------------------------------------------
    def stats(self) -> dict:
        repo_id = self.ensure_repo()
        q = lambda sql: self.conn.execute(sql, (repo_id,)).fetchone()[0]  # noqa: E731
        out = {
            "repo": str(self.root),
            "repo_id": repo_id,
            "files": q("SELECT COUNT(*) FROM files WHERE repo_id=?"),
            "symbols": q("SELECT COUNT(*) FROM symbols WHERE repo_id=?"),
            "edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=?"),
            "call_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND kind='call'"),
            "import_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND kind='import'"),
            "inherit_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND kind IN ('inherit','implements','extends')"),
            "resolved_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND resolved=1"),
            "unresolved_edges": q("SELECT COUNT(*) FROM edges WHERE repo_id=? AND resolved=0"),
            "parse_errors": q("SELECT COUNT(*) FROM files WHERE repo_id=? AND parse_ok=0"),
            "by_language": {
                r[0]: r[1]
                for r in self.conn.execute(
                    "SELECT language, COUNT(*) FROM files WHERE repo_id=? GROUP BY language", (repo_id,)
                )
            },
            "by_kind": {
                r[0]: r[1]
                for r in self.conn.execute(
                    "SELECT kind, COUNT(*) FROM symbols WHERE repo_id=? GROUP BY kind", (repo_id,)
                )
            },
            "db_bytes": os.path.getsize(self.cfg.db_path) if Path(self.cfg.db_path).exists() else 0,
        }
        return out


# --------------------------------------------------------------------------
# Resolution helpers
# --------------------------------------------------------------------------


def _module_symbol_id(ids: dict[str, int]) -> int | None:
    """Id of the module-level pseudo symbol if the file has one, else first symbol."""
    if not ids:
        return None
    # prefer the smallest start (top of file)
    return next(iter(ids.values()))


def _build_module_map(path_to_id: dict[str, int]) -> dict[str, int]:
    """Map dotted module names -> file id for import resolution."""
    out: dict[str, int] = {}
    for path, fid in path_to_id.items():
        p = Path(path)
        noext = p.with_suffix("")
        parts = list(noext.parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        out[".".join(parts)] = fid
        out["::".join(parts)] = fid
        # rust: src/foo/bar.rs -> foo::bar, and src/lib.rs -> crate root
        out.setdefault(parts[-1], fid)
    return out


def _match_module(module: str, module_map: dict[str, int], path_to_id: dict[str, int]) -> int | None:
    if not module:
        return None
    mod = module.strip().strip("'\"`")
    mod = mod.replace("/", ".").replace("::", ".")
    mod = mod.lstrip("./").replace("...", ".")
    if mod in module_map:
        return module_map[mod]
    # longest-prefix match (e.g. `from pkg.sub import x` -> pkg.sub)
    parts = mod.split(".")
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand in module_map:
            return module_map[cand]
    return None


def _split_qual(qual: str) -> tuple[str | None, str]:
    """Split a qualified name into (parent, leaf) using '.' or '::' separators."""
    for sep in ("::", "."):
        if sep in qual:
            parent, _, leaf = qual.rpartition(sep)
            return (parent or None), leaf
    return None, qual


def _join_qual(parent: str, leaf: str, language: str) -> str:
    sep = "::" if language == "rust" else "."
    return f"{parent}{sep}{leaf}"


def _resolve_call(
    c: ts.RawCall,
    rel: str,
    local_syms: dict[str, ts.RawSymbol],
    ids: dict[str, int],
    name_index: dict[str, list[tuple[int, str, str]]],
    module_map: dict[str, int],
    imports: list[ts.RawImport],
    path_to_id: dict[str, int],
    language: str = "python",
    local_name_index: dict[str, list[str]] | None = None,
    qual_index: dict[str, list[tuple[int, str, str]]] | None = None,
) -> tuple[int | None, bool, str | None]:
    """Resolve a call site to a symbol id.

    Returns (dst_id, resolved, strategy) where strategy names the rule that
    matched, so the resolution rate can be broken down by evidence type in the
    reports instead of being a single opaque number. The strategies are, in
    order: scope, receiver, file-unique, import, global-unique,
    callable-unique, dir-unique.
    """
    name = c.callee
    lni = local_name_index or {}
    qi = qual_index or {}

    # 1. enclosing-scope method: self.foo() / Self::foo() / this.foo()
    if c.caller:
        parent, _leaf = _split_qual(c.caller)
        if parent:
            cand = _join_qual(parent, name, language)
            if cand in ids:
                return ids[cand], True, "scope"

        # 2. receiver names a type: `Foo::bar()` / `Foo.bar()` / `Self::new()`
        if c.receiver:
            recv_leaf = _split_qual(c.receiver)[1]
            if recv_leaf in ("self", "Self", "this", "cls"):
                recv_leaf = parent if parent else recv_leaf
            cand2 = _join_qual(recv_leaf, name, language)
            if cand2 in ids:
                return ids[cand2], True, "receiver"
            # receiver is a local variable whose type was inferred from an
            # assignment in this file: `let e = Engine::new(...)` then `e.run()`.
            # The type may be defined in *this* file or anywhere else in the repo.
            if c.receiver_type:
                cand4 = _join_qual(c.receiver_type, name, language)
                if cand4 in ids:
                    return ids[cand4], True, "receiver"
                hits = qi.get(cand4, [])
                if len(hits) == 1:
                    return hits[0][0], True, "receiver"
            # fall back: receiver text happens to name a type in this file
            for q, s in local_syms.items():
                if s.kind in ("struct", "class", "interface", "trait") and s.name == recv_leaf:
                    cand3 = _join_qual(q, name, language)
                    if cand3 in ids:
                        return ids[cand3], True, "receiver"

        if name in ids:
            return ids[name], True, "scope"
        # unique same-file candidate
        same_file = lni.get(name, [])
        if len(same_file) == 1:
            return ids[same_file[0]], True, "file-unique"

    # 3. import-aware: this file imports `name` from a module we can map
    for imp in imports:
        if name in imp.names or imp.alias == name:
            fid = _match_module(imp.module, module_map, path_to_id)
            if fid is not None:
                for cand_id, _kind, p in name_index.get(name, []):
                    if path_to_id.get(p) == fid:
                        return cand_id, True, "import"

    # 4. globally unique name
    cands = name_index.get(name, [])
    if len(cands) == 1:
        return cands[0][0], True, "global-unique"

    # 5. unique among callables (filters out struct/enum ctors with the same name)
    callables = [x for x in cands if x[1] in ("function", "method")]
    if len(callables) == 1:
        return callables[0][0], True, "callable-unique"

    # 6. unique in the same directory
    same_dir = [x for x in cands if Path(x[2]).parent == Path(rel).parent]
    if len(same_dir) == 1:
        return same_dir[0][0], True, "dir-unique"

    return None, False, None
