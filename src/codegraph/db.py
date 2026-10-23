"""SQLite knowledge-graph schema and connection helpers.

Design notes
------------
* Single-file embedded DB (`.codegraph/graph.db`), no external graph service.
* `symbols` / `edges` / `files` are the normalized graph.
* `symbols_fts` is an FTS5 external-content index over symbol name + signature
  + docstring so we get fast prefix / token search without duplicating rows.
* All timings and token counters live in `llm_calls` so every number in the
  reports is queryable straight out of SQLite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path    TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    indexed_at   TEXT,
    git_commit   TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id       INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path          TEXT NOT NULL,             -- repo-relative, forward slashes
    abs_path      TEXT NOT NULL,
    language      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    line_count    INTEGER NOT NULL,
    parse_ok      INTEGER NOT NULL DEFAULT 1,
    parse_error   TEXT,
    indexed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (repo_id, path)
);

CREATE TABLE IF NOT EXISTS symbols (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    qualname     TEXT NOT NULL,             -- dotted / :: qualified name
    kind         TEXT NOT NULL,             -- function|method|class|struct|interface|trait|enum|const|module|impl
    language     TEXT NOT NULL,
    start_line   INTEGER NOT NULL,
    end_line     INTEGER NOT NULL,
    start_byte   INTEGER NOT NULL,
    end_byte     INTEGER NOT NULL,
    signature    TEXT,
    docstring    TEXT,
    is_public    INTEGER NOT NULL DEFAULT 1,
    parent_id    INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    complexity   INTEGER NOT NULL DEFAULT 0,   -- count of branch nodes in body
    body_lines   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (repo_id, file_id, qualname, start_line)
);

CREATE TABLE IF NOT EXISTS edges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    src_id       INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    dst_id       INTEGER REFERENCES symbols(id) ON DELETE CASCADE,  -- NULL = unresolved
    dst_file_id  INTEGER REFERENCES files(id) ON DELETE CASCADE,    -- set for file-level (import) edges
    kind         TEXT NOT NULL,   -- call|import|inherit|implements|contains|reference
    dst_name     TEXT,            -- unresolved target text (dynamic call evidence)
    line         INTEGER,
    resolved     INTEGER NOT NULL DEFAULT 1,
    via          TEXT,            -- resolution strategy: scope|receiver|import|unique|dir|heuristic
    UNIQUE (repo_id, src_id, dst_id, dst_file_id, kind, dst_name, line)
);

CREATE INDEX IF NOT EXISTS idx_symbols_name     ON symbols(repo_id, name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualname ON symbols(repo_id, qualname);
CREATE INDEX IF NOT EXISTS idx_symbols_file     ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_kind     ON symbols(repo_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_src        ON edges(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst        ON edges(dst_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst_file   ON edges(dst_file_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_kind       ON edges(repo_id, kind);
CREATE INDEX IF NOT EXISTS idx_files_repo       ON files(repo_id, path);

-- FTS5 external content index over symbols
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, qualname, signature, docstring,
    content='symbols', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, qualname, signature, docstring)
    VALUES (new.id, new.name, new.qualname, new.signature, new.docstring);
END;
CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualname, signature, docstring)
    VALUES ('delete', old.id, old.name, old.qualname, old.signature, old.docstring);
END;
CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualname, signature, docstring)
    VALUES ('delete', old.id, old.name, old.qualname, old.signature, old.docstring);
    INSERT INTO symbols_fts(rowid, name, qualname, signature, docstring)
    VALUES (new.id, new.name, new.qualname, new.signature, new.docstring);
END;

-- Per-file LLM summaries (cache layer)
CREATE TABLE IF NOT EXISTS file_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id         INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    content_hash    TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    model           TEXT NOT NULL,
    summary_json    TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (file_id, content_hash, prompt_version, model)
);

-- Every LLM call is recorded here: the single source of truth for token reports
CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id        INTEGER,
    run_id         TEXT NOT NULL,
    phase          TEXT NOT NULL,     -- summary|arch|guide|review|other
    file_path      TEXT,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_chars   INTEGER NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    token_source   TEXT NOT NULL,     -- server|estimate
    latency_ms     INTEGER NOT NULL,
    cache_hit      INTEGER NOT NULL DEFAULT 0,
    avoided_input_tokens  INTEGER NOT NULL DEFAULT 0,
    avoided_output_tokens INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_run   ON llm_calls(run_id, phase);
CREATE INDEX IF NOT EXISTS idx_llm_calls_repo  ON llm_calls(repo_id, phase);

-- Analysis runs (full / incremental / arch / guide)
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    repo_id     INTEGER REFERENCES repos(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,       -- full|incremental|arch|guide|review
    model       TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    files_total INTEGER DEFAULT 0,
    files_called INTEGER DEFAULT 0,
    files_cached INTEGER DEFAULT 0,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    avoided_input_tokens  INTEGER DEFAULT 0,
    avoided_output_tokens INTEGER DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS arch_artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id       INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    run_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,   -- architecture|guide|review
    entry_symbol  TEXT,
    content       TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and lazily create) the knowledge-graph database."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
