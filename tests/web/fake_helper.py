"""Fake helper speaking Contract 8 (docs/ARCHITECTURE.md) over OS pipes. No hardware, no real helper."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time

from arltuner.web.helperlink import HelperLink
from tests.web.fake_v3 import V3_OPS, V3Ops, topology_view

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789"  # [A-Z2-9] per Contract 8
_UNSET = object()


def preflight_report(mode: str = "write", *, unknown: bool = False, platform: str = "arl-s-285k") -> dict:
    """The real ``preflight`` result shape (arltuner.platform.preflight.PreflightReport.to_dict + ``writes_allowed``)."""
    checks = [{"name": "cpuid", "status": "pass", "reason": "family 6 model 198 stepping 2 on every online CPU"},
              {"name": "python", "status": "pass", "reason": "Python 3.12"},
              {"name": "stress-ng", "status": "missing", "reason": "missing: install stress-ng"}]
    if mode == "read-only":
        checks.append({"name": "lockdown", "status": "read-only", "reason": "lockdown is [integrity]: MSR writes are blocked"})
    elif mode == "refuse":
        checks.append({"name": "python", "status": "refuse", "reason": "Python 3.11 is older than 3.12"})
    if unknown:
        checks.append({"name": "msr_module", "status": "unknown", "reason": "cannot be determined without root"})
    return {"platform": platform, "mode": mode, "checks": checks, "writes_allowed": mode == "write" and not unknown}


def telemetry_values(*, cores: int = 3, with_power: bool = True) -> dict:
    """The ``values`` of a ``telemetry`` event (arltuner.telemetry.Sampler.collect): every leaf is ``Reading.to_json()``."""
    def ok(v, unit):
        return {"value": v, "unit": unit, "source": "fake", "status": "ok"}

    def na(unit, reason):
        return {"value": None, "unit": unit, "source": "fake", "status": "unavailable", "reason": reason}

    out = {
        "ts": 1760000000.5,
        "cores": {str(i): {"name": f"P{i}", "mhz": ok(5000.0 + i, "MHz"), "busy": ok(12.5, "%"), "vid": ok(1.25, "V") if i else na("V", "no MSR access (needs root)"),
                           "ratio": ok(50, "x")} for i in range(cores)},
        "temps": {"Package id 0": ok(41.0, "C"), "Core 0": ok(39.0, "C")},
        "vcore": na("V", "no hwmon device"),
        "power": {"package": ok(55.25, "W") if with_power else na("W", "powercap unreadable"), "core": na("W", "no 'core' subdomain")},
        "dram": {"read": na("GB/s", "IMC sampling not enabled"), "write": na("GB/s", "IMC sampling not enabled")},
        "limit_reasons_raw": ok(0, "raw"),
    }
    return out


def vf_dump_document(accepted_match=_UNSET, *, ops_done: int = 112, complete: bool = True) -> dict:
    """A ``vf.dump`` result: ``vfdump.to_json`` shape + ``ops_done`` + (G-107 addendum) ``accepted_match``: None | [{cpu, match, diffs:[{point, expected, actual}]}]."""
    def word(ratio, off):
        return {"word": "0x00000000", "ratio": ratio, "v_target_raw": 0, "v_target_v": 1.0, "static": False, "offset_raw": 0, "offset_mv": off}

    doc = {"simulator": True, "complete": complete, "stopped": None,
           "units": [{"name": "P0", "kind": "P", "cluster": None, "cluster_cpus": [0],
                      "cpus": [{"cpu": 0, "name": "P0", "oc": word(57, 0.0), "points": [{"point": n, **word(40 + n, -10.0 * n)} for n in range(1, 4)], "end": None}],
                      "measured": None}],
           "differences": [], "problems": [], "ops_done": ops_done}
    if accepted_match is not _UNSET:
        doc["accepted_match"] = accepted_match
    return doc


class FakeHelper(V3Ops):
    def __init__(self) -> None:
        req_r, req_w = os.pipe()    # server -> helper
        resp_r, resp_w = os.pipe()  # helper -> server
        self._in = os.fdopen(req_r, "rb")
        self._out = os.fdopen(resp_w, "wb", buffering=0)
        self.link = HelperLink(os.fdopen(resp_r, "rb"), os.fdopen(req_w, "wb", buffering=0)).start()
        self.requests: list[dict] = []
        self.plans: dict[str, dict] = {}
        self.simple_plans = False  # True: tune.plan / tune.apply answer with the plain nonce-bound plan/apply (nonce-replay tests through the server)
        self._wlock = threading.Lock()
        self.hold = threading.Event()  # clear it to make vf.dump hang until set again
        self.hold.set()
        # Opt-in behaviour for the dashboard tests; every default keeps the Contract 8 tests above this file unchanged.
        self.preflight_result: dict | None = None   # None -> {"ok": True}
        self.vf_result: dict | None = None          # None -> {"cpus": []}
        self.vf_error: dict | None = None           # {"code", "message", "detail"?} -> answer vf.dump with this error instead
        self.vf_progress = 0                        # progress events (steps) sent before a vf.dump is answered
        self.ops_budget = 600
        self.audit_records: list = []
        self.audit_truncated = False
        self.cancel_cancels = False                 # True -> cancel{op_id} ends the matching held vf.dump with CANCELLED (default: BUSY, as before)
        self._cancelled: set[str] = set()
        self._init_v3()                             # Contract 9 state (tests/web/fake_v3.py); inert until a v3 op is sent
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def emit(self, obj: dict) -> None:
        with self._wlock:
            try:
                self._out.write(json.dumps(obj).encode() + b"\n")
            except (ValueError, OSError):
                pass  # the test already closed the pipe

    # ---- events the test emits by hand (deterministic: no timer threads) ----
    def heartbeat(self, state: str = "idle", ops_used: int = 0, ops_budget: int | None = None, op_id: str | None = None) -> None:
        """G-107 addendum: heartbeat {state, ops_used, ops_budget}."""
        self.emit({"event": "heartbeat", "op_id": op_id, "state": state, "ops_used": ops_used,
                   "ops_budget": self.ops_budget if ops_budget is None else ops_budget})

    def progress(self, op_id: str, step: int, of: int, ops_used: int, ops_budget: int | None = None) -> None:
        self.emit({"event": "progress", "op_id": op_id, "step": step, "of": of, "ops_used": ops_used,
                   "ops_budget": self.ops_budget if ops_budget is None else ops_budget})

    def telemetry(self, values: dict | None = None, op_id: str = "t1") -> None:
        v = telemetry_values() if values is None else values
        self.emit({"event": "telemetry", "op_id": op_id, "t": v.get("ts"), "values": v})

    def raw(self, data: bytes) -> None:
        with self._wlock:
            self._out.write(data)

    def kill(self) -> None:
        self._out.close()

    def close(self) -> None:
        self.link.close()  # helper side reads EOF and exits
        for f, t in ((self._out, None), (self.link._r, self.link._thread), (self._in, self._t)):
            try:
                f.close() if t is None else (t.join(2), f.close())
            except OSError:
                pass

    def _reply(self, rid, ok, **kw) -> None:
        self.emit({"id": rid, "ok": ok, **kw})

    def _err(self, rid, code, msg, **detail) -> None:
        e = {"code": code, "message": msg}
        if detail:
            e["detail"] = detail
        self._reply(rid, False, error=e)

    def _serve(self) -> None:
        while True:
            line = self._in.readline(70000)
            if not line:
                return
            try:
                req = json.loads(line)
                assert set(req) == {"id", "op", "args"} and isinstance(req["id"], str) and len(req["id"]) <= 64
            except (ValueError, AssertionError):
                self._err(None, "BAD_REQUEST", "bad line")
                continue
            self.requests.append(req)
            rid, op, args = req["id"], req["op"], req["args"]
            if op == "preflight":
                res = {"ok": True} if self.preflight_result is None else self.preflight_result
                if "checks" in res and "topology" not in res and self.topology_enabled:   # a full report gains the live G-128 topology (the favored set can change per test)
                    res = {**res, "topology": topology_view()}
                self._reply(rid, True, result=res)
            elif op == "plan" or (self.simple_plans and op == "tune.plan"):
                steps = [{"step": 1, "what": "noop"}]
                plan_id = hashlib.sha256(json.dumps(steps).encode()).hexdigest()
                nonce = "".join(secrets.choice(ALPHABET) for _ in range(6))
                self.plans[plan_id] = {"nonce": nonce, "used": False}
                self._reply(rid, True, result={"plan_id": plan_id, "nonce": nonce, "steps": steps, "ops_needed": 1, "ops_left": 599,
                                               "expires_s": 600})
            elif op == "apply" or (self.simple_plans and op == "tune.apply"):
                p = self.plans.get(args.get("plan_id"))
                if p is None:
                    self._err(rid, "PLAN_STALE", "unknown plan")
                elif p["used"] or args.get("typed") != "CONFIRM " + p["nonce"]:
                    self._err(rid, "CONFIRM_MISMATCH", "typed text does not match")
                else:
                    p["used"] = True
                    self._reply(rid, True, result={"applied": True})
            elif op == "vf.dump":
                threading.Thread(target=self._vf_dump, args=(rid,), daemon=True).start()
            elif op == "audit.tail":
                self._reply(rid, True, result={"path": "/var/lib/arltuner/audit.jsonl", "records": self.audit_records[-args.get("n", 20):],
                                               "truncated": self.audit_truncated})
            elif op == "telemetry.start":
                iv = args.get("interval_s", 1.0)
                restarted = self.tele_interval is not None and iv != self.tele_interval
                same = self.tele_interval is not None and iv == self.tele_interval
                self.tele_interval = iv
                self._reply(rid, True, result={"running": True, "already_running": same, "restarted": restarted, "op_id": rid, "interval_s": iv})
            elif op == "telemetry.stop":
                self._reply(rid, True, result={"stopped": True, "op_id": "t1"})
            elif op in V3_OPS:
                self.handle_v3(rid, op, args)
            elif op == "cancel" and (self.cancel_cancels or args.get("op_id") in self._measuring):
                self._cancelled.add(args.get("op_id", ""))
                self._reply(rid, True, result={"cancelled": True, "op_id": args.get("op_id"), "state": "planning"})
            elif op == "cancel":
                self._err(rid, "BUSY", "busy", state="applying", internal_path="/root/secret")
            else:
                self._err(rid, "UNKNOWN_OP", op)

    def _vf_dump(self, rid: str) -> None:
        n = self.vf_progress
        for i in range(n):
            self.progress(rid, i + 1, n, 10 * (i + 1))
        deadline = time.monotonic() + 10
        while not self.hold.wait(0.02):
            if rid in self._cancelled:
                self._err(rid, "CANCELLED", "vf.dump was cancelled between transactions", state="planning", partial={"units": []})
                return
            if time.monotonic() > deadline:
                break
        if self.vf_error is not None:
            e = self.vf_error
            self._err(rid, e["code"], e["message"], **e.get("detail", {}))
        else:
            self._reply(rid, True, result={"cpus": []} if self.vf_result is None else self.vf_result)
