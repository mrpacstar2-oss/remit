# Remit quick guide for AI agents

Remit is a small, checked language for programs that call tools and models. A host gives you
`host.rmti` (capabilities you may call) and `policy.toml` (limits). You write a `.rmt` program. The checker tells you,
before anything runs, what the program can do, how often, at what worst-case cost, and whether untrusted data could
reach a sensitive parameter. **Always run the checker and fix every error before finishing.**

## Workflow

1. Read the host interface: capability names, parameter tags (`deny`, `approve`), return types, `idempotent`, costs.
2. Write the program. The entry point is `fn main(...)`, and its parameters are the program inputs.
3. Run `remit_check`, or `remit check file.rmt`. Fix errors until it passes, then read the manifest summary.
4. Optionally run it on fixtures: `remit_run_fixtures`, or `remit run file.rmt --fixtures f.json --approve-all`.
5. When editing an existing program, run `remit_authority_diff`, or `remit diff old.rmt new.rmt`. Do not widen
   authority unless the user asked for it.

## Skeleton

```remit
program expense_review
uses expenses.get, directory.lookup, llm, payroll.reimburse
budget usd 0.20

type Verdict = { category: "business" | "personal" | "unclear" }
type Row = { expense_id: Str, status: Str }

fn main(ids: List[Str] max 10) -> List[Row] max 10 {
  return for id in ids {
    review(id)
  }
}

fn review(id: Str) -> Row {
  let e = expenses.get(id)
  let who = directory.lookup(e.employee_email)          # trusted record from the host
  let v = llm.ask(Verdict, "Classify this expense note.", context: e.note) retry 1
  if v.category == "personal" {
    return { expense_id: id, status: "rejected" }
  }
  payroll.reimburse(employee_id: who.id, amount: e.amount, expense_id: id)
  return { expense_id: id, status: "reimbursed" }
}
```

## Rules that the checker enforces (and how to satisfy them)

| You will see | Meaning | Fix |
|---|---|---|
| E0401 loop over a list with no known size bound | Every loop must have a static bound. | Iterate a list with `max N` in its type, use `take(xs, N)` or `range(N)`, or add `for x in xs limit N`. |
| E0113 list may have ... items | A list must fit its declared `max`. Model output types must bound every list. | `List[T] max N` in types, `take(xs, N)` before returning. |
| E0301 data tagged X flows into ... which denies it | Untrusted or private data reached a forbidden parameter. **Cannot be approved.** | Get the value from a trusted capability. The host marks these with `clears untrusted`, for example a directory or CRM lookup. Never pass text from documents, tickets, web pages or model output into a `deny untrusted` parameter. |
| approval "possible" in the manifest | An `approve` parameter may receive tagged data, or the call sits under a branch that depends on tagged data. | Nothing to fix: a human approves at runtime. Do not try to avoid it. |
| E0405 not idempotent, so it cannot be retried | `retry` is only allowed on idempotent capabilities and model calls. | Remove `retry`. Use `else <fallback>` if failure is acceptable. |
| E0402 recursion | Programs are total. | Use a bounded `for` loop. |
| E0404 / E0502 / E0503 | The worst case exceeds the budget or the policy. | Lower loop bounds or retries, or ask the operator. |
| E0201 not declared in uses | Every capability or model used must be listed after `uses`. | Add it to the `uses` line. |
| E0111 not exhaustive / not a possible value | `match` on a union must cover every option. Literals must belong to the union. | Add arms or `_ =>`. Fix typos. |

## Syntax cheat sheet

- **Bindings:** `let x = e` (immutable), `var x = e` (mutable), `x = e` (assign to a var only).
- **Types:**
  - `Int Float Bool Str`, `List[T] max N`, records `{ a: Str, b: Int }`, unions `"a" | "b"`.
  - `type Name = ...`.
  - A string literal fits a union containing it.
- **Branching:**
  - `if c { ... } else { ... }` is an expression.
  - `match s { "a" => e, "b" => { ... } }`.
- **Loops:** `for x in xs [limit N] [if cond] { body }` evaluates to the list of body values. `break` exits.
- **Calls:**
  - Capability: `ns.cap(arg, name: arg)`, followed by optional `retry N`, `timeout 10s` and `else fallback`.
  - Model: `llm.ask(Type, "prompt {interp}", context: value)`. The output is validated against `Type`.
  - Agents: `agent Name { model: llm, system: "...", max_calls: 3 }`, then `Name.ask(Type, "prompt", context: x)`.
    Put one setting per line inside the braces.
- **Builtins:** `len take range str lower upper trim contains startswith endswith split join sum min max abs round
  int float first unique replace`.
- **Strings:** `"Hello {name}"` interpolates. Use `\{` for a literal brace.
- **Tests:** `test "name" fixtures "fixtures/x.json" { let r = main(...)  expect r == "ok" }`.
- **Missing features:** no null, no exceptions, no classes, no imports, no recursion, no while loops, and no ambient
  I/O, clock or randomness.

## Fixtures format (offline runs and tests)

```json
{"capabilities": {"ns.cap": [{"args": {"param": "value"}, "output": {}, "repeat": true}]},
 "model": [{"match": "substring of prompt", "output": {"category": "business"}}]}
```

The full specification is in LANGUAGE_SPEC.md: semantics, trust boundaries and the list of diagnostics.
