"""E1: deterministic unsafe-mutation suite. Axiom (checker + broker) vs Python baseline (same broker).

Each mutation is an equivalent unsafe edit applied textually to both implementations. Both are run against the
same fixtures, approver (deny-all) and policy. A second Python variant uses a STRICT policy that requires
approval on every call to the app's critical capabilities (what a careful engineer could do without
data-flow tracking). Nothing here calls a model or the network.

Usage: python -m bench.e1 [--json out.json]
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import traceback

from remit.broker import DenyAll, Grant, Policy, Trace, host_spec
from remit.interpreter import Interpreter
from remit.ir import program_hash
from remit.parser import parse_host, parse_program
from remit.checker import check
from remit.project import fixture_adapters, policy_diagnostics
from remit.broker import Broker
from remit.rterrors import RemitRuntimeError
from remit.values import unwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples")
APPS = {
    "support": ("support.rmt", "support.py"),
    "research": ("research.rmt", "research.py"),
    "invoices": ("invoices.rmt", "invoices.py"),
    "coding": ("fix.rmt", "coding.py"),
    "verify": ("verify.rmt", "verify.py"),
}
# Capabilities a careful engineer would put behind "approval always" in the strict Python variant.
CRITICAL = {
    "support": ["payments.refund", "email.send"],
    "invoices": ["payments.schedule"],
    "coding": ["repo.edit"],
    "research": [],
    "verify": [],
}


def apply(src: str, edits: list) -> str:
    for find, repl in edits:
        if find not in src:
            raise ValueError(f"mutation anchor not found: {find[:70]!r}")
        src = src.replace(find, repl, 1)
    return src


def load_fixtures(m):
    app = m["app"]
    base = m.get("base_fixtures", os.path.join(EX, app, "fixtures", "run.json"))
    with open(base if os.path.isabs(base) else os.path.join(ROOT, base)) as f:
        fx = json.load(f)
    over = m.get("fixtures", {})
    for k in ("capabilities", "models"):
        for name, entries in over.get(k, {}).items():
            fx.setdefault(k, {})[name] = entries
    if "model" in over:
        fx["model"] = over["model"]
    return fx


def effectful(app) -> set:
    with open(os.path.join(EX, app, "host.rmti")) as f:
        host = parse_host(f.read(), "host.rmti")
    return {c.name for c in host.capabilities if not c.idempotent}


def strict_policy(app) -> Policy:
    p = Policy.load(os.path.join(EX, app, "policy.toml"))
    p = copy.deepcopy(p)
    for name in CRITICAL[app]:
        g = p.grants.get(name)
        if g is not None:
            g.approval = "always"
    p.source += " (strict)"
    return p


def classify(m, events, error, app):
    eff = effectful(app)
    all_calls = [e for e in events if e.get("event") == "call"]
    calls = [e for e in all_calls if e.get("status") == "ok"]
    side_effects = [e for e in calls if e["resource"] in eff]
    unsafe = m["unsafe"]
    hit = False
    pool = all_calls if unsafe.get("count_errors") else calls
    matching = [e for e in pool if e["resource"] == unsafe["resource"]]
    if unsafe.get("no_approval"):
        hit = any(not (e.get("approval") or {}).get("approved_by") for e in matching)
    elif "min_calls" in unsafe:
        hit = len(matching) >= unsafe["min_calls"]
    else:
        for e in matching:
            ok = True
            for k, v in unsafe.get("args", {}).items():
                av = e["args"].get(k)
                if isinstance(v, str) and v.startswith("contains:"):
                    ok = ok and isinstance(av, str) and v[9:] in av
                else:
                    ok = ok and av == v
            hit = hit or ok
    approvals = sum(1 for e in events if e.get("event") == "pending_approval")
    return {
        "outcome": ("UNSAFE EFFECT EXECUTED" if hit else
                    (f"stopped at runtime ({error})" if error else "completed safely")),
        "unsafe_executed": hit,
        "side_effects_before_end": len(side_effects),
        "approval_requests": approvals,
        "spent_usd": round(sum(e.get("cost_usd") or 0 for e in calls), 4),
        "error": error,
    }


def run_axiom(m):
    app = m["app"]
    ax, _ = APPS[app]
    with open(os.path.join(EX, app, ax)) as f:
        src = apply(f.read(), m.get("axiom", []))
    with open(os.path.join(EX, app, "host.rmti")) as f:
        host = parse_host(f.read(), "host.rmti")
    try:
        prog = parse_program(src, ax)
    except Exception as e:
        return {"outcome": "rejected before run (syntax)", "unsafe_executed": False, "side_effects_before_end": 0,
                "approval_requests": 0, "spent_usd": 0.0, "error": str(e), "static_codes": ["E0100"]}
    c = check(prog, host)
    pol = Policy.load(os.path.join(EX, app, "policy.toml"))
    diags = [d for d in c.diagnostics if d.severity == "error"]
    if not diags:
        diags = policy_diagnostics(c, pol)
    if diags:
        return {"outcome": "rejected before run", "unsafe_executed": False, "side_effects_before_end": 0,
                "approval_requests": 0, "spent_usd": 0.0, "error": None,
                "static_codes": sorted({d.code for d in diags}),
                "first_message": diags[0].message}
    specs = host_spec(host)
    b = Broker(specs, fixture_adapters(load_fixtures(m), specs), pol, DenyAll(), Trace(), program_hash(prog),
               budget_usd=prog.budget)
    err = None
    try:
        Interpreter(c, b).run_main(m.get("inputs", _inputs(app)))
    except RemitRuntimeError as e:
        err = f"{e.code} {type(e).__name__}"
    r = classify(m, b.trace.events, err, app)
    r["static_codes"] = []
    return r


def _inputs(app):
    p = os.path.join(EX, app, "inputs.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def run_python(m, strict=False):
    app = m["app"]
    _, py = APPS[app]
    path = os.path.join(ROOT, "baselines", py)
    with open(path) as f:
        src = apply(f.read(), m.get("python", []))
    tmpdir = tempfile.mkdtemp()
    tmp = os.path.join(tmpdir, f"mut_{app}.py")
    with open(tmp, "w") as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location(f"mut_{app}_{m['id']}", tmp)
    mod = importlib.util.module_from_spec(spec)
    from baselines.runtime import make_runtime
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return {"outcome": "rejected before run (import error)", "unsafe_executed": False,
                "side_effects_before_end": 0, "approval_requests": 0, "spent_usd": 0.0, "error": repr(e)}
    pol = strict_policy(app) if strict else Policy.load(os.path.join(EX, app, "policy.toml"))
    rt = make_runtime(os.path.join(EX, app), tmp, fixtures=load_fixtures(m), approver=DenyAll(), policy=pol,
                      budget_usd=m.get("python_budget"))
    err = None
    try:
        mod.main(rt, **m.get("inputs", _inputs(app)))
    except RemitRuntimeError as e:
        err = f"{e.code} {type(e).__name__}"
    except Exception as e:  # a crash of the Python program itself
        err = f"python {type(e).__name__}: {str(e)[:80]}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return classify(m, rt.broker.trace.events, err, app)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    with open(os.path.join(ROOT, "bench", "mutations.json")) as f:
        muts = json.load(f)
    if a.only:
        muts = [m for m in muts if m["id"] in a.only.split(",")]
    rows = []
    for m in muts:
        row = {"id": m["id"], "app": m["app"], "category": m["category"], "description": m["description"]}
        for name, fn in (("axiom", lambda: run_axiom(m)), ("python", lambda: run_python(m)),
                         ("python_strict", lambda: run_python(m, strict=True))):
            try:
                row[name] = fn()
            except Exception as e:
                row[name] = {"outcome": f"HARNESS ERROR {e!r}", "unsafe_executed": None}
                traceback.print_exc()
        rows.append(row)
        print(f"{m['id']:<5} {m['category']:<22} axiom: {row['axiom']['outcome'][:42]:<42} "
              f"python: {row['python']['outcome'][:42]:<42} strict: {row['python_strict']['outcome'][:40]}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
