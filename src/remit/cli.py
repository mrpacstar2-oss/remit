"""remit CLI: check, build, run, test, fmt, replay, approve, resume, diff, ask, mcp."""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

from . import ast as A
from .broker import ApprovalStore, ApproveAll, DenyAll, Interactive, Policy, Trace, canonical, host_spec
from .errors import RemitError
from .formatter import format_host, format_program
from .interpreter import Interpreter, _ExpectFailed
from .ir import program_hash, to_json
from .parser import parse_host, parse_program
from .project import Loaded, fixture_adapters, live_adapters, load, make_broker, policy_diagnostics
from .rterrors import ApprovalRequired, RemitRuntimeError
from .values import unwrap

STATE_DIR = ".remit"


def _print_diags(diags, sources, as_json=False, stream=sys.stderr):
    if as_json:
        return
    for d in diags:
        print(d.render(sources), file=stream)


def _load_or_exit(path, host=None) -> Loaded:
    try:
        return load(path, host)
    except RemitError as e:
        srcs = {}
        try:
            srcs[e.diag.span.file] = open(e.diag.span.file).read()
        except Exception:
            pass
        print(e.diag.render(srcs), file=sys.stderr)
        sys.exit(1)


def _policy(loaded: Loaded, path, allow_all) -> Policy | None:
    if path:
        return Policy.load(path)
    cand = os.path.join(loaded.dir, "policy.toml")
    if os.path.exists(cand):
        return Policy.load(cand)
    if allow_all:
        return Policy.allow_all(host_spec(loaded.host))
    return None


def summary(m: dict) -> str:
    lines = [f"program {m['program']}: worst-case cost ${m['worst_case_cost_usd']:.4f}"
             + (f" (budget ${m['budget_usd']:.4f})" if m["budget_usd"] is not None else "")]
    for name, r in m["resources"].items():
        lines.append(f"  {name:<22} ≤ {r['max_calls']:>3} call(s)   ≤ ${r['worst_case_cost']:.4f}")
    for a, r in m["agents"].items():
        lines.append(f"  agent {a:<16} ≤ {r['max_calls_worst_case']:>3} call(s)  (max_calls {r['max_calls_declared']})")
    for s in m["approval_sites"]:
        lines.append(f"  approval {s['approval']:<8} {s['resource']} at line {s['line']}: {'; '.join(s['reasons'])}")
    for f in m["flows"]:
        lines.append(f"  flow {f['sink']:<26} receives {', '.join(f['tags'])}: {f['verdict']}")
    return "\n".join(lines)


def cmd_check(a):
    rc = 0
    out = []
    for path in a.files:
        L = _load_or_exit(path, a.host)
        diags = list(L.checked.diagnostics)
        pol = _policy(L, a.policy, False)
        if pol is not None and L.checked.ok:
            diags += policy_diagnostics(L.checked, pol)
        errors = [d for d in diags if d.severity == "error"]
        if a.json:
            out.append({"file": path, "ok": not errors, "diagnostics": [d.to_json() for d in diags],
                        "manifest": L.checked.manifest if not errors else None,
                        "policy": pol.source if pol else None})
        else:
            _print_diags(diags, L.sources)
            if not errors:
                print(summary(L.checked.manifest))
                print(f"ok: {path}" + (f" (policy {pol.source})" if pol else " (no policy checked)"))
            else:
                print(f"{len(errors)} error(s) in {path}", file=sys.stderr)
        if errors:
            rc = 1
    if a.json:
        print(json.dumps(out if len(out) > 1 else out[0], indent=2))
    return rc


def cmd_build(a):
    L = _load_or_exit(a.file, a.host)
    if not L.checked.ok:
        _print_diags(L.checked.diagnostics, L.sources)
        return 1
    _print_diags(L.checked.diagnostics, L.sources)
    os.makedirs(a.out, exist_ok=True)
    name = L.program.name
    h = program_hash(L.program)
    ir = {"remit_ir": 1, "program_hash": h, "program": to_json(L.program),
          "node_types": {str(k): str(v) for k, v in L.checked.node_types.items()}}
    ir_path = os.path.join(a.out, f"{name}.ir.json")
    man_path = os.path.join(a.out, f"{name}.manifest.json")
    with open(ir_path, "w") as f:
        json.dump(ir, f, indent=1)
    with open(man_path, "w") as f:
        json.dump({**L.checked.manifest, "program_hash": h}, f, indent=2)
    print(f"wrote {ir_path}\nwrote {man_path}\nprogram hash {h}")
    return 0


def _inputs(a) -> dict:
    inputs = {}
    if a.inputs:
        with open(a.inputs) as f:
            inputs.update(json.load(f))
    for kv in a.input or []:
        k, _, v = kv.partition("=")
        try:
            inputs[k] = json.loads(v)
        except json.JSONDecodeError:
            inputs[k] = v
    return inputs


def _adapters(L, a, specs):
    if a.live:
        return live_adapters(L.dir, specs), "live"
    fx = a.fixtures or os.path.join(L.dir, "fixtures", "run.json")
    if not os.path.exists(fx):
        print(f"error: no fixtures at {fx}; pass --fixtures or --live", file=sys.stderr)
        sys.exit(2)
    with open(fx) as f:
        return fixture_adapters(json.load(f), specs), f"fixtures:{fx}"


def _execute(L: Loaded, a, inputs, replay=None, run_id=None):
    if not L.checked.ok:
        _print_diags(L.checked.diagnostics, L.sources)
        print("refusing to run a program with errors", file=sys.stderr)
        return 1
    pol = _policy(L, a.policy, a.allow_all)
    if pol is None:
        print("error: no policy.toml next to the program; pass --policy or --allow-all", file=sys.stderr)
        return 2
    pdiags = policy_diagnostics(L.checked, pol)
    if pdiags:
        _print_diags(pdiags, L.sources)
        print("refusing to run: the program's worst case exceeds the deployment policy", file=sys.stderr)
        return 1
    specs = host_spec(L.host)
    adapters, mode = _adapters(L, a, specs)
    store = ApprovalStore(os.path.join(STATE_DIR, "approvals.json"))
    approver = ApproveAll() if getattr(a, "approve_all", False) else (Interactive(store) if a.interactive else store)
    if getattr(a, "approve_all", False):
        print("warning: --approve-all grants every approval (development and fixtures only)", file=sys.stderr)
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    trace_path = os.path.join(STATE_DIR, "runs", f"{run_id}.jsonl")
    broker = make_broker(L, policy=pol, adapters=adapters, approver=approver, trace_path=trace_path, replay=replay,
                         run_id=run_id)
    broker.emit({"event": "run_start", "run_id": run_id, "idempotency_root": broker.idempotency_root,
                 "program": L.program.name, "path": L.path,
                 "program_hash": broker.program_hash, "inputs": inputs, "mode": mode,
                 "replaying": len(broker.replay)})
    interp = Interpreter(L.checked, broker)
    status = 0
    try:
        out = interp.run_main(inputs)
        result = unwrap(out)
        broker.emit({"event": "run_end", "run_id": run_id, "status": "ok", "result": result,
                     "result_tags": sorted(out.tags), "spent_usd": broker.spent})
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except ApprovalRequired as e:
        broker.emit({"event": "run_end", "run_id": run_id, "status": "pending_approval", "digest": e.data["digest"]})
        print(e.render(), file=sys.stderr)
        print(f"  args: {json.dumps(e.data['args'], ensure_ascii=False)[:400]}", file=sys.stderr)
        print(f"  to approve this exact call: remit approve {e.data['digest']}\n"
              f"  then continue without repeating completed effects: remit resume {run_id}", file=sys.stderr)
        status = 3
    except RemitRuntimeError as e:
        broker.emit({"event": "run_end", "run_id": run_id, "status": "error", "code": e.code, "message": e.message})
        print(e.render(), file=sys.stderr)
        status = 1
    print(f"trace: {trace_path}  (spent ${broker.spent:.4f}, {broker.seq} call(s), mode {mode})", file=sys.stderr)
    return status


def cmd_run(a):
    L = _load_or_exit(a.file, a.host)
    _print_diags([d for d in L.checked.diagnostics if d.severity == "warning"], L.sources)
    return _execute(L, a, _inputs(a))


def _trace_for(run: str) -> str:
    if os.path.exists(run):
        return run
    p = os.path.join(STATE_DIR, "runs", f"{run}.jsonl")
    if not os.path.exists(p):
        print(f"error: no trace for run '{run}'", file=sys.stderr)
        sys.exit(2)
    return p


def cmd_resume(a):
    events = Trace.load(_trace_for(a.run))
    start = next(e for e in events if e["event"] == "run_start")
    L = _load_or_exit(a.file or start["path"], a.host)
    if program_hash(L.program) != start["program_hash"]:
        print("error: the program changed since this run; recorded results and approvals no longer apply",
              file=sys.stderr)
        return 1
    return _execute(L, a, start["inputs"], replay=events)


def cmd_replay(a):
    """Re-execute a finished run against its recorded results only (no live calls) and verify it matches."""
    events = Trace.load(_trace_for(a.run))
    start = next(e for e in events if e["event"] == "run_start")
    end = next((e for e in reversed(events) if e["event"] == "run_end"), None)
    L = _load_or_exit(a.file or start["path"], a.host)
    if program_hash(L.program) != start["program_hash"]:
        print("note: program hash differs from the recorded run; replay checks whether behaviour still matches",
              file=sys.stderr)
    specs = host_spec(L.host)
    pol = Policy.allow_all(specs)
    broker = make_broker(L, policy=pol, adapters={}, approver=DenyAll(), replay=events, live_after_replay=False)
    try:
        out = unwrap(Interpreter(L.checked, broker).run_main(start["inputs"]))
    except RemitRuntimeError as e:
        print(e.render(), file=sys.stderr)
        return 1
    calls = len(broker.replay)
    if broker.replayed != calls:
        print(f"replay diverged: program made {broker.replayed} of {calls} recorded calls", file=sys.stderr)
        return 1
    if end and end.get("status") == "ok" and canonical(end.get("result")) != canonical(out):
        print("replay diverged: final result differs from the recorded result", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"replay ok: {calls} recorded call(s) reproduced, no live effects", file=sys.stderr)
    return 0


def cmd_approve(a):
    store = ApprovalStore(os.path.join(STATE_DIR, "approvals.json"))
    matches = [d for d in store.data["pending"] if d.startswith(a.digest)]
    if len(matches) != 1:
        print(f"error: {'no' if not matches else 'ambiguous'} pending approval matching '{a.digest}'", file=sys.stderr)
        return 2
    req = store.data["pending"][matches[0]]
    print(json.dumps(req, indent=2, ensure_ascii=False))
    store.approve(matches[0], a.by or os.environ.get("USER", "cli"))
    print(f"approved {matches[0]} (single use, bound to this program version, call site and arguments)")
    return 0


def cmd_fmt(a):
    rc = 0
    for path in a.files:
        src = open(path).read()
        try:
            if path.endswith(".rmti"):
                out = format_host(parse_host(src, path))
            else:
                out = format_program(parse_program(src, path))
        except RemitError as e:
            print(e.diag.render({path: src}), file=sys.stderr)
            rc = 1
            continue
        if a.check:
            if out != src:
                print(f"would reformat {path}")
                rc = 1
        elif out != src:
            with open(path, "w") as f:
                f.write(out)
            print(f"formatted {path}")
    return rc


def run_tests(L: Loaded, verbose=True) -> tuple[int, int]:
    passed = failed = 0
    specs = host_spec(L.host)
    for t in L.program.tests:
        fixtures = {}
        if t.fixtures:
            with open(os.path.join(L.dir, t.fixtures)) as f:
                fixtures = json.load(f)
        approver = ApproveAll() if fixtures.get("approve_all") else DenyAll()
        broker = make_broker(L, policy=Policy.allow_all(specs), adapters=fixture_adapters(fixtures, specs),
                             approver=approver)
        interp = Interpreter(L.checked, broker)
        from .interpreter import Env
        try:
            interp.block(t.body, Env(None))
            passed += 1
            if verbose:
                print(f"  ok    {t.name}")
        except _ExpectFailed as e:
            failed += 1
            print(f"  FAIL  {t.name}: {e.span}: {e.msg}")
        except RemitRuntimeError as e:
            failed += 1
            print(f"  FAIL  {t.name}: {e.render()}")
    return passed, failed


def cmd_test(a):
    paths = a.paths or ["."]
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(f for f in glob.glob(os.path.join(p, "**", "*.rmt"), recursive=True)
                            if "/.venv/" not in f)
        else:
            files.append(p)
    total_p = total_f = 0
    for f in files:
        L = _load_or_exit(f, a.host)
        if not L.program.tests:
            continue
        if not L.checked.ok:
            _print_diags(L.checked.diagnostics, L.sources)
            total_f += len(L.program.tests)
            continue
        print(f"{f}")
        p, fl = run_tests(L)
        total_p += p
        total_f += fl
    print(f"{total_p} passed, {total_f} failed")
    return 1 if total_f else 0


def authority_diff(old: dict, new: dict) -> list[str]:
    """Human-readable list of ways `new` has more authority than `old` (empty = no widening)."""
    out = []
    for name, r in new["resources"].items():
        o = old["resources"].get(name)
        if o is None or o["max_calls"] == 0:
            if r["max_calls"] > 0:
                out.append(f"NEW resource '{name}' (≤ {r['max_calls']} call(s))")
        elif r["max_calls"] > o["max_calls"]:
            out.append(f"'{name}' worst-case calls {o['max_calls']} -> {r['max_calls']}")
    if new["worst_case_cost_usd"] > old["worst_case_cost_usd"] + 1e-12:
        out.append(f"worst-case cost ${old['worst_case_cost_usd']:.4f} -> ${new['worst_case_cost_usd']:.4f}")
    if (new["budget_usd"] or 0) > (old["budget_usd"] or 0) or (old["budget_usd"] is not None and new["budget_usd"] is None):
        out.append(f"budget {old['budget_usd']} -> {new['budget_usd']}")
    of = {f["sink"]: f for f in old["flows"]}
    for f in new["flows"]:
        o = of.get(f["sink"])
        added = sorted(set(f["tags"]) - set(o["tags"] if o else []))
        if added:
            out.append(f"flow into {f['sink']} now receives {', '.join(added)} ({f['verdict']})")
    osites = {(s["resource"], tuple(s["reasons"])) for s in old["approval_sites"]}
    oc = sum(1 for s in old["approval_sites"])
    if len(new["approval_sites"]) > oc:
        out.append(f"approval sites {oc} -> {len(new['approval_sites'])}")
    return out


def cmd_diff(a):
    L1 = _load_or_exit(a.old, a.host)
    L2 = _load_or_exit(a.new, a.host)
    for L in (L1, L2):
        if not L.checked.ok:
            _print_diags(L.checked.diagnostics, L.sources)
            return 1
    w = authority_diff(L1.checked.manifest, L2.checked.manifest)
    if a.json:
        print(json.dumps({"widened": bool(w), "changes": w}, indent=2))
    elif w:
        print("authority WIDENED:")
        for x in w:
            print(f"  + {x}")
    else:
        print("no authority widening")
    return 4 if w else 0


def cmd_ask(a):
    from .ask import ask, unified
    L = _load_or_exit(a.file, a.host)
    if not L.checked.ok:
        _print_diags(L.checked.diagnostics, L.sources)
        print("fix the current program first", file=sys.stderr)
        return 1
    pol = _policy(L, a.policy, False)
    spec_path = a.spec or os.path.join(os.path.dirname(__file__), "..", "..", "docs", "LANGUAGE_SPEC.md")
    with open(spec_path) as f:
        spec = f.read()
    with open(L.host_path) as f:
        host_src = f.read()
    r = ask(a.request, a.file, L.host, host_src, pol, spec, a.model)
    print(unified(r["original"], r["proposed"], a.file) or "(no textual change)")
    for rd in r["rounds"]:
        print(f"-- round {rd['round']}: {'refused' if rd['refused'] else (str(len(rd['errors'])) + ' error(s)')}: "
              f"{rd['explanation'][:400]}")
    print(f"model cost ${r['cost_usd']:.4f}")
    if r["refused"]:
        print("the model declined the change")
        return 5
    if r["errors"]:
        print(f"proposal still fails the checker after {len(r['rounds'])} round(s); nothing written", file=sys.stderr)
        for d in r["errors"]:
            print(d.render({os.path.basename(a.file): r["proposed"]}), file=sys.stderr)
        return 1
    widened = authority_diff(L.checked.manifest, r["checked"].manifest)
    if widened:
        print("authority WIDENED by this change:")
        for w in widened:
            print(f"  + {w}")
    else:
        print("no authority widening")
    if not a.apply:
        print("dry run: re-run with --apply to write the change")
        return 0
    if widened and not a.allow_widening:
        print("refusing to apply: the change widens authority (pass --allow-widening after review)", file=sys.stderr)
        return 4
    with open(a.file, "w") as f:
        f.write(r["proposed"])
    print(f"wrote {a.file}")
    return 0


def main(argv=None):
    import sys as _sys
    args = list(_sys.argv[1:] if argv is None else argv)
    if args[:1] == ["mcp"]:
        from .mcp_server import main as mcp_main
        return mcp_main(args[1:])
    ap = argparse.ArgumentParser(prog="remit", description="Remit: checked plans for model-written programs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, many=False):
        p.add_argument("--host", help="host interface (.rmti); default: nearest host.rmti")
        return p

    p = common(sub.add_parser("check", help="type-check, compute the manifest, compare with policy"))
    p.add_argument("files", nargs="+")
    p.add_argument("--policy")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)

    p = common(sub.add_parser("build", help="emit typed IR and manifest"))
    p.add_argument("file")
    p.add_argument("--out", default="build")
    p.set_defaults(fn=cmd_build)

    for name, fn, help_ in (("run", cmd_run, "run a program"), ("resume", cmd_resume, "resume a run after approval")):
        p = common(sub.add_parser(name, help=help_))
        if name == "run":
            p.add_argument("file")
            p.add_argument("--input", "-i", action="append", help="k=v (JSON value or string)")
            p.add_argument("--inputs", help="JSON file of inputs")
        else:
            p.add_argument("run", help="run id or trace path")
            p.add_argument("--file", help="program path (default: the one recorded)")
        p.add_argument("--policy")
        p.add_argument("--allow-all", action="store_true", help="grant every host capability (development only)")
        p.add_argument("--live", action="store_true", help="use live adapters (adapters.py, Claude CLI)")
        p.add_argument("--fixtures", help="fixture file (default: fixtures/run.json)")
        p.add_argument("--interactive", action="store_true", help="prompt for approvals on the terminal")
        p.add_argument("--approve-all", action="store_true", help="grant every approval (development/fixtures only)")
        p.set_defaults(fn=fn)

    p = common(sub.add_parser("replay", help="re-execute a run from its trace without live effects"))
    p.add_argument("run")
    p.add_argument("--file")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("approve", help="approve a pending call by digest (single use)")
    p.add_argument("digest")
    p.add_argument("--by")
    p.set_defaults(fn=cmd_approve)

    p = common(sub.add_parser("test", help="run test blocks"))
    p.add_argument("paths", nargs="*")
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("fmt", help="format .rmt and .rmti files")
    p.add_argument("files", nargs="+")
    p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_fmt)

    p = common(sub.add_parser("diff", help="report authority widening between two versions"))
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_diff)

    p = common(sub.add_parser("ask", help="have a model propose a checked change (dry run unless --apply)"))
    p.add_argument("request")
    p.add_argument("file")
    p.add_argument("--policy")
    p.add_argument("--spec")
    p.add_argument("--model", default=os.environ.get("REMIT_MODEL", "claude-haiku-4-5-20251001"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--allow-widening", action="store_true")
    p.set_defaults(fn=cmd_ask)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
