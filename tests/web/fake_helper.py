"""Fake helper speaking Contract 8 (docs/ARCHITECTURE.md) over OS pipes. No hardware, no real helper."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading

from arltuner.web.helperlink import HelperLink

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789"  # [A-Z2-9] per Contract 8


class FakeHelper:
    def __init__(self) -> None:
        req_r, req_w = os.pipe()    # server -> helper
        resp_r, resp_w = os.pipe()  # helper -> server
        self._in = os.fdopen(req_r, "rb")
        self._out = os.fdopen(resp_w, "wb", buffering=0)
        self.link = HelperLink(os.fdopen(resp_r, "rb"), os.fdopen(req_w, "wb", buffering=0)).start()
        self.requests: list[dict] = []
        self.plans: dict[str, dict] = {}
        self._wlock = threading.Lock()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def emit(self, obj: dict) -> None:
        with self._wlock:
            self._out.write(json.dumps(obj).encode() + b"\n")

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
                self._reply(rid, True, result={"ok": True})
            elif op == "plan":
                steps = [{"step": 1, "what": "noop"}]
                plan_id = hashlib.sha256(json.dumps(steps).encode()).hexdigest()
                nonce = "".join(secrets.choice(ALPHABET) for _ in range(6))
                self.plans[plan_id] = {"nonce": nonce, "used": False}
                self._reply(rid, True, result={"plan_id": plan_id, "nonce": nonce, "steps": steps, "ops_needed": 1, "ops_left": 599,
                                               "expires_s": 600})
            elif op == "apply":
                p = self.plans.get(args.get("plan_id"))
                if p is None:
                    self._err(rid, "PLAN_STALE", "unknown plan")
                elif p["used"] or args.get("typed") != "CONFIRM " + p["nonce"]:
                    self._err(rid, "CONFIRM_MISMATCH", "typed text does not match")
                else:
                    p["used"] = True
                    self._reply(rid, True, result={"applied": True})
            elif op == "busy":
                self._err(rid, "BUSY", "busy", state="applying")
            else:
                self._err(rid, "UNKNOWN_OP", op)
