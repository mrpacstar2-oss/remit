"""Regenerate the AI-facing files of the website from the docs (run before deploying).

Outputs: llms.txt (index for AI agents, llmstxt.org format), llms-full.txt (guide + spec + an example in one file),
guide.md and spec.md (raw Markdown), robots.txt, sitemap.xml. Set SITE_URL to the public URL.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.dirname(os.path.abspath(__file__))
URL = os.environ.get("SITE_URL", "https://example.com").rstrip("/")
NAME = os.environ.get("SITE_NAME", "Remit")


def read(p):
    return open(os.path.join(ROOT, p)).read()


guide, spec = read("docs/AI_GUIDE.md"), read("docs/LANGUAGE_SPEC.md")
example = read("examples/invoices/invoices.rmt")
host = read("examples/invoices/host.rmti")

open(os.path.join(OUT, "guide.md"), "w").write(guide)
open(os.path.join(OUT, "spec.md"), "w").write(spec)

open(os.path.join(OUT, "llms.txt"), "w").write(f"""# {NAME}

> A small, statically checked language for programs written by AI agents that call tools and models. Before a
> program runs, the checker reports which capabilities it can use, at most how many times, the worst-case cost, and
> whether untrusted data (documents, web pages, model output) can reach sensitive parameters such as payment
> details or email recipients. A runtime broker then enforces the same rules.

Use it when you need to write or modify an agent workflow whose actions must be reviewable before it runs.
Connect the MCP server for tools that check, format and test programs: {URL}/mcp (streamable HTTP), or run
`remit mcp` locally (stdio).

## Docs

- [Quick guide for AI agents]({URL}/guide.md): workflow, skeleton program, checker errors and how to fix them
- [Language specification]({URL}/spec.md): syntax, semantics, trust boundaries, diagnostics

## Tools

- [MCP server]({URL}/mcp): remit_guide, remit_check, remit_format, remit_authority_diff, remit_examples, remit_run_fixtures

## Optional

- [Everything in one file]({URL}/llms-full.txt)
""")

open(os.path.join(OUT, "llms-full.txt"), "w").write(
    f"# {NAME}: complete reference for AI agents\n\n{guide}\n\n---\n\n{spec}\n\n---\n\n"
    f"# Complete example: invoice intake\n\n## host.rmti\n\n```remit\n{host}```\n\n## invoices.rmt\n\n```remit\n{example}```\n")

open(os.path.join(OUT, "robots.txt"), "w").write(
    "# AI crawlers and agents are welcome.\nUser-agent: *\nAllow: /\n\n"
    f"Sitemap: {URL}/sitemap.xml\n")

pages = ["", "llms.txt", "llms-full.txt", "guide.md", "spec.md"]
open(os.path.join(OUT, "sitemap.xml"), "w").write(
    '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    + "".join(f"  <url><loc>{URL}/{p}</loc></url>\n" for p in pages) + "</urlset>\n")
print(f"built site files for {URL}")
