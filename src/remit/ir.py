"""Typed IR serialisation: AST <-> JSON, program hashing, call-site ids."""
from __future__ import annotations

import dataclasses
import hashlib
import json

from . import ast as A
from .errors import Span

_CLASSES = {c.__name__: c for c in vars(A).values() if isinstance(c, type) and issubclass(c, A.Node)}
_META = ("span", "nid", "comments", "trailing")


def to_json(n, with_meta: bool = True):
    if isinstance(n, A.Node):
        d = {"_": type(n).__name__}
        for f in dataclasses.fields(n):
            if f.name in _META:
                continue
            d[f.name] = to_json(getattr(n, f.name), with_meta)
        if with_meta:
            s = n.span
            d["@span"] = [s.file, s.line, s.col, s.end_line, s.end_col]
            d["@nid"] = n.nid
            if n.comments:
                d["@comments"] = n.comments
            if n.trailing:
                d["@trailing"] = n.trailing
        return d
    if isinstance(n, (list, tuple)):
        return [to_json(x, with_meta) for x in n]
    return n


def from_json(d):
    if isinstance(d, dict) and "_" in d:
        cls = _CLASSES[d["_"]]
        kw = {}
        for f in dataclasses.fields(cls):
            if f.name in _META:
                continue
            if f.name in d:
                kw[f.name] = from_json(d[f.name])
        if cls is A.TRecord or cls is A.RecordLit:
            kw["fields"] = [tuple(x) for x in kw["fields"]]
        sp = d.get("@span") or ["<ir>", 0, 0, 0, 0]
        n = cls(**kw, span=Span(*sp))
        n.nid = d.get("@nid", 0)
        n.comments = d.get("@comments", [])
        n.trailing = d.get("@trailing")
        return n
    if isinstance(d, list):
        return [from_json(x) for x in d]
    return d


def program_hash(prog: A.Program) -> str:
    """Hash of the program's semantics: ignores spans, comments and formatting."""
    body = json.dumps(to_json(prog, with_meta=False), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def assign_sites(prog: A.Program, is_effect) -> dict[int, str]:
    """Stable call-site ids: '<fn>#<k>' for the k-th effectful call (pre-order) in each function."""
    sites = {}
    for f in prog.fns:
        counter = [0]

        def walk(n):
            if isinstance(n, A.Call) and is_effect(n):
                sites[n.nid] = f"{f.name}#{counter[0]}"
                counter[0] += 1
            if isinstance(n, A.Node):
                for fld in dataclasses.fields(n):
                    if fld.name not in _META:
                        walk(getattr(n, fld.name))
            elif isinstance(n, (list, tuple)):
                for x in n:
                    walk(x)
        walk(f.body)
    return sites
