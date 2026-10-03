"""Recursive-descent parser for Remit programs (.rmt) and host interfaces (.rmti)."""
from __future__ import annotations

from typing import Optional

from . import ast as A
from .errors import RemitError, Diagnostic, Span
from .lexer import Token, lex


class Parser:
    def __init__(self, src: str, file: str):
        self.file = file
        self.src = src
        all_toks = lex(src, file)
        self.toks: list[Token] = []
        self.own_comments: list[Token] = []   # comments alone on their line
        self.trail_comments: dict[int, str] = {}  # line -> trailing comment text
        last_code_line = 0
        for t in all_toks:
            if t.kind == "COMMENT":
                if t.span.line == last_code_line:
                    self.trail_comments[t.span.line] = t.value
                else:
                    self.own_comments.append(t)
                continue
            if t.kind != "NEWLINE":
                last_code_line = t.span.line
            self.toks.append(t)
        self.i = 0
        self.next_id = 1
        self.last: Token = self.toks[0]

    # ----- token helpers -----
    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def advance(self) -> Token:
        t = self.toks[self.i]
        self.i = min(self.i + 1, len(self.toks) - 1)
        self.last = t
        return t

    def at(self, kind: str, value=None) -> bool:
        t = self.peek()
        return t.kind == kind and (value is None or t.value == value)

    def at_op(self, v: str) -> bool:
        return self.at("OP", v)

    def at_kw(self, v: str) -> bool:
        return self.at("KW", v)

    def error(self, msg: str, tok: Optional[Token] = None, hint: Optional[str] = None, code: str = "E0100"):
        tok = tok or self.peek()
        raise RemitError(Diagnostic(code, msg, tok.span, hint=hint))

    def describe(self, t: Token) -> str:
        if t.kind == "NEWLINE":
            return "end of line"
        if t.kind == "EOF":
            return "end of file"
        if t.kind == "STR":
            return "string literal"
        return f"'{t.text or t.value}'"

    def expect_op(self, v: str, hint: Optional[str] = None) -> Token:
        if not self.at_op(v):
            self.error(f"expected '{v}', found {self.describe(self.peek())}", hint=hint)
        return self.advance()

    def expect_kw(self, v: str) -> Token:
        if not self.at_kw(v):
            self.error(f"expected '{v}', found {self.describe(self.peek())}")
        return self.advance()

    def expect_ident(self, what: str = "identifier") -> Token:
        if not self.at("IDENT"):
            t = self.peek()
            hint = f"'{t.value}' is a reserved word" if t.kind == "KW" else None
            self.error(f"expected {what}, found {self.describe(t)}", hint=hint)
        return self.advance()

    def skip_newlines(self):
        while self.at("NEWLINE") or self.at_op(";"):
            self.advance()

    def end_stmt(self):
        if self.at("NEWLINE") or self.at_op(";"):
            self.skip_newlines()
            return
        if self.at_op("}") or self.at("EOF"):
            return
        self.error(f"expected end of statement, found {self.describe(self.peek())}",
                   hint="put each statement on its own line")

    def node(self, cls, start: Token, **kw):
        n = cls(**kw, span=Span(self.file, start.span.line, start.span.col, self.last.span.end_line, self.last.span.end_col))
        n.nid = self.next_id
        self.next_id += 1
        return n

    def take_comments(self, before_line: int) -> list[str]:
        out = []
        while self.own_comments and self.own_comments[0].span.line < before_line:
            out.append(self.own_comments.pop(0).value)
        return out

    def attach(self, n: A.Node, comments: list[str]):
        n.comments = comments
        if not (self.at("NEWLINE") or self.at("EOF") or self.at_op(";")):
            return n  # something else follows on this line; the enclosing statement owns the comment
        tr = self.trail_comments.pop(n.span.end_line, None)
        if tr:
            n.trailing = tr
        return n

    # ----- dotted names -----
    def dotted(self) -> tuple[str, Token]:
        t = self.expect_ident("name")
        parts = [t.value]
        while self.at_op(".") and self.peek(1).kind == "IDENT":
            self.advance()
            parts.append(self.advance().value)
        return ".".join(parts), t

    def number(self) -> float:
        neg = False
        if self.at_op("-"):
            self.advance()
            neg = True
        t = self.peek()
        if t.kind not in ("INT", "FLOAT"):
            self.error(f"expected a number, found {self.describe(t)}")
        self.advance()
        return -t.value if neg else t.value

    def int_lit(self, what: str) -> int:
        t = self.peek()
        if t.kind != "INT":
            self.error(f"expected an integer literal for {what}, found {self.describe(t)}",
                       hint=f"{what} must be a constant so that bounds can be computed before running")
        self.advance()
        return t.value

    def str_lit(self, what: str) -> str:
        t = self.peek()
        if t.kind != "STR" or any(not isinstance(p, str) for p in t.value):
            self.error(f"expected a plain string literal for {what}, found {self.describe(t)}")
        self.advance()
        return "".join(t.value)

    # ----- types -----
    def type_expr(self) -> A.TypeExpr:
        start = self.peek()
        if start.kind == "STR":
            opts = [self.str_lit("union option")]
            while self.at_op("|"):
                self.advance()
                opts.append(self.str_lit("union option"))
            return self.node(A.TUnion, start, options=opts)
        if self.at_op("{"):
            self.advance()
            fields = []
            seen = set()
            self.skip_newlines()
            while not self.at_op("}"):
                ft = self.expect_ident("field name")
                if ft.value in seen:
                    self.error(f"duplicate field '{ft.value}'", ft)
                seen.add(ft.value)
                self.expect_op(":")
                fields.append((ft.value, self.type_expr()))
                self.skip_newlines()
                if self.at_op(","):
                    self.advance()
                    self.skip_newlines()
                elif not self.at_op("}"):
                    self.error(f"expected ',' or '}}' in record type, found {self.describe(self.peek())}")
            self.expect_op("}")
            return self.node(A.TRecord, start, fields=fields)
        t = self.expect_ident("type name")
        args = []
        if self.at_op("["):
            self.advance()
            args.append(self.type_expr())
            while self.at_op(","):
                self.advance()
                args.append(self.type_expr())
            self.expect_op("]")
        mx = None
        if self.at_kw("max"):
            self.advance()
            mx = self.int_lit("a list size bound")
        return self.node(A.TName, start, name=t.value, args=args, max=mx)

    # ----- program -----
    def parse_program(self) -> A.Program:
        self.skip_newlines()
        start = self.peek()
        header_comments = self.take_comments(start.span.line)
        if not self.at_kw("program"):
            self.error("an Remit program must start with 'program <name>'",
                       hint="add a first line such as: program my_workflow")
        self.advance()
        name = self.expect_ident("program name").value
        self.end_stmt()
        uses: list[str] = []
        budget = None
        while self.at_kw("uses") or self.at_kw("budget"):
            if self.at_kw("uses"):
                self.advance()
                d, _ = self.dotted()
                uses.append(d)
                while self.at_op(","):
                    self.advance()
                    d, _ = self.dotted()
                    uses.append(d)
            else:
                bt = self.advance()
                if budget is not None:
                    self.error("duplicate 'budget' line", bt)
                self.expect_kw("usd")
                budget = float(self.number())
            self.end_stmt()
        types, fns, agents, tests, order = [], [], [], [], []
        while not self.at("EOF"):
            t = self.peek()
            pre = self.take_comments(t.span.line)
            if self.at_kw("type"):
                d = self.type_decl()
                types.append(d)
            elif self.at_kw("fn"):
                d = self.fn_decl()
                fns.append(d)
            elif self.at_kw("agent"):
                d = self.agent_decl()
                agents.append(d)
            elif self.at_kw("test"):
                d = self.test_decl()
                tests.append(d)
            elif self.at_kw("uses") or self.at_kw("budget"):
                self.error(f"'{t.value}' must appear in the program header, before any declaration")
            else:
                self.error(f"expected a declaration (type, fn, agent, test), found {self.describe(t)}")
            self.attach(d, pre)
            order.append(d)
            self.skip_newlines()
        prog = self.node(A.Program, start, name=name, uses=uses, budget=budget, types=types, fns=fns,
                         agents=agents, tests=tests, file=self.file)
        prog.header_comments = header_comments
        prog.order = order
        prog.comments = self.take_comments(10 ** 9)
        return prog

    def type_decl(self) -> A.TypeDecl:
        start = self.advance()
        name = self.expect_ident("type name").value
        self.expect_op("=")
        te = self.type_expr()
        d = self.node(A.TypeDecl, start, name=name, type=te)
        self.end_stmt()
        return d

    def params(self, capability: bool = False) -> list[A.Param]:
        self.expect_op("(")
        ps = []
        seen = set()
        while not self.at_op(")"):
            pt = self.expect_ident("parameter name")
            if pt.value in seen:
                self.error(f"duplicate parameter '{pt.value}'", pt)
            seen.add(pt.value)
            self.expect_op(":", hint="parameters need a type, e.g. (name: Str)")
            te = self.type_expr()
            deny, approve = [], []
            while capability and (self.at_kw("deny") or self.at_kw("approve")):
                which = self.advance().value
                tags = []
                if self.at_op("("):
                    self.advance()
                    tags.append(self.expect_ident("tag").value)
                    while self.at_op(","):
                        self.advance()
                        tags.append(self.expect_ident("tag").value)
                    self.expect_op(")")
                else:
                    tags.append(self.expect_ident("tag").value)
                (deny if which == "deny" else approve).extend(tags)
            ps.append(self.node(A.Param, pt, name=pt.value, type=te, deny=deny, approve=approve))
            if self.at_op(","):
                self.advance()
            elif not self.at_op(")"):
                self.error(f"expected ',' or ')' in parameter list, found {self.describe(self.peek())}")
        self.expect_op(")")
        return ps

    def fn_decl(self) -> A.FnDecl:
        start = self.advance()
        name = self.expect_ident("function name").value
        ps = self.params()
        ret = None
        if self.at_op("->"):
            self.advance()
            ret = self.type_expr()
        body = self.block()
        return self.node(A.FnDecl, start, name=name, params=ps, ret=ret, body=body)

    def agent_decl(self) -> A.AgentDecl:
        start = self.advance()
        name = self.expect_ident("agent name").value
        self.expect_op("{")
        self.skip_newlines()
        model, system, max_calls = None, None, None
        while not self.at_op("}"):
            kt = self.peek()
            if kt.kind not in ("IDENT", "KW"):
                self.error(f"expected an agent setting (model, system, max_calls), found {self.describe(kt)}")
            key = self.advance().value
            self.expect_op(":")
            if key == "model":
                model = self.expect_ident("model name").value
            elif key == "system":
                system = self.str_lit("system")
            elif key == "max_calls":
                max_calls = self.int_lit("max_calls")
            else:
                self.error(f"unknown agent setting '{key}'", kt, hint="valid settings: model, system, max_calls")
            if self.at_op(","):
                self.advance()
                self.skip_newlines()
            else:
                self.end_stmt()
        self.expect_op("}")
        if model is None:
            self.error(f"agent '{name}' needs a 'model:' setting", start)
        return self.node(A.AgentDecl, start, name=name, model=model, system=system or "", max_calls=max_calls)

    def test_decl(self) -> A.TestDecl:
        start = self.advance()
        name = self.str_lit("test name")
        fixtures = None
        if self.at("IDENT", "fixtures"):
            self.advance()
            fixtures = self.str_lit("fixtures path")
        body = self.block()
        return self.node(A.TestDecl, start, name=name, fixtures=fixtures, body=body)

    # ----- statements -----
    def block(self) -> A.Block:
        start = self.expect_op("{", hint="blocks are written with braces")
        stmts = []
        self.skip_newlines()
        while not self.at_op("}"):
            if self.at("EOF"):
                self.error("unclosed block: expected '}'", start)
            t = self.peek()
            pre = self.take_comments(t.span.line)
            s = self.statement()
            self.attach(s, pre)
            stmts.append(s)
            self.end_stmt()
        end = self.peek()
        b_comments = self.take_comments(end.span.line)
        self.advance()
        b = self.node(A.Block, start, stmts=stmts)
        b.comments = b_comments
        return b

    def statement(self) -> A.Stmt:
        start = self.peek()
        if self.at_kw("let") or self.at_kw("var"):
            mutable = self.advance().value == "var"
            name = self.expect_ident("variable name").value
            te = None
            if self.at_op(":"):
                self.advance()
                te = self.type_expr()
            self.expect_op("=", hint="variables must be initialised: let x = ...")
            v = self.expr()
            return self.node(A.Let, start, name=name, mutable=mutable, type=te, value=v)
        if self.at_kw("return"):
            self.advance()
            v = None
            if not (self.at("NEWLINE") or self.at_op("}") or self.at_op(";")):
                v = self.expr()
            return self.node(A.Return, start, value=v)
        if self.at_kw("expect"):
            self.advance()
            c = self.expr()
            msg = None
            if self.at_op(","):
                self.advance()
                msg = self.str_lit("expect message")
            return self.node(A.Expect, start, cond=c, message=msg)
        if self.at_kw("break"):
            self.advance()
            return self.node(A.Break, start)
        if start.kind == "IDENT" and self.peek(1).kind == "OP" and self.peek(1).value == "=":
            self.advance()
            self.advance()
            v = self.expr()
            return self.node(A.Assign, start, name=start.value, value=v)
        e = self.expr()
        return self.node(A.ExprStmt, start, expr=e)

    # ----- expressions -----
    def expr(self) -> A.Expr:
        return self.or_expr()

    def or_expr(self):
        left = self.and_expr()
        while self.at_kw("or"):
            start = self.advance()
            right = self.and_expr()
            left = self._bin("or", left, right)
        return left

    def _bin(self, op, left, right):
        n = A.Binary(op=op, left=left, right=right,
                     span=Span(self.file, left.span.line, left.span.col, self.last.span.end_line, self.last.span.end_col))
        n.nid = self.next_id
        self.next_id += 1
        return n

    def and_expr(self):
        left = self.not_expr()
        while self.at_kw("and"):
            self.advance()
            right = self.not_expr()
            left = self._bin("and", left, right)
        return left

    def not_expr(self):
        if self.at_kw("not"):
            start = self.advance()
            operand = self.not_expr()
            return self.node(A.Unary, start, op="not", operand=operand)
        return self.cmp_expr()

    def cmp_expr(self):
        left = self.coalesce_expr()
        if self.peek().kind == "OP" and self.peek().value in ("==", "!=", "<", "<=", ">", ">="):
            op = self.advance().value
            right = self.coalesce_expr()
            left = self._bin(op, left, right)
            if self.peek().kind == "OP" and self.peek().value in ("==", "!=", "<", "<=", ">", ">="):
                self.error("comparisons cannot be chained", hint="use 'and': a < b and b < c")
        elif self.at_kw("in"):
            self.advance()
            right = self.coalesce_expr()
            left = self._bin("in", left, right)
        return left

    def coalesce_expr(self):
        return self.add_expr()

    def add_expr(self):
        left = self.mul_expr()
        while self.peek().kind == "OP" and self.peek().value in ("+", "-"):
            op = self.advance().value
            right = self.mul_expr()
            left = self._bin(op, left, right)
        return left

    def mul_expr(self):
        left = self.unary_expr()
        while self.peek().kind == "OP" and self.peek().value in ("*", "/", "%"):
            op = self.advance().value
            right = self.unary_expr()
            left = self._bin(op, left, right)
        return left

    def unary_expr(self):
        if self.at_op("-"):
            start = self.advance()
            operand = self.unary_expr()
            return self.node(A.Unary, start, op="-", operand=operand)
        return self.postfix_expr()

    def _path(self, e) -> Optional[list[str]]:
        if isinstance(e, A.Name):
            return [e.name]
        if isinstance(e, A.Field):
            p = self._path(e.obj)
            return p + [e.name] if p is not None else None
        return None

    def postfix_expr(self):
        start = self.peek()
        e = self.primary()
        while True:
            if self.at_op("."):
                self.advance()
                nt = self.expect_ident("field name")
                e = self.node(A.Field, start, obj=e, name=nt.value)
            elif self.at_op("["):
                self.advance()
                idx = self.expr()
                self.expect_op("]")
                e = self.node(A.Index, start, obj=e, index=idx)
            elif self.at_op("("):
                path = self._path(e)
                if path is None:
                    self.error("only named functions and capabilities can be called")
                args = self.call_args()
                e = self.node(A.Call, start, callee=path, args=args)
                self.call_modifiers(e, start)
            else:
                return e

    def call_args(self) -> list[A.Arg]:
        self.expect_op("(")
        args = []
        named = False
        while not self.at_op(")"):
            at = self.peek()
            if at.kind == "IDENT" and self.peek(1).kind == "OP" and self.peek(1).value == ":":
                self.advance()
                self.advance()
                v = self.expr()
                args.append(self.node(A.Arg, at, name=at.value, value=v))
                named = True
            else:
                if named:
                    self.error("positional arguments must come before named arguments")
                v = self.expr()
                args.append(self.node(A.Arg, at, name=None, value=v))
            if self.at_op(","):
                self.advance()
            elif not self.at_op(")"):
                self.error(f"expected ',' or ')' in argument list, found {self.describe(self.peek())}")
        self.expect_op(")")
        return args

    def call_modifiers(self, call: A.Call, start: Token):
        while True:
            if self.at_kw("retry"):
                if call.retry is not None:
                    self.error("duplicate 'retry'")
                self.advance()
                call.retry = self.int_lit("retry count")
            elif self.at_kw("timeout"):
                if call.timeout is not None:
                    self.error("duplicate 'timeout'")
                self.advance()
                t = self.peek()
                if t.kind != "DURATION":
                    self.error("expected a duration such as 10s or 500ms after 'timeout'")
                self.advance()
                call.timeout = t.value
            elif self.at_kw("else"):
                self.advance()
                call.fallback = self.unary_expr()
            else:
                break
        call.span = Span(self.file, start.span.line, start.span.col, self.last.span.end_line, self.last.span.end_col)

    def primary(self):
        t = self.peek()
        if t.kind == "INT" or t.kind == "FLOAT":
            self.advance()
            return self.node(A.Lit, t, value=t.value)
        if t.kind == "DURATION":
            self.error("durations are only allowed after 'timeout'")
        if t.kind == "STR":
            self.advance()
            parts = []
            for p in t.value:
                if isinstance(p, str):
                    parts.append(p)
                else:
                    _, text, ln, col = p
                    sub = Parser(text, self.file)
                    sub.next_id = self.next_id + 100000
                    try:
                        e = sub.expr()
                        if not sub.at("NEWLINE") and not sub.at("EOF"):
                            sub.error("unexpected text in string interpolation")
                    except RemitError as ex:
                        d = ex.diag
                        if d.span:
                            d.span = Span(self.file, ln, col + d.span.col - 1, ln, col + d.span.end_col - 1)
                        raise
                    _shift(e, ln - 1, col - 1)
                    self.next_id = max(self.next_id, sub.next_id) + 1
                    parts.append(e)
            if all(isinstance(p, str) for p in parts):
                return self.node(A.Lit, t, value="".join(parts))
            return self.node(A.Interp, t, parts=parts)
        if t.kind == "KW":
            if t.value in ("true", "false"):
                self.advance()
                return self.node(A.Lit, t, value=(t.value == "true"))
            if t.value == "if":
                return self.if_expr()
            if t.value == "for":
                return self.for_expr()
            if t.value == "match":
                return self.match_expr()
            if t.value == "max":  # allow builtin max() as a function name
                self.advance()
                return self.node(A.Name, t, name="max")
            self.error(f"unexpected keyword '{t.value}' in expression")
        if t.kind == "IDENT":
            self.advance()
            return self.node(A.Name, t, name=t.value)
        if self.at_op("("):
            self.advance()
            e = self.expr()
            self.expect_op(")")
            return e
        if self.at_op("["):
            self.advance()
            items = []
            while not self.at_op("]"):
                items.append(self.expr())
                if self.at_op(","):
                    self.advance()
                elif not self.at_op("]"):
                    self.error(f"expected ',' or ']' in list, found {self.describe(self.peek())}")
            self.expect_op("]")
            return self.node(A.ListLit, t, items=items)
        if self.at_op("{"):
            self.advance()
            fields = []
            seen = set()
            self.skip_newlines()
            while not self.at_op("}"):
                ft = self.expect_ident("field name")
                if ft.value in seen:
                    self.error(f"duplicate field '{ft.value}'", ft)
                seen.add(ft.value)
                self.expect_op(":", hint="record fields are written name: value")
                fields.append((ft.value, self.expr()))
                self.skip_newlines()
                if self.at_op(","):
                    self.advance()
                    self.skip_newlines()
                elif not self.at_op("}"):
                    self.error(f"expected ',' or '}}' in record, found {self.describe(self.peek())}")
            self.expect_op("}")
            return self.node(A.RecordLit, t, fields=fields)
        self.error(f"expected an expression, found {self.describe(t)}")

    def if_expr(self):
        start = self.advance()
        cond = self.expr()
        then = self.block()
        els = None
        if self.at_kw("else"):
            self.advance()
            if self.at_kw("if"):
                els = self.if_expr()
            else:
                els = self.block()
        return self.node(A.IfExpr, start, cond=cond, then=then, els=els)

    def for_expr(self):
        start = self.advance()
        var = self.expect_ident("loop variable").value
        self.expect_kw("in")
        it = self.expr()
        limit = None
        where = None
        if self.at_kw("limit"):
            self.advance()
            limit = self.int_lit("loop limit")
        if self.at_kw("if"):
            self.advance()
            where = self.expr()
        body = self.block()
        return self.node(A.ForExpr, start, var=var, iter=it, body=body, limit=limit, where=where)

    def match_expr(self):
        start = self.advance()
        subj = self.expr()
        self.expect_op("{")
        self.skip_newlines()
        arms = []
        while not self.at_op("}"):
            at = self.peek()
            if at.kind == "STR":
                pat = self.str_lit("match pattern")
            elif at.kind == "IDENT" and at.value == "_":
                self.advance()
                pat = None
            else:
                self.error("match patterns must be string literals or '_'")
            self.expect_op("=>")
            if self.at_op("{"):
                body = self.block()
            else:
                bt = self.peek()
                e = self.expr()
                st = self.node(A.ExprStmt, bt, expr=e)
                body = self.node(A.Block, bt, stmts=[st])
            arms.append(self.node(A.MatchArm, at, pattern=pat, body=body))
            if self.at_op(","):
                self.advance()
            self.skip_newlines()
        self.expect_op("}")
        return self.node(A.MatchExpr, start, subject=subj, arms=arms)

    # ----- host interface -----
    def parse_host(self) -> A.HostInterface:
        self.skip_newlines()
        start = self.peek()
        types, caps, models, inputs = [], [], [], []
        while not self.at("EOF"):
            t = self.peek()
            pre = self.take_comments(t.span.line)
            if self.at_kw("type"):
                d = self.type_decl()
                types.append(d)
            elif self.at_kw("capability"):
                d = self.capability_decl()
                caps.append(d)
            elif self.at_kw("model"):
                self.advance()
                name = self.expect_ident("model name").value
                cost = 0.0
                if self.at_kw("cost"):
                    self.advance()
                    cost = float(self.number())
                d = self.node(A.ModelDecl, t, name=name, cost=cost)
                models.append(d)
                self.end_stmt()
            elif self.at_kw("input"):
                self.advance()
                name = self.expect_ident("input name").value
                self.expect_kw("tags")
                tags = [self.expect_ident("tag").value]
                while self.at_op(","):
                    self.advance()
                    tags.append(self.expect_ident("tag").value)
                d = self.node(A.InputDecl, t, name=name, tags=tags)
                inputs.append(d)
                self.end_stmt()
            else:
                self.error(f"expected a host declaration (type, capability, model, input), found {self.describe(t)}")
            self.attach(d, pre)
            self.skip_newlines()
        return self.node(A.HostInterface, start, types=types, capabilities=caps, models=models, inputs=inputs,
                         file=self.file)

    def capability_decl(self) -> A.CapabilityDecl:
        start = self.advance()
        name, nt = self.dotted()
        if "." not in name:
            self.error("capability names must be namespaced, e.g. email.send", nt)
        ps = self.params(capability=True)
        ret = None
        if self.at_op("->"):
            self.advance()
            ret = self.type_expr()
        d = self.node(A.CapabilityDecl, start, name=name, params=ps, ret=ret)
        self.end_stmt()
        while self.at_kw("tags") or self.at_kw("clears") or self.at_kw("cost") or self.at_kw("idempotent") \
                or self.at_kw("approval"):
            kw = self.advance().value
            if kw in ("tags", "clears"):
                lst = [self.expect_ident("tag").value]
                while self.at_op(","):
                    self.advance()
                    lst.append(self.expect_ident("tag").value)
                (d.tags if kw == "tags" else d.clears).extend(lst)
            elif kw == "cost":
                d.cost = float(self.number())
            elif kw == "idempotent":
                d.idempotent = True
            elif kw == "approval":
                self.expect_kw("always")
                d.approval_always = True
            self.end_stmt()
        return d


def _shift(e, dl: int, dc: int):
    """Shift spans of an interpolation sub-expression parsed from a substring."""
    from dataclasses import fields as dfields
    if isinstance(e, A.Node):
        s = e.span
        e.span = Span(s.file, s.line + dl, s.col + dc if s.line == 1 else s.col, s.end_line + dl,
                      s.end_col + dc if s.end_line == 1 else s.end_col)
        for f in dfields(e):
            if f.name in ("span", "nid", "comments", "trailing"):
                continue
            _shift(getattr(e, f.name), dl, dc)
    elif isinstance(e, list):
        for x in e:
            _shift(x, dl, dc)
    elif isinstance(e, tuple):
        for x in e:
            _shift(x, dl, dc)


def parse_program(src: str, file: str = "<input>") -> A.Program:
    return Parser(src, file).parse_program()


def parse_host(src: str, file: str = "<host>") -> A.HostInterface:
    return Parser(src, file).parse_host()
