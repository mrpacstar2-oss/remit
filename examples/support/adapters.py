"""Live adapters for the support example (trusted host code).

What is live: the model (Claude CLI, configured by the runtime). What is LOCAL SIMULATION: the CRM is
data/crm.json, payments are appended to data/ledger.jsonl, and email goes to data/outbox.jsonl. No real
payment or email system is contacted.
"""
import json
import os

from remit.broker import AdapterResult
from remit.rterrors import CapabilityFailed
from remit.stdadapters import Ledger


def make_adapters(app_dir):
    with open(os.path.join(app_dir, "data", "crm.json")) as f:
        crm = json.load(f)
    payments = Ledger(os.path.join(app_dir, "data", "ledger.jsonl"))
    outbox = Ledger(os.path.join(app_dir, "data", "outbox.jsonl"))

    def find_customer(args, ctx):
        for c in crm["customers"]:
            if c["email"].lower() == args["email"].lower():
                return c
        raise CapabilityFailed(f"no customer with email {args['email']!r}")

    def order(args, ctx):
        for o in crm["orders"]:
            if o["id"] == args["order_id"]:
                return o
        raise CapabilityFailed(f"no order {args['order_id']!r}")

    def refund(args, ctx):
        payments.append({"type": "refund", **args}, ctx.idempotency_key)
        return None

    def send(args, ctx):
        outbox.append({"type": "email", **args}, ctx.idempotency_key)
        return None

    return {"crm.find_customer": find_customer, "crm.order": order, "payments.refund": refund, "email.send": send}
