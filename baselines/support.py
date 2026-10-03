"""Python baseline for examples/support/support.rmt (same broker, policy, adapters and fixtures)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from baselines.runtime import Runtime


class Triage(BaseModel):
    intent: Literal["refund", "question", "other"]
    order_id: str
    summary: str


class Reply(BaseModel):
    body: str


def main(rt: Runtime, ticket: dict) -> str:
    # The recipient comes from the CRM, never from ticket text.
    customer = rt.call("crm.find_customer", "main#0", email=ticket["sender"])
    t = rt.ask("llm", "main#1", Triage, 'Classify this support ticket. Use order_id "" if none is given.',
               context=ticket["body"], retries=1)
    if t.intent == "refund":
        outcome = refund(rt, customer, t.order_id)
    elif t.intent == "question":
        outcome = "answered"
    else:
        outcome = "escalated to a human"
    reply = rt.ask("llm", "main#2", Reply,
                   f"Write a short, polite reply to {customer['name']}. Outcome: {outcome}",
                   context=t.summary, retries=1)
    rt.call("email.send", "main#3", to=customer["email"], subject=f"Re: {ticket['subject']}", body=reply.body)
    return outcome


def refund(rt: Runtime, c: dict, order_id: str) -> str:
    order = rt.call("crm.order", "refund#0", order_id=order_id)
    if order["customer_id"] != c["id"]:
        return "refused: the order belongs to another customer"
    if order["amount"] > 200:
        return "escalated: refund above the automatic limit"
    rt.call("payments.refund", "refund#1", order_id=order["id"], amount=order["amount"])
    return "refunded"
