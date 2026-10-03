"""Protocol-level tests: the MCP server over stdio, and the hosted HTTP app with API keys and metering."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = open(os.path.join(ROOT, "bench", "e6_app", "host.rmti")).read()
PROG = """program p
uses expenses.get
fn main(id: Str) -> Str {
  return expenses.get(id).note
}
"""


def _text(res):
    return res.content[0].text


async def _stdio_session():
    axiom = shutil.which("remit") or os.path.join(os.path.dirname(sys.executable), "remit")
    params = StdioServerParameters(command=axiom, args=["mcp"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            chk = json.loads(_text(await s.call_tool("remit_check", {"program": PROG, "host": HOST})))
            guide = _text(await s.call_tool("remit_guide", {}))
            run = json.loads(_text(await s.call_tool("remit_run_fixtures", {
                "program": PROG, "host": HOST, "inputs": json.dumps({"id": "X"}),
                "fixtures": json.dumps({"capabilities": {"expenses.get": [{"output": {
                    "id": "X", "employee_email": "a@b", "amount": 1.0, "currency": "EUR", "note": "taxi"}}]}})})))
            res = await s.read_resource("remit://guide")
            return tools, chk, guide, run, res


def test_stdio_server_end_to_end():
    tools, chk, guide, run, res = asyncio.run(_stdio_session())
    assert tools == ["remit_authority_diff", "remit_check", "remit_examples", "remit_format", "remit_guide",
                     "remit_run_fixtures"]
    assert chk["ok"] and "expenses.get" in chk["summary"]
    assert "Remit quick guide" in guide
    assert run["ok"] and run["result"] == "taxi"
    assert "Remit quick guide" in res.contents[0].text


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_hosted_http_requires_key_and_meters(tmp_path):
    import urllib.request
    from remit.mcp_server import Usage, hash_key
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [{"id": "acme", "sha256": hash_key("sk-test"), "monthly_limit": 3}]}))
    usage = tmp_path / "usage.sqlite"
    port = _free_port()
    axiom = shutil.which("remit") or os.path.join(os.path.dirname(sys.executable), "remit")
    proc = subprocess.Popen([axiom, "mcp", "--http", "--port", str(port), "--keys", str(keys), "--usage", str(usage)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}).encode()
        hdr = {"content-type": "application/json", "accept": "application/json, text/event-stream"}

        def post(key=None):
            h = dict(hdr)
            if key:
                h["authorization"] = f"Bearer {key}"
            req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, headers=h, method="POST")
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        assert post() == 401
        assert post("sk-wrong") == 401
        assert post("sk-test") == 200
        post("sk-test")
        post("sk-test")
        assert post("sk-test") == 429  # monthly limit of 3 reached
        assert ("acme", time.strftime("%Y-%m"), 4) in Usage(str(usage)).report()
    finally:
        proc.terminate()
        proc.wait(5)
