"""Internal type representation for the checker."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class Type:
    def show(self) -> str:
        raise NotImplementedError

    def __str__(self) -> str:
        return self.show()


@dataclass(frozen=True)
class Prim(Type):
    name: str  # Int Float Bool Str Unit Never

    def show(self):
        return self.name


INT, FLOAT, BOOL, STR, UNIT, NEVER = (Prim(n) for n in ("Int", "Float", "Bool", "Str", "Unit", "Never"))


@dataclass(frozen=True)
class ListT(Type):
    elem: Type
    max: Optional[int] = None  # None = unbounded

    def show(self):
        m = f" max {self.max}" if self.max is not None else ""
        return f"List[{self.elem.show()}]{m}"


@dataclass(frozen=True)
class RecordT(Type):
    fields: tuple  # tuple of (name, Type)
    name: Optional[str] = None

    def get(self, f: str) -> Optional[Type]:
        for n, t in self.fields:
            if n == f:
                return t
        return None

    def show(self):
        if self.name:
            return self.name
        return "{" + ", ".join(f"{n}: {t.show()}" for n, t in self.fields) + "}"


@dataclass(frozen=True)
class EnumT(Type):
    options: tuple  # tuple of str
    name: Optional[str] = None

    def show(self):
        if self.name:
            return self.name
        return " | ".join(f'"{o}"' for o in self.options)


def assignable(src: Type, dst: Type) -> bool:
    """Can a value of static type `src` be used where `dst` is expected (ignoring list bounds)?"""
    if src == NEVER:
        return True
    if isinstance(dst, Prim) and isinstance(src, Prim):
        return src == dst or (src == INT and dst == FLOAT)
    if dst == STR and isinstance(src, EnumT):
        return True
    if isinstance(dst, EnumT) and isinstance(src, EnumT):
        return set(src.options) <= set(dst.options)
    if isinstance(dst, ListT) and isinstance(src, ListT):
        return assignable(src.elem, dst.elem)
    if isinstance(dst, RecordT) and isinstance(src, RecordT):
        for n, t in dst.fields:
            st = src.get(n)
            if st is None or not assignable(st, t):
                return False
        return True
    return False


def bound_violation(src: Type, dst: Type) -> Optional[str]:
    """Return a description if src's static list bound does not fit dst's declared bound."""
    if isinstance(dst, ListT) and isinstance(src, ListT):
        if dst.max is not None and (src.max is None or src.max > dst.max):
            have = "unbounded" if src.max is None else f"up to {src.max}"
            return f"list may have {have} items but at most {dst.max} are allowed"
        return bound_violation(src.elem, dst.elem)
    if isinstance(dst, RecordT) and isinstance(src, RecordT):
        for n, t in dst.fields:
            st = src.get(n)
            if st is not None:
                v = bound_violation(st, t)
                if v:
                    return f"field '{n}': {v}"
    return None


def join(a: Type, b: Type) -> Optional[Type]:
    """Least common type of two branches, or None if incompatible."""
    if a == NEVER:
        return b
    if b == NEVER:
        return a
    if a == b:
        return a
    if {a, b} == {INT, FLOAT}:
        return FLOAT
    if isinstance(a, ListT) and isinstance(b, ListT):
        e = join(a.elem, b.elem)
        if e is None:
            return None
        mx = None if a.max is None or b.max is None else max(a.max, b.max)
        return ListT(e, mx)
    if isinstance(a, EnumT) and isinstance(b, EnumT):
        return EnumT(tuple(sorted(set(a.options) | set(b.options))))
    if (isinstance(a, EnumT) and b == STR) or (isinstance(b, EnumT) and a == STR):
        return STR
    if isinstance(a, RecordT) and isinstance(b, RecordT):
        if assignable(a, b):
            return b
        if assignable(b, a):
            return a
    return None


def to_json_schema(t: Type) -> dict:
    if t == INT:
        return {"type": "integer"}
    if t == FLOAT:
        return {"type": "number"}
    if t == BOOL:
        return {"type": "boolean"}
    if t == STR:
        return {"type": "string"}
    if isinstance(t, EnumT):
        return {"type": "string", "enum": list(t.options)}
    if isinstance(t, ListT):
        s = {"type": "array", "items": to_json_schema(t.elem)}
        if t.max is not None:
            s["maxItems"] = t.max
        return s
    if isinstance(t, RecordT):
        return {
            "type": "object",
            "properties": {n: to_json_schema(ft) for n, ft in t.fields},
            "required": [n for n, _ in t.fields],
            "additionalProperties": False,
        }
    raise ValueError(f"type {t.show()} cannot be produced by a model")


def type_to_json(t: Type) -> object:
    if isinstance(t, Prim):
        return t.name
    if isinstance(t, ListT):
        return {"list": type_to_json(t.elem), "max": t.max}
    if isinstance(t, RecordT):
        return {"record": [[n, type_to_json(ft)] for n, ft in t.fields], "name": t.name}
    if isinstance(t, EnumT):
        return {"enum": list(t.options), "name": t.name}
    raise ValueError(t)


def type_from_json(j: object) -> Type:
    if isinstance(j, str):
        return {"Int": INT, "Float": FLOAT, "Bool": BOOL, "Str": STR, "Unit": UNIT, "Never": NEVER}[j]
    if "list" in j:
        return ListT(type_from_json(j["list"]), j["max"])
    if "record" in j:
        return RecordT(tuple((n, type_from_json(ft)) for n, ft in j["record"]), j.get("name"))
    if "enum" in j:
        return EnumT(tuple(j["enum"]), j.get("name"))
    raise ValueError(j)
