"""Analysis pipeline: file summaries, caching, architecture, guide, review.

Token accounting invariants
---------------------------
* Every LLM call writes exactly one row into `llm_calls` with the *server*
  reported `prompt_eval_count` / `eval_count`.
* Cache hits write a row too (cache_hit=1) carrying the token counts the call
  *would* have cost, so "avoided tokens" is measured from real historical
  calls, not a guess.
* Runs are grouped by `run_id` in `runs`, so a report can be regenerated from
  SQLite without re-running anything.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import prompts
from .config import (
    DEFAULT_OUTPUT,
    IndexConfig,
    LLMConfig,
    PROMPT_TEMPLATE_VERSION,
    SUMMARY_TEMPLATE_VERSION,
)
from .db import connect, init_db
from .llm import OllamaClient
from .prompts import FileContext, MAX_SOURCE_CHARS
from .query import QueryEngine


# --------------------------------------------------------------------------
# Result containers
# --------------------------------------------------------------------------


@dataclass
class RunTotals:
    run_id: str
    kind: str
    files_total: int = 0
    files_called: int = 0
    files_cached: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    avoided_input_tokens: int = 0
    avoided_output_tokens: int = 0
    errors: int = 0
    elapsed_s: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        denom = self.files_called + self.files_cached
        return (self.files_cached / denom) if denom else 0.0

    @property
    def saved_ratio(self) -> float:
        would = self.total_tokens + self.avoided_input_tokens + self.avoided_output_tokens
        avoided = self.avoided_input_tokens + self.avoided_output_tokens
        return (avoided / would) if would else 0.0


def _extract_json(text: str) -> dict | list | None:
    """Robustly pull the first JSON object/array out of a model response.

    The earliest opening bracket wins, so a top-level array containing objects
    is returned as a list rather than the first inner object. Truncated output
    (num_predict cut-off) is repaired by closing any open brackets/strings.
    """
    if not text:
        return None
    text = text.strip()
    # strip markdown fences
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # try the whole payload first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (dict, list)):
            return parsed
    except json.JSONDecodeError:
        pass

    # otherwise scan for the first balanced JSON value
    i_obj = text.find("{")
    i_arr = text.find("[")
    starts = [i for i in (i_obj, i_arr) if i != -1]
    if not starts:
        return None
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    break

    # last resort: the model was cut off mid-object — repair and retry
    return _repair_truncated_json(text[start:])


def _repair_truncated_json(fragment: str) -> dict | list | None:
    """Close unterminated strings/arrays/objects in a truncated JSON fragment.

    Model output cut off by `num_predict` is the normal case for this project,
    so this has to handle every prefix of a valid document, not just the easy
    ones. The algorithm is: walk once tracking the bracket stack and whether we
    are inside a string; remember the last offset at which the document was
    *syntactically complete enough to close* (a value boundary, not a `:` or a
    comma); cut back there and append the missing closers.
    """
    if not fragment:
        return None
    fragment = fragment.strip()
    if not fragment:
        return None

    def scan(text: str) -> tuple[list[str], bool, bool, int]:
        """(stack, in_str, escaped, last_safe_offset)."""
        stack: list[str] = []
        in_str = False
        esc = False
        last_safe = -1
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                    # a string value is a complete value
                    last_safe = i
                continue
            if ch == '"':
                in_str = True
            elif ch in "[{":
                stack.append(ch)
            elif ch in "]}":
                if stack:
                    stack.pop()
                last_safe = i
            elif ch.isdigit() or ch in "eE.-+" and stack:
                # primitive value characters count as a safe cut point
                last_safe = i
            elif ch in "tf" and stack:  # true/false/null tail
                last_safe = i
        return stack, in_str, esc, last_safe

    stack, in_str, _esc, last_safe = scan(fragment)
    # cut back to the last safe value boundary when we are inside a string or
    # dangling after a `:`/`,` (those are *not* safe: `{"a":` cannot be closed
    # by appending a brace).
    if in_str or (stack and fragment.rstrip()[-1:] in ",:"):
        if last_safe > 0:
            fragment = fragment[: last_safe + 1]
            stack, in_str, _esc, _ = scan(fragment)

    repaired = fragment.rstrip().rstrip(",")
    if in_str:
        repaired += '"'
    # a trailing `:` or `,` after the cut means the key had no value yet
    repaired = repaired.rstrip().rstrip(":").rstrip(",")
    for opener in reversed(stack):
        repaired += "}" if opener == "{" else "]"

    try:
        parsed = json.loads(repaired)
        if isinstance(parsed, (dict, list)):
            return parsed
    except json.JSONDecodeError:
        pass

    # second attempt: progressively drop trailing incomplete members.
    # Bounded to the tail of the fragment: this path only runs for output that
    # is not close to parseable, and a full scan per cut would be O(n^2).
    floor = max(0, len(fragment) - 400)
    for cut in range(len(fragment) - 1, floor, -1):
        ch = fragment[cut]
        if ch not in ",}]{[\"0123456789":
            continue
        st2, in2, _e2, _ = scan(fragment[:cut])
        if in2:
            continue
        cand = fragment[:cut].rstrip().rstrip(",")
        for opener in reversed(st2):
            cand += "}" if opener == "{" else "]"
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, (dict, list)):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _arch_num_ctx(prompt: str) -> int:
    """num_ctx large enough for the whole architecture prompt (~3.5 chars/token).

    Rounded up to the next 2048 with a 15% safety margin, because a context
    window that is too small silently truncates the prompt server-side. The
    result is hard-clamped to ARCH_MAX_CTX: on an 8 GB GPU anything above 16k
    exhausts the KV-cache budget and OOMs, so the prompt must be trimmed to fit
    (see `_fit_arch_prompt`) rather than the context window grown.
    """
    need = int(len(prompt) / 3.5 * 1.15) + 512
    rounded = max(8192, -(-need // 2048) * 2048)
    return min(rounded, ARCH_MAX_CTX)


# The architecture/guide phases are single repo-wide calls and must carry far
# more context than a per-file summary. On an 8 GB GPU 16k context is the
# practical ceiling for a 7B q4 model (model ~4.7 GB + KV cache ~0.9 GB), so the
# prompt is trimmed to fit that window rather than being truncated silently by
# Ollama. Trimming drops the *lowest-value* file summaries (they are ordered
# entry-file-first, then by fan-in) — never the instructions.
ARCH_MAX_CTX = 16_384
ARCH_PROMPT_BUDGET_CHARS = int((ARCH_MAX_CTX - 1_600) * 3.5)  # reserve output tokens


def _fit_arch_prompt(build, summaries: dict[str, dict], **kwargs) -> str:
    """Build a repo-wide prompt, shedding low-value summaries until it fits."""
    items = list(summaries.items())
    prompt = build(items, **kwargs)
    while len(prompt) > ARCH_PROMPT_BUDGET_CHARS and len(items) > 10:
        # drop 10% from the tail each round, then rebuild
        keep = max(10, int(len(items) * 0.9))
        items = items[:keep]
        prompt = build(items, **kwargs)
    return prompt


def _normalize_summary(raw: dict | list | None, fallback_reason: str = "") -> dict:
    if isinstance(raw, dict):
        return {
            "responsibility": str(raw.get("responsibility", ""))[:400] or fallback_reason,
            "key_functions": [str(x)[:200] for x in (raw.get("key_functions") or [])][:8],
            "public_interface": [str(x)[:120] for x in (raw.get("public_interface") or [])][:12],
            "design_notes": [str(x)[:300] for x in (raw.get("design_notes") or [])][:5],
            "risk_flags": [str(x)[:300] for x in (raw.get("risk_flags") or [])][:5],
        }
    return {
        "responsibility": fallback_reason or "(unparsed model output)",
        "key_functions": [],
        "public_interface": [],
        "design_notes": [],
        "risk_flags": [],
    }


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


class Pipeline:
    def __init__(
        self,
        root: Path,
        cfg: IndexConfig | None = None,
        llm_cfg: LLMConfig | None = None,
        client: OllamaClient | None = None,
    ):
        self.root = Path(root).resolve()
        self.cfg = cfg or IndexConfig()
        self.llm_cfg = llm_cfg or LLMConfig()
        self.qe = QueryEngine(self.root, self.cfg)
        self.client = client or OllamaClient(self.llm_cfg)
        self.conn = self.qe.conn
        init_db(self.conn)
        self.repo_id = self.qe.repo_id

    # -- run bookkeeping ---------------------------------------------------
    def _new_run(self, kind: str, note: str = "") -> str:
        run_id = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.conn.execute(
            "INSERT INTO runs(run_id, repo_id, kind, model, note) VALUES (?,?,?,?,?)",
            (run_id, self.repo_id, kind, self.llm_cfg.model, note),
        )
        self.conn.commit()
        return run_id

    def _log_call(
        self,
        run_id: str,
        phase: str,
        file_path: str | None,
        res,
        prompt_version: str,
        cache_hit: bool = False,
        avoided_in: int = 0,
        avoided_out: int = 0,
    ) -> None:
        self.conn.execute(
            """INSERT INTO llm_calls(repo_id, run_id, phase, file_path, model, prompt_version,
               prompt_chars, input_tokens, output_tokens, token_source, latency_ms, cache_hit,
               avoided_input_tokens, avoided_output_tokens, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.repo_id,
                run_id,
                phase,
                file_path,
                res.model if res is not None else self.llm_cfg.model,
                prompt_version,
                res.prompt_chars if res is not None else 0,
                res.input_tokens if res is not None else 0,
                res.output_tokens if res is not None else 0,
                res.token_source if res is not None else "server",
                res.latency_ms if res is not None else 0,
                1 if cache_hit else 0,
                avoided_in,
                avoided_out,
                res.error if res is not None else None,
            ),
        )

    def _finish_run(self, run: RunTotals) -> None:
        self.conn.execute(
            """UPDATE runs SET finished_at=datetime('now'), files_total=?, files_called=?,
               files_cached=?, input_tokens=?, output_tokens=?, avoided_input_tokens=?,
               avoided_output_tokens=? WHERE run_id=?""",
            (
                run.files_total,
                run.files_called,
                run.files_cached,
                run.input_tokens,
                run.output_tokens,
                run.avoided_input_tokens,
                run.avoided_output_tokens,
                run.run_id,
            ),
        )
        self.conn.commit()

    # -- context building --------------------------------------------------
    def build_file_context(
        self,
        rel_path: str,
        neighbour_limit: int = 6,
        include_source: bool = True,
    ) -> FileContext:
        frow = self.qe.file_row(rel_path)
        if frow is None:
            raise KeyError(f"file not indexed: {rel_path}")
        syms = self.qe.file_symbols(rel_path)
        sym_dicts = [s.as_dict() for s in syms]

        # calls originating from this file
        out_calls: list[tuple[str, str, int]] = []
        unresolved: list[tuple[str, int]] = []
        for r in self.conn.execute(
            """SELECT e.kind, e.dst_name, e.line, e.resolved,
                      s.qualname AS src_q, d.qualname AS dst_q
               FROM edges e
               JOIN symbols s ON s.id=e.src_id
               JOIN files f ON f.id=s.file_id
               LEFT JOIN symbols d ON d.id=e.dst_id
               WHERE f.path=? AND f.repo_id=? AND e.kind='call'
               ORDER BY e.line""",
            (rel_path, self.repo_id),
        ):
            if r["resolved"] and r["dst_q"]:
                out_calls.append((r["src_q"], r["dst_q"], r["line"]))
            else:
                unresolved.append((r["dst_name"] or "?", r["line"]))

        imports: list[str] = [
            r["dst_name"] or ""
            for r in self.conn.execute(
                """SELECT e.dst_name FROM edges e
                   JOIN symbols s ON s.id=e.src_id JOIN files f ON f.id=s.file_id
                   WHERE f.path=? AND f.repo_id=? AND e.kind='import'""",
                (rel_path, self.repo_id),
            )
        ]

        deps = self.qe.file_dependencies(rel_path)

        # neighbour summaries: dependents first, then imports, then same dir
        neigh: dict[str, dict] = {}
        order = list(deps["dependents"]) + list(deps["imports"])
        same_dir = [
            r["path"]
            for r in self.conn.execute(
                "SELECT path FROM files WHERE repo_id=? AND path LIKE ? AND path != ?",
                (self.repo_id, str(Path(rel_path).parent.as_posix()) + "/%", rel_path),
            )
        ]
        order += same_dir
        for p in order:
            if len(neigh) >= neighbour_limit:
                break
            s = self.get_cached_summary(p)
            if s:
                neigh[p] = s

        excerpt = ""
        if include_source:
            abs_path = Path(frow["abs_path"])
            try:
                excerpt = abs_path.read_text(encoding="utf-8", errors="replace")[:MAX_SOURCE_CHARS]
            except OSError:
                excerpt = ""

        return FileContext(
            path=rel_path,
            language=frow["language"],
            line_count=frow["line_count"],
            symbols=sym_dicts,
            outgoing_calls=out_calls,
            unresolved_calls=unresolved,
            imports=[i for i in imports if i],
            dependents=deps["dependents"],
            dependencies=deps["imports"],
            neighbour_summaries=neigh,
            source_excerpt=excerpt,
        )

    # -- summary cache -----------------------------------------------------
    def get_cached_summary(self, rel_path: str) -> dict | None:
        frow = self.qe.file_row(rel_path)
        if frow is None:
            return None
        row = self.conn.execute(
            """SELECT summary_json FROM file_summaries
               WHERE file_id=? AND content_hash=? AND prompt_version=? AND model=?""",
            (frow["id"], frow["content_hash"], SUMMARY_TEMPLATE_VERSION, self.llm_cfg.model),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["summary_json"])
        except json.JSONDecodeError:
            return None

    def _cached_call_tokens(self, rel_path: str) -> tuple[int, int]:
        """Historical token cost of summarising this file (for avoided-token maths)."""
        row = self.conn.execute(
            """SELECT input_tokens, output_tokens FROM llm_calls
               WHERE repo_id=? AND file_path=? AND phase='summary' AND cache_hit=0
               AND error IS NULL ORDER BY id DESC LIMIT 1""",
            (self.repo_id, rel_path),
        ).fetchone()
        if row is None:
            return 0, 0
        return row["input_tokens"], row["output_tokens"]

    # -- phase: full / incremental summaries -------------------------------
    def summarize(
        self,
        paths: list[str] | None = None,
        *,
        kind: str = "full",
        force: bool = False,
        include_source: bool = True,
        limit: int | None = None,
        verbose: bool = True,
        progress_cb=None,
    ) -> RunTotals:
        t0 = time.perf_counter()
        run_id = self._new_run(kind, note=f"paths={len(paths) if paths else 'all'}")
        run = RunTotals(run_id=run_id, kind=kind)

        targets = paths if paths is not None else [r["path"] for r in self.qe.files()]
        if limit:
            targets = targets[:limit]
        run.files_total = len(targets)

        for i, rel in enumerate(targets, 1):
            if not force:
                cached = self.get_cached_summary(rel)
                if cached is not None:
                    ai, ao = self._cached_call_tokens(rel)
                    run.files_cached += 1
                    run.avoided_input_tokens += ai
                    run.avoided_output_tokens += ao
                    self._log_call(
                        run_id, "summary", rel, None, SUMMARY_TEMPLATE_VERSION,
                        cache_hit=True, avoided_in=ai, avoided_out=ao,
                    )
                    if progress_cb:
                        progress_cb(i, len(targets), rel, "cached")
                    continue

            try:
                ctx = self.build_file_context(rel, include_source=include_source)
            except KeyError:
                continue
            prompt = prompts.build_summary_prompt(ctx, include_source=include_source)
            res = self.client.generate(
                prompt, system=prompts.SUMMARY_SYSTEM, json_mode=True, num_predict=700
            )
            raw = _extract_json(res.text)
            summary = _normalize_summary(raw, fallback_reason=f"{ctx.language} file, {len(ctx.symbols)} symbols")

            if res.error:
                run.errors += 1
            else:
                run.files_called += 1
                run.input_tokens += res.input_tokens
                run.output_tokens += res.output_tokens

            self._log_call(run_id, "summary", rel, res, SUMMARY_TEMPLATE_VERSION)

            if not res.error:
                frow = self.qe.file_row(rel)
                self.conn.execute(
                    """INSERT INTO file_summaries(repo_id, file_id, content_hash, prompt_version,
                       model, summary_json, input_tokens, output_tokens, latency_ms)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(file_id, content_hash, prompt_version, model) DO UPDATE SET
                       summary_json=excluded.summary_json, input_tokens=excluded.input_tokens,
                       output_tokens=excluded.output_tokens, latency_ms=excluded.latency_ms,
                       created_at=datetime('now')""",
                    (
                        self.repo_id,
                        frow["id"],
                        frow["content_hash"],
                        SUMMARY_TEMPLATE_VERSION,
                        self.llm_cfg.model,
                        json.dumps(summary, ensure_ascii=False),
                        res.input_tokens,
                        res.output_tokens,
                        res.latency_ms,
                    ),
                )
                self.conn.commit()

            if progress_cb:
                progress_cb(i, len(targets), rel, "error" if res.error else "called")
            if verbose and (i % 10 == 0 or i == len(targets)):
                print(
                    f"  [{i}/{len(targets)}] called={run.files_called} cached={run.files_cached} "
                    f"tok={run.total_tokens:,}",
                    flush=True,
                )

        run.elapsed_s = time.perf_counter() - t0
        self._finish_run(run)
        return run

    # -- phase: architecture ----------------------------------------------
    def analyze_architecture(
        self,
        entry_qualname: str | None = None,
        *,
        max_summaries: int | None = None,
        verbose: bool = True,
    ) -> tuple[RunTotals, str]:
        t0 = time.perf_counter()
        run_id = self._new_run("arch", note=f"entry={entry_qualname}")
        run = RunTotals(run_id=run_id, kind="arch")

        entry = None
        if entry_qualname:
            # `--entry main` matches many symbols (build scripts, examples, tests).
            # Prefer exact-name hits over fuzzy FTS matches, rank by how much of
            # the graph each one actually drives, and demote test/build-script code.
            exact = self.conn.execute(
                """SELECT s.id, s.name, s.qualname, s.kind, f.path, s.start_line, s.end_line,
                          s.signature, s.is_public, s.complexity, s.body_lines
                   FROM symbols s JOIN files f ON f.id = s.file_id
                   WHERE s.repo_id=? AND (s.name=? OR s.qualname=?)
                     AND s.kind IN ('function','method')""",
                (self.repo_id, entry_qualname, entry_qualname),
            ).fetchall()

            pool: list[dict] = []
            for r in exact:
                pool.append({
                    "id": r["id"], "name": r["name"], "qualname": r["qualname"],
                    "kind": r["kind"], "language": "", "path": r["path"],
                    "start_line": r["start_line"], "end_line": r["end_line"],
                    "signature": r["signature"], "docstring": None,
                    "is_public": bool(r["is_public"]), "complexity": r["complexity"],
                    "body_lines": r["body_lines"],
                })
            if not pool:
                for c in self.qe.search(entry_qualname, limit=30):
                    if c.kind in ("function", "method"):
                        pool.append(c.as_dict())

            scored: list[tuple[int, dict]] = []
            for d in pool:
                path = d["path"].lower()
                out_deg = self.conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE src_id=? AND kind='call'", (d["id"],)
                ).fetchone()[0]
                penalty = 0
                if "test" in path or "example" in path or path.endswith("build.rs"):
                    penalty = 100
                scored.append((out_deg - penalty, d))
            scored.sort(key=lambda x: -x[0])
            if scored:
                entry = scored[0][1]
        if entry is None:
            eps = self.qe.entry_points(limit=5)
            if not eps:
                raise RuntimeError("no entry points found; index the repo first")
            entry = eps[0]

        chain = self.qe.call_chain(entry["id"], max_depth=4, max_nodes=150)

        # File summaries: entry file first, then highest fan-in files.
        # All of them by default — the architecture prompt is meant to carry the
        # whole repository picture, not a 25-file sample.
        summaries: dict[str, dict] = {}
        order = [entry["path"]]
        for r in self.conn.execute(
            """SELECT f.path, COUNT(*) c FROM edges e
               JOIN symbols d ON d.id=e.dst_id JOIN files f ON f.id=d.file_id
               WHERE e.repo_id=? AND e.kind='call' GROUP BY f.path ORDER BY c DESC""",
            (self.repo_id,),
        ):
            order.append(r["path"])
        for p in order:
            if max_summaries is not None and len(summaries) >= max_summaries:
                break
            if p in summaries:
                continue
            s = self.get_cached_summary(p)
            if s:
                summaries[p] = s

        module_stats: dict[str, int] = {}
        for r in self.conn.execute(
            "SELECT path FROM files WHERE repo_id=?", (self.repo_id,)
        ):
            top = r["path"].split("/")[0] if "/" in r["path"] else "(root)"
            module_stats[top] = module_stats.get(top, 0) + 1

        dep_edges: list[tuple[str, str]] = []
        for r in self.conn.execute(
            """SELECT DISTINCT f1.path a, f2.path b FROM edges e
               JOIN symbols s ON s.id=e.src_id JOIN files f1 ON f1.id=s.file_id
               JOIN symbols d ON d.id=e.dst_id JOIN files f2 ON f2.id=d.file_id
               WHERE e.repo_id=? AND e.kind='import' AND e.resolved=1 AND f1.path != f2.path""",
            (self.repo_id,),
        ):
            dep_edges.append((r["a"], r["b"]))

        # Build the repo-wide prompt, shedding the lowest-value summaries until it
        # fits ARCH_MAX_CTX. On an 8 GB GPU a 16k window is the practical ceiling
        # (model ~4.7 GB + KV cache), so trimming beats growing num_ctx.
        def _build(kept_items):
            return prompts.build_architecture_prompt(
                self.root.name, entry, chain, dict(kept_items), module_stats, dep_edges
            )

        prompt = _fit_arch_prompt(_build, summaries)
        res = self.client.generate(
            prompt, system=prompts.ARCH_SYSTEM, num_predict=1400, num_ctx=_arch_num_ctx(prompt)
        )
        run.files_total = len(summaries)
        if res.error:
            run.errors += 1
        else:
            run.files_called = 1
            run.input_tokens += res.input_tokens
            run.output_tokens += res.output_tokens
        self._log_call(run_id, "arch", entry["path"], res, prompts.build_architecture_prompt_version())

        self.conn.execute(
            """INSERT INTO arch_artifacts(repo_id, run_id, kind, entry_symbol, content)
               VALUES (?,?,?,?,?)""",
            (self.repo_id, run_id, "architecture", entry["qualname"], res.text),
        )
        self.conn.commit()

        run.elapsed_s = time.perf_counter() - t0
        self._finish_run(run)
        if verbose:
            print(f"  arch: {res.input_tokens:,} in / {res.output_tokens:,} out in {run.elapsed_s:.1f}s")
        return run, res.text

    # -- phase: developer guide -------------------------------------------
    def generate_guide(
        self,
        architecture_md: str,
        *,
        max_summaries: int = 200,
        verbose: bool = True,
    ) -> tuple[RunTotals, str]:
        t0 = time.perf_counter()
        run_id = self._new_run("guide", note="from architecture")
        run = RunTotals(run_id=run_id, kind="guide")

        # Inject every cached summary (ordered by call fan-in), not a 20-file
        # sample: the guide is a repo-wide single call and its input token count
        # is meant to be well above a per-file summary.
        key: dict[str, dict] = {}
        for r in self.conn.execute(
            """SELECT f.path, COUNT(*) c FROM edges e
               JOIN symbols d ON d.id=e.dst_id JOIN files f ON f.id=d.file_id
               WHERE e.repo_id=? AND e.kind='call' GROUP BY f.path ORDER BY c DESC""",
            (self.repo_id,),
        ):
            if len(key) >= max_summaries:
                break
            s = self.get_cached_summary(r["path"])
            if s:
                key[r["path"]] = s

        stats = self.qe.stats()
        eps = self.qe.entry_points(limit=10)

        prompt = prompts.build_guide_prompt(self.root.name, architecture_md, key, stats, eps)
        res = self.client.generate(
            prompt, system=prompts.GUIDE_SYSTEM, num_predict=1800, num_ctx=8192
        )
        run.files_total = len(key)
        if res.error:
            run.errors += 1
        else:
            run.files_called = 1
            run.input_tokens += res.input_tokens
            run.output_tokens += res.output_tokens
        self._log_call(run_id, "guide", None, res, prompts.build_guide_prompt_version())

        self.conn.execute(
            """INSERT INTO arch_artifacts(repo_id, run_id, kind, entry_symbol, content)
               VALUES (?,?,?,?,?)""",
            (self.repo_id, run_id, "guide", None, res.text),
        )
        self.conn.commit()

        run.elapsed_s = time.perf_counter() - t0
        self._finish_run(run)
        if verbose:
            print(f"  guide: {res.input_tokens:,} in / {res.output_tokens:,} out in {run.elapsed_s:.1f}s")
        return run, res.text

    # -- phase: review -----------------------------------------------------
    def review(
        self,
        paths: list[str],
        *,
        verbose: bool = True,
    ) -> tuple[RunTotals, list[dict]]:
        t0 = time.perf_counter()
        run_id = self._new_run("review", note=f"files={len(paths)}")
        run = RunTotals(run_id=run_id, kind="review")
        run.files_total = len(paths)
        findings: list[dict] = []

        for rel in paths:
            try:
                ctx = self.build_file_context(rel, include_source=True)
            except KeyError:
                continue
            prompt = prompts.build_review_prompt(ctx)
            res = self.client.generate(
                prompt, system=prompts.REVIEW_SYSTEM, json_mode=True, num_predict=700
            )
            self._log_call(run_id, "review", rel, res, prompts.build_review_prompt_version())
            if res.error:
                run.errors += 1
                continue
            run.files_called += 1
            run.input_tokens += res.input_tokens
            run.output_tokens += res.output_tokens
            raw = _extract_json(res.text)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        item["file"] = rel
                        findings.append(item)
            if verbose:
                print(f"  review {rel}: {len(raw) if isinstance(raw, list) else 0} findings")

        run.elapsed_s = time.perf_counter() - t0
        self._finish_run(run)
        return run, findings

    # -- reports -----------------------------------------------------------
    def token_report(self) -> dict:
        """Aggregate everything from llm_calls / runs — single source of truth."""
        rows = self.conn.execute(
            """SELECT phase, cache_hit, COUNT(*) n,
                      SUM(input_tokens) in_tok, SUM(output_tokens) out_tok,
                      SUM(avoided_input_tokens) av_in, SUM(avoided_output_tokens) av_out,
                      SUM(latency_ms) lat, SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) errs,
                      SUM(CASE WHEN token_source='estimate' THEN 1 ELSE 0 END) est
               FROM llm_calls WHERE repo_id=? GROUP BY phase, cache_hit""",
            (self.repo_id,),
        ).fetchall()
        by_phase: dict[str, dict] = {}
        for r in rows:
            p = by_phase.setdefault(
                r["phase"],
                {
                    "calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0,
                    "avoided_input_tokens": 0, "avoided_output_tokens": 0,
                    "latency_ms": 0, "errors": 0, "estimated_calls": 0,
                },
            )
            if r["cache_hit"]:
                p["cache_hits"] += r["n"]
            else:
                p["calls"] += r["n"]
            p["input_tokens"] += r["in_tok"] or 0
            p["output_tokens"] += r["out_tok"] or 0
            p["avoided_input_tokens"] += r["av_in"] or 0
            p["avoided_output_tokens"] += r["av_out"] or 0
            p["latency_ms"] += r["lat"] or 0
            p["errors"] += r["errs"] or 0
            p["estimated_calls"] += r["est"] or 0

        per_file = [
            dict(r)
            for r in self.conn.execute(
                """SELECT file_path, input_tokens, output_tokens, latency_ms, run_id
                   FROM llm_calls
                   WHERE repo_id=? AND phase='summary' AND cache_hit=0 AND error IS NULL
                   ORDER BY id""",
                (self.repo_id,),
            )
        ]
        totals = [r["input_tokens"] + r["output_tokens"] for r in per_file]
        totals_sorted = sorted(totals)
        n = len(totals_sorted)

        def pct(p: float) -> int:
            if not totals_sorted:
                return 0
            idx = min(n - 1, max(0, int(round((p / 100.0) * n + 0.5)) - 1))
            return totals_sorted[idx]

        runs = [dict(r) for r in self.conn.execute(
            "SELECT * FROM runs WHERE repo_id=? ORDER BY started_at", (self.repo_id,)
        )]

        return {
            "repo": str(self.root),
            "model": self.llm_cfg.model,
            "by_phase": by_phase,
            "summary_per_file": {
                "n": n,
                "mean": (sum(totals) / n) if n else 0,
                "p50": pct(50),
                "p95": pct(95),
                "max": max(totals) if totals else 0,
                "min": min(totals) if totals else 0,
            },
            "runs": runs,
        }
