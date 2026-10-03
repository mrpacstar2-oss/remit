"""E5: does structured (declaration-level AST) editing reduce model-introduced errors versus full-file rewrites?

Single model call per trial (no agent loop, no repair), so the edit format is the only variable. The model gets
the language spec, host interface, current program and the task (E3 feature tasks T1-T5). Condition `full`
returns the whole program; condition `patch` returns operations applied by remit.patch. Scored with the
same held-out checks as E3.

Usage: python -m bench.e5 --reps 3 --out bench/results/e5_haiku.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import tempfile

from remit.formatter import format_program
from remit.parser import parse_program
from remit.patch import apply_patch
from bench import e3

FULL = {"type": "object", "properties": {"program": {"type": "string"}}, "required": ["program"],
        "additionalProperties": False}
PATCH = {"type": "object", "properties": {"ops": {"type": "array", "items": {
    "type": "object",
    "properties": {"op": {"type": "string", "enum": ["replace", "insert", "delete", "header"]},
                   "name": {"type": "string"}, "after": {"type": "string"}, "source": {"type": "string"},
                   "uses": {"type": "array", "items": {"type": "string"}}, "budget": {"type": "number"}},
    "required": ["op"], "additionalProperties": False}}}, "required": ["ops"], "additionalProperties": False}

PATCH_HELP = """Return a list of edit operations on top-level declarations (types, functions, agents and tests;
tests are named by their string):
- {"op": "replace", "name": "<declaration name>", "source": "<the complete new declaration>"}
- {"op": "insert", "after": "<declaration name>", "source": "<one or more new declarations>"}
- {"op": "delete", "name": "<declaration name>"}
- {"op": "header", "uses": [...], "budget": <number>}   (only if the uses line or budget must change)
Only touch declarations that must change."""


def ask(prompt, schema, model):
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", None)
    p = subprocess.run(["claude", "-p", prompt, "--model", model, "--tools", "", "--system-prompt",
                        "You edit programs written in Axiom. Follow the requested output format exactly.",
                        "--no-session-persistence", "--json-schema", json.dumps(schema), "--output-format", "json"],
                       capture_output=True, text=True, timeout=600, cwd="/tmp", env=env)
    d = json.loads(p.stdout)
    out = d.get("structured_output")
    if out is None:
        out = json.loads(d.get("result") or "{}")
    return out, d.get("total_cost_usd"), (d.get("usage") or {}).get("output_tokens")


def trial(task, cond, model, rep):
    app = task["app"]
    prog_path = os.path.join(e3.EX, app, e3.PROG[app])
    original = open(prog_path).read()
    spec = open(os.path.join(e3.ROOT, "docs", "LANGUAGE_SPEC.md")).read()
    host = open(os.path.join(e3.EX, app, "host.rmti")).read()
    fmt = ("Return the COMPLETE revised program in `program`." if cond == "full" else PATCH_HELP)
    prompt = (f"<spec>\n{spec}\n</spec>\n<host>\n{host}\n</host>\n<program file=\"{e3.PROG[app]}\">\n{original}\n"
              f"</program>\n\nTask: {task['task']}\n\n{fmt}")
    res = {"task": task["id"], "cond": cond, "model": model, "rep": rep}
    try:
        out, cost, otoks = ask(prompt, FULL if cond == "full" else PATCH, model)
        res.update(cost_usd=cost, output_tokens=otoks)
        if cond == "full":
            new = out["program"]
        else:
            res["ops"] = out["ops"]
            new = format_program(apply_patch(parse_program(original, e3.PROG[app]), out["ops"]))
        res["apply_ok"] = True
    except Exception as e:
        res.update(apply_ok=False, apply_error=f"{type(e).__name__}: {str(e)[:300]}")
        new = None
    if new is not None:
        ws = tempfile.mkdtemp(prefix="e5_")
        shutil.copytree(os.path.join(e3.EX, app), os.path.join(ws, "examples", app),
                        ignore=shutil.ignore_patterns(".remit", "data", "__pycache__"))
        with open(os.path.join(ws, "examples", app, e3.PROG[app]), "w") as f:
            f.write(new)
        res["checks"] = e3.hidden(task["id"], e3.run_axiom, ws)
        d = subprocess.run(["diff", original_path := prog_path, os.path.join(ws, "examples", app, e3.PROG[app])],
                           capture_output=True, text=True).stdout
        res["changed_lines"] = sum(1 for l in d.splitlines() if l[:1] in "<>")
        # did untouched declarations survive byte-for-byte (comments, formatting)?
        res["comments_kept"] = all(c in new for c in [l.strip() for l in original.splitlines() if l.strip().startswith("#")])
        shutil.rmtree(ws, ignore_errors=True)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    tasks = [t for t in e3.TASKS if t["kind"] == "feature"]
    jobs = [(t, c, r) for r in range(a.reps) for t in tasks for c in ("full", "patch")]
    results = []
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for res in ex.map(lambda j: trial(j[0], j[1], a.model, j[2]), jobs):
            results.append(res)
            json.dump(results, open(a.out, "w"), indent=1)
            c = {k: v for k, v in (res.get("checks") or {}).items() if not k.startswith("_") and not k.endswith(":exception")}
            print(f"{res['task']} {res['cond']:<5} rep{res['rep']}: apply_ok={res['apply_ok']} "
                  f"checks={sum(c.values())}/{len(c)} cost=${res.get('cost_usd')} changed={res.get('changed_lines')} "
                  f"{res.get('apply_error', '')[:80]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
