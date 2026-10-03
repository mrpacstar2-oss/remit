"""E2: tightness of static worst-case bounds versus live runs (Axiom programs, live model and sources).

Each app runs N times live with an auto-approver (benchmark only). For each resource we compare the checker's
worst-case call bound with observed calls, and the worst-case cost with the cost the Claude CLI reported.
Usage: python -m bench.e2 --runs 3 --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from remit.broker import ApproveAll, Policy, Trace, host_spec
from remit.interpreter import Interpreter
from remit.project import live_adapters, load, make_broker
from remit.rterrors import RemitRuntimeError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples")
APPS = [("support", "support.rmt"), ("research", "research.rmt"), ("invoices", "invoices.rmt"),
        ("coding", "fix.rmt"), ("verify", "verify.rmt")]


def reset(app_dir):
    for p in ("data/work", "data/out", "data/ledger.jsonl", "data/outbox.jsonl"):
        full = os.path.join(app_dir, p)
        if os.path.isdir(full):
            shutil.rmtree(full)
        elif os.path.exists(full):
            os.remove(full)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--json")
    ap.add_argument("--apps")
    a = ap.parse_args(argv)
    rows = []
    for app, prog in APPS:
        if a.apps and app not in a.apps.split(","):
            continue
        d = os.path.join(EX, app)
        L = load(os.path.join(d, prog))
        m = L.checked.manifest
        inputs = {}
        if os.path.exists(os.path.join(d, "inputs.json")):
            with open(os.path.join(d, "inputs.json")) as f:
                inputs = json.load(f)
        for i in range(a.runs):
            reset(d)
            specs = host_spec(L.host)
            broker = make_broker(L, policy=Policy.load(os.path.join(d, "policy.toml")),
                                 adapters=live_adapters(d, specs), approver=ApproveAll())
            t0 = time.time()
            err = None
            try:
                Interpreter(L.checked, broker).run_main(inputs)
            except RemitRuntimeError as e:
                err = f"{e.code}: {e.message[:120]}"
            calls = {}
            for e in broker.trace.events:
                if e.get("event") == "call":
                    calls[e["resource"]] = calls.get(e["resource"], 0) + 1
            models = sorted({(e.get("meta") or {}).get("model") for e in broker.trace.events
                             if e.get("event") == "call" and (e.get("meta") or {}).get("model")})
            row = {"app": app, "run": i + 1, "error": err, "seconds": round(time.time() - t0, 1),
                   "spent_usd": round(broker.spent, 5), "worst_case_usd": m["worst_case_cost_usd"],
                   "models": models,
                   "resources": {r: {"observed": calls.get(r, 0), "bound": v["max_calls"]}
                                 for r, v in m["resources"].items()}}
            rows.append(row)
            print(f"{app:<9} run {i + 1}: spent ${row['spent_usd']:.4f} / worst ${row['worst_case_usd']:.2f}  "
                  + "  ".join(f"{r}={v['observed']}/{v['bound']}" for r, v in row["resources"].items())
                  + (f"  ERROR {err}" if err else ""), flush=True)
        reset(d)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
