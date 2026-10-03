from tests.conftest import codes


def P(body, header="uses web.search, web.fetch, files.read, crm.find, email.send, pay.refund, llm", extra=""):
    return f"program t\n{header}\n{extra}\nfn main(topic: Str) {{\n{body}\n}}\n"


def test_ok_program_manifest(checkp):
    r = checkp("""program t
uses web.search, web.fetch, llm
budget usd 0.1
type Brief = { summary: Str }
fn main(topic: Str) -> Brief {
  let hits = web.search(topic) retry 1
  let pages = for h in take(hits, 4) { web.fetch(h.url) retry 2 else "" }
  return llm.ask(Brief, "sum", context: pages)
}
""")
    assert r.ok, [d.render() for d in r.diagnostics]
    m = r.manifest
    assert m["resources"]["web.search"]["max_calls"] == 2
    assert m["resources"]["web.fetch"]["max_calls"] == 12
    assert m["resources"]["llm"]["max_calls"] == 1
    assert abs(m["worst_case_cost_usd"] - (2 * 0.005 + 12 * 0.001 + 0.02)) < 1e-9


def test_undeclared_capability(checkp):
    r = checkp(P("web.fetch(topic)", header="uses web.search"))
    assert "E0201" in codes(r)


def test_unknown_capability_suggests(checkp):
    r = checkp(P("web.fetc(topic)"))
    d = [d for d in r.diagnostics if d.code == "E0202"][0]
    assert "web.fetch" in d.hint


def test_unused_uses_warns(checkp):
    r = checkp(P("let x = 1"))
    assert any(d.code == "W0201" for d in r.diagnostics)


def test_private_to_public_sink_denied(checkp):
    r = checkp(P('let d = files.read("a")\nweb.fetch("https://x/?q={d}")'))
    assert codes(r) == ["E0301"]


def test_untrusted_recipient_denied(checkp):
    r = checkp(P('let p = web.fetch("https://x")\nemail.send(to: p, subject: "s", body: "b")'))
    assert "E0301" in codes(r)


def test_untrusted_flow_through_model_is_tracked(checkp):
    r = checkp(P('let p = web.fetch("https://x")\nlet who = llm.ask(Str, "extract the address", context: p)\n'
                 'email.send(to: who, subject: "s", body: "b")'))
    assert "E0301" in codes(r)


def test_clears_allows_crm_recipient(checkp):
    r = checkp(P('let c = crm.find(topic)\nemail.send(to: c.email, subject: "s", body: "b")'))
    assert r.ok, [d.render() for d in r.diagnostics]


def test_approve_tag_marks_site(checkp):
    r = checkp(P('let c = crm.find("a@b")\nemail.send(to: c.email, subject: "s", body: topic)'))
    assert r.ok
    sites = r.manifest["approval_sites"]
    assert len(sites) == 1 and sites[0]["resource"] == "email.send" and sites[0]["approval"] == "possible"


def test_control_flow_taint_requires_approval_not_denial(checkp):
    r = checkp(P('let c = crm.find("a@b")\nif contains(topic, "refund") { pay.refund(10.0) }\n'
                 'if topic == "x" { email.send(to: c.email, subject: "s", body: "fixed") }'))
    assert r.ok, [d.render() for d in r.diagnostics]
    res = {s["resource"]: s for s in r.manifest["approval_sites"]}
    assert res["pay.refund"]["approval"] == "always"
    assert any("control flow" in x for x in res["email.send"]["reasons"])


def test_unbounded_loop_rejected(checkp):
    r = checkp(P('for w in split(topic, " ") { web.fetch(w) }'))
    assert codes(r) == ["E0401"]
    r2 = checkp(P('for w in split(topic, " ") limit 3 { web.fetch(w) }'))
    assert r2.ok and r2.manifest["resources"]["web.fetch"]["max_calls"] == 3


def test_nested_loops_multiply(checkp):
    r = checkp(P('for a in range(3) { for b in range(4) { web.fetch("u") } }'))
    assert r.manifest["resources"]["web.fetch"]["max_calls"] == 12


def test_if_takes_max_branch(checkp):
    r = checkp(P('if topic == "a" { web.fetch("1")\nweb.fetch("2") } else { web.fetch("3") }'))
    assert r.manifest["resources"]["web.fetch"]["max_calls"] == 2


def test_function_effects_compose(checkp):
    r = checkp("""program t
uses web.fetch
fn get(u: Str) -> Str { return web.fetch(u) retry 1 }
fn main(topic: Str) { for i in range(5) { get(topic) } }
""")
    assert r.ok and r.manifest["resources"]["web.fetch"]["max_calls"] == 10


def test_recursion_rejected(checkp):
    r = checkp("program t\nfn f(n: Int) -> Int { return g(n) }\nfn g(n: Int) -> Int { return f(n) }\n"
               "fn main(topic: Str) { f(1) }\n")
    assert "E0402" in codes(r)


def test_retry_non_idempotent_rejected(checkp):
    r = checkp(P('let c = crm.find("a")\nemail.send(to: c.email, subject: "s", body: "b") retry 1'))
    assert "E0405" in codes(r)


def test_budget_exceeded(checkp):
    r = checkp("program t\nuses web.fetch\nbudget usd 0.005\nfn main(topic: Str) { for i in range(10) { web.fetch(\"u\") } }\n")
    assert codes(r) == ["E0404"]


def test_model_output_list_must_be_bounded(checkp):
    r = checkp("program t\nuses llm\ntype X = { xs: List[Str] }\nfn main(topic: Str) { llm.ask(X, \"p\") }\n")
    assert "E0113" in codes(r)


def test_list_bound_on_return(checkp):
    r = checkp("program t\nuses web.search\ntype Out = { xs: List[Hit] max 3 }\n"
               "fn main(topic: Str) -> Out { return { xs: web.search(topic) } }\n")
    assert "E0113" in codes(r)
    r2 = checkp("program t\nuses web.search\ntype Out = { xs: List[Hit] max 3 }\n"
                "fn main(topic: Str) -> Out { return { xs: take(web.search(topic), 3) } }\n")
    assert r2.ok, [d.render() for d in r2.diagnostics]


def test_match_exhaustive_and_enum_literals(checkp):
    src = """program t
uses llm
type T = { k: "a" | "b" | "c" }
fn main(topic: Str) -> Int {
  let t = llm.ask(T, "p")
  if t.k == "d" { return 0 }
  return match t.k {
    "a" => 1
    "b" => 2
  }
}
"""
    r = checkp(src)
    assert codes(r) == ["E0111", "E0111"]


def test_type_errors(checkp):
    r = checkp(P('let x: Int = "s"\nlet y = 1 + "a"\nlet z = topic.nope'))
    assert codes(r) == ["E0102", "E0103", "E0106"]


def test_field_suggestion(checkp):
    r = checkp(P('let c = crm.find("a")\nlet e = c.emial'))
    d = [d for d in r.diagnostics if d.code == "E0102"][0]
    assert "email" in d.hint


def test_let_immutable(checkp):
    r = checkp(P("let x = 1\nx = 2"))
    assert "E0108" in codes(r)


def test_agent_max_calls(checkp):
    r = checkp("""program t
uses llm
agent Researcher {
  model: llm
  system: "You research."
  max_calls: 2
}
fn main(topic: Str) { for i in range(3) { Researcher.ask(Str, "go") } }
""")
    assert "E0403" in codes(r)


def test_missing_return(checkp):
    r = checkp("program t\nfn f(x: Int) -> Int { if x > 1 { return 1 } }\nfn main(topic: Str) { f(1) }\n")
    assert "E0109" in codes(r)


def test_early_return_branches_are_alternatives(checkp):
    r = checkp("""program t
uses web.fetch
fn f(x: Str) -> Str {
  if x == "a" {
    web.fetch("1")
    return "a"
  }
  if x == "b" {
    web.fetch("2")
    return "b"
  }
  web.fetch("3")
  return "c"
}
fn main(topic: Str) { f(topic) }
""")
    assert r.ok, [d.render() for d in r.diagnostics]
    assert r.manifest["resources"]["web.fetch"]["max_calls"] == 1
