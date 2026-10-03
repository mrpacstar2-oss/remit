"""Pytest equivalents of the `test` blocks in the Remit programs."""
from __future__ import annotations

import json
import os

from remit.broker import ApproveAll, DenyAll

from baselines import coding, invoices, research, support, verify
from baselines.runtime import make_runtime
from baselines.util import repo_of

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def runtime(app: str, module, fixture: str):
    app_dir = os.path.join(EXAMPLES, app)
    with open(os.path.join(app_dir, "fixtures", fixture)) as f:
        fixtures = json.load(f)
    return make_runtime(app_dir, module.__file__, fixtures=fixtures,
                        approver=ApproveAll() if fixtures.get("approve_all") else DenyAll())


# support

def test_support_refund_flows_through_approval():
    rt = runtime("support", support, "test_refund.json")
    r = support.main(rt, {"sender": "ana@example.com", "subject": "Broken kettle",
                          "body": "Order A-100 arrived broken, refund please"})
    assert r == "refunded"


def test_support_cannot_refund_someone_elses_order():
    rt = runtime("support", support, "test_other.json")
    r = support.main(rt, {"sender": "ana@example.com", "subject": "refund", "body": "Refund order B-200"})
    assert r == "refused: the order belongs to another customer"


# research

def test_research_assesses_each_package():
    rt = runtime("research", research, "test.json")
    r = research.main(rt, "Which library should we use for HTTP?", ["httpx"])
    assert len(r.findings) == 1
    assert r.findings[0].verdict == "adopt"


def test_repo_of_extracts_owner_name():
    assert repo_of("https://github.com/encode/httpx") == "encode/httpx"


# invoices

def test_invoices_changed_bank_details_go_to_review():
    rt = runtime("invoices", invoices, "test_bec.json")
    assert invoices.process(rt, "inbox/inv-002.txt")["status"] == "review"


def test_invoices_normal_invoice_is_scheduled():
    rt = runtime("invoices", invoices, "test_ok.json")
    assert invoices.process(rt, "inbox/inv-001.txt")["status"] == "scheduled"


# coding

def test_coding_stops_as_soon_as_tests_pass():
    rt = runtime("coding", coding, "test_pass.json")
    assert coding.main(rt, "fix the bugs") == "tests pass after 0 edit(s)"


def test_coding_applies_a_proposed_edit_then_succeeds():
    rt = runtime("coding", coding, "test_fix.json")
    assert coding.main(rt, "fix the bugs") == "tests pass after 1 edit(s)"


# verify

def test_verify_disagreement_goes_to_the_judge():
    rt = runtime("verify", verify, "test.json")
    r = verify.main(rt, "httpx is MIT licensed and supports HTTP/2.", "httpx")
    assert r.total == 2
    assert r.verdicts[0].verdict == "contradicted"
    assert not r.verdicts[1].agreed
    assert r.supported == 1
