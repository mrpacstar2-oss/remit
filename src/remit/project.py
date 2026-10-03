"""Loading programs, host interfaces, policies and adapters; policy checks against the manifest."""
from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from typing import Optional

from . import ast as A
from .broker import AdapterResult, Approver, Broker, DenyAll, Policy, Trace, host_spec
from .checker import CheckResult, check
from .errors import RemitError, Diagnostic, Span
from .ir import from_json, program_hash
from .models import ClaudeCLIModel, FixtureModel
from .parser import parse_host, parse_program
from .rterrors import CapabilityFailed


@dataclass
class Loaded:
    path: str
    src: str
    program: A.Program
    host: A.HostInterface
    host_path: str
    checked: CheckResult
    sources: dict

    @property
    def dir(self):
        return os.path.dirname(os.path.abspath(self.path))


def find_host(prog_path: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    d = os.path.dirname(os.path.abspath(prog_path))
    while True:
        cand = os.path.join(d, "host.rmti")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            raise RemitError(Diagnostic("E0002", "no host.rmti found next to the program or in a parent directory",
                                        Span(prog_path, 1, 1), hint="pass --host path/to/host.rmti"))
        d = parent


def load(path: str, host_path: Optional[str] = None) -> Loaded:
    host_path = find_host(path, host_path)
    with open(host_path) as f:
        hsrc = f.read()
    host = parse_host(hsrc, os.path.relpath(host_path))
    if path.endswith(".json"):
        with open(path) as f:
            ir = json.load(f)
        prog = from_json(ir["program"])
        src = ""
    else:
        with open(path) as f:
            src = f.read()
        prog = parse_program(src, os.path.relpath(path))
    checked = check(prog, host)
    return Loaded(path, src, prog, host, host_path, checked,
                  {os.path.relpath(path): src, os.path.relpath(host_path): hsrc})


def policy_diagnostics(checked: CheckResult, policy: Policy) -> list[Diagnostic]:
    """Static comparison of the manifest with the deployment policy (policy cannot be widened by the program)."""
    out = []
    m = checked.manifest
    prog = checked.program
    sp = prog.span
    for name, r in m["resources"].items():
        g = policy.grants.get(name)
        if g is None:
            out.append(Diagnostic("E0501", f"policy {policy.source} does not grant '{name}'", sp,
                                  hint="the program cannot widen host policy; ask the operator or remove the use"))
            continue
        if g.max_calls is not None and r["max_calls"] > g.max_calls:
            out.append(Diagnostic("E0502", f"program may call '{name}' up to {r['max_calls']} time(s), "
                                           f"but policy allows {g.max_calls}", sp,
                                  hint="lower loop limits or retries around this call"))
    if policy.max_usd is not None and m["worst_case_cost_usd"] > policy.max_usd + 1e-12:
        out.append(Diagnostic("E0503", f"worst-case cost ${m['worst_case_cost_usd']:.4f} exceeds the policy budget "
                                       f"${policy.max_usd:.4f}", sp))
    return out


class FixtureCaps:
    """Offline capability adapter backed by a fixtures file. Every result is marked adapter=fixture."""

    def __init__(self, name: str, entries: list[dict]):
        self.name = name
        self.entries = entries
        self.used = [False] * len(entries)

    def __call__(self, args: dict, ctx):
        for i, e in enumerate(self.entries):
            if self.used[i] and not e.get("repeat"):
                continue
            want = e.get("args", {})
            if any(args.get(k) != v for k, v in want.items()):
                continue
            self.used[i] = True
            if "error" in e:
                raise CapabilityFailed(e["error"])
            return AdapterResult(e.get("output"), e.get("cost_usd"), {"adapter": "fixture"})
        raise CapabilityFailed(f"no fixture for {self.name}({json.dumps(args)[:120]})")


def fixture_adapters(fixtures: dict, specs: dict) -> dict:
    ad = {}
    caps = fixtures.get("capabilities", {})
    for name, spec in specs.items():
        if spec.kind == "model":
            ad[name] = FixtureModel(fixtures.get("models", {}).get(name, fixtures.get("model", [])))
        else:
            ad[name] = FixtureCaps(name, caps.get(name, []))
    return ad


def live_adapters(app_dir: str, specs: dict) -> dict:
    """Live mode: capability adapters come from the host's adapters.py; models use the Claude CLI."""
    ad = {}
    path = os.path.join(app_dir, "adapters.py")
    if os.path.exists(path):
        spec = importlib.util.spec_from_file_location("remit_host_adapters", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ad.update(mod.make_adapters(app_dir))
    model_id = os.environ.get("REMIT_MODEL", "claude-haiku-4-5-20251001")
    for name, s in specs.items():
        if s.kind == "model" and name not in ad:
            ad[name] = ClaudeCLIModel(model_id)
    return ad


def make_broker(loaded: Loaded, *, policy: Policy, adapters: dict, approver: Optional[Approver] = None,
                trace_path: Optional[str] = None, replay: Optional[list] = None, run_id: Optional[str] = None,
                live_after_replay: bool = True) -> Broker:
    specs = host_spec(loaded.host)
    return Broker(specs, adapters, policy, approver or DenyAll(), Trace(trace_path),
                  program_hash(loaded.program), budget_usd=loaded.program.budget, replay=replay, run_id=run_id,
                  live_after_replay=live_after_replay)
