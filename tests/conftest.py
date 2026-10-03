import pytest

from remit.parser import parse_host, parse_program
from remit.checker import check

HOST = '''
type Hit = { title: Str, url: Str }
type Customer = { id: Str, email: Str }

input topic tags untrusted
input note tags untrusted

capability web.search(query: Str deny private) -> List[Hit] max 10
  tags untrusted
  cost 0.005
  idempotent

capability web.fetch(url: Str deny private) -> Str
  tags untrusted
  cost 0.001
  idempotent

capability files.read(path: Str) -> Str
  tags private
  idempotent

capability crm.find(email: Str) -> Customer
  tags private
  clears untrusted
  idempotent

capability email.send(to: Str deny untrusted, subject: Str, body: Str approve untrusted)

capability pay.refund(amount: Float approve untrusted)
  approval always

model llm cost 0.02
'''


@pytest.fixture
def host():
    return parse_host(HOST, "host.rmti")


@pytest.fixture
def checkp(host):
    def _check(src: str):
        return check(parse_program(src, "t.rmt"), host)
    return _check


def codes(result):
    return sorted(d.code for d in result.diagnostics if d.severity == "error")
