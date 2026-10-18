"""Restore the accepted state through the installed helper (plan -> check -> apply), timed. Host lnx. Hardware: T2 volatile writes (D-047 standing go).
Usage: python3 desktop/progress/logs/restore_accepted.py [--apply]   (without --apply: plan only, prints the steps)"""
import json, sys, time
sys.path.insert(0, "src")
from arltuner.web.helperlink import connect_helper

APPLY = "--apply" in sys.argv
link = connect_helper()
try:
    p = link.call("tune.plan", {"intent": {"restore": "accepted"}}, timeout=60)
    if not p.get("ok"):
        print("PLAN ERROR", json.dumps(p.get("error"))); sys.exit(1)
    plan = p["result"]
    writes = [s for s in plan.get("steps", []) if str(s.get("kind", s.get("op", ""))).lower().startswith("write") or s.get("cmd") in ("0x11", 17)]
    print(f"plan_id {plan['plan_id'][:12]}… writes={plan.get('writes')} reads={plan.get('reads')} ops_needed={plan.get('ops_needed')} complete={plan.get('complete')}")
    for w in plan.get("warnings", []): print("  warning:", w.get("code"), w.get("text", "")[:140])
    for s in plan.get("steps", []):
        txt = s.get("text") or s.get("purpose") or json.dumps(s)[:160]
        if "write" in json.dumps(s).lower(): print("  step:", str(txt)[:170])
    if not APPLY:
        sys.exit(0)
    t = time.monotonic()
    r = link.call("tune.apply", {"plan_id": plan["plan_id"], "typed": f"CONFIRM {plan['nonce']}"}, timeout=600)
    dt = time.monotonic() - t
    if not r.get("ok"):
        print(f"APPLY ERROR after {dt:.2f} s", json.dumps(r.get("error"))); sys.exit(1)
    res = r["result"]
    rb = res.get("readback", [])
    print(f"apply wall {dt:.2f} s; ops_used={res.get('ops_used')} msr_writes={res.get('msr_writes')} writes_sent={res.get('writes_sent')} complete={res.get('complete')}")
    print(f"readback {sum(1 for x in rb if x.get('match'))}/{len(rb)} match; residual={res.get('residual')} ring_refused={res.get('ring_refused')} stop={res.get('stop')}")
    for w in res.get("warnings", []): print("  warning:", w if isinstance(w, str) else w.get("code"), "" if isinstance(w, str) else w.get("text", "")[:140])
finally:
    link.close()
