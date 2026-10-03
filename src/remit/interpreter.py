"""Tree-walking interpreter over the checked AST. No ambient effects: only the broker reaches the world."""
from __future__ import annotations

import json
from typing import Optional

from . import ast as A
from .broker import Broker
from .checker import BUILTINS, CheckResult
from .ir import assign_sites
from .models import validate
from .rterrors import (RemitRuntimeError, CallLimitExceeded, EvalError, ListBoundExceeded, ModelOutputInvalid,
                       NonIdempotentRetry)
from .types import BOOL, FLOAT, INT, STR, UNIT, ListT, Type, to_json_schema
from .values import EMPTY, LV, deep_prov, deep_tags, retag, unwrap, wrap


class _Return(Exception):
    def __init__(self, v):
        self.v = v


class _Break(Exception):
    pass


class _ExpectFailed(Exception):
    def __init__(self, msg, span):
        super().__init__(msg)
        self.msg = msg
        self.span = span


class Env:
    def __init__(self, parent=None):
        self.parent = parent
        self.vars = {}

    def get(self, name):
        e = self
        while e:
            if name in e.vars:
                return e.vars[name]
            e = e.parent
        raise EvalError(f"unknown variable '{name}'")

    def set(self, name, v):
        e = self
        while e:
            if name in e.vars:
                e.vars[name] = v
                return
            e = e.parent
        raise EvalError(f"unknown variable '{name}'")

    def define(self, name, v):
        self.vars[name] = v


def show(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


class Interpreter:
    def __init__(self, checked: CheckResult, broker: Broker):
        self.c = checked
        self.prog = checked.program
        self.broker = broker
        self.fns = {f.name: f for f in self.prog.fns}
        self.agents = {a.name: a for a in self.prog.agents}
        self.host = checked.host
        self.agent_calls: dict[str, int] = {}
        self.pc: list[frozenset] = []
        self.sites = assign_sites(self.prog, self._is_effect)
        self.var_types: dict[int, Type] = {}

    def _is_effect(self, call: A.Call) -> bool:
        p = call.callee
        if ".".join(p) in self.host.caps:
            return True
        return len(p) == 2 and p[1] == "ask" and (p[0] in self.host.models or p[0] in self.agents)

    def pc_tags(self) -> frozenset:
        return frozenset().union(*self.pc) if self.pc else EMPTY

    # ----- entry points -----
    def run_main(self, inputs: dict) -> LV:
        main = self.fns["main"]
        args = []
        for p in main.params:
            if p.name not in inputs:
                raise EvalError(f"missing input '{p.name}'")
            t = self.resolve(p.type)
            raw = validate(inputs[p.name], t, f"input.{p.name}") if t != UNIT else None
            tags = frozenset(self.host.inputs.get(p.name, ["untrusted"]))
            args.append(wrap(raw, tags))
        return self.call_fn(main, args)

    def resolve(self, te) -> Type:
        from .checker import Checker
        ck = Checker.__new__(Checker)
        ck.types = self.c.types
        ck.diags, ck._seen_diags = [], set()
        return Checker.resolve_type(ck, te)

    def call_fn(self, f: A.FnDecl, args: list[LV]) -> LV:
        env = Env(None)
        for p, a in zip(f.params, args):
            env.define(p.name, a)
        try:
            v = self.block(f.body, env)
        except _Return as r:
            return r.v
        return v if f.ret is not None else LV(None)

    # ----- statements -----
    def block(self, b: A.Block, parent: Env) -> LV:
        env = Env(parent)
        last = LV(None)
        for i, s in enumerate(b.stmts):
            v = self.stmt(s, env)
            last = v if (i == len(b.stmts) - 1 and isinstance(s, A.ExprStmt)) else LV(None)
        return last

    def stmt(self, s, env: Env) -> LV:
        if isinstance(s, A.Let):
            v = self.expr(s.value, env)
            if s.type is not None:
                t = self.resolve(s.type)
                self.var_types[id(s)] = t
                self.check_bound(v, t, s)
                if s.mutable:
                    env.vars[s.name + "\0type"] = t
            if s.mutable and self.pc:
                v = retag(v, add=self.pc_tags())
            env.define(s.name, v)
            return LV(None)
        if isinstance(s, A.Assign):
            v = self.expr(s.value, env)
            t = self._var_type(env, s.name)
            if t is not None:
                self.check_bound(v, t, s)
            if self.pc:
                v = retag(v, add=self.pc_tags())
            env.set(s.name, v)
            return LV(None)
        if isinstance(s, A.Return):
            v = self.expr(s.value, env) if s.value is not None else LV(None)
            if self.pc:
                v = retag(v, add=self.pc_tags())
            raise _Return(v)
        if isinstance(s, A.ExprStmt):
            return self.expr(s.expr, env)
        if isinstance(s, A.Expect):
            v = self.expr(s.cond, env)
            if v.v is not True:
                from .formatter import fmt_expr
                raise _ExpectFailed(s.message or f"expectation failed: {fmt_expr(s.cond)}", s.span)
            return LV(None)
        if isinstance(s, A.Break):
            raise _Break()
        raise AssertionError(s)

    def _var_type(self, env, name):
        e = env
        while e:
            if name in e.vars:
                return e.vars.get(name + "\0type")
            e = e.parent
        return None

    def check_bound(self, v: LV, t: Type, node):
        if isinstance(t, ListT) and t.max is not None and isinstance(v.v, list) and len(v.v) > t.max:
            raise ListBoundExceeded(f"list has {len(v.v)} items but its declared bound is {t.max}", node.span)

    # ----- expressions -----
    def expr(self, e, env: Env) -> LV:
        try:
            return self._expr(e, env)
        except RemitRuntimeError as err:
            if err.span is None:
                err.span = e.span
            raise

    def _expr(self, e, env: Env) -> LV:
        if isinstance(e, A.Lit):
            return LV(e.value)
        if isinstance(e, A.Interp):
            out, tags, prov = "", EMPTY, EMPTY
            for p in e.parts:
                if isinstance(p, str):
                    out += p
                else:
                    v = self.expr(p, env)
                    out += show(unwrap(v))
                    tags |= deep_tags(v)
                    prov |= deep_prov(v)
            return LV(out, tags, prov)
        if isinstance(e, A.Name):
            return env.get(e.name)
        if isinstance(e, A.Field):
            o = self.expr(e.obj, env)
            if not isinstance(o.v, dict) or e.name not in o.v:
                raise EvalError(f"value has no field '{e.name}'")
            f = o.v[e.name]
            return LV(f.v, f.tags | o.tags, f.prov | o.prov)
        if isinstance(e, A.Index):
            o = self.expr(e.obj, env)
            i = self.expr(e.index, env)
            if not isinstance(o.v, list):
                raise EvalError("indexing a non-list")
            if not (-len(o.v) <= i.v < len(o.v)):
                raise EvalError(f"index {i.v} is out of range for a list of length {len(o.v)}")
            x = o.v[i.v]
            return LV(x.v, x.tags | o.tags | i.tags, x.prov | o.prov | i.prov)
        if isinstance(e, A.ListLit):
            return LV([self.expr(x, env) for x in e.items])
        if isinstance(e, A.RecordLit):
            return LV({n: self.expr(x, env) for n, x in e.fields})
        if isinstance(e, A.Unary):
            o = self.expr(e.operand, env)
            if e.op == "not":
                return LV(not o.v, o.tags, o.prov)
            return LV(-o.v, o.tags, o.prov)
        if isinstance(e, A.Binary):
            return self.binary(e, env)
        if isinstance(e, A.Call):
            return self.call(e, env)
        if isinstance(e, A.IfExpr):
            c = self.expr(e.cond, env)
            self.pc.append(c.tags)
            try:
                if c.v:
                    r = self.block(e.then, env)
                elif e.els is None:
                    r = LV(None)
                elif isinstance(e.els, A.IfExpr):
                    r = self.expr(e.els, env)
                else:
                    r = self.block(e.els, env)
            finally:
                self.pc.pop()
            return LV(r.v, r.tags | c.tags, r.prov | c.prov)
        if isinstance(e, A.ForExpr):
            it = self.expr(e.iter, env)
            items = it.v if isinstance(it.v, list) else []
            if e.limit is not None:
                items = items[:e.limit]
            out = []
            self.pc.append(it.tags)
            try:
                for x in items:
                    inner = Env(env)
                    inner.define(e.var, LV(x.v, x.tags | it.tags, x.prov | it.prov))
                    if e.where is not None:
                        w = self.expr(e.where, inner)
                        if not w.v:
                            continue
                        self.pc.append(w.tags)
                        try:
                            out.append(self.block(e.body, inner))
                        finally:
                            self.pc.pop()
                    else:
                        out.append(self.block(e.body, inner))
            except _Break:
                pass
            finally:
                self.pc.pop()
            return LV(out, it.tags, it.prov)
        if isinstance(e, A.MatchExpr):
            s = self.expr(e.subject, env)
            self.pc.append(s.tags)
            try:
                for arm in e.arms:
                    if arm.pattern is None or arm.pattern == s.v:
                        r = self.block(arm.body, env)
                        return LV(r.v, r.tags | s.tags, r.prov | s.prov)
            finally:
                self.pc.pop()
            raise EvalError(f"no match arm for {s.v!r}")
        raise AssertionError(e)

    def binary(self, e: A.Binary, env) -> LV:
        if e.op in ("and", "or"):
            l = self.expr(e.left, env)
            if (e.op == "and" and not l.v) or (e.op == "or" and l.v):
                return LV(l.v, l.tags, l.prov)
            r = self.expr(e.right, env)
            return LV(r.v, l.tags | r.tags, l.prov | r.prov)
        l = self.expr(e.left, env)
        r = self.expr(e.right, env)
        tags = deep_tags(l) | deep_tags(r) if e.op in ("==", "!=", "in") else l.tags | r.tags
        prov = l.prov | r.prov
        a, b = l.v, r.v
        op = e.op
        if op == "+" and isinstance(a, list):
            return LV(a + b, tags, prov)
        if op in ("==", "!="):
            eq = unwrap(l) == unwrap(r)
            return LV(eq if op == "==" else not eq, tags, prov)
        if op == "in":
            if isinstance(b, list):
                return LV(unwrap(l) in [unwrap(x) for x in b], tags, prov)
            return LV(a in b, tags, prov)
        try:
            if op == "+":
                v = a + b
            elif op == "-":
                v = a - b
            elif op == "*":
                v = a * b
            elif op == "/":
                v = a / b
            elif op == "%":
                v = a % b
            elif op == "<":
                v = a < b
            elif op == "<=":
                v = a <= b
            elif op == ">":
                v = a > b
            elif op == ">=":
                v = a >= b
            else:
                raise AssertionError(op)
        except ZeroDivisionError:
            raise EvalError("division by zero")
        return LV(v, tags, prov)

    # ----- calls -----
    def call(self, e: A.Call, env) -> LV:
        path = e.callee
        dotted = ".".join(path)
        if self._is_effect(e):
            return self.effect_call(e, env)
        if len(path) == 1 and path[0] in self.fns:
            f = self.fns[path[0]]
            args = self.bind(e, [p.name for p in f.params], env)
            return self.call_fn(f, [args[p.name] for p in f.params])
        if len(path) == 1 and path[0] in BUILTINS:
            return self.builtin(path[0], [self.expr(a.value, env) for a in e.args], e)
        raise EvalError(f"unknown function '{dotted}'")

    def bind(self, e: A.Call, names: list[str], env) -> dict:
        out = {}
        pos = [a for a in e.args if a.name is None]
        for n, a in zip(names, pos):
            out[n] = self.expr(a.value, env)
        for a in e.args:
            if a.name is not None:
                out[a.name] = self.expr(a.value, env)
        return out

    def effect_call(self, e: A.Call, env) -> LV:
        site = self.sites[e.nid]
        dotted = ".".join(e.callee)
        attempts = 1 + (e.retry or 0)
        last_err: Optional[RemitRuntimeError] = None
        if dotted in self.host.caps:
            cap = self.host.caps[dotted]
            args = self.bind(e, [p.name for p in cap.params], env)
            idempotent = cap.idempotent

            def do():
                return self.broker.call(dotted, args, site=site, pc_tags=self.pc_tags(), timeout=e.timeout)
        else:
            idempotent = True
            target = e.callee[0]
            agent = self.agents.get(target)
            model = agent.model if agent else target
            tname = e.args[0].value.name
            out_t = {"Int": INT, "Float": FLOAT, "Bool": BOOL, "Str": STR}.get(tname) or self.c.types[tname]
            prompt = self.expr(e.args[1].value, env)
            ctx_arg = next((a for a in e.args if a.name == "context"), None)
            ctx = self.expr(ctx_arg.value, env) if ctx_arg else LV(None)
            req = {"schema": to_json_schema(out_t), "type_name": tname, "prompt": prompt.v,
                   "context": unwrap(ctx), "system": agent.system if agent else None,
                   "agent": agent.name if agent else None}

            def do():
                if agent is not None:
                    n = self.agent_calls.get(agent.name, 0)
                    if agent.max_calls is not None and n >= agent.max_calls:
                        raise CallLimitExceeded(f"agent '{agent.name}' reached max_calls={agent.max_calls}")
                    self.agent_calls[agent.name] = n + 1
                out = self.broker.call(model, {"prompt": prompt, "context": ctx}, site=site,
                                       pc_tags=self.pc_tags(), timeout=e.timeout, model_request=req)
                try:
                    raw = validate(unwrap(out), out_t)
                except ModelOutputInvalid as err:
                    self.broker.emit({"event": "invalid_output", "run_id": self.broker.run_id, "site": site,
                                      "resource": model, "message": err.message})
                    raise
                return wrap(raw, out.tags, out.prov)
        for i in range(attempts):
            try:
                return do()
            except RemitRuntimeError as err:
                if not err.operational:
                    raise
                last_err = err
                if i + 1 < attempts and not idempotent:
                    raise NonIdempotentRetry(f"refusing to retry non-idempotent '{dotted}'", e.span)
        if e.fallback is not None:
            fb = self.expr(e.fallback, env)
            self.broker.emit({"event": "fallback", "run_id": self.broker.run_id, "site": site,
                              "error": last_err.message if last_err else None})
            return fb
        raise last_err

    def builtin(self, name, args: list[LV], e) -> LV:
        tags = frozenset().union(*[a.tags for a in args]) if args else EMPTY
        prov = frozenset().union(*[a.prov for a in args]) if args else EMPTY
        v = [a.v for a in args]
        R = lambda x: LV(x, tags, prov)
        if name == "len":
            return R(len(v[0]))
        if name == "take":
            return LV(v[0][:v[1]], args[0].tags | args[1].tags, prov)
        if name == "range":
            return R([LV(i) for i in range(v[0])])
        if name == "str":
            return R(show(unwrap(args[0])))
        if name == "lower":
            return R(v[0].lower())
        if name == "upper":
            return R(v[0].upper())
        if name == "trim":
            return R(v[0].strip())
        if name == "startswith":
            return R(v[0].startswith(v[1]))
        if name == "endswith":
            return R(v[0].endswith(v[1]))
        if name == "contains":
            if isinstance(v[0], list):
                return LV(unwrap(args[1]) in [unwrap(x) for x in v[0]], tags | deep_tags(args[0]), prov)
            return R(v[1] in v[0])
        if name == "replace":
            return R(v[0].replace(v[1], v[2]))
        if name == "split":
            return R([LV(s, tags, prov) for s in v[0].split(v[1])])
        if name == "join":
            return LV(v[1].join(show(x.v) for x in v[0]), tags | deep_tags(args[0]), prov | deep_prov(args[0]))
        if name == "sum":
            return LV(sum(x.v for x in v[0]), tags | deep_tags(args[0]), prov | deep_prov(args[0]))
        if name == "min":
            return R(min(v[0], v[1]))
        if name == "max":
            return R(max(v[0], v[1]))
        if name == "abs":
            return R(abs(v[0]))
        if name == "round":
            return R(round(v[0], v[1]) if len(v) == 2 else int(round(v[0])))
        if name == "int":
            try:
                return R(int(v[0]))
            except (ValueError, TypeError):
                raise EvalError(f"cannot convert {v[0]!r} to Int")
        if name == "float":
            try:
                return R(float(v[0]))
            except (ValueError, TypeError):
                raise EvalError(f"cannot convert {v[0]!r} to Float")
        if name == "first":
            if not v[0]:
                raise EvalError("first() of an empty list")
            x = v[0][0]
            return LV(x.v, x.tags | args[0].tags, x.prov | args[0].prov)
        if name == "unique":
            seen, out = [], []
            for x in v[0]:
                u = unwrap(x)
                if u not in seen:
                    seen.append(u)
                    out.append(x)
            return LV(out, args[0].tags, args[0].prov)
        raise EvalError(f"unknown builtin '{name}'")
