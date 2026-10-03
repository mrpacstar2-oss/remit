"""`remit ask`: model-proposed program changes, validated before they can be applied.

The model returns a complete revised program. The proposal is parsed, type/flow/bound-checked and compared
with the deployment policy. Checker errors are fed back for at most two repair rounds (bounded). The CLI shows
a unified diff and an authority diff (new resources, higher worst-case counts or cost, new tagged flows, new
approval sites). Nothing is written without --apply, and authority widening additionally needs
--allow-widening. Host interface and policy are never edited by this command.
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess

from .broker import Policy
from .checker import check
from .errors import RemitError
from .parser import parse_program
from .project import policy_diagnostics

SCHEMA = {"type": "object", "properties": {"program": {"type": "string"}, "explanation": {"type": "string"},
                                           "refused": {"type": "boolean"}},
          "required": ["program", "explanation", "refused"], "additionalProperties": False}

SYSTEM = ("You modify programs written in Remit, a small checked language. Return the COMPLETE revised program. "
          "You cannot change the host interface or the policy. If the request needs authority the host does not "
          "grant, or would weaken a safety property, set refused=true, return the original program unchanged and "
          "explain why.")


def _model(prompt: str, model: str, timeout: float = 300) -> dict:
    cmd = ["claude", "-p", prompt, "--model", model, "--tools", "", "--system-prompt", SYSTEM,
           "--no-session-persistence", "--json-schema", json.dumps(SCHEMA), "--output-format", "json"]
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", None)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd="/tmp", env=env)
    d = json.loads(p.stdout)
    if d.get("is_error"):
        raise RuntimeError(str(d.get("result"))[:300])
    out = d.get("structured_output") or json.loads(d["result"])
    out["_cost"] = d.get("total_cost_usd")
    return out


def validate(src: str, path: str, host, policy: Policy | None):
    try:
        prog = parse_program(src, os.path.basename(path))
    except RemitError as e:
        return None, [e.diag]
    c = check(prog, host)
    diags = [d for d in c.diagnostics if d.severity == "error"]
    if not diags and policy is not None:
        diags = policy_diagnostics(c, policy)
    return c, diags


def ask(request: str, path: str, host, host_src: str, policy: Policy | None, spec: str, model: str,
        max_repairs: int = 2) -> dict:
    with open(path) as f:
        original = f.read()
    prompt = (f"Language reference:\n<spec>\n{spec}\n</spec>\n\nHost interface (read-only):\n<host>\n{host_src}\n"
              f"</host>\n\nCurrent program ({os.path.basename(path)}):\n<program>\n{original}\n</program>\n\n"
              f"Change request: {request}")
    rounds, cost = [], 0.0
    src = original
    for i in range(max_repairs + 1):
        out = _model(prompt, model)
        cost += out.get("_cost") or 0
        src = out["program"]
        c, diags = validate(src, path, host, policy)
        rounds.append({"round": i, "errors": [d.render({os.path.basename(path): src}) for d in diags],
                       "explanation": out["explanation"], "refused": out["refused"]})
        if out["refused"] or not diags:
            break
        prompt += (f"\n\nYour previous proposal:\n<program>\n{src}\n</program>\nThe checker rejected it:\n"
                   + "\n".join(rounds[-1]["errors"]) + "\nFix the errors and return the complete program again.")
    return {"original": original, "proposed": src, "checked": c, "errors": diags, "rounds": rounds,
            "refused": rounds[-1]["refused"], "cost_usd": round(cost, 5)}


def unified(a: str, b: str, path: str) -> str:
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{path}", f"b/{path}"))
