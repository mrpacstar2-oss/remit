"""Python baseline for examples/verify/verify.rmt."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

from baselines.runtime import Runtime
from baselines.util import call, repo_of

EXTRACTOR = "You extract short, independently checkable factual claims. You never add claims of your own."
VERIFIER = ("You check one claim strictly against the evidence provided. Quote the evidence. "
            "If it is not in the evidence, the verdict is unsupported.")
AUDITOR = ("You are a sceptical auditor. Check one claim against the evidence only. "
           "Prefer unsupported over guessing. Quote the evidence.")
JUDGE = "You resolve disagreements between two checkers by re-reading the evidence. Quote it."

Verdicts = Literal["supported", "unsupported", "contradicted"]


class Claims(BaseModel):
    claims: List[str] = Field(max_length=5)


class Check(BaseModel):
    verdict: Verdicts
    quote: str


class Verdict(BaseModel):
    claim: str
    verdict: Verdicts
    agreed: bool
    quote: str


class Report(BaseModel):
    package: str
    verdicts: List[Verdict] = Field(max_length=5)
    supported: int
    total: int


def main(rt: Runtime, statement: str, package: str) -> Report:
    info = call(rt, "pypi.info", "main#0", retry=1, name=package)
    releases = call(rt, "pypi.releases", "main#1", retry=1, fallback=[], name=package)[:8]
    readme = ""
    if "github.com/" in info["source_url"]:
        readme = call(rt, "github.readme", "main#2", retry=1, fallback="", repo=repo_of(info["source_url"]))
    evidence = {"pypi": info, "recent_releases": releases, "readme_start": "\n".join(readme.split("\n")[:80])}
    claims = rt.ask("llm", "main#3", Claims,
                    f"List the factual claims this statement makes about the package {package}.",
                    context=statement, system=EXTRACTOR, agent="Extractor")
    verdicts = [check(rt, c, evidence) for c in claims.claims]
    supported = [v for v in verdicts if v.verdict == "supported"]
    return Report(package=package, verdicts=verdicts, supported=len(supported), total=len(verdicts))


def check(rt: Runtime, claim: str, evidence: dict) -> Verdict:
    a = rt.ask("llm", "check#0", Check, f"Claim: {claim}", context=evidence, system=VERIFIER, agent="Verifier")
    b = rt.ask("llm", "check#1", Check, f"Claim: {claim}", context=evidence, system=AUDITOR, agent="Auditor")
    if a.verdict == b.verdict:
        return Verdict(claim=claim, verdict=a.verdict, agreed=True, quote=a.quote)
    j = rt.ask("llm", "check#2", Check,
               f"Claim: {claim}\nChecker A said {a.verdict} (quote: {a.quote}). "
               f"Checker B said {b.verdict} (quote: {b.quote}).",
               context=evidence, system=JUDGE, agent="Judge")
    return Verdict(claim=claim, verdict=j.verdict, agreed=False, quote=j.quote)
