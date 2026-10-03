"""Approval burden on normal (unmutated) runs: how many calls needed a human approval per run.

Uses offline fixtures (fixtures/run.json and the test fixtures) with an approve-all approver, counting calls that
carried an approval. Compares Axiom (approval only when an `approve` tag or `approval always` applies) with the
Python baseline under the same policy and under the strict policy used in E1.
"""
import json
import os

from remit.broker import ApproveAll
from bench import e1

SCENARIOS = [
    ("support", "refund ticket", "fixtures/test_refund.json",
     {"ticket": {"sender": "ana@example.com", "subject": "Broken kettle", "body": "Order A-100 arrived broken"}}),
    ("support", "question ticket", None, None),
    ("invoices", "3 invoices", "fixtures/run.json", {}),
    ("coding", "one-edit fix", "fixtures/test_fix.json", {"task": "fix"}),
    ("research", "3 packages", "fixtures/run.json", None),
    ("verify", "2 claims", "fixtures/test.json", {"statement": "httpx is MIT licensed and supports HTTP/2.", "package": "httpx"}),
]


def count(events):
    return sum(1 for e in events if e.get("event") == "call" and e.get("approval"))


def main():
    orig = e1.DenyAll
    e1.DenyAll = ApproveAll  # count approvals instead of stopping at the first one
    rows = []
    try:
        for app, name, fixtures, inputs in SCENARIOS:
            m = {"id": name, "app": app, "unsafe": {"resource": "none", "min_calls": 1}}
            if fixtures:
                m["base_fixtures"] = os.path.join("examples", app, fixtures)
            if inputs is not None:
                m["inputs"] = inputs
            if name == "question ticket":
                m["fixtures"] = {"model": [{"match": "Classify", "output": {"intent": "question", "order_id": "", "summary": "q"}},
                                           {"match": "Write a short", "output": {"body": "hi"}}]}
                m["base_fixtures"] = os.path.join("examples", app, "fixtures/test_refund.json")
                m["inputs"] = {"ticket": {"sender": "ana@example.com", "subject": "Hours?", "body": "When are you open?"}}
            out = {}
            for k, fn in (("axiom", lambda: e1.run_axiom(m)), ("python", lambda: e1.run_python(m)),
                          ("python_strict", lambda: e1.run_python(m, strict=True))):
                captured = {}
                orig_classify = e1.classify

                def cap(mm, events, error, a, _c=captured):
                    _c["n"] = count(events)
                    _c["err"] = error
                    return orig_classify(mm, events, error, a)
                e1.classify = cap
                try:
                    fn()
                finally:
                    e1.classify = orig_classify
                out[k] = captured.get("n")
                if captured.get("err"):
                    out[k + "_error"] = captured["err"]
            rows.append({"app": app, "scenario": name, **out})
            print(f"{app:<9} {name:<16} axiom={out['axiom']}  python={out['python']}  python_strict={out['python_strict']}"
                  + "".join(f"  {k}={v}" for k, v in out.items() if k.endswith("_error")))
    finally:
        e1.DenyAll = orig
    json.dump(rows, open("bench/results/approval_burden.json", "w"), indent=1)


if __name__ == "__main__":
    main()
