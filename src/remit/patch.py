"""Declaration-level structured patches over the AST.

Operations (JSON):
  {"op": "replace", "name": "<decl name>", "source": "<full new declaration>"}
  {"op": "insert", "after": "<decl name or null for first>", "source": "<declaration(s)>"}
  {"op": "delete", "name": "<decl name>"}
  {"op": "header", "uses": [...], "budget": <number|null>}     # optional: change uses/budget
Declarations are types, functions, agents and tests (tests are named by their string). Each source fragment is
parsed on its own, so a malformed fragment is rejected with a located error before anything is applied.
"""
from __future__ import annotations

from . import ast as A
from .errors import RemitError, Diagnostic, Span
from .parser import parse_program


def decl_name(d) -> str:
    return d.name


def _parse_decls(source: str, file: str):
    prog = parse_program("program _patch\n" + source + "\n", file)
    return prog.order


def apply_patch(prog: A.Program, ops: list[dict], file: str = "patch") -> A.Program:
    order = list(prog.order)
    names = lambda: [decl_name(d) for d in order]
    uses, budget = list(prog.uses), prog.budget
    for i, op in enumerate(ops):
        kind = op.get("op")
        if kind == "replace":
            if op.get("name") not in names():
                raise RemitError(Diagnostic("P0001", f"patch op {i}: no declaration named {op.get('name')!r}",
                                            Span(file, 1, 1), hint=f"declarations: {', '.join(names())}"))
            new = _parse_decls(op["source"], f"{file}#op{i}")
            j = names().index(op["name"])
            order[j:j + 1] = new
        elif kind == "insert":
            new = _parse_decls(op["source"], f"{file}#op{i}")
            after = op.get("after")
            if after is None:
                j = 0
            elif after in names():
                j = names().index(after) + 1
            else:
                raise RemitError(Diagnostic("P0001", f"patch op {i}: no declaration named {after!r}", Span(file, 1, 1)))
            order[j:j] = new
        elif kind == "delete":
            if op.get("name") not in names():
                raise RemitError(Diagnostic("P0001", f"patch op {i}: no declaration named {op.get('name')!r}",
                                            Span(file, 1, 1)))
            order.pop(names().index(op["name"]))
        elif kind == "header":
            if "uses" in op:
                uses = list(op["uses"])
            if "budget" in op:
                budget = op["budget"]
        else:
            raise RemitError(Diagnostic("P0002", f"patch op {i}: unknown op {kind!r}", Span(file, 1, 1)))
    out = A.Program(name=prog.name, uses=uses, budget=budget,
                    types=[d for d in order if isinstance(d, A.TypeDecl)],
                    fns=[d for d in order if isinstance(d, A.FnDecl)],
                    agents=[d for d in order if isinstance(d, A.AgentDecl)],
                    tests=[d for d in order if isinstance(d, A.TestDecl)],
                    file=prog.file, span=prog.span)
    out.order = order
    out.header_comments = prog.header_comments
    out.comments = prog.comments
    return out
