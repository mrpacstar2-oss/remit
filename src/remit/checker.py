"""Semantic analysis: types, capabilities, data-flow tags, worst-case bounds, budget, manifest."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Optional

from . import ast as A
from .errors import Diagnostic, Span
from .types import (BOOL, FLOAT, INT, NEVER, STR, UNIT, EnumT, ListT, Prim, RecordT, Type, assignable,
                    bound_violation, join)

Taint = dict  # tag -> frozenset[str] of human-readable origins
Effects = dict  # resource -> worst-case count


def t_union(*ts: Taint) -> Taint:
    out: dict = {}
    for t in ts:
        for k, v in t.items():
            out[k] = out.get(k, frozenset()) | v
    return out


def t_clear(t: Taint, tags) -> Taint:
    return {k: v for k, v in t.items() if k not in tags}


def e_add(*es: Effects) -> Effects:
    out: dict = {}
    for e in es:
        for k, v in e.items():
            out[k] = out.get(k, 0) + v
    return out


def e_scale(e: Effects, n: int) -> Effects:
    return {k: v * n for k, v in e.items()}


def e_max(*es: Effects) -> Effects:
    out: dict = {}
    for e in es:
        for k, v in e.items():
            out[k] = max(out.get(k, 0), v)
    return out


@dataclass
class Info:
    type: Type
    taint: Taint = field(default_factory=dict)
    eff: Effects = field(default_factory=dict)


@dataclass
class VarInfo:
    type: Type
    taint: Taint
    mutable: bool


BUILTINS = {"len", "take", "str", "lower", "upper", "trim", "contains", "startswith", "endswith", "split", "join",
            "sum", "min", "max", "range", "round", "abs", "replace", "int", "float", "first", "unique"}


@dataclass
class HostInfo:
    types: dict
    caps: dict  # name -> CapabilityDecl
    models: dict  # name -> ModelDecl
    inputs: dict  # name -> list[str]


@dataclass
class CheckResult:
    program: A.Program
    host: HostInfo
    diagnostics: list[Diagnostic]
    manifest: dict
    types: dict  # resolved named types
    node_types: dict  # nid -> Type

    @property
    def ok(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    def errors(self):
        return [d for d in self.diagnostics if d.severity == "error"]


class _Return(Exception):
    pass


class Checker:
    def __init__(self, prog: A.Program, host: A.HostInterface):
        self.prog = prog
        self.hostdecl = host
        self.diags: list[Diagnostic] = []
        self._seen_diags: set = set()
        self.types: dict[str, Type] = {}
        self.node_types: dict[int, Type] = {}
        self.fns = {f.name: f for f in prog.fns}
        self.agents = {a.name: a for a in prog.agents}
        self.call_sites: dict[int, dict] = {}  # nid -> site record (merged across contexts)
        self.flows: dict[tuple, dict] = {}
        self.used_resources: set[str] = set()
        self.fn_cache: dict = {}
        self.in_test = False

    # ----- diagnostics -----
    def err(self, code, msg, node_or_span, hint=None, notes=None, severity="error"):
        span = node_or_span.span if isinstance(node_or_span, A.Node) else node_or_span
        key = (code, msg, span.line if span else None, span.col if span else None)
        if key in self._seen_diags:
            return
        self._seen_diags.add(key)
        self.diags.append(Diagnostic(code, msg, span, severity=severity, hint=hint, notes=notes or []))

    def warn(self, code, msg, node, hint=None):
        self.err(code, msg, node, hint=hint, severity="warning")

    @staticmethod
    def suggest(name, options) -> Optional[str]:
        m = difflib.get_close_matches(name, list(options), n=1, cutoff=0.6)
        return f"did you mean '{m[0]}'?" if m else None

    # ----- types -----
    def resolve_type(self, te: A.TypeExpr, name: Optional[str] = None) -> Type:
        if isinstance(te, A.TUnion):
            if len(set(te.options)) != len(te.options):
                self.err("E0105", "duplicate option in union type", te)
            return EnumT(tuple(te.options), name)
        if isinstance(te, A.TRecord):
            return RecordT(tuple((n, self.resolve_type(t)) for n, t in te.fields), name)
        prims = {"Int": INT, "Float": FLOAT, "Bool": BOOL, "Str": STR, "Unit": UNIT}
        if te.name in prims:
            if te.args or te.max is not None:
                self.err("E0105", f"type '{te.name}' takes no parameters", te)
            return prims[te.name]
        if te.name == "List":
            if len(te.args) != 1:
                self.err("E0105", "List takes exactly one type parameter, e.g. List[Str]", te)
                return ListT(UNIT, te.max)
            return ListT(self.resolve_type(te.args[0]), te.max)
        if te.max is not None:
            self.err("E0105", "'max' applies only to List types", te)
        if te.name in self.types:
            return self.types[te.name]
        self.err("E0105", f"unknown type '{te.name}'", te,
                 hint=self.suggest(te.name, list(self.types) + list(prims) + ["List"]))
        return NEVER

    def declare_types(self, decls: list[A.TypeDecl], origin: str):
        for d in decls:
            if d.name in self.types:
                self.err("E0112", f"type '{d.name}' is already defined", d,
                         hint="host interface types cannot be redefined by the program" if origin == "program" else None)
                continue
            self.types[d.name] = self.resolve_type(d.type, d.name)

    # ----- entry -----
    def check(self) -> CheckResult:
        h = self.hostdecl
        self.declare_types(h.types, "host")
        caps = {}
        for c in h.capabilities:
            if c.name in caps:
                self.err("E0112", f"capability '{c.name}' declared twice in host interface", c)
            caps[c.name] = c
        self.host = HostInfo(types=dict(self.types), caps=caps, models={m.name: m for m in h.models},
                             inputs={i.name: i.tags for i in h.inputs})
        self.declare_types(self.prog.types, "program")
        self.cap_types = {}
        for c in h.capabilities:
            ps = [(p.name, self.resolve_type(p.type), p) for p in c.params]
            ret = self.resolve_type(c.ret) if c.ret else UNIT
            self.cap_types[c.name] = (ps, ret)

        uses = self.prog.uses
        for u in uses:
            if u not in caps and u not in self.host.models:
                self.err("E0202", f"program uses '{u}', which this host does not provide", self.prog,
                         hint=self.suggest(u, list(caps) + list(self.host.models)))
        if len(set(uses)) != len(uses):
            self.warn("W0202", "duplicate entry in 'uses'", self.prog)

        for a in self.prog.agents:
            if a.model not in self.host.models:
                self.err("E0203", f"agent '{a.name}' uses model '{a.model}', which this host does not provide", a,
                         hint=self.suggest(a.model, self.host.models))
            elif a.model not in uses:
                self.err("E0201", f"agent '{a.name}' uses model '{a.model}', which is not declared in 'uses'", a,
                         hint=f"add '{a.model}' to the program's uses line")
            if a.name in self.fns or a.name in caps:
                self.err("E0112", f"'{a.name}' is defined more than once", a)

        seen = set()
        for f in self.prog.fns:
            if f.name in seen:
                self.err("E0112", f"function '{f.name}' is defined more than once", f)
            seen.add(f.name)
            if f.name in BUILTINS:
                self.err("E0112", f"function '{f.name}' shadows a builtin", f)

        self.check_recursion()

        main = self.fns.get("main")
        eff: Effects = {}
        ret_t = UNIT
        if main is None:
            self.err("E0107", "program has no 'main' function", self.prog,
                     hint="add: fn main() { ... }")
        else:
            arg_infos = []
            for p in main.params:
                tags = self.host.inputs.get(p.name, ["untrusted"])
                arg_infos.append(Info(self.resolve_type(p.type),
                                      {t: frozenset([f"input '{p.name}'"]) for t in tags}))
            ret_t, ret_taint, eff = self.analyze_fn(main, arg_infos, {}, main)

        # analyse functions that main never calls, for type errors only
        for f in self.prog.fns:
            if not any(k[0] == f.name for k in self.fn_cache):
                if f.name != "main":
                    self.warn("W0101", f"function '{f.name}' is never called from main", f)
                self.analyze_fn(f, [Info(self.resolve_type(p.type)) for p in f.params], {}, f, record=False)

        self.in_test = True
        for t in self.prog.tests:
            self.check_block(t.body, Scope(None), {}, loop_depth=0, record=False)
        self.in_test = False

        for u in uses:
            if u in caps or u in self.host.models:
                if u not in self.used_resources:
                    self.warn("W0201", f"'{u}' is declared in uses but never used", self.prog,
                              hint="remove it from 'uses' to keep the program's authority minimal")

        for a in self.prog.agents:
            n = eff.get(f"agent:{a.name}", 0)
            if a.max_calls is not None and n > a.max_calls:
                self.err("E0403", f"agent '{a.name}' may be called up to {n} times, but its max_calls is {a.max_calls}", a,
                         hint="reduce loop limits or retries around this agent, or raise max_calls")

        manifest = self.build_manifest(eff)
        if self.prog.budget is not None and manifest["worst_case_cost_usd"] > self.prog.budget + 1e-12:
            parts = [f"{r}: {c['max_calls']} × ${c['cost_per_call']:.4f}" for r, c in manifest["resources"].items()
                     if c["max_calls"] and c["cost_per_call"]]
            self.err("E0404",
                     f"worst-case cost ${manifest['worst_case_cost_usd']:.4f} exceeds the program budget ${self.prog.budget:.4f}",
                     self.prog, notes=parts, hint="lower loop limits or retries, or raise the budget")
        return CheckResult(self.prog, self.host, self.diags, manifest, self.types, self.node_types)

    def check_recursion(self):
        graph = {f.name: set() for f in self.prog.fns}

        def walk(n, acc):
            if isinstance(n, A.Call) and len(n.callee) == 1 and n.callee[0] in self.fns:
                acc.add(n.callee[0])
            if isinstance(n, A.Node):
                for k, v in vars(n).items():
                    if k not in ("span", "nid", "comments", "trailing"):
                        walk(v, acc)
            elif isinstance(n, (list, tuple)):
                for x in n:
                    walk(x, acc)
        for f in self.prog.fns:
            walk(f.body, graph[f.name])
        state = {}

        def dfs(u, path):
            state[u] = 1
            for v in graph.get(u, ()):
                if state.get(v) == 1:
                    cyc = path[path.index(v):] + [v] if v in path else [u, v]
                    self.err("E0402", f"recursion is not allowed: {' -> '.join(cyc)}", self.fns[v],
                             hint="Remit programs are total so that bounds can be computed; use a bounded for-loop")
                elif state.get(v) is None:
                    dfs(v, path + [v])
            state[u] = 2
        for f in self.prog.fns:
            if state.get(f.name) is None:
                dfs(f.name, [f.name])
        self.recursive = any(d.code == "E0402" for d in self.diags)

    # ----- functions -----
    def analyze_fn(self, f: A.FnDecl, args: list[Info], pc: Taint, site, record=True):
        key = (f.name, tuple(_freeze(a.taint) for a in args), _freeze(pc), record)
        if key in self.fn_cache:
            return self.fn_cache[key]
        self.fn_cache[key] = (self.resolve_type(f.ret) if f.ret else UNIT, {}, {})  # guard against cycles
        scope = Scope(None)
        for p, a in zip(f.params, args):
            scope.define(p.name, VarInfo(self.resolve_type(p.type), a.taint, False))
        ret_t = self.resolve_type(f.ret) if f.ret else UNIT
        prev = getattr(self, "_fn_stack", [])
        self._fn_stack = prev + [(f, ret_t, [])]
        saved_in_test, self.in_test = self.in_test, False
        try:
            body = self.check_block(f.body, scope, pc, loop_depth=0, record=record)
        finally:
            self.in_test = saved_in_test
        _, _, rets = self._fn_stack[-1]
        self._fn_stack = prev
        taint = t_union(*[r.taint for r in rets], body.taint)
        if f.ret is not None and ret_t != UNIT:
            if not self.block_returns(f.body):
                self.err("E0109", f"function '{f.name}' must return a value of type {ret_t.show()} on every path",
                         f, hint="end the function with a return statement")
        result = (ret_t, taint, body.eff)
        self.fn_cache[key] = result
        return result

    def block_returns(self, b: A.Block) -> bool:
        for s in b.stmts:
            if isinstance(s, A.Return):
                return True
            if isinstance(s, A.ExprStmt) and isinstance(s.expr, A.IfExpr):
                if self.if_returns(s.expr):
                    return True
            if isinstance(s, A.ExprStmt) and isinstance(s.expr, A.MatchExpr):
                if s.expr.arms and all(self.block_returns(a.body) for a in s.expr.arms):
                    return True
        return False

    def if_returns(self, e: A.IfExpr) -> bool:
        if e.els is None:
            return False
        els_ok = self.if_returns(e.els) if isinstance(e.els, A.IfExpr) else self.block_returns(e.els)
        return self.block_returns(e.then) and els_ok

    # ----- blocks and statements -----
    def check_block(self, b: A.Block, parent: "Scope", pc: Taint, loop_depth: int, record: bool) -> Info:
        scope = Scope(parent)
        return self._stmts(b.stmts, scope, pc, loop_depth, record)

    def _stmts(self, stmts: list, scope: "Scope", pc: Taint, loop_depth: int, record: bool) -> Info:
        """Effects of a statement sequence. An `if` with a branch that always returns makes that branch an
        alternative to the rest of the block (path-sensitive bound), instead of adding to it."""
        eff: Effects = {}
        for i, s in enumerate(stmts):
            last_stmt = i == len(stmts) - 1
            if isinstance(s, A.ExprStmt) and isinstance(s.expr, A.IfExpr) and not last_stmt:
                e = s.expr
                then_ret = self.block_returns(e.then)
                else_ret = e.els is not None and (self.if_returns(e.els) if isinstance(e.els, A.IfExpr)
                                                  else self.block_returns(e.els))
                if then_ret or else_ret:
                    c, th, el = self.if_parts(e, scope, pc, loop_depth, record)
                    self.node_types[e.nid] = UNIT
                    if then_ret and else_ret:
                        rest_eff = {}
                    else:
                        rest_eff = self._stmts(stmts[i + 1:], scope, pc, loop_depth, record).eff
                    if then_ret and not else_ret:
                        branch = e_max(th.eff, e_add(el.eff, rest_eff))
                    elif else_ret and not then_ret:
                        branch = e_max(e_add(th.eff, rest_eff), el.eff)
                    else:
                        branch = e_max(th.eff, el.eff)
                    return Info(UNIT, {}, e_add(eff, c.eff, branch))
            info = self.check_stmt(s, scope, pc, loop_depth, record)
            eff = e_add(eff, info.eff)
            if last_stmt and isinstance(s, A.ExprStmt):
                return Info(info.type, info.taint, eff)
        return Info(UNIT, {}, eff)

    def if_parts(self, e: A.IfExpr, scope, pc, ld, rec):
        c = self.expr(e.cond, scope, pc, ld, rec)
        if c.type not in (BOOL, NEVER):
            self.err("E0103", f"'if' condition must be Bool, found {c.type.show()}", e.cond)
        inner_pc = t_union(pc, c.taint)
        th = self.check_block(e.then, scope, inner_pc, ld, rec)
        if e.els is None:
            el = Info(UNIT)
        elif isinstance(e.els, A.IfExpr):
            el = self.expr(e.els, scope, inner_pc, ld, rec)
        else:
            el = self.check_block(e.els, scope, inner_pc, ld, rec)
        return c, th, el

    def check_stmt(self, s, scope, pc, loop_depth, record) -> Info:
        if isinstance(s, A.Let):
            v = self.expr(s.value, scope, pc, loop_depth, record)
            t = v.type
            if s.type is not None:
                dt = self.resolve_type(s.type)
                if not assignable(v.type, dt):
                    self.err("E0103", f"'{s.name}' is declared as {dt.show()} but the value has type {v.type.show()}", s.value)
                elif bound_violation(v.type, dt) and not s.mutable:
                    self.err("E0113", f"'{s.name}': {bound_violation(v.type, dt)}", s.value,
                             hint="use take(xs, N) to bound the list")
                t = dt
            elif s.mutable:
                t = _widen(t)
                if isinstance(t, ListT):
                    t = ListT(t.elem, None)  # an unannotated var list may grow in loops: treat as unbounded
            if scope.lookup_local(s.name):
                self.err("E0112", f"'{s.name}' is already defined in this block", s, hint="choose a different name")
            scope.define(s.name, VarInfo(t, t_union(v.taint, pc) if s.mutable else v.taint, s.mutable))
            return Info(UNIT, {}, v.eff)
        if isinstance(s, A.Assign):
            var = scope.lookup(s.name)
            v = self.expr(s.value, scope, pc, loop_depth, record)
            if var is None:
                self.err("E0101", f"unknown variable '{s.name}'", s, hint=self.suggest(s.name, scope.names()))
                return Info(UNIT, {}, v.eff)
            if not var.mutable:
                self.err("E0108", f"cannot assign to '{s.name}': it was declared with 'let'", s,
                         hint=f"declare it with 'var {s.name} = ...' if it must change")
            elif not assignable(v.type, var.type):
                self.err("E0103", f"cannot assign a value of type {v.type.show()} to '{s.name}' of type {var.type.show()}", s.value)
            var.taint = t_union(var.taint, v.taint, pc)
            return Info(UNIT, {}, v.eff)
        if isinstance(s, A.Return):
            if self.in_test or not getattr(self, "_fn_stack", None):
                self.err("E0109", "'return' is only allowed inside a function", s)
                return Info(NEVER)
            f, rt, rets = self._fn_stack[-1]
            if s.value is None:
                if rt != UNIT:
                    self.err("E0109", f"'{f.name}' must return a value of type {rt.show()}", s)
                return Info(NEVER)
            v = self.expr(s.value, scope, pc, loop_depth, record)
            if not assignable(v.type, rt):
                self.err("E0109", f"'{f.name}' returns {rt.show()} but this value has type {v.type.show()}", s.value)
            else:
                bv = bound_violation(v.type, rt)
                if bv:
                    self.err("E0113", f"return value of '{f.name}': {bv}", s.value, hint="use take(xs, N) to bound the list")
            rets.append(Info(v.type, t_union(v.taint, pc)))
            return Info(NEVER, {}, v.eff)
        if isinstance(s, A.ExprStmt):
            return self.expr(s.expr, scope, pc, loop_depth, record)
        if isinstance(s, A.Expect):
            if not self.in_test:
                self.err("E0110", "'expect' is only allowed inside a test block", s)
            v = self.expr(s.cond, scope, pc, loop_depth, record)
            if v.type not in (BOOL, NEVER):
                self.err("E0103", f"'expect' needs a Bool condition, found {v.type.show()}", s.cond)
            return Info(UNIT, {}, v.eff)
        if isinstance(s, A.Break):
            if loop_depth == 0:
                self.err("E0110", "'break' outside of a for-loop", s)
            return Info(NEVER)
        raise AssertionError(s)

    # ----- expressions -----
    def expr(self, e, scope, pc, loop_depth, record) -> Info:
        info = self._expr(e, scope, pc, loop_depth, record)
        self.node_types[e.nid] = info.type
        return info

    def _expr(self, e, scope, pc, ld, rec) -> Info:
        if isinstance(e, A.Lit):
            v = e.value
            if isinstance(v, str):
                return Info(EnumT((v,)))  # singleton literal type; widens to Str or to a union containing it
            t = BOOL if isinstance(v, bool) else INT if isinstance(v, int) else FLOAT
            return Info(t)
        if isinstance(e, A.Interp):
            parts = [self.expr(p, scope, pc, ld, rec) for p in e.parts if isinstance(p, A.Node)]
            return Info(STR, t_union(*[p.taint for p in parts]), e_add(*[p.eff for p in parts]))
        if isinstance(e, A.Name):
            v = scope.lookup(e.name)
            if v is None:
                hint = self.suggest(e.name, scope.names())
                if e.name in self.fns or e.name in BUILTINS:
                    hint = f"'{e.name}' is a function; call it with ()"
                self.err("E0101", f"unknown variable '{e.name}'", e, hint=hint)
                return Info(NEVER)
            return Info(v.type, dict(v.taint))
        if isinstance(e, A.Field):
            o = self.expr(e.obj, scope, pc, ld, rec)
            if o.type == NEVER:
                return Info(NEVER, o.taint, o.eff)
            if not isinstance(o.type, RecordT):
                self.err("E0102", f"type {o.type.show()} has no field '{e.name}'", e)
                return Info(NEVER, o.taint, o.eff)
            ft = o.type.get(e.name)
            if ft is None:
                self.err("E0102", f"{o.type.show()} has no field '{e.name}'", e,
                         hint=self.suggest(e.name, [n for n, _ in o.type.fields]) or
                         f"available fields: {', '.join(n for n, _ in o.type.fields)}")
                return Info(NEVER, o.taint, o.eff)
            return Info(ft, o.taint, o.eff)
        if isinstance(e, A.Index):
            o = self.expr(e.obj, scope, pc, ld, rec)
            i = self.expr(e.index, scope, pc, ld, rec)
            if i.type not in (INT, NEVER):
                self.err("E0103", f"list index must be Int, found {i.type.show()}", e.index)
            if isinstance(o.type, ListT):
                return Info(o.type.elem, t_union(o.taint, i.taint), e_add(o.eff, i.eff))
            if o.type != NEVER:
                self.err("E0103", f"cannot index into {o.type.show()}", e)
            return Info(NEVER, o.taint, e_add(o.eff, i.eff))
        if isinstance(e, A.ListLit):
            items = [self.expr(x, scope, pc, ld, rec) for x in e.items]
            et = NEVER
            for it, node in zip(items, e.items):
                j = join(et, it.type)
                if j is None:
                    self.err("E0103", f"list items must have the same type: {et.show()} vs {it.type.show()}", node)
                else:
                    et = j
            return Info(ListT(et, len(items)), t_union(*[i.taint for i in items]), e_add(*[i.eff for i in items]))
        if isinstance(e, A.RecordLit):
            fs = [(n, self.expr(x, scope, pc, ld, rec)) for n, x in e.fields]
            return Info(RecordT(tuple((n, i.type) for n, i in fs)), t_union(*[i.taint for _, i in fs]),
                        e_add(*[i.eff for _, i in fs]))
        if isinstance(e, A.Unary):
            o = self.expr(e.operand, scope, pc, ld, rec)
            if e.op == "not":
                if o.type not in (BOOL, NEVER):
                    self.err("E0106", f"'not' needs a Bool, found {o.type.show()}", e.operand)
                return Info(BOOL, o.taint, o.eff)
            if o.type not in (INT, FLOAT, NEVER):
                self.err("E0106", f"unary '-' needs a number, found {o.type.show()}", e.operand)
            return Info(o.type, o.taint, o.eff)
        if isinstance(e, A.Binary):
            return self.binary(e, scope, pc, ld, rec)
        if isinstance(e, A.Call):
            return self.call(e, scope, pc, ld, rec)
        if isinstance(e, A.IfExpr):
            c, th, el = self.if_parts(e, scope, pc, ld, rec)
            if e.els is None:
                return Info(UNIT, {}, e_add(c.eff, th.eff))
            t = join(th.type, el.type)
            if t is None:
                t = UNIT  # branches disagree: usable only as a statement
            return Info(t, t_union(th.taint, el.taint, c.taint), e_add(c.eff, e_max(th.eff, el.eff)))
        if isinstance(e, A.ForExpr):
            return self.for_expr(e, scope, pc, ld, rec)
        if isinstance(e, A.MatchExpr):
            return self.match_expr(e, scope, pc, ld, rec)
        raise AssertionError(e)

    def binary(self, e: A.Binary, scope, pc, ld, rec) -> Info:
        l = self.expr(e.left, scope, pc, ld, rec)
        r = self.expr(e.right, scope, pc, ld, rec)
        taint = t_union(l.taint, r.taint)
        eff = e_add(l.eff, r.eff)
        lt, rt, op = l.type, r.type, e.op
        if NEVER in (lt, rt):
            res = BOOL if op in ("==", "!=", "<", "<=", ">", ">=", "and", "or", "in") else (rt if lt == NEVER else lt)
            return Info(res, taint, eff)
        if op in ("and", "or"):
            for side, t in ((e.left, lt), (e.right, rt)):
                if t != BOOL:
                    self.err("E0106", f"'{op}' needs Bool operands, found {t.show()}", side)
            return Info(BOOL, taint, eff)
        if op in ("==", "!="):
            for a, b, node in ((lt, rt, e.right), (rt, lt, e.left)):
                if isinstance(a, EnumT) and isinstance(node, A.Lit) and isinstance(node.value, str):
                    if node.value not in a.options:
                        self.err("E0111", f'"{node.value}" is not a possible value of {a.show()}', node,
                                 hint=f"possible values: {', '.join(repr(o) for o in a.options)}")
            if join(lt, rt) is None:
                self.err("E0106", f"cannot compare {lt.show()} with {rt.show()}", e)
            return Info(BOOL, taint, eff)
        if op in ("<", "<=", ">", ">="):
            ok = (lt in (INT, FLOAT) and rt in (INT, FLOAT)) or (lt == STR and rt == STR)
            if not ok:
                self.err("E0106", f"cannot order {lt.show()} and {rt.show()} with '{op}'", e)
            return Info(BOOL, taint, eff)
        if op == "in":
            if isinstance(rt, ListT):
                if join(lt, rt.elem) is None:
                    self.err("E0106", f"cannot look for {lt.show()} in {rt.show()}", e)
            elif rt == STR or isinstance(rt, EnumT):
                if not (lt == STR or isinstance(lt, EnumT)):
                    self.err("E0106", f"'in' on a string needs a Str on the left, found {lt.show()}", e.left)
            else:
                self.err("E0106", f"'in' needs a List or Str on the right, found {rt.show()}", e.right)
            return Info(BOOL, taint, eff)
        if op == "+":
            if (lt == STR or isinstance(lt, EnumT)) and (rt == STR or isinstance(rt, EnumT)):
                return Info(STR, taint, eff)
            if isinstance(lt, ListT) and isinstance(rt, ListT):
                j = join(lt, rt)
                if j is None:
                    self.err("E0106", f"cannot concatenate {lt.show()} and {rt.show()}", e)
                    return Info(lt, taint, eff)
                mx = None if lt.max is None or rt.max is None else lt.max + rt.max
                return Info(ListT(j.elem, mx), taint, eff)
        if op in ("+", "-", "*", "/", "%"):
            if lt in (INT, FLOAT) and rt in (INT, FLOAT):
                if op == "/":
                    return Info(FLOAT, taint, eff)
                return Info(FLOAT if FLOAT in (lt, rt) else INT, taint, eff)
            hint = 'use string interpolation: "{a}{b}"' if op == "+" and STR in (lt, rt) else None
            self.err("E0106", f"cannot apply '{op}' to {lt.show()} and {rt.show()}", e, hint=hint)
            return Info(lt, taint, eff)
        raise AssertionError(op)

    def for_expr(self, e: A.ForExpr, scope, pc, ld, rec) -> Info:
        it = self.expr(e.iter, scope, pc, ld, rec)
        n: Optional[int] = None
        elem = NEVER
        if isinstance(it.type, ListT):
            elem = it.type.elem
            n = it.type.max
        elif it.type != NEVER:
            self.err("E0103", f"for-loops iterate over lists, found {it.type.show()}", e.iter)
        if e.limit is not None:
            n = e.limit if n is None else min(n, e.limit)
        if n is None:
            n = 0
            if it.type != NEVER:
                self.err("E0401", "loop over a list with no known size bound", e.iter,
                         hint=f"add a limit, e.g. 'for {e.var} in ... limit 20', so the worst case is known before running",
                         notes=[f"'{_src(e.iter)}' has type {it.type.show()}"])
        inner = Scope(scope)
        inner.define(e.var, VarInfo(elem, dict(it.taint), False))
        inner_pc = t_union(pc, it.taint)
        weff: Effects = {}
        if e.where is not None:
            w = self.expr(e.where, inner, inner_pc, ld + 1, rec)
            if w.type not in (BOOL, NEVER):
                self.err("E0103", f"loop filter must be Bool, found {w.type.show()}", e.where)
            weff = w.eff
            inner_pc = t_union(inner_pc, w.taint)
        body = self.check_block(e.body, inner, inner_pc, ld + 1, rec)
        eff = e_add(it.eff, e_scale(e_add(weff, body.eff), n))
        return Info(ListT(body.type, n), t_union(body.taint, it.taint), eff)

    def match_expr(self, e: A.MatchExpr, scope, pc, ld, rec) -> Info:
        s = self.expr(e.subject, scope, pc, ld, rec)
        if not (isinstance(s.type, EnumT) or s.type in (STR, NEVER)):
            self.err("E0111", f"match needs a string or a union of string literals, found {s.type.show()}", e.subject)
        inner_pc = t_union(pc, s.taint)
        seen = set()
        has_default = False
        t = NEVER
        effs = []
        taint = dict(s.taint)
        for arm in e.arms:
            if arm.pattern is None:
                if has_default:
                    self.err("E0111", "duplicate '_' arm", arm)
                has_default = True
            else:
                if arm.pattern in seen:
                    self.err("E0111", f'duplicate match arm "{arm.pattern}"', arm)
                seen.add(arm.pattern)
                if isinstance(s.type, EnumT) and arm.pattern not in s.type.options:
                    self.err("E0111", f'"{arm.pattern}" is not a possible value of {s.type.show()}', arm,
                             hint=f"possible values: {', '.join(repr(o) for o in s.type.options)}")
            b = self.check_block(arm.body, scope, inner_pc, ld, rec)
            effs.append(b.eff)
            taint = t_union(taint, b.taint)
            j = join(t, b.type)
            t = j if j is not None else UNIT
        if not has_default:
            if isinstance(s.type, EnumT):
                missing = [o for o in s.type.options if o not in seen]
                if missing:
                    self.err("E0111", f"match is not exhaustive: missing {', '.join(repr(m) for m in missing)}", e,
                             hint="add the missing arms or a '_ =>' arm")
            elif s.type == STR:
                self.err("E0111", "match on Str needs a '_ =>' arm", e)
        return Info(t, taint, e_add(s.eff, e_max(*effs) if effs else {}))

    # ----- calls -----
    def call(self, e: A.Call, scope, pc, ld, rec) -> Info:
        path = e.callee
        dotted = ".".join(path)
        if len(path) == 2 and path[1] == "ask" and (path[0] in self.host.models or path[0] in self.agents):
            return self.model_call(e, scope, pc, ld, rec)
        if dotted in self.host.caps:
            return self.cap_call(e, scope, pc, ld, rec)
        if e.retry is not None or e.timeout is not None or e.fallback is not None:
            self.err("E0107", "retry, timeout and else apply only to capability and model calls", e)
        if len(path) == 1 and path[0] in self.fns:
            return self.fn_call(e, scope, pc, ld, rec)
        if len(path) == 1 and path[0] in BUILTINS:
            return self.builtin_call(e, scope, pc, ld, rec)
        if len(path) >= 2:
            if path[0] in self.host.models or path[0] in self.agents:
                self.err("E0203", f"models and agents are called with .ask(Type, prompt, context: ...)", e)
            else:
                ns = [c for c in self.host.caps if c.startswith(path[0] + ".")]
                self.err("E0202", f"unknown capability '{dotted}'", e,
                         hint=self.suggest(dotted, self.host.caps) or
                         (f"capabilities in '{path[0]}': {', '.join(ns)}" if ns else "the host interface does not provide it"))
        else:
            self.err("E0107", f"unknown function '{path[0]}'", e,
                     hint=self.suggest(path[0], list(self.fns) + sorted(BUILTINS)))
        for a in e.args:
            self.expr(a.value, scope, pc, ld, rec)
        return Info(NEVER)

    def bind_args(self, e: A.Call, params: list, what: str):
        """Map call args to params; params is list of (name, type, node). Returns dict name->Arg or None."""
        out = {}
        pos = [a for a in e.args if a.name is None]
        named = [a for a in e.args if a.name is not None]
        names = [p[0] for p in params]
        if len(pos) > len(params):
            self.err("E0104", f"{what} takes {len(params)} argument(s) but {len(e.args)} were given", e)
            return None
        for p, a in zip(params, pos):
            out[p[0]] = a
        for a in named:
            if a.name not in names:
                self.err("E0104", f"{what} has no parameter '{a.name}'", a,
                         hint=self.suggest(a.name, names) or f"parameters: {', '.join(names)}")
                return None
            if a.name in out:
                self.err("E0104", f"argument '{a.name}' given twice", a)
                return None
            out[a.name] = a
        missing = [n for n in names if n not in out]
        if missing:
            self.err("E0104", f"{what} is missing argument(s): {', '.join(missing)}", e)
            return None
        return out

    def check_arg(self, a: A.Arg, info: Info, pt: Type, what: str):
        if not assignable(info.type, pt):
            self.err("E0103", f"{what} expects {pt.show()}, found {info.type.show()}", a.value)
            return
        bv = bound_violation(info.type, pt)
        if bv:
            self.err("E0113", f"{what}: {bv}", a.value, hint="use take(xs, N) to bound the list")

    def cap_call(self, e: A.Call, scope, pc, ld, rec) -> Info:
        name = ".".join(e.callee)
        cap: A.CapabilityDecl = self.host.caps[name]
        params, ret = self.cap_types[name]
        if name not in self.prog.uses and not self.in_test:
            self.err("E0201", f"capability '{name}' is used but not declared in the program's 'uses' line", e,
                     hint=f"add '{name}' to 'uses' (the host policy must also grant it)")
        if self.in_test:
            self.err("E0110", "tests may not call capabilities directly; call program functions instead", e)
        self.used_resources.add(name)
        bound = self.bind_args(e, [(n, t, p) for n, t, p in params], f"'{name}'")
        arg_infos = {}
        eff: Effects = {}
        for a in e.args:
            arg_infos[id(a)] = self.expr(a.value, scope, pc, ld, rec)
            eff = e_add(eff, arg_infos[id(a)].eff)
        site = self.call_sites.setdefault(e.nid, {
            "site": e.nid, "line": e.span.line, "col": e.span.col, "resource": name,
            "approval": "always" if cap.approval_always else "none", "reasons": [],
            "retry": e.retry or 0, "timeout_s": e.timeout, "fallback": e.fallback is not None,
        })
        if cap.approval_always:
            r = "host requires approval for every call"
            if r not in site["reasons"]:
                site["reasons"].append(r)
        all_taint = dict(pc)
        if bound is not None:
            for pname, ptype, pnode in params:
                a = bound[pname]
                info = arg_infos[id(a)]
                self.check_arg(a, info, ptype, f"'{name}' parameter '{pname}'")
                all_taint = t_union(all_taint, info.taint)
                data = info.taint
                denied = [t for t in pnode.deny if t in data]
                if denied:
                    origins = sorted(set().union(*[data[t] for t in denied]))
                    self.err("E0301",
                             f"data tagged {', '.join(denied)} flows into '{name}' parameter '{pname}', which denies it",
                             a.value, notes=[f"'{_src(a.value)}' is {t} because it derives from {', '.join(sorted(data[t]))}"
                                             for t in denied],
                             hint="the host forbids this flow; it cannot be approved. Use a different source for this value")
                needs = [t for t in pnode.approve if t in data or t in pc]
                if needs:
                    for t in needs:
                        srcs = sorted(data.get(t, frozenset()) | pc.get(t, frozenset()))
                        via = "data" if t in data else "control flow"
                        r = f"'{pname}' may be {t} ({via}: {', '.join(srcs)})"
                        if r not in site["reasons"]:
                            site["reasons"].append(r)
                    if site["approval"] == "none":
                        site["approval"] = "possible"
                key = (name, pname)
                fl = self.flows.setdefault(key, {"sink": f"{name}.{pname}", "tags": set(), "verdict": "allowed"})
                for t in data:
                    fl["tags"].add(t)
                if denied:
                    fl["verdict"] = "denied"
                elif needs and fl["verdict"] == "allowed":
                    fl["verdict"] = "requires approval"
        if e.retry and not cap.idempotent:
            self.err("E0405", f"'{name}' is not idempotent, so it cannot be retried", e,
                     hint="remove 'retry', or have the host declare the capability idempotent (e.g. with an idempotency key)")
        attempts = 1 + (e.retry or 0)
        eff = e_add(eff, {name: attempts})
        out_t = ret
        taint = t_clear(t_union(all_taint, {t: frozenset([f"{name} (line {e.span.line})"]) for t in cap.tags}), cap.clears)
        if e.fallback is not None:
            fb = self.expr(e.fallback, scope, pc, ld, rec)
            if not assignable(fb.type, ret):
                self.err("E0103", f"fallback after 'else' must have type {ret.show()}, found {fb.type.show()}", e.fallback)
            elif isinstance(ret, ListT) and isinstance(fb.type, ListT):
                out_t = join(ret, fb.type) or ret
            eff = e_add(eff, fb.eff)
            taint = t_union(taint, fb.taint)
        return Info(out_t, taint, eff)

    def model_call(self, e: A.Call, scope, pc, ld, rec) -> Info:
        target = e.callee[0]
        agent = self.agents.get(target)
        model = agent.model if agent else target
        if model not in self.prog.uses and not agent:
            self.err("E0201", f"model '{model}' is used but not declared in the program's 'uses' line", e,
                     hint=f"add '{model}' to 'uses'")
        if self.in_test:
            self.err("E0110", "tests may not call models directly; call program functions instead", e)
        self.used_resources.add(model)
        pos = [a for a in e.args if a.name is None]
        named = {a.name: a for a in e.args if a.name is not None}
        out_t: Type = NEVER
        if not pos or not isinstance(pos[0].value, A.Name):
            self.err("E0203", f"'{target}.ask' needs the output type first, e.g. {target}.ask(Summary, \"prompt\")", e)
        else:
            tn = pos[0].value.name
            prims = {"Int": INT, "Float": FLOAT, "Bool": BOOL, "Str": STR}
            if tn in prims:
                out_t = prims[tn]
            elif tn in self.types:
                out_t = self.types[tn]
            else:
                self.err("E0105", f"unknown output type '{tn}'", pos[0].value, hint=self.suggest(tn, list(self.types) + list(prims)))
        if len(pos) != 2:
            self.err("E0203", f"'{target}.ask' takes an output type and a prompt, plus an optional context: argument", e)
        for n in named:
            if n != "context":
                self.err("E0104", f"'{target}.ask' has no parameter '{n}'", named[n], hint="the only named argument is context:")
        infos = []
        eff: Effects = {}
        value_args = ([pos[1]] if len(pos) > 1 else []) + ([named["context"]] if "context" in named else [])
        for a in value_args:
            i = self.expr(a.value, scope, pc, ld, rec)
            infos.append(i)
            eff = e_add(eff, i.eff)
            if a.name is None and i.type not in (STR, NEVER) and not isinstance(i.type, EnumT):
                self.err("E0103", f"prompt must be Str, found {i.type.show()}", a.value)
        if self.has_list_unbounded(out_t):
            self.err("E0113", f"model output type {out_t.show()} contains a list with no 'max' bound", pos[0].value,
                     hint="add a bound in the type, e.g. List[Str] max 10, so the output size is known")
        site = self.call_sites.setdefault(e.nid, {
            "site": e.nid, "line": e.span.line, "col": e.span.col, "resource": model,
            "agent": agent.name if agent else None, "approval": "none", "reasons": [],
            "retry": e.retry or 0, "timeout_s": e.timeout, "fallback": e.fallback is not None,
        })
        attempts = 1 + (e.retry or 0)
        eff = e_add(eff, {model: attempts})
        if agent:
            eff = e_add(eff, {f"agent:{agent.name}": attempts})
        taint = t_union(*[i.taint for i in infos])
        if e.fallback is not None:
            fb = self.expr(e.fallback, scope, pc, ld, rec)
            if out_t != NEVER and not assignable(fb.type, out_t):
                self.err("E0103", f"fallback after 'else' must have type {out_t.show()}, found {fb.type.show()}", e.fallback)
            eff = e_add(eff, fb.eff)
            taint = t_union(taint, fb.taint)
        return Info(out_t, taint, eff)

    def has_list_unbounded(self, t: Type) -> bool:
        if isinstance(t, ListT):
            return t.max is None or self.has_list_unbounded(t.elem)
        if isinstance(t, RecordT):
            return any(self.has_list_unbounded(ft) for _, ft in t.fields)
        return False

    def fn_call(self, e: A.Call, scope, pc, ld, rec) -> Info:
        f = self.fns[e.callee[0]]
        params = [(p.name, self.resolve_type(p.type), p) for p in f.params]
        bound = self.bind_args(e, params, f"'{f.name}'")
        infos = {}
        eff: Effects = {}
        for a in e.args:
            infos[id(a)] = self.expr(a.value, scope, pc, ld, rec)
            eff = e_add(eff, infos[id(a)].eff)
        if bound is None:
            return Info(self.resolve_type(f.ret) if f.ret else UNIT, {}, eff)
        args = []
        for pname, ptype, _ in params:
            a = bound[pname]
            self.check_arg(a, infos[id(a)], ptype, f"'{f.name}' parameter '{pname}'")
            args.append(infos[id(a)])
        if self.recursive:
            return Info(self.resolve_type(f.ret) if f.ret else UNIT, {}, eff)
        saved = getattr(self, "_fn_stack", [])
        rt, taint, feff = self.analyze_fn(f, args, pc, e, record=rec)
        self._fn_stack = saved
        return Info(rt, taint, e_add(eff, feff))

    def builtin_call(self, e: A.Call, scope, pc, ld, rec) -> Info:
        name = e.callee[0]
        if any(a.name for a in e.args):
            self.err("E0104", f"builtin '{name}' takes positional arguments only", e)
        infos = [self.expr(a.value, scope, pc, ld, rec) for a in e.args]
        taint = t_union(*[i.taint for i in infos])
        eff = e_add(*[i.eff for i in infos])
        ts = [i.type for i in infos]

        def arity(*ns):
            if len(infos) not in ns:
                self.err("E0104", f"'{name}' takes {' or '.join(map(str, ns))} argument(s), got {len(infos)}", e)
                return False
            return True

        def need(i, ok, desc):
            if ts[i] != NEVER and not ok(ts[i]):
                self.err("E0103", f"'{name}' argument {i + 1} must be {desc}, found {ts[i].show()}", e.args[i].value)

        is_str = lambda t: t == STR or isinstance(t, EnumT)
        is_num = lambda t: t in (INT, FLOAT)
        is_list = lambda t: isinstance(t, ListT)
        R = lambda t: Info(t, taint, eff)
        if name == "len":
            if arity(1):
                need(0, lambda t: is_list(t) or is_str(t), "a List or Str")
            return R(INT)
        if name == "take":
            if arity(2):
                need(0, is_list, "a List")
                if not (isinstance(e.args[1].value, A.Lit) and isinstance(e.args[1].value.value, int)):
                    self.err("E0113", "take() needs an integer literal as its second argument", e.args[1].value,
                             hint="the bound must be a constant so the checker can use it")
                    return R(ts[0] if is_list(ts[0]) else NEVER)
                k = e.args[1].value.value
                if is_list(ts[0]):
                    mx = k if ts[0].max is None else min(k, ts[0].max)
                    return R(ListT(ts[0].elem, mx))
            return R(NEVER)
        if name == "range":
            if arity(1):
                if not (isinstance(e.args[0].value, A.Lit) and isinstance(e.args[0].value.value, int)):
                    self.err("E0113", "range() needs an integer literal", e.args[0].value)
                    return R(ListT(INT, None))
                return R(ListT(INT, e.args[0].value.value))
            return R(ListT(INT, 0))
        if name in ("str",):
            arity(1)
            return R(STR)
        if name in ("lower", "upper", "trim"):
            if arity(1):
                need(0, is_str, "Str")
            return R(STR)
        if name in ("startswith", "endswith"):
            if arity(2):
                need(0, is_str, "Str")
                need(1, is_str, "Str")
            return R(BOOL)
        if name == "contains":
            if arity(2):
                need(0, lambda t: is_str(t) or is_list(t), "a Str or List")
            return R(BOOL)
        if name == "replace":
            if arity(3):
                for i in range(3):
                    need(i, is_str, "Str")
            return R(STR)
        if name == "split":
            if arity(2):
                need(0, is_str, "Str")
                need(1, is_str, "Str")
            return R(ListT(STR, None))
        if name == "join":
            if arity(2):
                need(0, lambda t: is_list(t) and (is_str(t.elem) or t.elem == NEVER), "a List[Str]")
                need(1, is_str, "Str")
            return R(STR)
        if name == "sum":
            if arity(1):
                need(0, lambda t: is_list(t) and (is_num(t.elem) or t.elem == NEVER), "a list of numbers")
                if is_list(ts[0]) and ts[0].elem == FLOAT:
                    return R(FLOAT)
            return R(INT)
        if name in ("min", "max"):
            if arity(2):
                need(0, is_num, "a number")
                need(1, is_num, "a number")
                return R(FLOAT if FLOAT in ts else INT)
            return R(INT)
        if name == "abs":
            if arity(1):
                need(0, is_num, "a number")
                return R(ts[0] if is_num(ts[0]) else INT)
            return R(INT)
        if name == "round":
            if arity(1, 2):
                need(0, is_num, "a number")
                return R(FLOAT if len(ts) == 2 else INT)
            return R(INT)
        if name == "int":
            if arity(1):
                need(0, lambda t: is_num(t) or is_str(t), "a number or Str")
            return R(INT)
        if name == "float":
            if arity(1):
                need(0, lambda t: is_num(t) or is_str(t), "a number or Str")
            return R(FLOAT)
        if name == "first":
            if arity(1):
                need(0, is_list, "a List")
                return R(ts[0].elem if is_list(ts[0]) else NEVER)
            return R(NEVER)
        if name == "unique":
            if arity(1):
                need(0, is_list, "a List")
                return R(ts[0] if is_list(ts[0]) else NEVER)
            return R(NEVER)
        raise AssertionError(name)

    # ----- manifest -----
    def build_manifest(self, eff: Effects) -> dict:
        resources = {}
        total = 0.0
        for name, n in sorted(eff.items()):
            if name.startswith("agent:"):
                continue
            if name in self.host.caps:
                c = self.host.caps[name]
                cost = c.cost
                kind = "capability"
                extra = {"idempotent": c.idempotent, "approval_always": c.approval_always,
                         "output_tags": sorted(c.tags), "clears": sorted(c.clears)}
            else:
                cost = self.host.models[name].cost if name in self.host.models else 0.0
                kind = "model"
                extra = {}
            resources[name] = {"kind": kind, "max_calls": n, "cost_per_call": cost, "worst_case_cost": n * cost, **extra}
            total += n * cost
        for u in self.prog.uses:
            if u not in resources and (u in self.host.caps or u in self.host.models):
                resources[u] = {"kind": "capability" if u in self.host.caps else "model", "max_calls": 0,
                                "cost_per_call": 0.0, "worst_case_cost": 0.0}
        agents = {a.name: {"model": a.model, "max_calls_declared": a.max_calls,
                           "max_calls_worst_case": eff.get(f"agent:{a.name}", 0)} for a in self.prog.agents}
        sites = sorted(self.call_sites.values(), key=lambda s: (s["line"], s["col"]))
        return {
            "program": self.prog.name,
            "uses": list(self.prog.uses),
            "resources": resources,
            "agents": agents,
            "worst_case_cost_usd": round(total, 6),
            "budget_usd": self.prog.budget,
            "call_sites": sites,
            "approval_sites": [s for s in sites if s["approval"] != "none"],
            "flows": sorted(({**v, "tags": sorted(v["tags"])} for v in self.flows.values() if v["tags"]),
                            key=lambda f: f["sink"]),
            "inputs": {p.name: self.host.inputs.get(p.name, ["untrusted"])
                       for p in (self.fns["main"].params if "main" in self.fns else [])},
            "ambient_effects": "none: time, randomness, network and files are reachable only through declared capabilities",
        }


class Scope:
    def __init__(self, parent: Optional["Scope"]):
        self.parent = parent
        self.vars: dict[str, VarInfo] = {}

    def define(self, name, info):
        self.vars[name] = info

    def lookup(self, name) -> Optional[VarInfo]:
        s = self
        while s:
            if name in s.vars:
                return s.vars[name]
            s = s.parent
        return None

    def lookup_local(self, name):
        return self.vars.get(name)

    def names(self):
        out = set()
        s = self
        while s:
            out |= set(s.vars)
            s = s.parent
        return out


def _widen(t: Type) -> Type:
    """Widen unnamed literal enum types to Str (for mutable variables)."""
    if isinstance(t, EnumT) and t.name is None:
        return STR
    if isinstance(t, ListT):
        return ListT(_widen(t.elem), t.max)
    return t


def _freeze(t: Taint):
    return tuple(sorted((k, tuple(sorted(v))) for k, v in t.items()))


def _src(e) -> str:
    """Short source-ish rendering of an expression for messages."""
    from .formatter import fmt_expr
    try:
        s = fmt_expr(e)
    except Exception:
        s = type(e).__name__
    return s if len(s) <= 60 else s[:57] + "..."


def check(prog: A.Program, host: A.HostInterface) -> CheckResult:
    return Checker(prog, host).check()
