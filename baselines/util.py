"""Helpers shared by the baselines."""
from __future__ import annotations

from typing import Any

from remit.rterrors import RemitRuntimeError
from baselines.runtime import Runtime

_RAISE = object()


def call(rt: Runtime, resource: str, site: str, *, retry: int = 0, fallback: Any = _RAISE, **args) -> Any:
    """rt.call with Remit's `retry N else fallback`: only operational failures are retried or replaced."""
    last: RemitRuntimeError | None = None
    for _ in range(retry + 1):
        try:
            return rt.call(resource, site, **args)
        except RemitRuntimeError as e:
            if not e.operational:
                raise
            last = e
    if fallback is _RAISE:
        raise last
    return fallback


def repo_of(url: str) -> str:
    parts = url.replace("https://", "").replace("http://", "").split("/")
    return f"{parts[1]}/{parts[2]}"
