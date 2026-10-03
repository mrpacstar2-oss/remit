"""E4: crash-and-resume recovery, Axiom vs Python baseline (same broker).

For each app and each call position k, the process "crashes" right after the k-th adapter call has executed
(the downstream effect happened) but before the broker records it. The run is then resumed by replaying the
trace with the same, stateful downstream adapters, which de-duplicate non-idempotent effects by idempotency key.
We compare the final result with an uninterrupted run and count duplicated effects.
"""
from __future__ import annotations

import importlib
import json
import os
import sys

from remit.broker import ApproveAll, Broker, Policy, Trace, canonical, host_spec
from remit.checker import check
from remit.interpreter import Interpreter
from remit.ir import program_hash
from remit.parser import parse_host, parse_program
from remit.project import fixture_adapters
from remit.values import unwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples")
APPS = {"support": "support.rmt", "research": "research.rmt", "invoices": "invoices.rmt", "coding": "fix.rmt",
        "verify": "verify.rmt"}


class Crash(BaseException):
    pass


class Downstream:
    """Stateful external world: executes adapters once per idempotency key for non-idempotent resources."""

    def __init__(self, specs, fixtures, dedupe=True):
        self.dedupe = dedupe
        self.inner = fixture_adapters(fixtures, specs)
        self.specs = specs
        self.executed = []      # every adapter execution (resource, key)
        self.effects = {}       # key -> result, for non-idempotent resources
        self.crash_at = None
        self.n = 0
        self.reads = {}         # idempotent reads answer the same way when asked again

    def wrap(self, name):
        def call(arg, ctx):
            self.n += 1
            nonidem = not self.specs[name].idempotent
            rkey = (name, ctx.idempotency_key)  # the same call re-executed after a crash, not a new call
            if nonidem and ctx.idempotency_key in self.effects:
                out = self.effects[ctx.idempotency_key]
                if not self.dedupe:
                    self.executed.append((name, ctx.idempotency_key))  # performed a second time
            elif not nonidem and rkey in self.reads:
                out = self.reads[rkey]
            else:
                out = self.inner[name](arg, ctx)
                self.executed.append((name, ctx.idempotency_key))
                if nonidem:
                    self.effects[ctx.idempotency_key] = out
                else:
                    self.reads[rkey] = out
            if self.crash_at is not None and self.n == self.crash_at:
                raise Crash()
            return out
        return call

    def adapters(self):
        return {n: self.wrap(n) for n in self.specs}


def inputs_for(app):
    if app == "verify":
        return {"statement": "httpx is MIT licensed and supports HTTP/2.", "package": "httpx"}
    p = os.path.join(EX, app, "inputs.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def make_axiom(app):
    host = parse_host(open(os.path.join(EX, app, "host.rmti")).read(), "host.rmti")
    prog = parse_program(open(os.path.join(EX, app, APPS[app])).read(), APPS[app])
    c = check(prog, host)
    assert c.ok
    specs = host_spec(host)

    def run(adapters, replay=None):
        b = Broker(specs, adapters, Policy.load(os.path.join(EX, app, "policy.toml")), ApproveAll(), Trace(),
                   program_hash(prog), budget_usd=prog.budget, replay=replay)
        b.trace.emit({"event": "run_start", "run_id": b.run_id, "idempotency_root": b.idempotency_root})
        return b, lambda: unwrap(Interpreter(c, b).run_main(inputs_for(app)))
    return specs, run


def make_python(app):
    from baselines.runtime import make_runtime
    mod = importlib.import_module(f"baselines.{'coding' if app == 'coding' else app}")
    host = parse_host(open(os.path.join(EX, app, "host.rmti")).read(), "host.rmti")
    specs = host_spec(host)

    def run(adapters, replay=None):
        rt = make_runtime(os.path.join(EX, app), mod.__file__, fixtures={}, approver=ApproveAll(), replay=replay)
        rt.broker.adapters = adapters
        b = rt.broker
        if replay:
            b.idempotency_root = next(e["idempotency_root"] for e in replay if e.get("event") == "run_start")
        b.trace.emit({"event": "run_start", "run_id": b.run_id, "idempotency_root": b.idempotency_root})

        def go():
            out = mod.main(rt, **inputs_for(app))
            return json.loads(json.dumps(out, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else o))
        return b, go
    return specs, run


def experiment(app, maker, dedupe=True):
    fixtures = json.load(open(os.path.join(EX, app, "fixtures", "run.json")))
    specs, run = maker(app)
    ds = Downstream(specs, fixtures)
    b, go = run(ds.adapters())
    expected = go()
    n_calls = ds.n
    base_effects = len([e for e in ds.executed if not specs[e[0]].idempotent])
    rows = []
    for k in range(1, n_calls + 1):
        ds = Downstream(specs, fixtures, dedupe=dedupe)
        ds.crash_at = k
        b, go = run(ds.adapters())
        try:
            go()
            crashed = False
        except Crash:
            crashed = True
        ds.crash_at = None
        b2, go2 = run(ds.adapters(), replay=b.trace.events)
        out = go2()
        effects = len([e for e in ds.executed if not specs[e[0]].idempotent])
        rows.append({"k": k, "crashed": crashed, "same_result": canonical(out) == canonical(expected),
                     "effects": effects, "duplicated_effects": effects - base_effects,
                     "reexecuted_calls": ds.n - n_calls})
    return {"app": app, "calls": n_calls, "effects_uninterrupted": base_effects, "trials": rows,
            "all_same_result": all(r["same_result"] for r in rows),
            "max_duplicated_effects": max((r["duplicated_effects"] for r in rows), default=0)}


def main(argv=None):
    out = []
    for dedupe in (True, False):
        print(f"--- downstream de-duplicates by idempotency key: {dedupe}")
        for app in APPS:
            for lang, maker in (("axiom", make_axiom), ("python", make_python)):
                try:
                    r = experiment(app, maker, dedupe)
                except Exception as e:
                    r = {"app": app, "error": f"{type(e).__name__}: {e}"}
                r["lang"] = lang
                r["downstream_dedupe"] = dedupe
                out.append(r)
                if "error" in r:
                    print(f"{app:<9} {lang:<6} ERROR {r['error']}")
                else:
                    dup = sum(1 for t in r["trials"] if t["duplicated_effects"] > 0)
                    print(f"{app:<9} {lang:<6} crash points={r['calls']:<3} identical results={r['all_same_result']}  "
                          f"crash points with a duplicated effect={dup}")
    path = os.path.join(ROOT, "bench", "results", "e4_recovery.json")
    json.dump(out, open(path, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
