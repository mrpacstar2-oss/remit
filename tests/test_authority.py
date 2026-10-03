from remit.cli import authority_diff
from tests.conftest import codes


BASE = """program t
uses web.fetch, crm.find, email.send
fn main(topic: Str) {
  let c = crm.find("a@b")
  for i in range(2) { web.fetch("https://x") }
  email.send(to: c.email, subject: "s", body: "fixed")
}
"""


def test_no_widening_for_identical_programs(checkp):
    m = checkp(BASE).manifest
    assert authority_diff(m, m) == []


def test_widening_detected_for_more_calls_new_flows_and_resources(checkp):
    old = checkp(BASE).manifest
    more = checkp(BASE.replace("range(2)", "range(5)")).manifest
    assert any("web.fetch" in w and "2 -> 5" in w for w in authority_diff(old, more))
    flow = checkp(BASE.replace('body: "fixed"', "body: topic")).manifest
    w = authority_diff(old, flow)
    assert any("email.send.body" in x for x in w) and any("approval sites" in x for x in w)
    new = checkp(BASE.replace("uses web.fetch, crm.find, email.send", "uses web.fetch, crm.find, email.send, files.read")
                 .replace('  let c = crm.find("a@b")', '  let c = crm.find("a@b")\n  let d = files.read("x")')).manifest
    assert any("NEW resource 'files.read'" in x for x in authority_diff(old, new))


def test_narrowing_is_not_widening(checkp):
    old = checkp(BASE).manifest
    less = checkp(BASE.replace("range(2)", "range(1)")).manifest
    assert authority_diff(old, less) == []
