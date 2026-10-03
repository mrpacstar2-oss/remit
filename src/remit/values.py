"""Runtime values carry data-flow tags and provenance (ids of the trace events they derive from)."""
from __future__ import annotations

from dataclasses import dataclass, field

EMPTY = frozenset()


@dataclass
class LV:
    """A labelled value. `v` is int | float | bool | str | None | list[LV] | dict[str, LV]."""
    v: object
    tags: frozenset = EMPTY
    prov: frozenset = EMPTY

    def __repr__(self):
        t = f"{set(self.tags)}" if self.tags else ""
        return f"LV({self.v!r}{t})"


def lit(v) -> LV:
    return LV(v)


def deep_tags(x: LV) -> frozenset:
    t = x.tags
    if isinstance(x.v, list):
        for i in x.v:
            t = t | deep_tags(i)
    elif isinstance(x.v, dict):
        for i in x.v.values():
            t = t | deep_tags(i)
    return t


def deep_prov(x: LV) -> frozenset:
    p = x.prov
    if isinstance(x.v, list):
        for i in x.v:
            p = p | deep_prov(i)
    elif isinstance(x.v, dict):
        for i in x.v.values():
            p = p | deep_prov(i)
    return p


def wrap(raw, tags=EMPTY, prov=EMPTY) -> LV:
    """Wrap a plain Python value (from an adapter or model), tagging every node."""
    if isinstance(raw, list):
        return LV([wrap(i, tags, prov) for i in raw], tags, prov)
    if isinstance(raw, dict):
        return LV({k: wrap(v, tags, prov) for k, v in raw.items()}, tags, prov)
    return LV(raw, tags, prov)


def unwrap(x: LV):
    if isinstance(x.v, list):
        return [unwrap(i) for i in x.v]
    if isinstance(x.v, dict):
        return {k: unwrap(v) for k, v in x.v.items()}
    return x.v


def retag(x: LV, add=EMPTY, remove=EMPTY, prov=EMPTY) -> LV:
    t = (x.tags | add) - remove
    p = x.prov | prov
    if isinstance(x.v, list):
        return LV([retag(i, add, remove, prov) for i in x.v], t, p)
    if isinstance(x.v, dict):
        return LV({k: retag(v, add, remove, prov) for k, v in x.v.items()}, t, p)
    return LV(x.v, t, p)
