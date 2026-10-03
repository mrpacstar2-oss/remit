"""Live adapters for invoice intake (trusted host code).

Live: reading files from data/inbox, the model (Claude CLI). LOCAL SIMULATION: the vendor master is
data/vendors.json; payments, the review queue and the report are files under data/out/. No bank is contacted.
"""
import json
import os

from remit.rterrors import CapabilityFailed
from remit.stdadapters import Ledger, RootedFiles


def make_adapters(app_dir):
    data = os.path.join(app_dir, "data")
    files = RootedFiles(data)
    out = RootedFiles(os.path.join(data, "out"))
    with open(os.path.join(data, "vendors.json")) as f:
        vendors = json.load(f)["vendors"]
    payments = Ledger(os.path.join(data, "out", "payments.jsonl"))
    review = Ledger(os.path.join(data, "out", "review.jsonl"))

    def lookup(args, ctx):
        for v in vendors:
            if v["name"].lower() == args["name"].strip().lower():
                return v
        raise CapabilityFailed(f"unknown vendor {args['name']!r}")

    return {
        "inbox.list": lambda a, c: files.list(a["folder"]),
        "inbox.read": lambda a, c: files.read(a["path"]),
        "vendors.lookup": lookup,
        "payments.schedule": lambda a, c: payments.append({"type": "payment", **a}, c.idempotency_key) and None,
        "review.queue": lambda a, c: review.append({"type": "review", **a}, c.idempotency_key) and None,
        "report.write": lambda a, c: out.write(a["name"], a["content"]),
    }
