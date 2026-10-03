"""Canonical formatter: AST -> source. Preserves comments attached by the parser."""
from __future__ import annotations

from . import ast as A

IND = "  "

PREC = {"or": 1, "and": 2, "==": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4, "in": 4,
        "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}


def fmt_type(t: A.TypeExpr) -> str:
    if isinstance(t, A.TUnion):
        return " | ".join(_q(o) for o in t.options)
    if isinstance(t, A.TRecord):
        return "{ " + ", ".join(f"{n}: {fmt_type(ft)}" for n, ft in t.fields) + " }" if t.fields else "{}"
    s = t.name
    if t.args:
        s += "[" + ", ".join(fmt_type(a) for a in t.args) + "]"
    if t.max is not None:
        s += f" max {t.max}"
    return s


def _q(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    out = out.replace("{", "\\{").replace("}", "\\}")
    return f'"{out}"'


def _num(v) -> str:
    if isinstance(v, float):
        r = repr(v)
        return r
    return str(v)


def _dur(sec: float) -> str:
    if sec < 1 and abs(sec * 1000 - round(sec * 1000)) < 1e-9:
        return f"{int(round(sec * 1000))}ms"
    if float(sec).is_integer():
        return f"{int(sec)}s"
    return f"{int(round(sec * 1000))}ms"


def fmt_expr(e, depth: int = 0, parent_prec: int = 0) -> str:
    if isinstance(e, A.Lit):
        v = e.value
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return _q(v)
        return _num(v)
    if isinstance(e, A.Interp):
        out = ""
        for p in e.parts:
            if isinstance(p, str):
                out += _q(p)[1:-1]
            else:
                out += "{" + fmt_expr(p, depth) + "}"
        return f'"{out}"'
    if isinstance(e, A.Name):
        return e.name
    if isinstance(e, A.Field):
        return f"{fmt_expr(e.obj, depth, 9)}.{e.name}"
    if isinstance(e, A.Index):
        return f"{fmt_expr(e.obj, depth, 9)}[{fmt_expr(e.index, depth)}]"
    if isinstance(e, A.ListLit):
        return "[" + ", ".join(fmt_expr(x, depth) for x in e.items) + "]"
    if isinstance(e, A.RecordLit):
        if not e.fields:
            return "{}"
        inner = ", ".join(f"{n}: {fmt_expr(x, depth)}" for n, x in e.fields)
        one = "{ " + inner + " }"
        if len(one) <= 80 and "\n" not in one:
            return one
        pad = IND * (depth + 1)
        return "{\n" + ",\n".join(f"{pad}{n}: {fmt_expr(x, depth + 1)}" for n, x in e.fields) + "\n" + IND * depth + "}"
    if isinstance(e, A.Unary):
        if e.op == "not":
            s = f"not {fmt_expr(e.operand, depth, 3)}"
            return f"({s})" if parent_prec > 3 else s
        return f"-{fmt_expr(e.operand, depth, 8)}"
    if isinstance(e, A.Binary):
        p = PREC[e.op]
        right_prec = p + 1
        s = f"{fmt_expr(e.left, depth, p)} {e.op} {fmt_expr(e.right, depth, right_prec)}"
        return f"({s})" if p < parent_prec else s
    if isinstance(e, A.Call):
        s = ".".join(e.callee) + "(" + ", ".join(
            (f"{a.name}: " if a.name else "") + fmt_expr(a.value, depth) for a in e.args) + ")"
        if e.retry is not None:
            s += f" retry {e.retry}"
        if e.timeout is not None:
            s += f" timeout {_dur(e.timeout)}"
        if e.fallback is not None:
            s += f" else {fmt_expr(e.fallback, depth, 8)}"
        return f"({s})" if parent_prec >= 9 and (e.retry or e.timeout or e.fallback) else s
    if isinstance(e, A.IfExpr):
        s = f"if {fmt_expr(e.cond, depth)} {fmt_block(e.then, depth)}"
        if e.els is not None:
            if isinstance(e.els, A.IfExpr):
                s += " else " + fmt_expr(e.els, depth)
            else:
                s += " else " + fmt_block(e.els, depth)
        return f"({s})" if parent_prec > 0 else s
    if isinstance(e, A.ForExpr):
        s = f"for {e.var} in {fmt_expr(e.iter, depth)}"
        if e.limit is not None:
            s += f" limit {e.limit}"
        if e.where is not None:
            s += f" if {fmt_expr(e.where, depth)}"
        s += " " + fmt_block(e.body, depth)
        return f"({s})" if parent_prec > 0 else s
    if isinstance(e, A.MatchExpr):
        pad = IND * (depth + 1)
        lines = [f"match {fmt_expr(e.subject, depth)} {{"]
        for arm in e.arms:
            pat = "_" if arm.pattern is None else _q(arm.pattern)
            b = arm.body
            if len(b.stmts) == 1 and isinstance(b.stmts[0], A.ExprStmt) and not b.comments \
                    and not b.stmts[0].comments and not b.stmts[0].trailing:
                lines.append(f"{pad}{pat} => {fmt_expr(b.stmts[0].expr, depth + 1)}")
            else:
                lines.append(f"{pad}{pat} => {fmt_block(b, depth + 1)}")
        lines.append(IND * depth + "}")
        s = "\n".join(lines)
        return f"({s})" if parent_prec > 0 else s
    raise TypeError(type(e).__name__)


def fmt_block(b: A.Block, depth: int) -> str:
    if not b.stmts and not b.comments:
        return "{}"
    pad = IND * (depth + 1)
    lines = ["{"]
    for s in b.stmts:
        for c in s.comments:
            lines.append(pad + c)
        line = pad + fmt_stmt(s, depth + 1)
        if s.trailing:
            line += "  " + s.trailing
        lines.append(line)
    for c in b.comments:
        lines.append(pad + c)
    lines.append(IND * depth + "}")
    return "\n".join(lines)


def fmt_stmt(s, depth: int) -> str:
    if isinstance(s, A.Let):
        kw = "var" if s.mutable else "let"
        t = f": {fmt_type(s.type)}" if s.type is not None else ""
        return f"{kw} {s.name}{t} = {fmt_expr(s.value, depth)}"
    if isinstance(s, A.Assign):
        return f"{s.name} = {fmt_expr(s.value, depth)}"
    if isinstance(s, A.Return):
        return "return" if s.value is None else f"return {fmt_expr(s.value, depth)}"
    if isinstance(s, A.ExprStmt):
        return fmt_expr(s.expr, depth)
    if isinstance(s, A.Expect):
        m = f", {_q(s.message)}" if s.message else ""
        return f"expect {fmt_expr(s.cond, depth)}{m}"
    if isinstance(s, A.Break):
        return "break"
    raise TypeError(type(s).__name__)


def _params(ps: list[A.Param]) -> str:
    out = []
    for p in ps:
        s = f"{p.name}: {fmt_type(p.type)}"
        if p.deny:
            s += " deny " + (p.deny[0] if len(p.deny) == 1 else "(" + ", ".join(p.deny) + ")")
        if p.approve:
            s += " approve " + (p.approve[0] if len(p.approve) == 1 else "(" + ", ".join(p.approve) + ")")
        out.append(s)
    return ", ".join(out)


def _decl_lines(d) -> list[str]:
    if isinstance(d, A.TypeDecl):
        return [f"type {d.name} = {fmt_type(d.type)}"]
    if isinstance(d, A.FnDecl):
        ret = f" -> {fmt_type(d.ret)}" if d.ret is not None else ""
        return [f"fn {d.name}({_params(d.params)}){ret} {fmt_block(d.body, 0)}"]
    if isinstance(d, A.AgentDecl):
        lines = [f"agent {d.name} {{", f"{IND}model: {d.model}"]
        if d.system:
            lines.append(f"{IND}system: {_q(d.system)}")
        if d.max_calls is not None:
            lines.append(f"{IND}max_calls: {d.max_calls}")
        lines.append("}")
        return ["\n".join(lines)]
    if isinstance(d, A.TestDecl):
        fx = f" fixtures {_q(d.fixtures)}" if d.fixtures else ""
        return [f"test {_q(d.name)}{fx} {fmt_block(d.body, 0)}"]
    if isinstance(d, A.CapabilityDecl):
        ret = f" -> {fmt_type(d.ret)}" if d.ret is not None else ""
        lines = [f"capability {d.name}({_params(d.params)}){ret}"]
        if d.tags:
            lines.append(f"{IND}tags {', '.join(d.tags)}")
        if d.clears:
            lines.append(f"{IND}clears {', '.join(d.clears)}")
        if d.cost:
            lines.append(f"{IND}cost {_num(d.cost)}")
        if d.idempotent:
            lines.append(f"{IND}idempotent")
        if d.approval_always:
            lines.append(f"{IND}approval always")
        return ["\n".join(lines)]
    if isinstance(d, A.ModelDecl):
        return [f"model {d.name}" + (f" cost {_num(d.cost)}" if d.cost else "")]
    if isinstance(d, A.InputDecl):
        return [f"input {d.name} tags {', '.join(d.tags)}"]
    raise TypeError(type(d).__name__)


def _emit_decl(d, out: list[str]):
    for c in d.comments:
        out.append(c)
    text = "\n".join(_decl_lines(d))
    if d.trailing:
        first, *rest = text.split("\n")
        text = "\n".join([first + "  " + d.trailing] + rest) if not rest else text + "  " + d.trailing
    out.append(text)


def format_program(p: A.Program) -> str:
    out: list[str] = list(p.header_comments)
    out.append(f"program {p.name}")
    if p.uses:
        out.append("uses " + ", ".join(p.uses))
    if p.budget is not None:
        b = p.budget
        out.append(f"budget usd {_num(b) if not float(b).is_integer() else int(b)}")
    prev = None
    for d in p.order:
        if prev is None or not (isinstance(d, A.TypeDecl) and isinstance(prev, A.TypeDecl) and not d.comments):
            out.append("")
        _emit_decl(d, out)
        prev = d
    if p.comments:
        out.append("")
        out.extend(p.comments)
    return "\n".join(out) + "\n"


def format_host(h: A.HostInterface) -> str:
    out: list[str] = []
    decls = sorted(h.types + h.capabilities + h.models + h.inputs, key=lambda d: (d.span.line, d.span.col))
    prev = None
    for d in decls:
        if prev is not None and not (type(d) is type(prev) and isinstance(d, (A.TypeDecl, A.ModelDecl, A.InputDecl))
                                     and not d.comments):
            out.append("")
        _emit_decl(d, out)
        prev = d
    return "\n".join(out) + "\n"
