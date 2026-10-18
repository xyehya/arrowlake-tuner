#!/usr/bin/env python3
"""Record the Curves + Stress fixtures (G-218) from the SIMULATOR helper. No hardware, no sudo, no installed helper.

    python3 desktop/app/ui/fixtures/g218/gen_fixtures_g218.py

Same method as ../gen_fixtures.py (it reuses its Client): a private temp socket / audit log / lock / state dir, the wire protocol (JSON lines), and what the
helper really answered is written next to this file, plus `fixtures-g218.js` (all of the JSON as one ES module: Chrome checks JSON modules and fetch()
against connect-src, a JS module is governed by script-src 'self', see ../README.md). The files live in this sub-directory so that the G-205 check
"fixtures.js equals the *.json files of fixtures/" stays about G-205's files. Nothing here is a reference-machine number (D-036): every value is the
simulator's own model. Python stdlib only.
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
sys.path.insert(0, str(HERE.parent))
import gen_fixtures as base  # noqa: E402  (the G-205 recorder: Client, protocol helpers)

BUNDLE: dict[str, Any] = {}
TMP: list[str] = []  # the temp dir of this run: a host path, not part of a fixture
TOPOLOGY_UNITS = ["P0", "P1", "EC0"]


def dump(name: str, obj: Any) -> None:
    text = json.dumps(obj, indent=1, sort_keys=True) + "\n"
    try:  # the host's boot id is not part of a fixture
        text = text.replace(Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "00000000-0000-4000-8000-000000000000")
    except OSError:
        pass
    for t in TMP:
        text = text.replace(t, "/var/lib/arltuner")
    (HERE / name).write_text(text, encoding="utf-8")
    BUNDLE[name.removesuffix(".json")] = json.loads(text)
    print("wrote", name, len(text), "bytes")


def long_op(c: base.Client, op: str, args: dict[str, Any], *, cancel_after_events: int = 0, stop_op: bool = False) -> dict[str, Any]:
    """Send a long op; return {request, events, response}. With cancel_after_events, `cancel {op_id}` (or `stress.stop` with stop_op) is sent once that many events have arrived."""
    rid = c.send(op, args)
    events: list[dict[str, Any]] = []
    cancelled = cancel_after_events <= 0
    cancel_reply: dict[str, Any] | None = None
    cancel_id = ""
    while True:
        msg = c.line()
        if msg is None:
            raise RuntimeError(f"no answer to {op}")
        if msg.get("id") == rid:
            out: dict[str, Any] = {"request": {"op": op, "args": args}, "events": events, "response": msg}
            if cancel_reply is not None:
                out["cancel"] = {"request": {"op": "stress.stop", "args": {}} if stop_op else {"op": "cancel", "args": {"op_id": rid}}, "response": cancel_reply}
            return out
        if cancel_id and msg.get("id") == cancel_id:
            cancel_reply = msg
            continue
        events.append(msg)
        if not cancelled and len(events) >= cancel_after_events:
            cancelled = True
            cancel_id = c.send(*(("stress.stop", {}) if stop_op else ("cancel", {"op_id": rid})))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="g218-fixtures-"))
    TMP.append(str(tmp))
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
        c = base.Client(str(sock))

        # ---- Curves ----
        dump("curves-list-empty.json", c.call("curves.list"))
        stock = long_op(c, "vf.measure", {"mode": "quick", "units": TOPOLOGY_UNITS, "name": "stock"})
        dump("vf-measure-stock.json", stock)
        dense = long_op(c, "vf.measure", {"mode": "dense", "units": ["P0", "EC0"], "name": "second"})
        dump("vf-measure-dense.json", dense)
        listed = c.call("curves.list")
        dump("curves-list.json", listed)
        sid = stock["response"]["result"]["curve_id"]
        did = dense["response"]["result"]["curve_id"]
        dump("curves-get.json", {"request": {"op": "curves.get", "args": {"id": sid}}, "response": c.call("curves.get", {"id": sid})})
        dump("curves-update-rename.json", {"request": {"op": "curves.update", "args": {"id": did, "name": "renamed"}}, "response": c.call("curves.update", {"id": did, "name": "renamed"})})
        dump("curves-update-baseline.json", {"request": {"op": "curves.update", "args": {"id": sid, "baseline": True}}, "response": c.call("curves.update", {"id": sid, "baseline": True})})
        cancelled = long_op(c, "vf.measure", {"mode": "dense", "units": ["P0", "P1", "EC0"], "name": "to-cancel"}, cancel_after_events=24)
        dump("vf-measure-cancelled.json", cancelled)
        after = c.call("curves.list")
        dump("curves-list-after-cancel.json", after)
        victim = (cancelled["response"].get("error") or {}).get("detail", {}).get("curve_id") or did
        dump("curves-delete.json", {"request": {"op": "curves.delete", "args": {"id": victim}}, "response": c.call("curves.delete", {"id": victim})})
        # the 12-unit quick run is only recorded to see whether the helper omits units from a record at the 64 KiB line
        full = long_op(c, "vf.measure", {"mode": "quick", "name": "all-units"})
        rec = full["response"].get("result", {}).get("record", {})
        print("12-unit quick: omitted_units =", rec.get("omitted_units"), "record bytes", len(json.dumps(rec)))
        if rec.get("omitted_units"):
            dump("vf-measure-all-units.json", full)

        # ---- Stress ----
        dump("stress-start.json", long_op(c, "stress.start", {"kind": "cpu", "method": "integer", "duration_s": 30, "units": ["P0", "EC0"]}))
        dump("stress-start-memory.json", long_op(c, "stress.start", {"kind": "memory", "duration_s": 30, "memory_mb": 2048}))
        halted = long_op(c, "stress.start", {"kind": "cpu", "method": "vector-math", "duration_s": 3600, "ratio_cap": 48}, cancel_after_events=4, stop_op=True)
        dump("stress-start-cancelled.json", halted)
        dump("stress-list.json", c.call("stress.list"))
        # a stop with nothing running: the helper's own answer
        dump("stress-stop-idle.json", {"request": {"op": "stress.stop", "args": {}}, "response": c.call("stress.stop")})
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    body = json.dumps(BUNDLE, indent=1, sort_keys=True)
    (HERE / "fixtures-g218.js").write_text("// GENERATED by gen_fixtures_g218.py from the *.json files next to it. Do not edit.\nexport default " + body + ";\n", encoding="utf-8")
    print("wrote fixtures-g218.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
