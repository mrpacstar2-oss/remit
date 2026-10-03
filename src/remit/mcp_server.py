"""MCP server exposing Remit to AI agents.

Local (free, stdio):     remit mcp
Hosted (HTTP, API keys): remit mcp --http --port 8000 --keys keys.json --usage usage.sqlite

Tools are pure and offline: they parse, check, format, diff and run programs on caller-supplied fixtures.
Nothing here performs live capability calls, so a hosted instance never acts on anyone's behalf.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from importlib import resources
from typing import Optional

from .broker import ApproveAll, Broker, Policy, Trace, host_spec
from .checker import check
from .cli import authority_diff, summary
from .errors import RemitError
from .formatter import format_program
from .interpreter import Interpreter
from .ir import program_hash
from .parser import parse_host, parse_program
from .project import fixture_adapters, policy_diagnostics
from .rterrors import RemitRuntimeError
from .values import unwrap

INSTRUCTIONS = (
    "Remit is a small checked language for programs that call tools and models. Start with remit_guide. "
    "Write a .rmt program against the host interface (host.rmti) you are given, then call remit_check and fix every "
    "error before finishing. When editing an existing program, call remit_authority_diff and do not widen "
    "authority unless the user asked for it. remit_examples returns complete working programs.")

MAX_SRC = 200_000


def _data(name: str) -> str:
    return resources.files("remit").joinpath("data", name).read_text()


def _examples() -> dict:
    root = resources.files("remit").joinpath("data", "examples")
    out = {}
    for d in root.iterdir():
        if d.is_dir():
            out[d.name] = {f.name: f.read_text() for f in d.iterdir() if f.is_file()}
    return out


def _too_big(*xs):
    return any(x is not None and len(x) > MAX_SRC for x in xs)


def _parse(program: str, host: str):
    h = parse_host(host, "host.rmti")
    p = parse_program(program, "program.rmt")
    return h, p


def tool_guide(section: str = "guide") -> str:
    """Return the quick guide (default) or the full language specification (section='spec')."""
    return _data("LANGUAGE_SPEC.md") if section == "spec" else _data("AI_GUIDE.md")


def tool_check(program: str, host: str, policy: Optional[str] = None) -> dict:
    """Type-check a program against a host interface (and optional policy TOML). Returns errors with locations and
    hints, and on success the authority manifest (resources, worst-case calls and cost, approval sites, flows)."""
    if _too_big(program, host, policy):
        return {"ok": False, "errors": ["input too large"]}
    try:
        h, p = _parse(program, host)
    except RemitError as e:
        src = program if e.diag.span and e.diag.span.file == "program.rmt" else host
        return {"ok": False, "errors": [e.diag.render({e.diag.span.file: src} if e.diag.span else None)]}
    c = check(p, h)
    diags = list(c.diagnostics)
    if c.ok and policy:
        pol = _policy_from_text(policy)
        diags += policy_diagnostics(c, pol)
    errs = [d for d in diags if d.severity == "error"]
    warns = [d for d in diags if d.severity == "warning"]
    out = {"ok": not errs, "errors": [d.render({"program.rmt": program}) for d in errs],
           "warnings": [d.render({"program.rmt": program}) for d in warns]}
    if not errs:
        out["summary"] = summary(c.manifest)
        out["manifest"] = c.manifest
        out["program_hash"] = program_hash(p)
    return out


def _policy_from_text(text: str) -> Policy:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return Policy.load(path)
    finally:
        os.unlink(path)


def tool_format(program: str) -> dict:
    """Return the program in canonical format (comments preserved)."""
    try:
        return {"ok": True, "program": format_program(parse_program(program, "program.rmt"))}
    except RemitError as e:
        return {"ok": False, "errors": [e.diag.render({"program.rmt": program})]}


def tool_authority_diff(old_program: str, new_program: str, host: str) -> dict:
    """Compare two versions of a program: does the new one gain resources, higher worst-case calls or cost, new
    tagged data flows or new approval sites? Use this after editing a program."""
    try:
        h = parse_host(host, "host.rmti")
        old = check(parse_program(old_program, "old.rmt"), h)
        new = check(parse_program(new_program, "new.rmt"), h)
    except RemitError as e:
        return {"ok": False, "errors": [e.diag.render()]}
    if not new.ok:
        return {"ok": False, "errors": [d.render({"new.rmt": new_program}) for d in new.errors()]}
    if not old.ok:
        return {"ok": False, "errors": ["the old program does not check; cannot compare"]}
    w = authority_diff(old.manifest, new.manifest)
    return {"ok": True, "widened": bool(w), "changes": w}


def tool_examples(name: Optional[str] = None) -> dict:
    """List example apps, or return one (program, host.rmti, policy.toml) by name."""
    ex = _examples()
    if not name:
        return {"examples": sorted(ex)}
    if name not in ex:
        return {"error": f"unknown example {name!r}", "examples": sorted(ex)}
    return ex[name]


def tool_run_fixtures(program: str, host: str, fixtures: str, inputs: str = "{}", policy: Optional[str] = None) -> dict:
    """Run a checked program OFFLINE on JSON fixtures (canned capability and model outputs). Approvals are
    auto-granted. Returns the result and the list of calls made. No real capability is ever invoked."""
    if _too_big(program, host, fixtures, inputs, policy):
        return {"ok": False, "errors": ["input too large"]}
    chk = tool_check(program, host, policy)
    if not chk["ok"]:
        return chk
    h, p = _parse(program, host)
    c = check(p, h)
    specs = host_spec(h)
    pol = _policy_from_text(policy) if policy else Policy.allow_all(specs)
    try:
        fx = json.loads(fixtures)
        inp = json.loads(inputs or "{}")
    except json.JSONDecodeError as e:
        return {"ok": False, "errors": [f"invalid JSON: {e}"]}
    b = Broker(specs, fixture_adapters(fx, specs), pol, ApproveAll(), Trace(), program_hash(p), budget_usd=p.budget)
    calls = lambda: [{"resource": e["resource"], "args": e["args"], "status": e["status"],
                      **({"approval_needed": True} if e.get("approval") else {})}
                     for e in b.trace.events if e.get("event") == "call"]
    deadline = time.time() + 10
    try:
        out = Interpreter(c, b).run_main(inp)
        return {"ok": True, "result": unwrap(out), "calls": calls()}
    except RemitRuntimeError as e:
        return {"ok": False, "runtime_error": e.render(), "calls": calls()}


# ---------------------------------------------------------------- server construction
def build_server():
    from mcp.server.mcpserver import MCPServer
    srv = MCPServer(name="remit", title="Remit language tools", instructions=INSTRUCTIONS,
                    website_url=os.environ.get("REMIT_WEBSITE_URL"), version="0.1.0")
    srv.tool(name="remit_guide", description=tool_guide.__doc__)(tool_guide)
    srv.tool(name="remit_check", description=tool_check.__doc__)(tool_check)
    srv.tool(name="remit_format", description=tool_format.__doc__)(tool_format)
    srv.tool(name="remit_authority_diff", description=tool_authority_diff.__doc__)(tool_authority_diff)
    srv.tool(name="remit_examples", description=tool_examples.__doc__)(tool_examples)
    srv.tool(name="remit_run_fixtures", description=tool_run_fixtures.__doc__)(tool_run_fixtures)

    @srv.resource("remit://guide", name="guide", description="Quick guide for AI agents", mime_type="text/markdown")
    def guide_res() -> str:
        return _data("AI_GUIDE.md")

    @srv.resource("remit://spec", name="spec", description="Full language specification", mime_type="text/markdown")
    def spec_res() -> str:
        return _data("LANGUAGE_SPEC.md")
    return srv


# ---------------------------------------------------------------- hosted mode: API keys + usage metering
class Usage:
    """Per-key, per-month request counter in SQLite (the basis for metered billing)."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        with sqlite3.connect(path) as db:
            db.execute("create table if not exists usage (key_id text, month text, requests integer, "
                       "primary key (key_id, month))")

    def add(self, key_id: str) -> int:
        month = time.strftime("%Y-%m")
        with self.lock, sqlite3.connect(self.path) as db:
            db.execute("insert into usage values (?, ?, 1) on conflict(key_id, month) do update set "
                       "requests = requests + 1", (key_id, month))
            return db.execute("select requests from usage where key_id=? and month=?", (key_id, month)).fetchone()[0]

    def report(self) -> list:
        with sqlite3.connect(self.path) as db:
            return db.execute("select key_id, month, requests from usage order by month, key_id").fetchall()


def load_keys(path: str) -> dict:
    """keys.json: {"keys": [{"id": "acme", "sha256": "<hex of the key>", "monthly_limit": 10000}]}"""
    with open(path) as f:
        d = json.load(f)
    return {k["sha256"]: k for k in d["keys"]}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def make_http_app(keys_path: Optional[str], usage_path: Optional[str], free_monthly: int = 0):
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse
    srv = build_server()

    @srv.custom_route("/api/check", methods=["POST"])
    async def api_check(request):
        """Website demo endpoint: check a program (no execution). Metered like /mcp."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "errors": ["send JSON: {\"program\": ..., \"example\": ...}"]}, 400)
        program = body.get("program", "")
        host = body.get("host")
        policy = body.get("policy")
        ex = body.get("example")
        if ex:
            files = _examples().get(ex)
            if not files:
                return JSONResponse({"ok": False, "errors": [f"unknown example {ex!r}"]}, 400)
            host = files.get("host.rmti")
            policy = files.get("policy.toml")
        if not isinstance(program, str) or not isinstance(host, str):
            return JSONResponse({"ok": False, "errors": ["program and host (or example) are required"]}, 400)
        res = tool_check(program, host, policy)
        res.pop("manifest", None) if not body.get("manifest") else None
        return JSONResponse(res)

    app = srv.streamable_http_app()
    keys = load_keys(keys_path) if keys_path else None
    usage = Usage(usage_path) if usage_path else None

    class Gate(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path in ("/health", "/healthz"):
                return PlainTextResponse("ok")
            client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
                (request.client.host if request.client else "unknown")
            key_id = f"anon:{client_ip}"   # anonymous use is metered per client IP
            limit = free_monthly
            if keys is not None:
                auth = request.headers.get("authorization", "")
                token = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
                rec = keys.get(hash_key(token)) if token else None
                if rec is None:
                    if not free_monthly:
                        return JSONResponse({"error": "missing or invalid API key"}, status_code=401)
                else:
                    key_id, limit = rec["id"], rec.get("monthly_limit", 0)
            if usage is not None and request.method == "POST" and request.url.path in ("/mcp", "/mcp/", "/api/check"):
                n = usage.add(key_id)
                if limit and n > limit:
                    return JSONResponse({"error": f"monthly limit of {limit} requests reached for {key_id}"},
                                        status_code=429)
            return await call_next(request)
    app.add_middleware(Gate)
    site_dir = os.environ.get("REMIT_SITE_DIR")
    if site_dir and os.path.isdir(site_dir):
        # Simple mode: serve the website (index.html, llms.txt, docs) from the same port as /mcp.
        from starlette.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=site_dir, html=True), name="site")
    return app


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="remit mcp")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--keys", help="keys.json (hosted mode: require API keys)")
    ap.add_argument("--usage", help="usage.sqlite (hosted mode: meter requests per key)")
    ap.add_argument("--free-monthly", type=int, default=0, help="requests per month allowed without a key (0 = none)")
    a = ap.parse_args(argv)
    if not a.http:
        build_server().run("stdio")
        return 0
    import uvicorn
    uvicorn.run(make_http_app(a.keys, a.usage, a.free_monthly), host=a.host, port=a.port, log_level="info")
    return 0
