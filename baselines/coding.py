"""Python baseline for examples/coding/fix.rmt."""
from __future__ import annotations

from pydantic import BaseModel

from baselines.runtime import Runtime
from baselines.util import call


class Patch(BaseModel):
    path: str
    find: str
    replace: str
    explanation: str


def main(rt: Runtime, task: str) -> str:
    status = "not fixed after 3 edits"
    for attempt in range(3):
        result = rt.call("tests.run", "main#0")
        if result["passed"]:
            status = f"tests pass after {attempt} edit(s)"
            break
        sources = [{"path": f, "content": rt.call("repo.read", "main#2", path=f)}
                   for f in rt.call("repo.files", "main#1") if f.endswith(".py")]
        patch = propose(rt, task, result["output"], sources)
        applied = call(rt, "repo.edit", "main#3", fallback=False, path=patch.path, find=patch.find,
                       replace=patch.replace)
        if not applied:
            status = "an edit could not be applied"
    if status.startswith("not fixed") and rt.call("tests.run", "main#4")["passed"]:
        status = "tests pass after 3 edit(s)"
    return status


def propose(rt: Runtime, task: str, output: str, sources: list[dict]) -> Patch:
    ctx = {"failing_test_output": output, "files": sources}
    return rt.ask("llm", "propose#0", Patch,
                  f"Task: {task}\nThe tests fail. Propose ONE minimal edit to a file under src/. `find` must be an "
                  "exact substring of that file (include enough lines to be unique) and `replace` its replacement. "
                  "Never edit tests.", context=ctx, retries=1)
