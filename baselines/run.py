"""Run a baseline app: python -m baselines.run <app> [--live] [--fixtures f.json] [--inputs f.json] [--approve-all]"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

from remit.broker import ApproveAll, DenyAll
from remit.rterrors import RemitRuntimeError
from baselines.runtime import make_runtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--fixtures")
    ap.add_argument("--inputs")
    ap.add_argument("--approve-all", action="store_true")
    a = ap.parse_args(argv)
    app_dir = os.path.join(ROOT, "examples", a.app)
    mod = importlib.import_module(f"baselines.{a.app}")
    fixtures = None
    if not a.live:
        with open(a.fixtures or os.path.join(app_dir, "fixtures", "run.json")) as f:
            fixtures = json.load(f)
    inputs = {}
    p = a.inputs or os.path.join(app_dir, "inputs.json")
    if os.path.exists(p):
        with open(p) as f:
            inputs = json.load(f)
    rt = make_runtime(app_dir, mod.__file__, live=a.live, fixtures=fixtures,
                      approver=ApproveAll() if a.approve_all else DenyAll())
    try:
        out = mod.main(rt, **inputs)
        print(json.dumps(out if not hasattr(out, "model_dump") else out.model_dump(), indent=2, default=str))
    except RemitRuntimeError as e:
        print(e.render(), file=sys.stderr)
        return 1
    finally:
        print(f"spent ${rt.broker.spent:.4f}, {rt.broker.seq} call(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
