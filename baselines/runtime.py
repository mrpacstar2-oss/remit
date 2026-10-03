"""Shared runtime for the Python baselines.

The baselines use the SAME broker, host declarations, deployment policy, adapters and model adapter as Remit,
so runtime guarantees (grants, call limits, budget reservation, argument-bound approvals, idempotency keys,
traces, replay) are identical. Structured outputs use Pydantic. The broker's heuristic taint is ON: plain values
are tagged when they contain, or are contained in, a string a tagged capability returned. This is the strongest
cheap data-flow defence available to library code that has no access to the language's value semantics.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from remit.broker import Approver, Broker, DenyAll, Policy, Trace, host_spec
from remit.parser import parse_host
from remit.project import fixture_adapters, live_adapters
from remit.rterrors import RemitRuntimeError, ModelOutputInvalid
from remit.values import unwrap

T = TypeVar("T")


@dataclass
class Runtime:
    broker: Broker

    def call(self, resource: str, site: str, **args) -> Any:
        """Invoke a capability through the broker and return a plain Python value."""
        return unwrap(self.broker.call(resource, args, site=site))

    def ask(self, model: str, site: str, out: Type[T], prompt: str, context: Any = None,
            system: Optional[str] = None, agent: Optional[str] = None, retries: int = 0) -> T:
        """Structured model call: JSON Schema from Pydantic, validated with Pydantic, retried on invalid output."""
        if isinstance(out, type) and issubclass(out, BaseModel):
            schema = out.model_json_schema()
            schema.setdefault("additionalProperties", False)
        else:
            schema = {str: {"type": "string"}, int: {"type": "integer"}, float: {"type": "number"},
                      bool: {"type": "boolean"}}[out]
        if isinstance(context, BaseModel):
            context = context.model_dump()
        req = {"schema": schema, "prompt": prompt, "context": context, "system": system, "agent": agent}
        last: Exception | None = None
        for _ in range(retries + 1):
            try:
                raw = unwrap(self.broker.call(model, {"prompt": prompt, "context": context}, site=site,
                                              model_request=req))
                if isinstance(out, type) and issubclass(out, BaseModel):
                    return out.model_validate(raw)
                if not isinstance(raw, out):
                    raise ModelOutputInvalid(f"expected {out.__name__}")
                return raw
            except ValidationError as e:
                last = ModelOutputInvalid(str(e)[:200])
            except RemitRuntimeError as e:
                if not e.operational:
                    raise
                last = e
        raise last


def program_hash_of(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def make_runtime(app_dir: str, program_path: str, *, live: bool = False, fixtures: Optional[dict] = None,
                 approver: Optional[Approver] = None, policy: Optional[Policy] = None,
                 trace_path: Optional[str] = None, budget_usd: Optional[float] = None,
                 heuristic_taint: bool = True, replay=None) -> Runtime:
    with open(os.path.join(app_dir, "host.rmti")) as f:
        host = parse_host(f.read(), "host.rmti")
    specs = host_spec(host)
    if live:
        adapters = live_adapters(app_dir, specs)
    else:
        adapters = fixture_adapters(fixtures or {}, specs)
    pol = policy or Policy.load(os.path.join(app_dir, "policy.toml"))
    b = Broker(specs, adapters, pol, approver or DenyAll(), Trace(trace_path), program_hash_of(program_path),
               budget_usd=budget_usd, heuristic_taint=heuristic_taint, replay=replay)
    return Runtime(b)
