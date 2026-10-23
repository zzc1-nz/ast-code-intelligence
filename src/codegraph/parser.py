"""Tree-sitter based AST extraction.

We deliberately do NOT use tree-sitter's `Query` DSL for extraction: the node
shapes we need differ per language and a hand-written visitor keeps the
resolution logic (parent scope tracking, receiver-qualified call names,
decorator handling) explicit and debuggable. Queries are used only where they
are a clear win.

Every extractor yields the same intermediate structures so the rest of the
pipeline (graph builder, summarizer, prompt builder) is language agnostic:

    ParsedFile(
        language, path,
        symbols: list[RawSymbol],
        calls:   list[RawCall],      # (caller qualname, callee text, line)
        imports: list[RawImport],    # (importer qualname, module, names, line)
        inherits:list[RawInherit],   # (class qualname, base text, line)
        dynamic_calls: int,          # calls whose target text is not statically nameable
    )
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from tree_sitter import Language, Node, Parser

# --------------------------------------------------------------------------
# Language registry
# --------------------------------------------------------------------------

_LANG_CACHE: dict[str, Language] = {}


def get_language(name: str) -> Language:
    """Load a tree-sitter Language by short name (cached)."""
    if name in _LANG_CACHE:
        return _LANG_CACHE[name]

    if name == "python":
        import tree_sitter_python as m

        lang = Language(m.language())
    elif name == "rust":
        import tree_sitter_rust as m

        lang = Language(m.language())
    elif name == "javascript":
        import tree_sitter_javascript as m

        lang = Language(m.language())
    elif name == "typescript":
        import tree_sitter_typescript as m

        lang = Language(m.language_typescript())
    elif name == "tsx":
        import tree_sitter_typescript as m

        lang = Language(m.language_tsx())
    else:
        raise ValueError(f"unsupported language: {name}")

    _LANG_CACHE[name] = lang
    return lang


_PARSER_CACHE: dict[str, Parser] = {}


def get_parser(language: str) -> Parser:
    if language not in _PARSER_CACHE:
        _PARSER_CACHE[language] = Parser(get_language(language))
    return _PARSER_CACHE[language]


# --------------------------------------------------------------------------
# Intermediate structures
# --------------------------------------------------------------------------


@dataclass
class RawSymbol:
    name: str
    qualname: str
    kind: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str
    docstring: str | None
    is_public: bool
    parent_qualname: str | None
    complexity: int
    body_lines: int


@dataclass
class RawCall:
    caller: str          # qualname of enclosing symbol, "" for module level
    callee: str          # textual target (possibly receiver-qualified)
    line: int
    receiver: str | None = None
    # Type of the receiver variable when it can be inferred from an assignment
    # in the same file (`let e = Engine::new(...)` -> "Engine", `c = Child()` ->
    # "Child"). Lets the graph builder resolve `e.run()` without guessing.
    receiver_type: str | None = None


@dataclass
class RawImport:
    importer: str
    module: str
    names: list[str]
    line: int
    alias: str | None = None


@dataclass
class RawInherit:
    child: str
    base: str
    line: int
    kind: str = "inherit"


@dataclass
class ParsedFile:
    path: str
    language: str
    symbols: list[RawSymbol] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    imports: list[RawImport] = field(default_factory=list)
    inherits: list[RawInherit] = field(default_factory=list)
    dynamic_calls: int = 0
    total_calls: int = 0
    parse_ok: bool = True
    parse_error: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _text(src: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _clean(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _signature(src: bytes, node: Node, body_field: str | None = "body") -> str:
    """Source slice from node start to the body, collapsed to one line."""
    body = node.child_by_field_name(body_field) if body_field else None
    end = body.start_byte if body is not None else node.end_byte
    raw = src[node.start_byte : end].decode("utf-8", errors="replace")
    # strip trailing decorators-only prefix, keep it simple
    return _clean(raw)[:400]


def _docstring_python(src: bytes, node: Node) -> str | None:
    body = node.child_by_field_name("body")
    if body is None or body.named_child_count == 0:
        return None
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_child_count:
        inner = first.named_children[0]
        if inner.type in ("string", "concatenated_string"):
            txt = _text(src, inner)
            txt = re.sub(r'^[rbfuRBFU]{0,2}("""|\'\'\'|"|\')', "", txt)
            txt = re.sub(r'("""|\'\'\'|"|\')$', "", txt)
            return _clean(txt)[:600]
    return None


def _leading_comment_doc(src: bytes, node: Node) -> str | None:
    """Collect immediately preceding `//` or `/** */` comment lines."""
    parts: list[str] = []
    prev = node.prev_named_sibling
    while prev is not None and prev.type in (
        "line_comment",
        "block_comment",
        "comment",
    ):
        parts.append(_text(src, prev))
        prev = prev.prev_named_sibling
        if len(parts) >= 6:
            break
    if not parts:
        return None
    joined = "\n".join(reversed(parts))
    joined = re.sub(r"^\s*(/\*\*?|\*/|//+|///|#)\s?", "", joined, flags=re.MULTILINE)
    return _clean(joined)[:600]


_BRANCH_NODE_HINTS = (
    "if_statement",
    "if_expression",
    "for_statement",
    "for_expression",
    "while_statement",
    "while_expression",
    "match_expression",
    "match_statement",
    "switch_statement",
    "case_clause",
    "try_statement",
    "catch_clause",
    "elif_clause",
    "else_clause",
    "ternary_expression",
    "conditional_expression",
    "&&",
    "||",
    "and",
    "or",
    "loop_expression",
)


def _complexity(node: Node) -> int:
    """Cheap cyclomatic-ish complexity: count branch nodes in the body."""
    count = 0
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type in _BRANCH_NODE_HINTS:
            count += 1
        for ch in cur.named_children:
            stack.append(ch)
    return count


def _is_public_name(name: str, language: str) -> bool:
    if language == "python":
        return not name.startswith("_")
    return not name.startswith("_")


# --------------------------------------------------------------------------
# Python extractor
# --------------------------------------------------------------------------

_PY_FUNC = {"function_definition"}
_PY_CLASS = {"class_definition"}
_PY_BRANCH = _BRANCH_NODE_HINTS


def _py_name(src: bytes, node: Node) -> str:
    return _text(src, node.child_by_field_name("name"))


def _unwrap_decorated(node: Node) -> Node:
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is not None:
            return inner
    return node


def _parse_python(src: bytes, path: str) -> ParsedFile:
    pf = ParsedFile(path=path, language="python")
    root = get_parser("python").parse(src).root_node

    def visit(node: Node, parent: str | None, in_class: bool) -> None:
        for child in node.named_children:
            target = _unwrap_decorated(child)
            ntype = target.type

            if ntype in _PY_FUNC:
                name = _py_name(src, target)
                if not name:
                    continue
                qual = f"{parent}.{name}" if parent else name
                body = target.child_by_field_name("body")
                pf.symbols.append(
                    RawSymbol(
                        name=name,
                        qualname=qual,
                        kind="method" if in_class else "function",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        signature=_signature(src, target, "body"),
                        docstring=_docstring_python(src, target),
                        is_public=_is_public_name(name, "python"),
                        parent_qualname=parent,
                        complexity=_complexity(body) if body is not None else 0,
                        body_lines=(body.end_point[0] - body.start_point[0] + 1) if body is not None else 0,
                    )
                )
                visit(target, qual, in_class)
                continue

            if ntype in _PY_CLASS:
                name = _py_name(src, target)
                if not name:
                    continue
                qual = f"{parent}.{name}" if parent else name
                bases: list[str] = []
                supers = target.child_by_field_name("superclasses")
                if supers is not None:
                    for b in supers.named_children:
                        bases.append(_clean(_text(src, b)))
                body = target.child_by_field_name("body")
                pf.symbols.append(
                    RawSymbol(
                        name=name,
                        qualname=qual,
                        kind="class",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        signature=_signature(src, target, "body"),
                        docstring=_docstring_python(src, target),
                        is_public=_is_public_name(name, "python"),
                        parent_qualname=parent,
                        complexity=0,
                        body_lines=(body.end_point[0] - body.start_point[0] + 1) if body is not None else 0,
                    )
                )
                for b in bases:
                    base_name = b.split(".")[-1]
                    if base_name and base_name not in ("object", "ABC", "Protocol"):
                        pf.inherits.append(
                            RawInherit(child=qual, base=base_name, line=target.start_point[0] + 1)
                        )
                visit(target, qual, True)
                continue

            if ntype in ("import_statement", "import_from_statement"):
                _py_imports(src, target, pf)
                continue

            # recurse into other constructs (if/try blocks at module level)
            if ntype in ("if_statement", "try_statement", "with_statement", "block"):
                visit(target, parent, in_class)

    visit(root, None, False)

    # calls, scoped by enclosing symbol
    _collect_calls(src, root, pf, "python")
    return pf


def _py_imports(src: bytes, node: Node, pf: ParsedFile) -> None:
    line = node.start_point[0] + 1
    if node.type == "import_statement":
        for ch in node.named_children:
            if ch.type == "dotted_name":
                pf.imports.append(RawImport(importer="", module=_clean(_text(src, ch)), names=[], line=line))
            elif ch.type == "aliased_import":
                mod = _text(src, ch.child_by_field_name("name"))
                alias = _text(src, ch.child_by_field_name("alias"))
                pf.imports.append(
                    RawImport(importer="", module=_clean(mod), names=[], line=line, alias=_clean(alias))
                )
    else:
        mod_node = node.child_by_field_name("module_name")
        module = _clean(_text(src, mod_node)) if mod_node is not None else ""
        names: list[str] = []
        for ch in node.named_children:
            if ch is mod_node:
                continue
            if ch.type in ("dotted_name", "identifier"):
                names.append(_clean(_text(src, ch)))
            elif ch.type == "aliased_import":
                names.append(_clean(_text(src, ch.child_by_field_name("name"))))
            elif ch.type == "wildcard_import":
                names.append("*")
        pf.imports.append(RawImport(importer="", module=module, names=names, line=line))


# --------------------------------------------------------------------------
# Rust extractor
# --------------------------------------------------------------------------

_RS_KINDS = {
    "function_item": "function",
    "struct_item": "struct",
    "enum_item": "enum",
    "trait_item": "trait",
    "impl_item": "impl",
    "mod_item": "module",
    "const_item": "const",
    "static_item": "const",
    "type_item": "type",
    "macro_definition": "macro",
    "union_item": "struct",
}


def _rust_is_public(src: bytes, node: Node) -> bool:
    """True when the item carries any `pub` visibility modifier.

    tree-sitter-rust 0.23+ exposes visibility as a `visibility_modifier` child
    node (`pub`, `pub(crate)`, `pub(super)`, `pub(in path)`), not as a field.
    """
    for ch in node.children:
        if ch.type == "visibility_modifier":
            return _text(src, ch).strip().startswith("pub")
    return False


def _parse_rust(src: bytes, path: str) -> ParsedFile:
    pf = ParsedFile(path=path, language="rust")
    root = get_parser("rust").parse(src).root_node

    def visit(node: Node, parent: str | None, impl_ctx: str | None) -> None:
        for child in node.named_children:
            ntype = child.type

            if ntype in _RS_KINDS:
                kind = _RS_KINDS[ntype]
                name_node = child.child_by_field_name("name")
                name = _text(src, name_node)
                if ntype == "impl_item":
                    # `impl Foo` / `impl Trait for Foo` / `impl<'a> Foo<'a>`
                    ty = _clean(_text(src, child.child_by_field_name("type")))
                    ty = re.sub(r"<[^>]*>", "", ty).strip()   # drop generic args
                    ty = ty.split("::")[-1].strip()
                    tr = child.child_by_field_name("trait")
                    name = ty or "impl"
                    qual = name
                    if tr is not None:
                        trait_name = _clean(_text(src, tr)).split("::")[-1]
                        trait_name = re.sub(r"<[^>]*>", "", trait_name).strip()
                        pf.inherits.append(
                            RawInherit(
                                child=ty,
                                base=trait_name,
                                line=child.start_point[0] + 1,
                                kind="implements",
                            )
                        )
                    pf.symbols.append(
                        RawSymbol(
                            name=name.split("::")[-1],
                            # `impl Foo` is a *container*, not a symbol of its own:
                            # prefixing keeps its qualname from colliding with `struct Foo`.
                            qualname=f"impl {qual}",
                            kind="impl",
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            start_byte=child.start_byte,
                            end_byte=child.end_byte,
                            signature=_clean(_text(src, child))[:200],
                            docstring=_leading_comment_doc(src, child),
                            is_public=True,
                            parent_qualname=None,
                            complexity=0,
                            body_lines=child.end_point[0] - child.start_point[0] + 1,
                        )
                    )
                    visit(child, qual, qual)
                    continue

                if not name:
                    continue
                # nested modules -> dotted
                qual = f"{parent}::{name}" if parent else name
                body = child.child_by_field_name("body")
                # Rust default visibility is private: only `pub` / `pub(crate)` /
                # `pub(super)` / `pub(in path)` are externally visible. The grammar
                # exposes it as a `visibility_modifier` *named child*, not a field.
                is_pub = _rust_is_public(src, child)
                sym_kind = kind
                if impl_ctx and ntype == "function_item":
                    sym_kind = "method"
                    qual = f"{impl_ctx}::{name}"
                elif impl_ctx and ntype in ("const_item", "static_item"):
                    sym_kind = "const"
                    qual = f"{impl_ctx}::{name}"
                pf.symbols.append(
                    RawSymbol(
                        name=name,
                        qualname=qual,
                        kind=sym_kind,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        signature=_signature(src, child, "body"),
                        docstring=_leading_comment_doc(src, child),
                        is_public=is_pub,
                        parent_qualname=parent,
                        complexity=_complexity(body) if body is not None else 0,
                        body_lines=(body.end_point[0] - body.start_point[0] + 1) if body is not None else 0,
                    )
                )
                if ntype == "mod_item" and body is not None:
                    visit(body, qual, None)
                elif ntype in ("function_item",) and body is not None:
                    visit(body, qual, impl_ctx)
                elif body is not None:
                    visit(body, qual, impl_ctx if ntype == "impl_item" else None)
                continue

            if ntype == "function_signature_item":
                # trait method *declarations* (`fn hello(&self) -> String;`) have
                # no body, so they were dropped entirely. They are real API surface
                # and call targets, so index them as methods.
                name = _text(src, child.child_by_field_name("name"))
                if not name:
                    continue
                qual = f"{parent}::{name}" if parent else name
                pf.symbols.append(
                    RawSymbol(
                        name=name,
                        qualname=qual,
                        kind="method" if parent else "function",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        signature=_signature(src, child, None),
                        docstring=_leading_comment_doc(src, child),
                        is_public=_rust_is_public(src, child),
                        parent_qualname=parent,
                        complexity=0,
                        body_lines=0,
                    )
                )
                continue

            if ntype == "use_declaration":
                arg = child.child_by_field_name("argument")
                mod = _clean(_text(src, arg)) if arg is not None else ""
                pf.imports.append(
                    RawImport(importer="", module=mod, names=[], line=child.start_point[0] + 1)
                )
                continue

            if ntype in ("attribute_item",):
                continue

            visit(child, parent, impl_ctx)

    visit(root, None, None)
    _collect_calls(src, root, pf, "rust")
    return pf


# --------------------------------------------------------------------------
# JS / TS extractor
# --------------------------------------------------------------------------

_JS_KINDS = {
    "function_declaration": "function",
    "function_expression": "function",
    "generator_function_declaration": "function",
    "arrow_function": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "lexical_declaration": None,  # handled specially for `const f = () => {}`
    "variable_declaration": None,
}

# declarations that introduce a named type-ish symbol with an optional body
_JS_TYPE_DECL_TYPES = {
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "enum_declaration",
    "type_alias_declaration",
}


def _js_heritage(src: bytes, node: Node) -> list[tuple[str, str]]:
    """(base_name, edge_kind) pairs from `extends` / `implements` clauses.

    TS/JS grammars nest these in `class_heritage` (class) or
    `extends_type_clause` (interface), so a simple field lookup misses them.
    """
    out: list[tuple[str, str]] = []
    for ch in node.named_children:
        if ch.type == "class_heritage":
            for clause in ch.named_children:
                kind = "implements" if clause.type == "implements_clause" else "extends"
                for item in clause.named_children:
                    txt = _clean(_text(src, item))
                    # `extends Foo<T>` -> Foo
                    base = re.sub(r"<.*$", "", txt).split(".")[-1].strip()
                    if base:
                        out.append((base, kind))
        elif ch.type == "extends_type_clause":
            for item in ch.named_children:
                txt = _clean(_text(src, item))
                base = re.sub(r"<.*$", "", txt).split(".")[-1].strip()
                if base:
                    out.append((base, "extends"))
    return out


def _parse_js(src: bytes, path: str, language: str) -> ParsedFile:
    pf = ParsedFile(path=path, language=language)
    root = get_parser(language).parse(src).root_node

    def visit(node: Node, parent: str | None, in_class: bool) -> None:
        for child in node.named_children:
            ntype = child.type

            if ntype in _JS_TYPE_DECL_TYPES:
                name = _text(src, child.child_by_field_name("name"))
                if not name:
                    continue
                qual = f"{parent}.{name}" if parent else name
                body = child.child_by_field_name("body")
                pf.symbols.append(
                    RawSymbol(
                        name=name,
                        qualname=qual,
                        kind=_JS_KINDS[ntype],
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        signature=_signature(src, child, "body"),
                        docstring=_leading_comment_doc(src, child),
                        is_public=True,
                        parent_qualname=parent,
                        complexity=0,
                        body_lines=(body.end_point[0] - body.start_point[0] + 1) if body is not None else 0,
                    )
                )
                # extends / implements live in a `class_heritage` node (classes) or
                # an `extends_type_clause` (interfaces), never in a `heritage` field.
                for base, kind in _js_heritage(src, child):
                    pf.inherits.append(
                        RawInherit(child=qual, base=base, line=child.start_point[0] + 1, kind=kind)
                    )
                if body is not None:
                    visit(body, qual, True)
                continue

            if ntype in ("function_declaration", "generator_function_declaration", "method_definition",
                         "abstract_method_signature", "method_signature"):
                name_node = child.child_by_field_name("name")
                name = _text(src, name_node) if name_node is not None else ""
                if not name:
                    continue
                qual = f"{parent}.{name}" if parent else name
                body = child.child_by_field_name("body")
                kind = "method" if (in_class or ntype == "method_definition") else "function"
                pf.symbols.append(
                    RawSymbol(
                        name=name,
                        qualname=qual,
                        kind=kind,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        signature=_signature(src, child, "body"),
                        docstring=_leading_comment_doc(src, child),
                        is_public=True,
                        parent_qualname=parent,
                        complexity=_complexity(body) if body is not None else 0,
                        body_lines=(body.end_point[0] - body.start_point[0] + 1) if body is not None else 0,
                    )
                )
                visit(child, qual, in_class)
                continue

            if ntype in ("lexical_declaration", "variable_declaration"):
                # const foo = (...) => {...}  /  const foo = function() {}
                for decl in child.named_children:
                    if decl.type != "variable_declarator":
                        continue
                    nm = decl.child_by_field_name("name")
                    val = decl.child_by_field_name("value")
                    if nm is None or val is None:
                        continue
                    if val.type not in ("arrow_function", "function_expression", "function"):
                        continue
                    name = _text(src, nm)
                    qual = f"{parent}.{name}" if parent else name
                    body = val.child_by_field_name("body")
                    pf.symbols.append(
                        RawSymbol(
                            name=name,
                            qualname=qual,
                            kind="function",
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            start_byte=child.start_byte,
                            end_byte=child.end_byte,
                            signature=_signature(src, decl, "value"),
                            docstring=_leading_comment_doc(src, child),
                            is_public=True,
                            parent_qualname=parent,
                            complexity=_complexity(body) if body is not None else 0,
                            body_lines=(body.end_point[0] - body.start_point[0] + 1) if body is not None else 0,
                        )
                    )
                continue

            if ntype in ("import_statement", "import_declaration"):
                mod = ""
                src_node = child.child_by_field_name("source")
                if src_node is not None:
                    mod = _clean(_text(src, src_node)).strip("'\"`")
                else:
                    mod = _clean(_text(src, child))
                pf.imports.append(
                    RawImport(importer="", module=mod, names=[], line=child.start_point[0] + 1)
                )
                continue

            if ntype == "export_statement":
                visit(child, parent, in_class)
                continue

            if ntype in ("statement_block", "class_body", "program", "block"):
                visit(child, parent, in_class)

    visit(root, None, False)
    _collect_calls(src, root, pf, language)
    return pf


# --------------------------------------------------------------------------
# Generic call collection (scoped by enclosing symbol)
# --------------------------------------------------------------------------

_CALL_TYPES = {
    "python": ("call",),
    "rust": ("call_expression", "macro_invocation"),
    "javascript": ("call_expression", "new_expression"),
    "typescript": ("call_expression", "new_expression"),
    "tsx": ("call_expression", "new_expression"),
}

# nodes that do NOT open a new call scope
_TRANSPARENT = {
    "block",
    "statement_block",
    "class_body",
    "declaration_list",
    "expression_statement",
    "return_statement",
    "if_statement",
    "for_statement",
    "while_statement",
    "match_expression",
    "match_statement",
    "try_statement",
    "with_statement",
    "assignment",
    "let_declaration",
    "lexical_declaration",
    "variable_declaration",
}


def _callee_text(src: bytes, node: Node, language: str) -> tuple[str | None, str | None]:
    """Return (callee_text, receiver) for a call node, or (None, None) if dynamic."""
    if language == "python":
        fn = node.child_by_field_name("function")
        if fn is None:
            return None, None
        if fn.type == "identifier":
            return _text(src, fn), None
        if fn.type == "attribute":
            obj = fn.child_by_field_name("object")
            attr = fn.child_by_field_name("attribute")
            recv = _text(src, obj) if obj is not None else None
            name = _text(src, attr) if attr is not None else None
            if name:
                return name, recv
        if fn.type == "subscript":
            value = fn.child_by_field_name("value")
            return _text(src, value) if value is not None else None, None
        return None, None

    if language == "rust":
        if node.type == "macro_invocation":
            mac = node.child_by_field_name("macro")
            return (_text(src, mac) if mac is not None else None), None
        fn = node.child_by_field_name("function")
        if fn is None:
            return None, None
        if fn.type == "identifier":
            return _text(src, fn), None
        if fn.type in ("field_expression", "scoped_identifier"):
            # a::b::c  or  self.x.foo
            txt = _clean(_text(src, fn))
            parts = re.split(r"::|\.", txt)
            parts = [p for p in parts if p]
            if parts:
                return parts[-1], (parts[-2] if len(parts) > 1 else None)
        if fn.type == "generic_function":
            inner = fn.named_children[0] if fn.named_child_count else None
            if inner is not None:
                return _callee_text(src, inner, language)
        return None, None

    # js/ts
    fn = node.child_by_field_name("function")
    if fn is None and node.type == "new_expression":
        fn = node.child_by_field_name("constructor")
    if fn is None:
        return None, None
    if fn.type == "identifier":
        return _text(src, fn), None
    if fn.type == "member_expression":
        obj = fn.child_by_field_name("object")
        prop = fn.child_by_field_name("property")
        recv = _text(src, obj) if obj is not None else None
        name = _text(src, prop) if prop is not None else None
        if name:
            return name, recv
    if fn.type == "generic_function":
        inner = fn.named_children[0] if fn.named_child_count else None
        if inner is not None:
            return _callee_text(src, inner, language)
    return None, None


def _walk_calls_generic(
    src: bytes,
    root: Node,
    call_type: str,
    fn_field: str,
    caller: str,
    receiver_fields: Iterable[str] = ("function",),
) -> list[RawCall]:
    out: list[RawCall] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type == call_type:
            fn = cur.child_by_field_name(fn_field)
            if fn is not None and fn.type == "identifier":
                out.append(RawCall(caller=caller, callee=_text(src, fn), line=cur.start_point[0] + 1))
        for ch in cur.named_children:
            stack.append(ch)
    return out


# --------------------------------------------------------------------------
# Receiver type inference
# --------------------------------------------------------------------------
#
# `let e = Engine::new(); e.run()` cannot be resolved from the call site alone:
# `e` is just a local variable. We recover the declared/constructed type from the
# assignment and attach it to the call so the resolver can try `Engine::run`.
# Only same-file, single-assignment evidence is used — deliberately conservative.

_ASSIGN_NODE_TYPES = {
    "python": ("assignment",),
    "rust": ("let_declaration",),
    "javascript": ("variable_declarator",),
    "typescript": ("variable_declarator",),
    "tsx": ("variable_declarator",),
}


def _callee_type(src: bytes, node: Node | None, language: str) -> str | None:
    """Best-effort type name for the *value* side of an assignment."""
    if node is None:
        return None
    if language == "python":
        # `c = Child()` / `c = pkg.Child()` / `c: Child = Child()`
        if node.type != "call":
            return None
        fn = node.child_by_field_name("function")
        if fn is None:
            return None
        txt = _clean(_text(src, fn))
        if fn.type in ("identifier", "attribute"):
            return txt.split(".")[-1] or None
        return None
    if language == "rust":
        # `Engine::new(..)`, `Thing::build(..)`, `Thing { .. }`
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is None:
                return None
            if fn.type == "scoped_identifier":
                parts = [p for p in re.split(r"::|\.", _clean(_text(src, fn))) if p]
                if len(parts) >= 2:
                    return re.sub(r"<[^>]*>", "", parts[-2]).strip() or None
                return None
            if fn.type == "identifier":
                # `Engine::new()` without path -> type is named after the ctor
                name = _text(src, fn)
                return name[:1].upper() + name[1:] if name else None
            return None
        if node.type == "struct_expression":
            name = node.child_by_field_name("name")
            return _text(src, name) if name is not None else None
        return None
    # js / ts
    if node.type == "new_expression":
        ctor = node.child_by_field_name("constructor")
        if ctor is None:
            return None
        txt = _clean(_text(src, ctor))
        return txt.split(".")[-1] or None
    return None


def _collect_receiver_types(src: bytes, root: Node, language: str) -> dict[str, str]:
    """variable name -> inferred type name, for one file."""
    assign_types = _ASSIGN_NODE_TYPES.get(language, ())
    out: dict[str, str] = {}
    if not assign_types:
        return out
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type in assign_types:
            if language == "python":
                target = cur.child_by_field_name("left")
                value = cur.child_by_field_name("right")
            else:
                target = cur.child_by_field_name("pattern" if language == "rust" else "name")
                value = cur.child_by_field_name("value")
            if target is not None and value is not None and target.type in (
                "identifier",
                "variable_pattern",
            ):
                name = _text(src, target).strip()
                typ = _callee_type(src, value, language)
                if name and typ and name not in out:
                    out[name] = typ
        for ch in cur.named_children:
            stack.append(ch)
    return out


def _collect_calls(src: bytes, root: Node, pf: ParsedFile, language: str) -> None:
    call_types = _CALL_TYPES.get(language, ("call_expression",))
    # Build a scope map: symbol start_byte -> qualname
    scopes: list[tuple[int, int, str]] = [
        (s.start_byte, s.end_byte, s.qualname) for s in pf.symbols
    ]
    scopes.sort()
    receiver_types = _collect_receiver_types(src, root, language)

    def enclosing(byte: int) -> str:
        """Innermost symbol whose byte range contains `byte` (most specific scope)."""
        best = ""
        best_len = None
        for s, e, q in scopes:
            if s <= byte < e:
                span = e - s
                if best_len is None or span < best_len:
                    best_len = span
                    best = q
        return best

    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type in call_types:
            pf.total_calls += 1
            callee, recv = _callee_text(src, cur, language)
            if callee is None:
                pf.dynamic_calls += 1
            else:
                recv_type = None
                if recv is not None:
                    leaf = recv.split(".")[-1]
                    recv_type = receiver_types.get(leaf)
                pf.calls.append(
                    RawCall(
                        caller=enclosing(cur.start_byte),
                        callee=callee,
                        line=cur.start_point[0] + 1,
                        receiver=recv,
                        receiver_type=recv_type,
                    )
                )
        for ch in cur.named_children:
            stack.append(ch)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def parse_source(source: bytes, path: str, language: str) -> ParsedFile:
    """Parse one file. Never raises: returns ParsedFile(parse_ok=False) on error."""
    try:
        if language == "python":
            pf = _parse_python(source, path)
        elif language == "rust":
            pf = _parse_rust(source, path)
        elif language in ("javascript", "typescript", "tsx"):
            pf = _parse_js(source, path, language)
        else:
            raise ValueError(f"unsupported language: {language}")

        root = get_parser(language).parse(source).root_node
        if root.has_error:
            pf.parse_error = "syntax error in tree"
        return pf
    except Exception as exc:  # noqa: BLE001 - parser must be total
        return ParsedFile(
            path=path,
            language=language,
            parse_ok=False,
            parse_error=f"{type(exc).__name__}: {exc}",
        )


def detect_language(path: Path, ext_map: dict[str, str]) -> str | None:
    return ext_map.get(path.suffix.lower())


def count_lines(source: bytes) -> int:
    return source.count(b"\n") + (0 if source.endswith(b"\n") else 1)
