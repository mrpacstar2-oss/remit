"""Capability broker: the single path from a program to the outside world.

Language-neutral on purpose: the Remit interpreter and the Python baseline both call `Broker.call`, so
runtime guarantees (policy, budgets, approvals, idempotency, tracing, replay) are never credited to the language.
What the broker cannot do by itself is know where a plain Python string came from; Remit values arrive tagged.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .rterrors import (ApprovalRequired, RemitRuntimeError, BudgetExceeded, CallLimitExceeded, CapabilityFailed,
                       NotGranted, PolicyViolation, ReplayDivergence, Timeout)
from .values import EMPTY, LV, deep_tags, deep_prov, unwrap, wrap


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------- host specification (from host.rmti) ----------------
@dataclass
class ParamSpec:
    name: str
    deny: list
    approve: list


@dataclass
class CapSpec:
    name: str
    params: list  # list[ParamSpec]
    tags: list
    clears: list
    cost: float
    idempotent: bool
    approval_always: bool
    kind: str = "capability"  # or "model"


def host_spec(host) -> dict:
    """Build broker specs from a parsed HostInterface."""
    specs = {}
    for c in host.capabilities:
        specs[c.name] = CapSpec(c.name, [ParamSpec(p.name, list(p.deny), list(p.approve)) for p in c.params],
                                list(c.tags), list(c.clears), c.cost, c.idempotent, c.approval_always)
    for m in host.models:
        specs[m.name] = CapSpec(m.name, [ParamSpec("prompt", [], []), ParamSpec("context", [], [])], [], [], m.cost,
                                True, False, kind="model")
    return specs


# ---------------- policy (deployment, outside the program) ----------------
@dataclass
class Grant:
    max_calls: Optional[int] = None
    approval: Optional[str] = None  # "always" strengthens the host declaration
    arg_prefix: dict = field(default_factory=dict)  # param -> list of allowed prefixes
    arg_one_of: dict = field(default_factory=dict)  # param -> list of allowed values


@dataclass
class Policy:
    grants: dict  # resource -> Grant
    max_usd: Optional[float] = None
    source: str = "<default>"

    @staticmethod
    def load(path: str) -> "Policy":
        with open(path, "rb") as f:
            d = tomllib.load(f)
        grants = {}
        for name, g in d.get("grant", {}).items():
            grants[name] = Grant(max_calls=g.get("max_calls"), approval=g.get("approval"),
                                 arg_prefix=g.get("arg_prefix", {}), arg_one_of=g.get("arg_one_of", {}))
        return Policy(grants, d.get("budget", {}).get("max_usd"), path)

    @staticmethod
    def allow_all(specs: dict, max_usd: Optional[float] = None) -> "Policy":
        return Policy({n: Grant() for n in specs}, max_usd, "<allow-all>")


# ---------------- approvals ----------------
class Approver:
    def decide(self, request: dict) -> Optional[str]:
        """Return an identifier of who approved, or None if not approved."""
        raise NotImplementedError


class DenyAll(Approver):
    def decide(self, request):
        return None


class ApproveAll(Approver):
    """Only for tests and benchmarks; never the CLI default."""

    def decide(self, request):
        return "auto-approve(test)"


class ApprovalStore(Approver):
    """File-backed single-use approvals keyed by the request digest."""

    def __init__(self, path: str):
        self.path = path
        self.data = {"approved": {}, "consumed": {}, "pending": {}}
        if os.path.exists(path):
            with open(path) as f:
                self.data.update(json.load(f))

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)

    def decide(self, request):
        d = request["digest"]
        if d in self.data["approved"]:
            who = self.data["approved"].pop(d)["by"]
            self.data["consumed"][d] = {"by": who, "run_id": request["run_id"], "at": time.time()}
            self.data["pending"].pop(d, None)
            self.save()
            return who
        self.data["pending"][d] = {k: request[k] for k in ("resource", "site", "args", "program_hash", "reasons")}
        self.save()
        return None

    def approve(self, digest: str, by: str) -> bool:
        req = self.data["pending"].get(digest)
        if req is None:
            return False
        self.data["approved"][digest] = {"by": by, "at": time.time(), "request": req}
        self.save()
        return True


class Interactive(Approver):
    def __init__(self, fallback: Optional[Approver] = None):
        self.fallback = fallback

    def decide(self, request):
        import sys
        if self.fallback:
            who = self.fallback.decide(request)
            if who:
                return who
        if not sys.stdin.isatty():
            return None
        print("\n=== approval required ===", file=sys.stderr)
        print(json.dumps({k: request[k] for k in ("resource", "args", "reasons")}, indent=2), file=sys.stderr)
        print(f"digest {request['digest']}", file=sys.stderr)
        ans = input("approve this exact call? [y/N] ").strip().lower()
        return "interactive" if ans in ("y", "yes") else None


# ---------------- trace ----------------
class Trace:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.events: list[dict] = []
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            open(path, "w").close()

    def emit(self, ev: dict):
        self.events.append(ev)
        if self.path:
            with open(self.path, "a") as f:
                f.write(canonical(ev) + "\n")

    @staticmethod
    def load(path: str) -> list[dict]:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]


@dataclass
class CallContext:
    idempotency_key: str
    timeout: Optional[float]
    run_id: str


@dataclass
class AdapterResult:
    value: object
    cost_usd: Optional[float] = None
    meta: dict = field(default_factory=dict)


# ---------------- broker ----------------
class Broker:
    def __init__(self, specs: dict, adapters: dict, policy: Policy, approver: Approver, trace: Trace,
                 program_hash: str, budget_usd: Optional[float] = None, replay: Optional[list] = None,
                 live_after_replay: bool = True, heuristic_taint: bool = False, run_id: Optional[str] = None,
                 idempotency_root: Optional[str] = None):
        self.specs = specs
        self.adapters = adapters
        self.policy = policy
        self.approver = approver
        self.trace = trace
        self.program_hash = program_hash
        limits = [b for b in (budget_usd, policy.max_usd) if b is not None]
        self.budget = min(limits) if limits else None
        self.spent = 0.0
        self.reserved = 0.0
        self.calls: dict[str, int] = {}
        self.site_seq: dict[str, int] = {}
        self.seq = 0
        self.lock = threading.Lock()
        self.run_id = run_id or uuid.uuid4().hex[:12]
        # Idempotency keys must survive resume: a resumed run re-executing a call whose effect happened but was
        # never recorded (crash window) must present the same key so the downstream system can de-duplicate.
        root = idempotency_root
        if root is None and replay:
            root = next((e.get("idempotency_root") or e.get("run_id") for e in replay if e.get("event") == "run_start"),
                        None) or next((e.get("run_id") for e in replay if e.get("run_id")), None)
        self.idempotency_root = root or self.run_id
        self.replay = [e for e in (replay or []) if e.get("event") == "call" and e.get("status") == "ok"]
        self.replayed = 0
        self.live_after_replay = live_after_replay
        self.heuristic_taint = heuristic_taint
        self._seen_outputs: list[tuple[str, frozenset]] = []  # baseline: substring taint memory

    # -- helpers --
    def _grant(self, resource):
        g = self.policy.grants.get(resource)
        if g is None:
            raise NotGranted(f"host policy does not grant '{resource}'", data={"resource": resource})
        return g

    def _heuristic(self, raw) -> frozenset:
        """Baseline-only: tag a plain value if it contains a previously returned tagged string."""
        if not self.heuristic_taint:
            return EMPTY
        text = (raw if isinstance(raw, str) else canonical(raw)).lower()
        out = EMPTY
        for s, tags in self._seen_outputs:
            s = s.lower()
            # both directions: the argument embeds an output, or was extracted from one
            if (len(s) >= 6 and s in text) or (len(text) >= 6 and text in s):
                out |= tags
        return out

    def _remember(self, raw, tags):
        if not self.heuristic_taint or not tags:
            return
        if isinstance(raw, str):
            self._seen_outputs.append((raw, tags))
        elif isinstance(raw, list):
            for x in raw:
                self._remember(x, tags)
        elif isinstance(raw, dict):
            for x in raw.values():
                self._remember(x, tags)

    def emit(self, ev):
        self.trace.emit(ev)

    # -- main entry --
    def call(self, resource: str, args: dict, *, site: str, pc_tags: frozenset = EMPTY, timeout: Optional[float] = None,
             model_request: Optional[dict] = None) -> LV:
        spec = self.specs.get(resource)
        if spec is None:
            raise NotGranted(f"unknown resource '{resource}'")
        grant = self._grant(resource)
        # normalise args to LV (the Python baseline passes plain values)
        largs = {}
        for k, v in args.items():
            if isinstance(v, LV):
                largs[k] = v
            else:
                largs[k] = wrap(v, self._heuristic(v))
        raw_args = {k: unwrap(v) for k, v in largs.items()}
        arg_tags = {k: deep_tags(v) for k, v in largs.items()}
        prov = frozenset().union(*[deep_prov(v) for v in largs.values()]) if largs else EMPTY

        # 1. data-flow policy (precise tags for Remit values; heuristic or none for plain values)
        for p in spec.params:
            t = arg_tags.get(p.name, EMPTY)
            bad = [x for x in p.deny if x in t]
            if bad:
                self.emit({"event": "violation", "run_id": self.run_id, "resource": resource, "site": site,
                           "param": p.name, "tags": sorted(bad)})
                raise PolicyViolation(f"data tagged {', '.join(sorted(bad))} reached '{resource}' parameter "
                                      f"'{p.name}', which denies it", data={"resource": resource, "param": p.name})
        # 2. argument constraints from the deployment policy
        for pname, prefixes in grant.arg_prefix.items():
            v = raw_args.get(pname)
            if not isinstance(v, str) or not any(v.startswith(px) for px in prefixes):
                raise PolicyViolation(f"'{resource}' argument '{pname}' is outside the allowed prefixes {prefixes}",
                                      data={"resource": resource, "param": pname})
        for pname, allowed in grant.arg_one_of.items():
            if raw_args.get(pname) not in allowed:
                raise PolicyViolation(f"'{resource}' argument '{pname}' is not one of the allowed values",
                                      data={"resource": resource, "param": pname})
        # 3. call limits
        n = self.calls.get(resource, 0)
        if grant.max_calls is not None and n >= grant.max_calls:
            raise CallLimitExceeded(f"policy allows at most {grant.max_calls} call(s) to '{resource}'",
                                    data={"resource": resource})

        k = self.site_seq.get(site, 0)
        self.site_seq[site] = k + 1
        idem_key = sha256(canonical([self.idempotency_root, site, k]))[:32]

        # 4. replay: return the recorded result if this call was already performed
        if self.replayed < len(self.replay):
            rec = self.replay[self.replayed]
            if rec["resource"] != resource or rec["site"] != site or canonical(rec["args"]) != canonical(raw_args):
                raise ReplayDivergence(
                    f"replay diverged at call #{self.replayed + 1}: recorded {rec['resource']}@{rec['site']} "
                    f"but program requested {resource}@{site} with different arguments",
                    data={"recorded": rec, "requested": {"resource": resource, "site": site, "args": raw_args}})
            self.replayed += 1
            self.calls[resource] = n + 1
            self.spent += rec.get("cost_usd") or 0.0
            self.seq += 1
            ev_id = self.seq
            self.emit({**rec, "seq": ev_id, "run_id": self.run_id, "replayed": True})
            out_tags = (frozenset(spec.tags) | frozenset().union(*arg_tags.values(), pc_tags)) - frozenset(spec.clears)
            return wrap(rec["result"], out_tags, prov | {ev_id})
        if self.replay and not self.live_after_replay:
            raise ReplayDivergence("replay finished but the program requested another call", data={"resource": resource})

        # 5. approval bound to (program version, call site, resource, exact arguments)
        reasons = []
        if spec.approval_always or grant.approval == "always":
            reasons.append("always requires approval")
        for p in spec.params:
            t = arg_tags.get(p.name, EMPTY) | pc_tags
            for x in p.approve:
                if x in t:
                    reasons.append(f"'{p.name}' is {x}" + (" (control flow)" if x not in arg_tags.get(p.name, EMPTY) else ""))
        approval = None
        if reasons:
            req = {"program_hash": self.program_hash, "site": site, "resource": resource, "args": raw_args}
            digest = sha256(canonical(req))
            request = {**req, "digest": digest, "reasons": reasons, "run_id": self.run_id}
            who = self.approver.decide(request)
            approval = {"digest": digest, "reasons": reasons, "approved_by": who}
            if not who:
                self.emit({"event": "pending_approval", "run_id": self.run_id, "resource": resource, "site": site,
                           "args": raw_args, "digest": digest, "reasons": reasons})
                raise ApprovalRequired(f"'{resource}' needs approval ({'; '.join(reasons)}); digest {digest[:16]}",
                                       data={"digest": digest, "resource": resource, "args": raw_args,
                                             "reasons": reasons})

        # 6. budget: reserve the declared worst-case cost before dispatch
        reserve = spec.cost
        with self.lock:
            if self.budget is not None and self.spent + self.reserved + reserve > self.budget + 1e-12:
                raise BudgetExceeded(
                    f"calling '{resource}' could exceed the budget: spent ${self.spent:.4f}, reserved "
                    f"${self.reserved:.4f}, this call reserves ${reserve:.4f}, budget ${self.budget:.4f}",
                    data={"resource": resource})
            self.reserved += reserve
            self.calls[resource] = n + 1
        adapter = self.adapters.get(resource)
        t0 = time.time()
        status, err, result, cost, meta = "ok", None, None, None, {}
        try:
            if adapter is None:
                raise CapabilityFailed(f"no adapter configured for '{resource}'")
            ctx = CallContext(idem_key, timeout, self.run_id)
            out = adapter(model_request, ctx) if spec.kind == "model" else adapter(raw_args, ctx)
            if isinstance(out, AdapterResult):
                result, cost, meta = out.value, out.cost_usd, out.meta
            else:
                result = out
        except RemitRuntimeError as e:
            status, err = "error", e
        except TimeoutError as e:
            status, err = "error", Timeout(f"'{resource}' timed out")
        except Exception as e:  # adapter bug or remote failure
            status, err = "error", CapabilityFailed(f"'{resource}' failed: {type(e).__name__}: {e}")
        actual = spec.cost if cost is None else cost
        with self.lock:
            self.reserved -= reserve
            self.spent += actual
        self.seq += 1
        ev_id = self.seq
        ev = {"event": "call", "seq": ev_id, "run_id": self.run_id, "resource": resource, "site": site,
              "args": raw_args, "arg_tags": {k: sorted(v) for k, v in arg_tags.items()}, "pc_tags": sorted(pc_tags),
              "derived_from": sorted(prov), "idempotency_key": idem_key, "approval": approval,
              "reserved_usd": reserve, "cost_usd": actual, "duration_ms": int((time.time() - t0) * 1000),
              "status": status, "meta": meta}
        if actual > reserve + 1e-12:
            ev["overrun_usd"] = actual - reserve
        if status == "ok":
            ev["result"] = result
        else:
            ev["error"] = {"code": err.code, "message": err.message}
        self.emit(ev)
        if status != "ok":
            raise err
        out_tags = (frozenset(spec.tags) | frozenset().union(*arg_tags.values(), pc_tags)) - frozenset(spec.clears)
        self._remember(result, out_tags)
        return wrap(result, out_tags, prov | {ev_id})
