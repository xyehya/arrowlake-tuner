"""H3 (desktop track, D-047): live gate for the G-212 ring intent — one ring point offset through tune.plan {ring:...}, read back, then revert
by restoring the accepted state. Host lnx. Usage: python3 desktop/progress/logs/h3_ring_gate.py"""
import json, sys, time
sys.path.insert(0, "src")
from arltuner.web.helperlink import connect_helper

def plan_apply(link, intent, label):
    p = link.call("tune.plan", {"intent": intent}, timeout=60)
    if not p.get("ok"):
        print(label, "PLAN ERROR", json.dumps(p.get("error"))); sys.exit(1)
    pl = p["result"]
    ws = [s for s in pl.get("steps", []) if str(s.get("kind", "")).startswith("write")]
    print(f"{label}: plan writes={pl.get('writes')} steps={[ (s.get('kind'), s.get('unit'), s.get('point'), s.get('offset_mv')) for s in ws]}")
    t = time.monotonic()
    r = link.call("tune.apply", {"plan_id": pl["plan_id"], "typed": f"CONFIRM {pl['nonce']}"}, timeout=300)
    dt = time.monotonic() - t
    if not r.get("ok"):
        print(label, f"APPLY ERROR after {dt:.2f}s", json.dumps(r.get("error"))); sys.exit(1)
    res = r["result"]; rb = res.get("readback", [])
    print(f"{label}: apply {dt:.2f}s ops={res.get('ops_used')} writes_sent={res.get('writes_sent')} readback {sum(1 for x in rb if x.get('match'))}/{len(rb)} "
          f"ring_refused={res.get('ring_refused')} stop={res.get('stop')}")
    for x in rb:
        if x.get("unit") == "ring": print(f"   ring P2={x.get('point')} expected={x.get('expected')} actual={x.get('actual')} match={x.get('match')}")
    return res

link = connect_helper()
try:
    rd = link.call("tune.read", {}, timeout=300)
    feats = (rd.get("result") or {}).get("intent_features")
    print("tune.read ok", rd.get("ok"), "intent_features", feats)
    if "ring" not in (feats or []):
        print("installed helper lacks the ring intent; stop"); sys.exit(1)
    plan_apply(link, {"ring": {"points": {"7": -5}}}, "ring P7 -5 mV")
    plan_apply(link, {"restore": "accepted"}, "revert (restore accepted)")
    last = (link.call("tune.last", {}, timeout=30).get("result") or {}).get("result") or {}
    am = last.get("accepted_match") or []
    print("accepted_match", sum(1 for c in am if c.get("match") or c.get("equivalent")), "/", len(am))
finally:
    link.close()
