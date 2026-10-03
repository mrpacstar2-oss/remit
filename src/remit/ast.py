"""AST node definitions. Every node carries a Span and a stable id (assigned by the parser)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from .errors import Span


@dataclass
class Node:
    span: Span = field(repr=False, compare=False, kw_only=True)
    nid: int = field(default=0, repr=False, compare=False, kw_only=True)
    comments: list[str] = field(default_factory=list, repr=False, compare=False, kw_only=True)
    trailing: Optional[str] = field(default=None, repr=False, compare=False, kw_only=True)


# ---------- type expressions ----------
@dataclass
class TName(Node):
    name: str
    args: list["TypeExpr"] = field(default_factory=list)
    max: Optional[int] = None  # only for List


@dataclass
class TRecord(Node):
    fields: list[tuple[str, "TypeExpr"]]


@dataclass
class TUnion(Node):  # union of string literals: "a" | "b"
    options: list[str]


TypeExpr = Union[TName, TRecord, TUnion]


# ---------- expressions ----------
@dataclass
class Lit(Node):
    value: object  # int | float | bool | str


@dataclass
class Interp(Node):  # interpolated string
    parts: list[Union[str, "Expr"]]


@dataclass
class Name(Node):
    name: str


@dataclass
class Field(Node):
    obj: "Expr"
    name: str


@dataclass
class Index(Node):
    obj: "Expr"
    index: "Expr"


@dataclass
class ListLit(Node):
    items: list["Expr"]


@dataclass
class RecordLit(Node):
    fields: list[tuple[str, "Expr"]]


@dataclass
class Unary(Node):
    op: str
    operand: "Expr"


@dataclass
class Binary(Node):
    op: str
    left: "Expr"
    right: "Expr"


@dataclass
class Arg(Node):
    name: Optional[str]
    value: "Expr"


@dataclass
class Call(Node):
    """f(args) or ns.cap(args) or Agent.ask(T, ...). `callee` is a dotted path."""
    callee: list[str]
    args: list[Arg]
    retry: Optional[int] = None
    timeout: Optional[float] = None
    fallback: Optional["Expr"] = None


@dataclass
class IfExpr(Node):
    cond: "Expr"
    then: "Block"
    els: Optional[Union["Block", "IfExpr"]] = None


@dataclass
class ForExpr(Node):
    var: str
    iter: "Expr"
    body: "Block"
    limit: Optional[int] = None
    where: Optional["Expr"] = None


@dataclass
class MatchArm(Node):
    pattern: Union[str, None]  # string literal or None for `_`
    body: "Block"


@dataclass
class MatchExpr(Node):
    subject: "Expr"
    arms: list[MatchArm]


Expr = Union[Lit, Interp, Name, Field, Index, ListLit, RecordLit, Unary, Binary, Call, IfExpr, ForExpr, MatchExpr]


# ---------- statements ----------
@dataclass
class Let(Node):
    name: str
    mutable: bool
    type: Optional[TypeExpr]
    value: Expr


@dataclass
class Assign(Node):
    name: str
    value: Expr


@dataclass
class Return(Node):
    value: Optional[Expr]


@dataclass
class ExprStmt(Node):
    expr: Expr


@dataclass
class Expect(Node):
    cond: Expr
    message: Optional[str] = None


@dataclass
class Break(Node):
    pass


Stmt = Union[Let, Assign, Return, ExprStmt, Expect, Break]


@dataclass
class Block(Node):
    stmts: list[Stmt]


# ---------- declarations ----------
@dataclass
class Param(Node):
    name: str
    type: TypeExpr
    deny: list[str] = field(default_factory=list)      # capability params only
    approve: list[str] = field(default_factory=list)   # capability params only


@dataclass
class TypeDecl(Node):
    name: str
    type: TypeExpr


@dataclass
class FnDecl(Node):
    name: str
    params: list[Param]
    ret: Optional[TypeExpr]
    body: Block


@dataclass
class AgentDecl(Node):
    name: str
    model: str
    system: str
    max_calls: Optional[int]


@dataclass
class TestDecl(Node):
    name: str
    fixtures: Optional[str]
    body: Block


@dataclass
class Program(Node):
    name: str
    uses: list[str]
    budget: Optional[float]
    types: list[TypeDecl]
    fns: list[FnDecl]
    agents: list[AgentDecl]
    tests: list[TestDecl]
    file: str = ""
    header_comments: list[str] = field(default_factory=list)
    order: list[Node] = field(default_factory=list)  # declaration order for the formatter


# ---------- host interface (.rmti) ----------
@dataclass
class CapabilityDecl(Node):
    name: str  # dotted, e.g. "email.send"
    params: list[Param]
    ret: Optional[TypeExpr]
    tags: list[str] = field(default_factory=list)
    clears: list[str] = field(default_factory=list)
    cost: float = 0.0
    idempotent: bool = False
    approval_always: bool = False


@dataclass
class ModelDecl(Node):
    name: str
    cost: float = 0.0


@dataclass
class InputDecl(Node):
    name: str  # parameter name of main
    tags: list[str]


@dataclass
class HostInterface(Node):
    types: list[TypeDecl]
    capabilities: list[CapabilityDecl]
    models: list[ModelDecl]
    inputs: list[InputDecl]
    file: str = ""
