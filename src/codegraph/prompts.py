"""Prompt construction: AST-grounded context for the local model.

This module is the heart of the "AST beats grep" claim. Every prompt is built
from *graph facts* rather than raw text dumps:

    symbol table (kind / line range / signature / complexity)
  + intra-file call edges (resolved targets and unresolved dynamic calls)
  + cross-file dependency neighbourhood (imports + dependents, with their
    one-line responsibilities taken from the cached summaries)
  + optional source excerpt capped by a token budget

The token budget is enforced by a character budget derived from the same
CHARS_PER_TOKEN ratio used by the estimator, so prompts never silently blow
past `num_ctx`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import (
    GUIDE_TEMPLATE_VERSION,
    SUMMARY_TEMPLATE_VERSION,
    ARCH_TEMPLATE_VERSION,
    REVIEW_TEMPLATE_VERSION,
)
from .llm import CHARS_PER_TOKEN

MAX_PROMPT_CHARS = int(5_000 * CHARS_PER_TOKEN)  # ~5k tokens of context
MAX_SOURCE_CHARS = int(2_600 * CHARS_PER_TOKEN)


def _fit_prompt(head: str, source_block: str, tail: str) -> str:
    """Assemble head + optional source + tail within MAX_PROMPT_CHARS.

    The source excerpt is the only elastic part: the instructions/schema in
    `tail` must always survive, because a prompt cut off before "TASK:" is
    useless (the model gets no output contract and the call is wasted).
    Returns a string of length <= MAX_PROMPT_CHARS whenever tail fits the budget.
    """
    budget = MAX_PROMPT_CHARS - len(head) - len(tail)
    if source_block and budget > 200:
        # `source_block` is "\\nHEADER:\\n```lang\\n<body>\\n```\\n"; trim the body
        fence = source_block.find("```")
        if fence != -1:
            header = source_block[: fence + 3] + "\n"
            body = source_block[len(header):]
            closing = "\n```\n"
            keep = max(0, budget - len(header) - len(closing))
            source_block = header + body[:keep] + closing
        else:
            source_block = source_block[:budget]
    elif budget <= 200:
        source_block = ""
    return head + source_block + tail


@dataclass
class FileContext:
    """Everything the graph knows about one file, ready for prompting."""

    path: str
    language: str
    line_count: int
    symbols: list[dict]                 # name, kind, lines, signature, complexity, docstring
    outgoing_calls: list[tuple[str, str, int]]   # (caller, callee, line)
    unresolved_calls: list[tuple[str, int]]      # (callee_text, line)
    imports: list[str]
    dependents: list[str]
    dependencies: list[str]
    neighbour_summaries: dict[str, dict]  # path -> summary json
    source_excerpt: str = ""

    @property
    def public_symbols(self) -> list[dict]:
        return [s for s in self.symbols if s.get("is_public")]


def _fmt_symbols(symbols: list[dict], limit: int = 40) -> str:
    lines = []
    for s in symbols[:limit]:
        sig = (s.get("signature") or s["name"]).strip()
        sig = sig.replace("\n", " ")[:160]
        lines.append(f"- [{s['kind']}] {s['qualname']}  (L{s['start_line']}-{s['end_line']})  {sig}")
    if len(symbols) > limit:
        lines.append(f"- ... and {len(symbols) - limit} more symbols")
    return "\n".join(lines)


def _fmt_calls(ctx: FileContext, limit: int = 60) -> str:
    if not ctx.outgoing_calls:
        return "(no statically resolved calls)"
    lines = []
    for caller, callee, line in ctx.outgoing_calls[:limit]:
        lines.append(f"- {caller or '<module>'} -> {callee}  (L{line})")
    if len(ctx.outgoing_calls) > limit:
        lines.append(f"- ... and {len(ctx.outgoing_calls) - limit} more call edges")
    return "\n".join(lines)


def _fmt_neighbours(ctx: FileContext, limit: int = 8) -> str:
    if not ctx.neighbour_summaries:
        return "(no neighbouring summaries available)"
    parts = []
    for path, summ in list(ctx.neighbour_summaries.items())[:limit]:
        resp = (summ.get("responsibility") or "").strip()
        iface = ", ".join(summ.get("public_interface", [])[:6])
        parts.append(f"- {path}: {resp} | interface: {iface}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# File summary
# --------------------------------------------------------------------------

SUMMARY_SYSTEM = (
    "You are a senior software engineer performing static code analysis. "
    "You only state facts supported by the provided AST data. "
    "You never invent function names. Answer with a single JSON object."
)

SUMMARY_SCHEMA_HINT = """{
  "responsibility": "one sentence (max 40 words)",
  "key_functions": ["name: what it does (max 5 items, max 20 words each)"],
  "public_interface": ["exported symbol names (max 10)"],
  "design_notes": ["notable design or algorithmic points (max 2, max 25 words each)"],
  "risk_flags": ["concrete suspicious points with line numbers (max 2, empty list if none)"]
}"""


def build_summary_prompt(ctx: FileContext, *, include_source: bool = True) -> str:
    """Structured summary prompt built from AST facts + neighbour summaries."""
    src = ""
    if include_source and ctx.source_excerpt:
        excerpt = ctx.source_excerpt[:MAX_SOURCE_CHARS]
        src = f"\nSOURCE EXCERPT (truncated to budget):\n```{ctx.language}\n{excerpt}\n```\n"

    head = f"""FILE: {ctx.path}
LANGUAGE: {ctx.language}
LINES: {ctx.line_count}

SYMBOLS (from Tree-sitter AST):
{_fmt_symbols(ctx.symbols)}

STATIC CALL EDGES (caller -> callee, from AST):
{_fmt_calls(ctx)}

UNRESOLVED / DYNAMIC CALLS (target not statically nameable):
{", ".join(name for name, _ in ctx.unresolved_calls[:20]) or "(none)"}

IMPORTS: {", ".join(ctx.imports[:15]) or "(none)"}
IMPORTED BY (files that depend on this one): {", ".join(ctx.dependents[:10]) or "(none)"}

NEIGHBOURING FILE SUMMARIES (for cross-file context):
{_fmt_neighbours(ctx)}
"""
    tail = f"""
TASK: Summarise this file for a code-intelligence index.
Rules:
1. Ground every claim in the AST data above. Do not invent symbols.
2. "risk_flags" must cite a line number and a symbol name when possible.
3. Output ONLY a JSON object matching this schema:
{SUMMARY_SCHEMA_HINT}
"""
    return _fit_prompt(head, src, tail)


def build_summary_prompt_version() -> str:
    return SUMMARY_TEMPLATE_VERSION


# --------------------------------------------------------------------------
# Architecture analysis
# --------------------------------------------------------------------------

ARCH_SYSTEM = (
    "You are a software architect. You explain cross-file structure using only "
    "the call graph and file summaries provided. Cite file paths and symbol names."
)


def _fmt_chain(chain: list[dict], limit: int = 150) -> str:
    lines = []
    for step in chain[:limit]:
        sym = step["symbol"]
        indent = "  " * step["depth"]
        sig = (sym.get("signature") or "").replace("\n", " ").strip()[:110]
        lines.append(
            f"{indent}{sym['qualname']} [{sym.get('kind', '?')}] "
            f"({sym['path']}:{sym.get('start_line', 0)}) {sig}"
        )
    if len(chain) > limit:
        lines.append(f"... and {len(chain) - limit} more chain nodes")
    return "\n".join(lines)


def _fmt_summary_detail(path: str, s: dict) -> str:
    """One file summary with every field the model produced (not just the name)."""
    parts = [f"- {path}: {s.get('responsibility', '')}"]
    iface = ", ".join(s.get("public_interface", [])[:8])
    if iface:
        parts.append(f"interface: {iface}")
    keyf = "; ".join(s.get("key_functions", [])[:4])
    if keyf:
        parts.append(f"key: {keyf}")
    notes = "; ".join(s.get("design_notes", [])[:2])
    if notes:
        parts.append(f"notes: {notes}")
    risks = "; ".join(s.get("risk_flags", [])[:2])
    if risks:
        parts.append(f"risks: {risks}")
    return " | ".join(parts)


def build_architecture_prompt(
    repo_name: str,
    entry: dict,
    call_chain: list[dict],
    file_summaries: dict[str, dict],
    module_stats: dict[str, int],
    dep_edges: list[tuple[str, str]],
) -> str:
    """Cross-file architecture prompt.

    Deliberately *not* capped by MAX_PROMPT_CHARS: this is a single call over the
    whole repository, so its context is expected to be an order of magnitude
    larger than a per-file summary. Callers pass num_ctx=8192 and the prompt is
    kept within ~28k chars (~8k tokens) by the per-section limits below.
    """
    chain_text = _fmt_chain(call_chain, limit=150)
    summ_lines = [_fmt_summary_detail(p, s) for p, s in file_summaries.items()]
    dep_lines = [f"- {a} -> {b}" for a, b in dep_edges]
    mod_lines = [
        f"- {k}: {v} files" for k, v in sorted(module_stats.items(), key=lambda x: -x[1])[:30]
    ]

    return f"""REPOSITORY: {repo_name}

ENTRY POINT: {entry['qualname']} ({entry['path']}:{entry['start_line']})

CALL CHAIN FROM ENTRY (depth-indented, from SQLite call graph):
{chain_text or "(no resolved calls)"}

FILE SUMMARIES (generated from AST + local LLM):
{chr(10).join(summ_lines) or "(none)"}

MODULE LAYOUT (directory -> file count):
{chr(10).join(mod_lines)}

CROSS-FILE DEPENDENCY EDGES (importer -> imported):
{chr(10).join(dep_lines) or "(none)"}

TASK: Produce a Markdown architecture analysis with these sections:
## 1. Entry point and control flow
Explain how execution enters and flows across files. Cite file:line.
## 2. Module responsibilities
For each significant module, state its role and who depends on it.
## 3. Data / control dependencies
Describe the dependency direction and any layering or cycles you can infer.
## 4. Extension points
Where would a new feature most naturally attach? Cite symbols.
## 5. Risks
Concrete structural risks visible in the graph (high fan-in, cycles, orphan modules).

Ground every statement in the data above. Do not invent files or symbols.
"""


def build_architecture_prompt_version() -> str:
    return ARCH_TEMPLATE_VERSION


# --------------------------------------------------------------------------
# Developer guide
# --------------------------------------------------------------------------

GUIDE_SYSTEM = (
    "You are writing an onboarding guide for engineers new to this repository. "
    "Use only the supplied architecture analysis and file summaries."
)


def build_guide_prompt(
    repo_name: str,
    architecture_md: str,
    key_summaries: dict[str, dict],
    stats: dict,
    entry_points: list[dict],
) -> str:
    """Onboarding-guide prompt.

    Like the architecture prompt this is a single repo-wide call, so it is not
    clamped to MAX_PROMPT_CHARS. The architecture analysis is injected whole and
    every file summary contributes its responsibility, key functions and
    interface, so the guide prompt carries the whole repository picture.
    """
    key_lines = []
    for path, s in key_summaries.items():
        resp = (s.get("responsibility") or "").strip()
        key_lines.append(f"- {path}: {resp}")
        for fn in s.get("key_functions", [])[:4]:
            key_lines.append(f"    * {fn}")
        iface = ", ".join(s.get("public_interface", [])[:8])
        if iface:
            key_lines.append(f"    interface: {iface}")
        notes = "; ".join(s.get("design_notes", [])[:2])
        if notes:
            key_lines.append(f"    notes: {notes}")

    ep_lines = [
        f"- {e['qualname']} [{e.get('kind', '?')}] ({e['path']}:{e.get('start_line', 0)}) "
        f"out_degree={e.get('out_deg', 0)} in_degree={e.get('in_deg', 0)}"
        for e in entry_points[:20]
    ]

    return f"""REPOSITORY: {repo_name}

INDEX STATISTICS:
- files: {stats.get('files')}, symbols: {stats.get('symbols')}, edges: {stats.get('edges')}
- call edges: {stats.get('call_edges')}, import edges: {stats.get('import_edges')}
- resolved edges: {stats.get('resolved_edges')}, unresolved: {stats.get('unresolved_edges')}
- languages: {json.dumps(stats.get('by_language', {}))}

KEY ENTRY POINTS:
{chr(10).join(ep_lines)}

ARCHITECTURE ANALYSIS (previously generated from the call graph):
{architecture_md[:24000]}

KEY FILE SUMMARIES:
{chr(10).join(key_lines)}

TASK: Write a Markdown developer guide with these sections:
## Quick start orientation
## Mental model of the codebase (how the pieces fit)
## Where to start reading (ordered file list with reasons)
## How to add a feature (concrete steps citing real symbols)
## Testing and verification entry points
## Common pitfalls

Be concrete. Cite real file paths and symbol names from the data above.
"""


def build_guide_prompt_version() -> str:
    return GUIDE_TEMPLATE_VERSION


# --------------------------------------------------------------------------
# Code review
# --------------------------------------------------------------------------

REVIEW_SYSTEM = (
    "You are a meticulous code reviewer focused on correctness, error handling, "
    "resource lifetime, and security. You only report issues you can point to."
)


def build_review_prompt(ctx: FileContext, *, max_items: int = 5) -> str:
    src = f"""
SOURCE:
```{ctx.language}
{ctx.source_excerpt[:MAX_SOURCE_CHARS]}
```
"""
    head = f"""FILE: {ctx.path}
LANGUAGE: {ctx.language}

SYMBOLS:
{_fmt_symbols(ctx.symbols, limit=30)}

CALL EDGES:
{_fmt_calls(ctx, limit=40)}

UNRESOLVED CALLS: {", ".join(n for n, _ in ctx.unresolved_calls[:15]) or "(none)"}
"""
    tail = f"""
TASK: Review this file. Report at most {max_items} issues as a JSON array:
[
  {{"line": <int>, "symbol": "<symbol name>", "severity": "high|medium|low",
    "category": "error_handling|resource_leak|security|logic|api_misuse",
    "issue": "what is wrong", "suggestion": "concrete fix"}}
]
If nothing is wrong, return []. Output ONLY the JSON array.
"""
    return _fit_prompt(head, src, tail)


def build_review_prompt_version() -> str:
    return REVIEW_TEMPLATE_VERSION
