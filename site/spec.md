# Remit language specification (v0.1, prototype)

This document describes **what is implemented** in `src/remit` as of this commit. Proposals are collected in §13 and are clearly marked.

## 1. Purpose and scope

Remit is a small, **total**, statically checked language for programs that act in the world through capabilities.
These are programs that models write or edit, and that hosts must be able to review and bound **before** running
them. A checked Remit program comes with a manifest that answers four questions before any effect happens:

1. Which capabilities and models can the program use, and **at most how many times** each?
2. What is the **worst-case spend**?
3. Which tagged data (for example `untrusted` or `private`) can reach which capability parameters, and are those
   flows denied, allowed, or allowed only with approval?
4. Which call sites can require a human **approval**, and why?

At runtime the same rules are enforced again, precisely and per value, by a broker that the program cannot bypass.

**Non-goals.** Remit is not a general-purpose language. It has no recursion, unbounded loops, first-class
functions, classes, ambient I/O, or foreign-function interface. It is not a sandbox: see §11 for what the
interpreter, broker and OS each guarantee.

## 2. Files

| File | Written by | Trust | Contents |
|---|---|---|---|
| `*.rmt` program | model or developer | **untrusted** | `program` header, types, functions, agents, tests |
| `host.rmti` host interface | host operator | trusted | shared types, capability signatures with tags, costs and idempotency; model declarations; input tags |
| `policy.toml` deployment policy | host operator | trusted | grants, call limits, argument constraints, budget cap, extra approval rules |
| `adapters.py` | host operator | **trusted code** | live implementations of capabilities |
| `fixtures/*.json` | developer | test data | offline outputs for tests and demos, recorded as `adapter: fixture` |

A program cannot modify the host interface, the policy or the adapters. Its authority is the intersection of what
it declares in `uses`, what the host interface defines, and what the policy grants.

## 3. Lexical structure

- **Comments:** `#` to end of line. The formatter preserves own-line and trailing comments.
- **Newlines:** newlines end statements, except inside `( )` and `[ ]`. `;` also separates statements.
- **Literals:**
  - Integers `42` and `1_000`, floats `0.5` and booleans `true`/`false`.
  - Strings `"..."` with escapes `\n \t \" \\ \{ \}`.
  - Durations `500ms`, `10s` and `2m`, used only after `timeout`.
- **Interpolation:** `"Hello {name}"` evaluates the expression between braces.
- **Keywords:** `program uses budget usd type fn let var return if else for in limit match and or not true false
  retry timeout capability model agent tags cost idempotent approval always deny approve clears max test expect
  break input import as`.

## 4. Program structure

```remit
program <name>
uses <resource>, <resource>, ...     # capabilities (ns.name) and models the program may use
budget usd <number>                  # optional program-level spend cap

type ...      fn ...      agent ...      test ...
```

`fn main(...)` is the entry point. Its parameters are the program's **inputs**. The host tags each input
(`input ticket tags untrusted`); an input without a declaration is tagged `untrusted`.

## 5. Types

| Type | Notes |
|---|---|
| `Int`, `Float`, `Bool`, `Str`, `Unit` | `Int` is accepted where `Float` is expected |
| `List[T]` and `List[T] max N` | The optional static bound is used by the bound analysis and enforced on model output. |
| `{ field: T, ... }` | Structural records. A value with extra fields is accepted (width subtyping). |
| `"a" \| "b" \| "c"` | Unions of string literals (enums). Accepted where `Str` is expected. Comparing to a literal outside the union is a compile error. |
| `type Name = T` | A named alias. Host types are visible to programs and cannot be redefined. |

**No null.** Failure is handled with `else` fallbacks (§8), not with optional values.

## 6. Declarations and statements

```remit
fn name(p: T, q: U) -> R { ... }     # no '-> R' means Unit; every path must return R
agent Name { model: llm  system: "..."  max_calls: 3 }   # one setting per line
test "name" fixtures "path.json" { ... expect cond, "message" ... }
```

| Statement | Form | Notes |
|---|---|---|
| Immutable binding | `let x = e` or `let x: T = e` | |
| Mutable binding | `var x = e` | An unannotated mutable `List` is treated as unbounded, because it may grow in a loop. |
| Assignment | `x = e` | Only to a `var`. |
| Return | `return e` | |
| Expression | `e` | |
| Assertion | `expect e` | Tests only. |
| Loop exit | `break` | Inside a `for` only. |

**Expressions:**

- **Operators:** `or`, `and` and `not`; comparisons `== != < <= > >= in`, which cannot be chained; `+ - * / %`.
  `+` also concatenates strings and lists.
- **Access and literals:** field access `r.f`, indexing `xs[i]`, list literals `[...]` and record literals
  `{f: e}`.
- **`if c { ... } else { ... }`:** an expression whose value is the last expression of the taken block.
- **`for x in xs [limit N] [if cond] { body }`:** an expression producing the list of body values.
- **`match s { "a" => e, _ => e }`:** on `Str` or an enum. Matches on enums must be exhaustive, and `Str` needs `_`.
- **Calls:** `f(args)` for program functions and builtins, `ns.cap(args)` for capabilities, and
  `model.ask(T, prompt, context: e)` or `Agent.ask(T, prompt, context: e)` for models. Named arguments
  `name: value` follow positional arguments.
- **Call modifiers,** for capability and model calls only: `retry N`, `timeout 10s` and `else <expr>`.

**Builtins** are pure and deterministic: `len take range str lower upper trim contains startswith endswith split
join sum min max abs round int float first unique`. `take(xs, N)` and `range(N)` require integer literals, because
their results carry static bounds.

## 7. Capabilities, tags and the host interface

```remit
capability payments.refund(order_id: Str deny untrusted approve untrusted, amount: Float approve untrusted)
  tags private          # tags added to the result
  clears untrusted      # tags the host removes from the result (it vouches for the data)
  cost 0.01             # worst-case USD per call, reserved before dispatch
  idempotent            # safe to retry
  approval always       # every call needs approval
model llm cost 0.02     # worst-case USD per model call
input ticket tags untrusted
```

**Tags** are host-defined names. The examples use `untrusted`, for attacker-influenced data, and `private`, for
confidential data. They propagate through every operation, as the union of operand tags.

- A **capability result** carries `(declared tags ∪ argument tags ∪ control tags) − clears`.
- A **model result** carries the union of the tags of its prompt and context. A model that reads untrusted text
  produces untrusted output: this is the prompt-injection rule.
- **Explicit flows** are values passed as arguments. They are checked against both `deny` and `approve`.
- **Control flows** are the tags of the conditions of enclosing `if`, `match`, loop filters, and of the list being
  iterated (the "pc"). They are checked against `approve` only.
  - **Known gap:** a branch on secret data can reveal about one bit per branch through a `deny` sink. CaMeL calls
    the equivalent setting non-strict mode. The strict variant is in §13.
- **`deny t`:** passing a value tagged `t` is a compile error (E0301). At runtime the broker raises
  `PolicyViolation` before dispatch.
- **`approve t`:** if `t` reaches the parameter through data or control, the call site requires approval. The
  manifest lists it as `possible`.

## 8. Operational semantics of an effectful call

Evaluating `ns.cap(a1, ..., an) retry R timeout D else F` proceeds as follows. Here `pc` is the union of the
current control tags.

1. Evaluate the arguments left to right to tagged values `v1..vn` with tags `T1..Tn` and provenance `P1..Pn`.
   Provenance is the set of trace event ids the value derives from.
2. For each attempt `k` in `0..R`, with `R > 0` allowed only if the capability is `idempotent`
   (E0405 statically, R0405 at runtime):
   1. The broker checks, in order:
      1. **Grant:** the policy grants `ns.cap`; otherwise `NotGranted`.
      2. **Deny tags:** every `deny` tag is absent from the corresponding `Ti`; otherwise `PolicyViolation`.
      3. **Argument constraints:** the policy's `arg_prefix` and `arg_one_of` hold.
      4. **Call limit:** the policy's `max_calls` is not yet reached.
   2. **Replay:** if the run is replaying and the next recorded call has the same resource, site and canonical
      arguments, the recorded result is returned without dispatch. A mismatch raises `ReplayDivergence`.
   3. **Approval:**
      - It is needed when the capability or policy says `always`, or when an `approve` tag is present in
        `Ti ∪ pc`.
      - The request digest is `sha256(program_hash, site, resource, canonical(args))`.
      - An approval is **single use**, and binds to the program's semantic hash (spans and comments excluded), the
        call site and the exact arguments.
      - Without an approval the run stops with `ApprovalRequired` and records a pending request.
   4. **Budget:** the broker reserves the declared cost. If `spent + reserved + cost > budget` it raises
      `BudgetExceeded` before dispatch. After the call it reconciles the actual cost reported by the adapter and
      records any overrun.
   5. **Dispatch:**
      - The adapter runs with an idempotency key `sha256(run_id, site, k)` and the timeout `D`.
      - The trace records arguments, tags, provenance, approval, reserved and actual cost, result or error.
      - The result is tagged per §7, and its provenance becomes `P1 ∪ … ∪ Pn ∪ {event id}`.
   6. **Retrying:** operational failures (`CapabilityFailed`, `Timeout`, `ModelOutputInvalid`) go on to the next
      attempt. Security and limit errors propagate immediately and are **never** retried or caught by `else`.
3. If every attempt failed operationally and `else F` is present, evaluate `F`. Otherwise propagate the last error.

**Model calls** follow the same path. The adapter receives a JSON Schema derived from `T`, and the interpreter
validates the output against `T` (types, enum membership, `maxItems`, no extra fields). An invalid output is an
operational error, so `retry` applies. Each agent's `max_calls` is enforced on every attempt.

**Call-site identifiers** are structural: `<function>#<k>` is the k-th effectful call in pre-order. They survive
reformatting. Any semantic edit changes the program hash, which invalidates pending approvals and recorded runs.

## 9. Static bounds and budget

The checker computes, for every resource, an upper bound on the number of calls:

| Construct | Bound |
|---|---|
| Sequence | sum |
| `if` and `match` | per-resource maximum over branches |
| `for x in xs` | `n ×` (filter + body), where `n` is `xs`'s static bound, capped by `limit` |
| `retry R` | `(R + 1) ×` |
| `else F` | `+ F` |
| function call | callee summary, analysed in context |
| recursion | rejected (E0402) |

**Where list bounds come from:**

- list literals;
- `List[T] max N` in capability return types;
- `take(xs, N)` and `range(N)`;
- model output types, which **must** be bounded (E0113);
- concatenation, which adds the bounds.

A loop over a list with no known bound is a compile error (E0401) unless it has a `limit`.

**Budget:** the worst-case cost is the sum over resources of `max_calls × declared cost`. If it exceeds the program
`budget` it is error E0404. If it exceeds the policy `max_usd`, or a resource's bound exceeds the policy's
`max_calls`, it is error E0502 or E0503 at `remit check` time, and `remit run` refuses to start.

**These bounds are sound only relative to the host's declarations.** An adapter that costs more than declared is
caught at reconciliation and recorded as an overrun. Later reservations then fail sooner, but the overrun itself
has already happened.

## 10. Determinism, traces, replay and resume

- **No ambient effects.** The language has no ambient clock, randomness, environment, filesystem or network. All
  non-determinism enters through broker calls, which are recorded.
- **Exact replay.** Re-executing a program with the recorded results is therefore exact. `remit replay <run>`
  re-executes with **no live adapters** and fails on any divergence.
- **Resume.** `remit resume <run>` replays the completed calls of a stopped run, for example one waiting for
  approval, and continues live from the first unrecorded call. Completed effects are **not repeated**. It refuses
  to resume if the program's semantic hash changed.
- **Trace.** The trace is a JSONL file in `.remit/runs/`. Each call event records `derived_from`, the provenance
  ids, and its approval digest and approver. Following those links answers which data and which approval
  authorised an effect.

## 11. Trust boundaries: what guarantees what

| Layer | Guarantees | Does **not** guarantee |
|---|---|---|
| **Checker** (`remit check`) | Before running: undeclared or ungranted resources, denied flows, unbounded loops, recursion, non-idempotent retries, over-budget worst case, type errors, list bounds. Produces the manifest. | Anything about adapter behaviour; implicit flows into `deny` sinks; the truth of host cost declarations. |
| **Interpreter** | Programs can reach the world only through the broker. Precise per-value tags and provenance; pc tracking; model-output validation; agent call limits. | Isolation from the host process. It is Python code in the same process as the adapters. |
| **Broker** | Grants, deny tags, argument constraints, call limits, budget reservation, argument-bound single-use approvals, idempotency keys, trace, replay. | That plain values from non-Remit callers (the Python baseline) carry correct tags. |
| **Adapters** | Whatever the operator implements, such as host allowlists or idempotency-key deduplication. | They are trusted code. A buggy adapter defeats everything above it. |
| **OS sandbox** | **Not implemented.** Run the interpreter in a container or VM with no ambient credentials and an egress allowlist. | — |

**Specific risks:**

- **Prompt injection:** handled by tagging, not by detection. Model output over untrusted input is untrusted.
- **Non-idempotent retries:** prevented statically and at runtime.
- **Replay safety:** completed effects are never re-executed on resume. Replay compares canonical arguments.
- **Budget races:** reservations are taken under a lock. The interpreter is single-threaded today.
- **Approval invalidation:** by program hash, site and arguments; single use.
- **Secret leakage:** programs never receive credentials, because adapters hold them. Traces record arguments and
  results in clear text, so redaction is a proposal.
- **Imported modules:** modules do not exist yet.

## 12. Diagnostics

Every diagnostic has a code, a source location, a caret excerpt and usually a hint.

| Code | Meaning |
|---|---|
| E0001, E0100 | lexical and syntax errors |
| E0101–E0113 | names, fields, types, arity, operators, mutability, returns, match, duplicates, list bounds |
| E0201 / E0202 / E0203 / W0201 | resource not in `uses` / unknown resource / model call shape / unused `uses` entry |
| E0301 | denied data flow |
| E0401 / E0402 / E0403 / E0404 / E0405 | unbounded loop / recursion / agent max_calls / budget / non-idempotent retry |
| E0501–E0503 | not granted by policy / exceeds policy max_calls / exceeds policy budget |
| R0101–R0601 | runtime: evaluation, list bound, policy violation, approval required, not granted, budget, call limit, capability failure, invalid model output, timeout, replay divergence |

## 13. Proposals (NOT implemented)

- **Strict implicit-flow mode:** apply pc tags to `deny` sinks as well.
- **Declassification and endorsement in the program:** an `endorse x by approval` form that turns an approval into a
  tag change for a value.
- **Structured concurrency:** `parallel for` with per-branch budget reservation and cancellation.
- **Modules:** `import`, with module-level manifests and trust declarations for third-party modules.
- **Trace redaction:** for parameters marked `secret`, plus signed, tamper-evident traces.
- **Policy compiled to Cedar:** reuse Cedar's analysis instead of the TOML format.
- **Hardened interpreter:** in Rust or WASM, with OS-level isolation shipped by default.
- **Cost model per model call:** derived from `max_tokens` instead of a single declared number.
- **Durable approvals inbox:** with expiry, plus a multi-party approval quorum.
