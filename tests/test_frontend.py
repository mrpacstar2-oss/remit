import pytest

from remit.errors import RemitError
from remit.formatter import format_program, format_host
from remit.ir import from_json, program_hash, to_json
from remit.lexer import lex
from remit.parser import parse_program, parse_host
from tests.conftest import HOST


def test_lexer_durations_and_strings():
    toks = lex('timeout 500ms 2s "a {b} \\{c\\}"', "x")
    kinds = [(t.kind, t.value) for t in toks if t.kind not in ("NEWLINE", "EOF")]
    assert kinds[1] == ("DURATION", 0.5)
    assert kinds[2] == ("DURATION", 2.0)
    assert kinds[3][0] == "STR" and kinds[3][1][0] == "a " and kinds[3][1][2] == " {c}"


def test_unterminated_string_has_location():
    with pytest.raises(RemitError) as e:
        lex('let x = "abc\n', "f.rmt")
    assert e.value.diag.span.line == 1 and "unterminated" in e.value.diag.message


def test_program_header_required():
    with pytest.raises(RemitError) as e:
        parse_program("fn main() {}\n", "f.rmt")
    assert "must start with 'program" in e.value.diag.message


def test_parse_errors_are_specific():
    with pytest.raises(RemitError) as e:
        parse_program("program p\nfn main() {\n  let x = (1 + \n}\n", "f.rmt")
    assert e.value.diag.span.line >= 3
    with pytest.raises(RemitError) as e:
        parse_program("program p\nfn main() { let x = 1 < 2 < 3 }\n", "f.rmt")
    assert "chained" in e.value.diag.message


SRC = '''# header comment
program demo
uses web.search, llm
budget usd 0.5

type Brief = { summary: Str, claims: List[Str] max 8 }

# entry point
fn main(topic: Str) -> Brief {
  let hits = take(web.search(topic) retry 2, 5)  # bounded
  let pages = for h in hits limit 3 if contains(h.title, "x") {
    h.url
  }
  let k = match "a" {
    "a" => 1
    _ => 2
  }
  return llm.ask(Brief, "Summarise {topic} \\{literal\\}", context: pages) timeout 30s else { summary: "", claims: [] }
}
'''


def test_format_is_idempotent_and_preserves_comments():
    p = parse_program(SRC, "d.rmt")
    out = format_program(p)
    assert "# header comment" in out and "# entry point" in out and "# bounded" in out
    assert format_program(parse_program(out, "d.rmt")) == out


def test_format_preserves_semantics():
    p1 = parse_program(SRC, "d.rmt")
    p2 = parse_program(format_program(p1), "d.rmt")
    assert program_hash(p1) == program_hash(p2)


def test_host_format_roundtrip():
    h = parse_host(HOST, "h.rmti")
    out = format_host(h)
    assert format_host(parse_host(out, "h.rmti")) == out


def test_ir_roundtrip():
    p = parse_program(SRC, "d.rmt")
    q = from_json(to_json(p))
    assert program_hash(p) == program_hash(q)
    assert format_program(q) == format_program(p)


def test_hash_changes_with_semantics():
    p1 = parse_program(SRC, "d.rmt")
    p2 = parse_program(SRC.replace("limit 3", "limit 4"), "d.rmt")
    assert program_hash(p1) != program_hash(p2)


def test_structured_patch_replace_insert_delete():
    from remit.patch import apply_patch
    p = parse_program(SRC, "d.rmt")
    q = apply_patch(p, [
        {"op": "replace", "name": "Brief", "source": "type Brief = { summary: Str, claims: List[Str] max 3 }"},
        {"op": "insert", "after": "main", "source": "fn helper(x: Str) -> Str {\n  return lower(x)\n}"},
        {"op": "header", "uses": ["web.search", "llm"], "budget": 0.25},
    ])
    out = format_program(q)
    assert "max 3" in out and "fn helper" in out and "budget usd 0.25" in out
    assert "# entry point" in out  # untouched declarations keep their comments
    r = apply_patch(q, [{"op": "delete", "name": "helper"}])
    assert "fn helper" not in format_program(r)


def test_structured_patch_errors_are_located():
    from remit.patch import apply_patch
    p = parse_program(SRC, "d.rmt")
    with pytest.raises(RemitError) as e:
        apply_patch(p, [{"op": "replace", "name": "nope", "source": "type X = Int"}])
    assert "no declaration named" in e.value.diag.message
    with pytest.raises(RemitError):
        apply_patch(p, [{"op": "replace", "name": "Brief", "source": "type Brief = {"}])
