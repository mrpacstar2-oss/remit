"""Change-review effort: which agent-written changes (E3) widen authority according to `remit diff`?

For every Axiom trial with a diff, apply it to the original program, check it, and compute the authority diff.
A change that passes the checker and does not widen authority could be merged without a separate security
review of the code; a widening change (or a failing one) needs a human. Python changes have no equivalent
mechanical signal, so every Python change needs a human read for safety.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from remit.checker import check
from remit.cli import authority_diff
from remit.parser import parse_host, parse_program
from bench import e3


def main(paths):
    rows = []
    for path in paths:
        for r in json.load(open(path)):
            if r["lang"] != "axiom" or not r.get("diff"):
                continue
            app = r["app"]
            host = parse_host(open(os.path.join(e3.EX, app, "host.rmti")).read(), "host.rmti")
            orig_path = os.path.join(e3.EX, app, e3.PROG[app])
            tmp = tempfile.mkdtemp()
            new_path = os.path.join(tmp, e3.PROG[app])
            shutil.copy(orig_path, new_path)
            subprocess.run(["patch", "-s", new_path], input=r["diff"], text=True, capture_output=True)
            old = check(parse_program(open(orig_path).read(), "o.rmt"), host)
            try:
                new = check(parse_program(open(new_path).read(), "n.rmt"), host)
                ok = new.ok
            except Exception:
                ok = False
            widened = authority_diff(old.manifest, new.manifest) if ok else None
            rows.append({"model": r["model"], "task": r["task"], "kind": r["kind"], "rep": r["rep"], "checks_ok": ok,
                         "widened": widened, "src_lines_changed": r["diff_lines"]})
            verdict = ("REJECTED by checker" if not ok else
                       ("needs review: " + "; ".join(widened)) if widened else "auto-mergeable (no widening)")
            print(f"{r['model'][:12]:<12} {r['task']} rep{r['rep']} ({r['kind']}): {verdict}")
            shutil.rmtree(tmp)
    json.dump(rows, open("bench/results/review.json", "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
