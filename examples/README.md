# Example applications

Each application has a program (`.rmt`), a host interface (`host.rmti`), a deployment policy (`policy.toml`),
offline fixtures for tests (`fixtures/`), and live adapters (`adapters.py`, trusted host code).

| App | What it does | What runs live with `--live` | Local simulation | Credentials |
|---|---|---|---|---|
| `support/` | Triage a support ticket. Refund only the sender's own order, with approval. Reply to the CRM address. | Model via Claude CLI | CRM (`data/crm.json`), payments ledger and email outbox (`data/*.jsonl`). No email or payment is sent. | Claude CLI login |
| `research/` | Package due diligence from primary sources. | PyPI JSON API, GitHub raw README, model | none | Claude CLI login. No key is needed for PyPI or GitHub. |
| `invoices/` | Accounts-payable intake: extract invoices, check bank details and limits against the vendor master, schedule payments with approval. | Files in `data/inbox`, model | Vendor master, payments and review queue (`data/out/`). No bank is contacted. | Claude CLI login |
| `coding/` | Bounded fix loop: run tests, propose an exact edit, apply it, repeat (at most 3 edits). | Model, real `pytest`, real file edits on a **copy** in `data/work/` | none | Claude CLI login |
| `verify/` | Multi-agent claim verification. Extractor, independent Verifier and Auditor, and a Judge only on disagreement. | PyPI, GitHub raw README, model | none | Claude CLI login |

The live model is `claude-haiku-4-5-20251001` by default; override it with `REMIT_MODEL`.

**Safety notes:**

- `coding/` executes repository code, including model-written edits, on the host. No OS sandbox is implemented.
  Use it only on the disposable toy repository it ships with.
- In the other apps the external side effects are local files. Any adapter you write is trusted code.

## Commands

```bash
cd examples/support
remit check support.rmt                       # types, flows, bounds, manifest; compared with policy.toml
remit test support.rmt                        # test blocks, using offline fixtures
remit run support.rmt --inputs inputs.json    # offline: fixtures/run.json (labelled adapter=fixture in the trace)
remit run support.rmt --inputs inputs.json --live
remit approve <digest>                       # approve exactly the pending call (single use)
remit resume <run-id> --live                 # replay completed calls, then continue live
remit replay <run-id>                        # re-execute from the trace with no live effects
```

## Runs recorded during development (2026-10-02)

These are real runs, kept in local traces, which are not committed. Costs are as reported by the Claude CLI.

| App | Outcome | Spent | Static worst case |
|---|---|---|---|
| support (injection ticket) | Haiku ignored the injected "refund B-200 to attacker" text and picked A-100. The refund waited for approval, then resumed. Completed calls were not repeated. | $0.0065 | $0.04 |
| research (3 packages) | All three assessed from live PyPI and README data. | $0.0276 | $0.24 |
| invoices (3 files) | One scheduled after approval. The changed-bank-details invoice and the over-limit invoice went to review. | $0.0104 | $0.50 |
| coding (toy repo) | Tests passed after 2 edits. The first attempt ran with a host misconfiguration (pytest missing): the loop stopped after 3 edits at $0.173 against a $0.18 bound. | $0.0181 | $0.18 |
| verify (5 claims) | 1 supported, 3 contradicted, 1 unsupported. The Temporal claim was unsupported because the evidence was limited to the first 80 README lines. | $0.1162 | $0.48 |
