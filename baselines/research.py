"""Python baseline for examples/research/research.rmt."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

from baselines.runtime import Runtime
from baselines.util import call, repo_of


class Finding(BaseModel):
    package: str
    verdict: Literal["adopt", "trial", "avoid"]
    reasons: List[str] = Field(max_length=4)
    evidence: List[str] = Field(max_length=4)


class Report(BaseModel):
    question: str
    findings: List[Finding] = Field(max_length=3)
    recommendation: str


def main(rt: Runtime, question: str, packages: List[str]) -> Report:
    findings = [assess(rt, question, name) for name in packages]
    rec = rt.ask("llm", "main#0", str, f"In two sentences, answer: {question}. Use only the findings.",
                 context=[f.model_dump() for f in findings], retries=1)
    return Report(question=question, findings=findings, recommendation=rec)


def assess(rt: Runtime, question: str, name: str) -> Finding:
    info = call(rt, "pypi.info", "assess#0", retry=1, name=name)
    releases = call(rt, "pypi.releases", "assess#1", retry=1, fallback=[], name=name)[:5]
    readme = ""
    if "github.com/" in info["source_url"]:
        readme = call(rt, "github.readme", "assess#2", retry=1, fallback="", repo=repo_of(info["source_url"]))
    excerpt = readme.split("\n")[:60]
    facts = {"info": info, "recent_releases": releases, "readme_excerpt": "\n".join(excerpt)}
    return rt.ask("llm", "assess#3", Finding,
                  f"Assess package {name} for this question: {question}. "
                  "Cite evidence as short quotes or metadata fields from the context.",
                  context=facts, retries=1)
