"""Live adapters for package research (trusted host code).

Live: pypi.org JSON API and raw.githubusercontent.com, both reachable from the development sandbox.
The adapter enforces its own host allowlist in addition to the deployment policy.
"""
import json

from remit.rterrors import CapabilityFailed
from remit.stdadapters import http_get

ALLOW = ["pypi.org", "raw.githubusercontent.com"]


def _pypi(name):
    if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise CapabilityFailed(f"invalid package name {name!r}")
    return json.loads(http_get(f"https://pypi.org/pypi/{name}/json", ALLOW, max_bytes=30_000_000))


def make_adapters(app_dir):
    def info(args, ctx):
        d = _pypi(args["name"])
        i = d["info"]
        files = d.get("urls") or []
        urls = i.get("project_urls") or {}
        src = ""
        for key in ("Source", "Source Code", "Repository", "Code", "GitHub", "Homepage", "Home"):
            if urls.get(key) and "github.com" in urls[key]:
                src = urls[key]
                break
        if not src and i.get("home_page") and "github.com" in i["home_page"]:
            src = i["home_page"]
        return {"name": i["name"], "version": i["version"], "summary": i.get("summary") or "",
                "released": (files[0]["upload_time"][:10] if files else ""), "source_url": src.rstrip("/"),
                "requires_python": i.get("requires_python") or "",
                "license": (i.get("license_expression") or i.get("license") or "")[:80]}

    def releases(args, ctx):
        d = _pypi(args["name"])
        rel = [(fs[0]["upload_time"][:10], v) for v, fs in d["releases"].items() if fs]
        rel.sort(reverse=True)
        return [{"version": v, "date": t} for t, v in rel[:20]]

    def readme(args, ctx):
        repo = args["repo"]
        if repo.count("/") != 1:
            raise CapabilityFailed(f"repo must be owner/name, got {repo!r}")
        for branch in ("main", "master"):
            try:
                return http_get(f"https://raw.githubusercontent.com/{repo}/{branch}/README.md", ALLOW)[:20000]
            except CapabilityFailed:
                continue
        raise CapabilityFailed(f"no README.md found for {repo}")

    return {"pypi.info": info, "pypi.releases": releases, "github.readme": readme}
