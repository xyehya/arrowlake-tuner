"""H4 (desktop track, D-047): live gate for the G-214 power ops. PL2 295 -> 275 W (operator's 2026-10-15 runtime choice), revert to the boot
values, re-apply 275 W. Host lnx. Usage: python3 desktop/progress/logs/h4_power_gate.py"""
import json, sys, time
sys.path.insert(0, "src")
from arltuner.web.helperlink import connect_helper

def step(link, args, label):
    p = link.call("power.plan", args, timeout=60)
    if not p.get("ok"):
        print(label, "PLAN ERROR", json.dumps(p.get("error"))); sys.exit(1)
    pl = p["result"]
    print(f"{label}: plan steps={json.dumps(pl.get('steps'))[:300]} warnings={[w if isinstance(w, str) else w.get('code') for w in pl.get('warnings', [])]}")
    t = time.monotonic()
    r = link.call("power.apply", {"plan_id": pl["plan_id"], "typed": f"CONFIRM {pl['nonce']}"}, timeout=120)
    dt = time.monotonic() - t
    if not r.get("ok"):
        print(label, f"APPLY ERROR after {dt:.2f}s", json.dumps(r.get("error"))); sys.exit(1)
    print(f"{label}: apply {dt:.3f}s result={json.dumps(r['result'])[:400]}")

def show(link, label):
    r = link.call("power.read", {}, timeout=60)["result"]
    pc, m = r["powercap"], r["msr"]["limit"]
    print(f"{label}: powercap PL1 {pc['pl1_w']} PL2 {pc['pl2_w']} tau {pc['tau_s']:.3f} | MSR 0x610 PL1 {m['pl1_w']} PL2 {m['pl2_w']} tau {m['tau_s']:.3f} locked {m['locked']}")

link = connect_helper()
try:
    show(link, "before")
    step(link, {"power": {"pl2_w": 275}}, "PL2 275")
    show(link, "after 275")
    step(link, {"revert": True}, "revert")
    show(link, "after revert")
    step(link, {"power": {"pl2_w": 275}}, "PL2 275 (final)")
    show(link, "final")
finally:
    link.close()
