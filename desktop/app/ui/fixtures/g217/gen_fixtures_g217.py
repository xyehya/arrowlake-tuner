#!/usr/bin/env python3
"""Record the power.* / runtime.* fixtures from the SIMULATOR helper (G-217). No hardware, no sudo, no installed helper, temp paths only.

    python3 desktop/app/ui/fixtures/g217/gen_fixtures_g217.py

One simulator run on the helper of THIS checkout (D-049: the CPU's own limits are the only bounds, so no [power] / [runtime] policy keys are needed; the temp copy of policies/default.toml is
its own, the operator's policy is never touched). The helper reads the tuning words first (`tune.read`: the CPU's voltage ceiling and OC ratio limit, 0x28 / 0x01), then plans, applies, reads back
and reverts. `cpu_limits` is recorded as the helper returns it on each read (the reads of the simulator hold the per-op nulls: tune.read has no power / cpufreq / uncore part, power.read and
runtime.read have theirs). Every `policy` object is dropped from the recordings. The recordings keep the refusals that are CPU / process rules (NO_JOURNAL, the held parking BAD_REQUEST, UNCORE_RANGE).
Files: power-*.json / runtime-*.json next to this script (request + response where a request matters); then fixtures-g217.js is rebuilt from EVERY *.json here (the format gen_fixtures.py uses).
Nothing here is a reference-machine measurement (D-036): the numbers are the simulator's own model. Python stdlib only.
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
sys.path.insert(0, str(HERE.parent))
from gen_fixtures import ROOT, Client  # noqa: E402

POWER_SET = {"pl1_w": 230, "pl2_w": 280, "tau_s": 56}


def strip_policy(x: Any) -> Any:
    """D-049: the GUI shows no policy, so a recording carries none."""
    if isinstance(x, dict):
        return {k: strip_policy(v) for k, v in x.items() if k != "policy"}
    if isinstance(x, list):
        return [strip_policy(v) for v in x]
    return x


def write(name: str, obj: Any) -> None:
    text = json.dumps(strip_policy(obj), indent=1, sort_keys=True) + "\n"
    try:  # the host's boot id is not part of a fixture
        text = text.replace(Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "00000000-0000-4000-8000-000000000000")
    except OSError:
        pass
    (HERE / name).write_text(text, encoding="utf-8")
    print("wrote", name)


def scrub(resp: dict[str, Any], tmp: Path) -> dict[str, Any]:
    """The temp dir of the run is not part of a fixture (journal paths)."""
    return json.loads(json.dumps(resp).replace(str(tmp), "/var/lib/arltuner"))


def pair(op: str, args: dict[str, Any], resp: dict[str, Any], tmp: Path) -> dict[str, Any]:
    shown = dict(args)
    if "typed" in shown:
        shown["typed"] = "CONFIRM <nonce>"
    return {"request": {"op": op, "args": shown}, "response": scrub(resp, tmp)}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="g217-fixtures-"))
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

        def call(op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
            return c.call(op, args)

        # ---- before tune.read: the reads with the CPU's voltage ceiling / OC ratio limit not read yet (cpu_limits.P/E/ring = null: the "Read CPU state first" shape) ----
        write("power-read-unread.json", scrub(call("power.read"), tmp))
        write("runtime-read-unread.json", scrub(call("runtime.read"), tmp))
        tr = call("tune.read")  # reads 0x28 / 0x01 (simulator): from here on cpu_limits.P / E / ring hold the CPU's limits
        assert tr.get("ok"), tr

        # ---- before any write: the reads, and the refusals that are CPU / process rules ----
        write("power-revert-nojournal.json", pair("power.plan", {"revert": True}, call("power.plan", {"revert": True}), tmp))
        write("runtime-revert-nojournal.json", pair("runtime.plan", {"revert": True}, call("runtime.plan", {"revert": True}), tmp))
        write("runtime-plan-parking.json", pair("runtime.plan", {"parking": {"8": False}}, call("runtime.plan", {"parking": {"8": False}}), tmp))
        write("power-read.json", scrub(call("power.read"), tmp))
        rt = scrub(call("runtime.read"), tmp)
        write("runtime-read.json", rt)
        rows = rt["result"]["caps"]
        pcpu = next(r["cpu"] for r in rows if r["kind"] == "P" and r["online"])
        ecpu = next(r["cpu"] for r in rows if r["kind"] == "E" and r["online"])

        # ---- power: plan -> apply -> read -> revert -> read ----
        plan = call("power.plan", {"power": POWER_SET})
        assert plan.get("ok"), plan
        write("power-plan.json", pair("power.plan", {"power": POWER_SET}, plan, tmp))
        res = plan["result"]
        ev: list[dict[str, Any]] = []
        applied = c.call("power.apply", {"plan_id": res["plan_id"], "typed": "CONFIRM " + res["nonce"]}, events=ev)
        assert applied.get("ok"), applied
        write("power-apply.json", pair("power.apply", {"plan_id": res["plan_id"], "typed": "CONFIRM " + res["nonce"]}, applied, tmp))
        write("power-apply-events.json", scrub({"events": ev}, tmp)["events"])
        write("power-read-applied.json", scrub(call("power.read"), tmp))
        rplan = call("power.plan", {"revert": True})
        assert rplan.get("ok"), rplan
        write("power-revert-plan.json", pair("power.plan", {"revert": True}, rplan, tmp))
        rres = rplan["result"]
        rapplied = call("power.apply", {"plan_id": rres["plan_id"], "typed": "CONFIRM " + rres["nonce"]})
        assert rapplied.get("ok"), rapplied
        write("power-revert-apply.json", pair("power.apply", {"plan_id": rres["plan_id"], "typed": "CONFIRM " + rres["nonce"]}, rapplied, tmp))
        write("power-read-reverted.json", scrub(call("power.read"), tmp))

        # ---- runtime: plan (a P cap, an E-cluster cap the cpufreq grid cannot hit, an uncore max) -> apply -> read -> revert -> read ----
        rt_args = {"caps": {str(pcpu): 50, str(ecpu): 23}, "uncore": {"max_ratio": 38}}
        rplan = call("runtime.plan", rt_args)
        assert rplan.get("ok"), rplan
        write("runtime-plan.json", pair("runtime.plan", rt_args, rplan, tmp))
        res = rplan["result"]
        ev = []
        applied = c.call("runtime.apply", {"plan_id": res["plan_id"], "typed": "CONFIRM " + res["nonce"]}, events=ev)
        assert applied.get("ok"), applied
        write("runtime-apply.json", pair("runtime.apply", {"plan_id": res["plan_id"], "typed": "CONFIRM " + res["nonce"]}, applied, tmp))
        write("runtime-apply-events.json", scrub({"events": ev}, tmp)["events"])
        write("runtime-read-applied.json", scrub(call("runtime.read"), tmp))
        rplan = call("runtime.plan", {"revert": True})
        assert rplan.get("ok"), rplan
        write("runtime-revert-plan.json", pair("runtime.plan", {"revert": True}, rplan, tmp))
        rres = rplan["result"]
        rapplied = call("runtime.apply", {"plan_id": rres["plan_id"], "typed": "CONFIRM " + rres["nonce"]})
        assert rapplied.get("ok"), rapplied
        write("runtime-revert-apply.json", pair("runtime.apply", {"plan_id": rres["plan_id"], "typed": "CONFIRM " + rres["nonce"]}, rapplied, tmp))
        write("runtime-read-reverted.json", scrub(call("runtime.read"), tmp))
        out_of_range = {"uncore": {"max_ratio": 45}}
        write("runtime-plan-range.json", pair("runtime.plan", out_of_range, call("runtime.plan", out_of_range), tmp))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    bundle = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(HERE.glob("*.json"))}
    (HERE / "fixtures-g217.js").write_text("// GENERATED by gen_fixtures_g217.py from the *.json files next to it. Do not edit.\nexport default " + json.dumps(bundle, indent=1, sort_keys=True) + ";\n", encoding="utf-8")
    print("wrote fixtures-g217.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
