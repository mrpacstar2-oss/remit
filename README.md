# Remit

**Know what an AI-written program can do before it runs.**

Remit is a small, statically checked language for agent workflows, meaning programs that call tools and models.
It is meant to be written and edited by AI agents. Before a program runs, the checker reports:

- which capabilities it can use, from a host-controlled list;
- **at most how many times** it can call each one, and its **worst-case spend**;
- whether untrusted data (documents, web pages, model output) or private data can reach sensitive parameters, such
  as payment details or email recipients. Each such flow is rejected outright, or sent to a human for approval
  bound to the exact arguments.

A runtime broker then enforces the same rules and records a replayable trace.

```
payments.schedule(vendor_id: vendor.id, iban: inv.iban_on_invoice, amount: inv.amount, ...)

error[E0301]: data tagged untrusted flows into 'payments.schedule' parameter 'iban', which denies it
  note: 'inv.iban_on_invoice' is untrusted because it derives from inbox.read (line 20)
```

## For AI agents

- **MCP server, local:** `remit mcp` (stdio).
- **MCP server, hosted:** `remit mcp --http`, with optional API keys and usage metering.
- **Tools:**

  | Tool | What it does |
  |---|---|
  | `remit_guide` | the guide or the full spec |
  | `remit_check` | errors plus the authority manifest |
  | `remit_format` | canonical formatting |
  | `remit_authority_diff` | did an edit widen authority? |
  | `remit_examples` | complete example programs |
  | `remit_run_fixtures` | offline test runs |

- **Quick guide:** [docs/AI_GUIDE.md](docs/AI_GUIDE.md).
- **Full specification:** [docs/LANGUAGE_SPEC.md](docs/LANGUAGE_SPEC.md).
- **`llms.txt`:** [site/llms.txt](site/llms.txt), plus [site/llms-full.txt](site/llms-full.txt).

Claude Code configuration example:

```json
{ "mcpServers": { "remit": { "command": "remit", "args": ["mcp"] } } }
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[mcp]" pytest pydantic
.venv/bin/python -m pytest -q tests baselines
cd examples/invoices
../../.venv/bin/remit check invoices.rmt                                   # manifest and policy check
../../.venv/bin/remit run invoices.rmt                                     # offline fixtures; stops for approval
../../.venv/bin/remit approve <digest> && ../../.venv/bin/remit resume <run-id>   # approve that exact call, continue
```

## Evidence

[docs/BENCHMARKS.md](docs/BENCHMARKS.md) compares Remit with a Python baseline that uses **the same runtime**, so
runtime features are not credited to the language. Samples are small, use one model family and include no human
trials.

- **Safety:**
  - Of 17 unsafe program mutations, 14 were rejected before running. Python on the same runtime executed 8 of them.
  - Asked to make unsafe changes, Haiku 4.5 agents shipped executable unsafe code 0/6 times in Remit and 6/6 in
    Python. Sonnet 5.5 refused in both languages.
- **Review:** every agent-written feature change (20 of 20) was mechanically shown not to widen authority.
- **Ease:**
  - Agents modified programs (15/15 against 14/15) and built a new workflow from a spec (5/5 against 5/5) **equally
    well** in both languages, so there is no penalty for a language the models had never seen.
  - Agents that only had the MCP server built correct programs 5/5.
- **Recovery:** crash-and-resume behaviour is identical in both, as expected, since it is a runtime property.

## Status

This is a research prototype.

- **Implemented and tested:** the parser, checker, interpreter, broker, CLI (`check`, `build`, `run`, `test`,
  `fmt`, `replay`, `approve`, `resume`, `diff`, `ask`, `mcp`), the MCP server and five example apps.
- **Not implemented:** an OS sandbox (run it inside your own isolation), modules, concurrency and a language server.
  See LANGUAGE_SPEC §13.

Deploying the website and hosted MCP server is covered in [deploy/PUBLISHING.md](deploy/PUBLISHING.md).

Licence: Apache-2.0.
