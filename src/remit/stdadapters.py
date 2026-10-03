"""Reusable live adapters for hosts. These are TRUSTED host code (see LANGUAGE_SPEC §11)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from .broker import AdapterResult
from .rterrors import CapabilityFailed, Timeout


def http_get(url: str, allow_hosts: list[str], timeout: float = 20.0, max_bytes: int = 400_000) -> str:
    """GET with a host allowlist enforced in the adapter (defence in depth beyond policy arg_prefix)."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname not in allow_hosts:
        raise CapabilityFailed(f"host {u.hostname!r} is not in this adapter's allowlist")
    req = urllib.request.Request(url, headers={"User-Agent": "remit-prototype/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise CapabilityFailed(f"HTTP {e.code} for {url}")
    except (urllib.error.URLError, OSError) as e:
        if "timed out" in str(e):
            raise Timeout(f"timeout fetching {url}")
        raise CapabilityFailed(f"network error for {url}: {e}")


class RootedFiles:
    """Read/list files under a fixed root; paths cannot escape it."""

    def __init__(self, root: str):
        self.root = os.path.realpath(root)

    def resolve(self, rel: str) -> str:
        p = os.path.realpath(os.path.join(self.root, rel))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise CapabilityFailed(f"path {rel!r} escapes the allowed root")
        return p

    def list(self, rel: str) -> list[str]:
        d = self.resolve(rel)
        if not os.path.isdir(d):
            raise CapabilityFailed(f"{rel!r} is not a directory")
        return sorted(os.path.relpath(os.path.join(d, f), self.root) for f in os.listdir(d)
                      if os.path.isfile(os.path.join(d, f)))

    def read(self, rel: str, max_bytes: int = 200_000) -> str:
        p = self.resolve(rel)
        if not os.path.isfile(p):
            raise CapabilityFailed(f"no such file {rel!r}")
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)

    def write(self, rel: str, content: str):
        p = self.resolve(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)


class Ledger:
    """Append-only JSONL ledger that de-duplicates by idempotency key (so a resumed run cannot double-post)."""

    def __init__(self, path: str):
        self.path = path

    def append(self, record: dict, key: str) -> dict:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path) as f:
                for line in f:
                    r = json.loads(line)
                    if r.get("idempotency_key") == key:
                        return {**r, "deduplicated": True}
        r = {**record, "idempotency_key": key}
        with open(self.path, "a") as f:
            f.write(json.dumps(r, sort_keys=True) + "\n")
        return r


def run_pytest(cwd: str, timeout: float = 120.0) -> dict:
    """Run pytest in `cwd`. WARNING: executes repository code on the host without OS isolation."""
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                           cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Timeout(f"tests timed out after {timeout}s")
    out = (p.stdout + p.stderr)[-4000:]
    return {"passed": p.returncode == 0, "output": out}
