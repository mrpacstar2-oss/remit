"""Python baseline for examples/invoices/invoices.rmt."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from baselines.runtime import Runtime


class Invoice(BaseModel):
    vendor: str
    invoice_no: str
    amount: float
    currency: Literal["EUR", "GBP", "USD"]
    iban_on_invoice: str


def main(rt: Runtime) -> list[dict]:
    outcomes = [process(rt, path) for path in rt.call("inbox.list", "main#0", folder="inbox")]
    lines = [f"{o['status']}\t{o['path']}\t{o['detail']}" for o in outcomes]
    rt.call("report.write", "main#1", name="intake-report.tsv", content="\n".join(lines))
    return outcomes


def process(rt: Runtime, path: str) -> dict:
    text = rt.call("inbox.read", "process#0", path=path)
    inv = rt.ask("llm", "process#1", Invoice, 'Extract the invoice fields. Use "" for a missing IBAN.',
                 context=text, retries=1)
    vendor = rt.call("vendors.lookup", "process#2", name=inv.vendor)
    if inv.iban_on_invoice != "" and inv.iban_on_invoice.replace(" ", "") != vendor["iban"]:
        rt.call("review.queue", "process#3", path=path, reason="bank details on the invoice differ from the vendor master")
        return {"path": path, "status": "review", "detail": "bank details differ"}
    if inv.amount > vendor["max_auto_pay"]:
        rt.call("review.queue", "process#4", path=path, reason="amount above the vendor's automatic limit")
        return {"path": path, "status": "review", "detail": f"amount {inv.amount} over limit"}
    # Bank details always come from the vendor master, never from the document.
    rt.call("payments.schedule", "process#5", vendor_id=vendor["id"], iban=vendor["iban"], amount=inv.amount,
            reference=inv.invoice_no)
    return {"path": path, "status": "scheduled", "detail": f"{inv.amount} {inv.currency}"}
