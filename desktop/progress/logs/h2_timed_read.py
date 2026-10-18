"""H2 (desktop track, D-047): one timed tune.read on the reference machine through the installed helper, then audit.tail gap stats.
Host: lnx. Hardware: mailbox READS only (each one a WRMSR 0x150 through the guard). Usage: python3 desktop/progress/logs/h2_timed_read.py"""
import json, statistics, sys, time
from datetime import datetime
sys.path.insert(0, "src")
from arltuner.web.helperlink import connect_helper

t0 = time.monotonic()
link = connect_helper()
t_conn = time.monotonic() - t0
try:
    t1 = time.monotonic()
    resp = link.call("tune.read", {}, timeout=600)
    t_read = time.monotonic() - t1
    ok = resp.get("ok")
    res = resp.get("result") or {}
    print(f"connect+preflight {t_conn:.2f} s; tune.read ok={ok} wall {t_read:.2f} s; ops_done={res.get('ops_done')}; error={resp.get('error')}")
    am = res.get("accepted_match")
    if isinstance(am, list):
        print("accepted_match:", sum(1 for c in am if c.get("match")), "match /", len(am))
    tail = link.call("audit.tail", {"n": 200}, timeout=60).get("result", {})
finally:
    link.close()

def ts(x):
    t = x.get("ts")
    if isinstance(t, (int, float)): return float(t)
    try: return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except Exception: return None

recs = [r for r in tail.get("records", []) if ts(r) is not None]
recs.sort(key=ts)
cut = time.time() - t_read - 5
recs = [r for r in recs if ts(r) >= cut]
gaps, tx = [], []
la = lr = None
for r in recs:
    t = ts(r)
    if r.get("event") == "attempt":
        if lr is not None: gaps.append(t - lr)
        la = t
    elif r.get("event") == "result" and la is not None:
        tx.append(t - la); lr = t
for name, v in (("attempt->result", tx), ("result->next attempt", gaps)):
    if v: print(f"{name}: n={len(v)} median {statistics.median(v)*1000:.1f} ms  p90 {sorted(v)[max(0,int(len(v)*.9)-1)]*1000:.1f} ms  max {max(v)*1000:.1f} ms")
print("records in window:", len(recs))
