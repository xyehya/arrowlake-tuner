#!/usr/bin/env python3
"""Regenerate the JSON fixtures in this directory from the SIMULATOR helper (G-205). No hardware, no sudo, no installed helper.

    python3 desktop/app/ui/fixtures/gen_fixtures.py

Starts `tools/arltuner-helper.py --sim --socket` on a private temp socket / audit / lock / state dir (shipped `policies/default.toml` copied
into the temp dir), talks the wire protocol (JSON lines, docs/ARCHITECTURE.md contract 8/9) and writes what the helper really answered.
Also writes fixtures.js (all of them as one ES module, see the end of main()). Nothing here is a reference-machine number (D-036): every value comes from the simulator's own model. Python stdlib only.
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
TELEMETRY_S = 10.0
INTERVAL_S = 1.0


class Client:
    def __init__(self, path: str) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        self.buf = b""
        self.seq = 0

    def send(self, op: str, args: dict[str, Any] | None = None) -> str:
        self.seq += 1
        rid = f"c{self.seq}"
        self.sock.sendall((json.dumps({"id": rid, "op": op, "args": args or {}}, separators=(",", ":")) + "\n").encode())
        return rid

    def line(self, timeout: float = 120) -> dict[str, Any] | None:
        """Next JSON line, or None if `timeout` seconds pass without a complete one."""
        end = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except (TimeoutError, socket.timeout):
                return None
            if not chunk:
                raise RuntimeError("helper closed the connection")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        return json.loads(raw)

    def call(self, op: str, args: dict[str, Any] | None = None, *, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Send one request and return its response (ok or error); events seen meanwhile go to `events`."""
        rid = self.send(op, args)
        while True:
            msg = self.line()
            if msg is None:
                raise RuntimeError(f"no answer to {op}")
            if msg.get("id") == rid:
                return msg
            if events is not None:
                events.append(msg)

    def collect(self, seconds: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            msg = self.line(left)
            if msg is not None:
                out.append(msg)
        return out


BUNDLE: dict[str, Any] = {}


def dump(name: str, obj: Any) -> None:
    text = json.dumps(obj, indent=1, sort_keys=True) + "\n"
    try:  # the host's boot id is not part of a fixture
        text = text.replace(Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "00000000-0000-4000-8000-000000000000")
    except OSError:
        pass
    (HERE / name).write_text(text, encoding="utf-8")
    BUNDLE[name.removesuffix(".json")] = json.loads(text)
    print("wrote", name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="g205-fixtures-"))
    toml, policy, sock = tmp / "helper.toml", tmp / "policy.toml", tmp / "h.sock"
    user, group = pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name
    toml.write_text('[access]\nusers = ["%s"]\ngroup = "%s"\n' % (user, group), encoding="utf-8")
    os.chmod(toml, 0o600)
    shutil.copy(ROOT / "policies" / "default.toml", policy)
    os.chmod(policy, 0o600)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(ROOT / "tools" / "arltuner-helper.py"), "--sim", "--socket", "--socket-path", str(sock), "--helper-toml", str(toml),
         "--policy", str(policy), "--audit", str(tmp / "audit.jsonl"), "--lock", str(tmp / "lock"), "--state-dir", str(tmp / "state")],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=tmp)
    try:
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.1)
        time.sleep(0.2)
        c = Client(str(sock))

        pre = c.call("preflight")
        pre["result"]["board"] = "Simulated board"  # the helper reads DMI from the host even in --sim
        dump("preflight.json", pre)

        dump("telemetry-start.json", c.call("telemetry.start", {"interval_s": INTERVAL_S}))
        events = c.collect(TELEMETRY_S)
        dump("telemetry-stop.json", c.call("telemetry.stop", events=events))
        dump("telemetry-events.json", {"interval_s": INTERVAL_S, "events": events})

        side: list[dict[str, Any]] = []
        read = c.call("tune.read", events=side)
        dump("tune-read.json", read)
        dump("tune-read-events.json", side)

        # one P-core point: pick the first row of P0's V/F table the read reports
        intent = {"scope": "unit", "rows": [{"target": "P0", "offset": {"kind": "points", "mv": {"53": -20}}, "v_target_v": None, "ratio": None}]}
        plan = c.call("tune.plan", {"intent": intent})
        dump("tune-plan.json", {"request": {"op": "tune.plan", "args": {"intent": intent}}, "response": plan})
        if plan.get("ok"):
            res = plan["result"]
            apply_args = {"plan_id": res["plan_id"], "typed": "CONFIRM " + res["nonce"]}
            ev: list[dict[str, Any]] = []
            applied = c.call("tune.apply", apply_args, events=ev)
            dump("tune-apply.json", {"request": {"op": "tune.apply", "args": {"plan_id": res["plan_id"], "typed": "CONFIRM <nonce>"}}, "response": applied})
            dump("tune-apply-events.json", ev)
        dump("profiles-list.json", c.call("profiles.list"))
        tail = c.call("audit.tail", {"n": 20})
        tail["result"]["path"] = "/var/lib/arltuner/audit.jsonl"  # the temp path of the run
        dump("audit-tail.json", tail)
        dump("tune-last.json", c.call("tune.last"))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    # The same data as one ES module: Chrome checks JSON module imports against connect-src (which the strict CSP leaves at `ipc:`), while a JS module
    # is governed by script-src 'self'. The mock backend imports this file; shell.test.mjs checks it equals the JSON files.
    # bundle every recording in this directory (other generators, e.g. gen_profiles_fixtures.py, add their own *.json; G-213)
    for extra in sorted(HERE.glob("*.json")):
        BUNDLE.setdefault(extra.stem, json.loads(extra.read_text(encoding="utf-8")))
    body = json.dumps(BUNDLE, indent=1, sort_keys=True)
    (HERE / "fixtures.js").write_text("// GENERATED by gen_fixtures.py from the *.json files next to it. Do not edit.\nexport default " + body + ";\n", encoding="utf-8")
    print("wrote fixtures.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
