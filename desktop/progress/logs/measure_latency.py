"""Read-only latency measurement from the helper's audit tail (no hardware access: preflight reads files, audit.tail reads the log).
Host: lnx. Usage: python3 desktop/progress/logs/measure_latency.py"""
import json, sys, statistics
from datetime import datetime
sys.path.insert(0, "src")
from arltuner.web.helperlink import connect_helper, HelperConnectError

try:
    link = connect_helper()
except HelperConnectError as e:
    print("CONNECT", e.kind, e); sys.exit(1)
try:
    res = link.call("audit.tail", {"n": 200}, timeout=30)
finally:
    link.close()
r = res.get("result", res)
recs = r.get("records", [])
print("records", len(recs), "truncated", r.get("truncated"), "path", r.get("path"))
def ts(x):
    t = x.get("ts")
    if isinstance(t, (int, float)): return float(t)
    try: return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except Exception: return None
kinds = {}
for x in recs: kinds[x.get("event")] = kinds.get(x.get("event"), 0) + 1
print("events", kinds)
print("keys", sorted(recs[-1].keys()) if recs else None)
# per-transaction: attempt -> result (same seq or consecutive), result -> next attempt gap
seq = [(ts(x), x.get("event"), x.get("cmd"), x.get("cpu")) for x in recs if ts(x) is not None]
seq.sort()
att_res, gaps, rb = [], [], []
last_att = last_res = None
for t, ev, cmd, cpu in seq:
    if ev == "attempt":
        if last_res is not None and t - last_res < 30: gaps.append(t - last_res)
        last_att = t
    elif ev == "result" and last_att is not None:
        att_res.append(t - last_att); last_res = t
    elif ev == "readback" and last_res is not None:
        rb.append(t - last_res)
def s(name, v):
    if v: print(f"{name}: n={len(v)} median={statistics.median(v)*1000:.1f} ms p90={sorted(v)[int(len(v)*0.9)-1]*1000:.1f} ms max={max(v)*1000:.1f} ms")
    else: print(name, "none")
s("attempt->result", att_res); s("result->readback", rb); s("result->next attempt", gaps)
if seq: print("span", datetime.fromtimestamp(seq[0][0]).isoformat(), "->", datetime.fromtimestamp(seq[-1][0]).isoformat())
