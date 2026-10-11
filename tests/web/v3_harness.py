"""Dev harness for the v3 web UI (G-113): the real ``WebServer`` over the contract-9 ``FakeHelper`` with a telemetry + heartbeat feeder.
No hardware and no helper code. The server's ``OPS`` allowlist is the real one (G-111 / G-112 merged), so every request the page sends is
schema-checked exactly as it will be against the real helper.

    python3 -m tests.web.v3_harness            # serves on 127.0.0.1, prints the one-time launcher token; Ctrl-C to stop (never starts a helper)
"""
import math
import random
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from arltuner.web.server import Config, WebServer  # noqa: E402
from tests.web.fake_helper import FakeHelper, preflight_report  # noqa: E402


def tele_values(t: float) -> dict:
    def ok(v, unit):
        return {"value": v, "unit": unit, "source": "fake", "status": "ok"}

    cores = {}
    for cpu in range(24):
        kind = "P" if cpu < 8 else "E"
        base = 5400 if kind == "P" else 4300
        load = 0.5 + 0.5 * math.sin(t / 3.0 + cpu)
        cores[str(cpu)] = {"name": ("P-Core %d" % cpu) if kind == "P" else ("E-Core %d" % cpu),
                           "mhz": ok(round(base * (0.35 + 0.65 * load) + random.uniform(-20, 20), 1), "MHz"),
                           "busy": ok(round(100 * load, 1), "%"), "vid": ok(round(0.8 + 0.5 * load, 3), "V"), "ratio": ok(round(base * (0.35 + 0.65 * load) / 100), "x")}
    temps = {"Package id 0": {**ok(48 + 10 * math.sin(t / 7), "C"), "name": "Package"}}
    for cpu in range(8):
        temps["Core %d" % cpu] = {**ok(44 + 8 * math.sin(t / 5 + cpu), "C"), "name": "Core %d" % cpu, "cpu": cpu}
    return {"ts": time.time(), "cores": cores, "temps": temps, "vcore": ok(1.12 + 0.05 * math.sin(t / 4), "V"),
            "power": {"package": ok(95 + 40 * math.sin(t / 3), "W"), "core": ok(70 + 35 * math.sin(t / 3), "W")},
            "dram": {"read": ok(12 + 8 * math.sin(t / 2), "GB/s"), "write": ok(6 + 4 * math.sin(t / 2.5), "GB/s")},
            "limit_reasons_raw": ok(0, "raw")}


def start(measure_delay: float = 0.0, apply_delay: float = 0.0, seed: bool = True, stop: threading.Event | None = None):
    fake = FakeHelper()
    fake.preflight_result = preflight_report("write")
    fake.measure_delay = measure_delay
    fake.apply_delay = apply_delay
    if seed:
        fake.seed_curve("Stock baseline", baseline=True, offset_mv=0.0, created="2026-10-07T20:41:09Z")
        fake.seed_curve("P -25 / E -50", created="2026-10-09T18:02:44Z")
    srv = WebServer(Config(web_root=ROOT / "web"), fake.link).start()
    stop = stop or threading.Event()

    def feed():
        t0 = time.time()
        last_hb = 0.0
        last_t = 0.0
        while not stop.is_set():
            now = time.time()
            if now - last_hb >= 1.0:
                st, opid = fake.busy_state()
                fake.heartbeat(st, ops_used=fake.ops_used_v3, ops_budget=600, op_id=opid)
                last_hb = now
            if now - last_t >= (fake.tele_interval or 1.0):
                fake.telemetry(tele_values(now - t0))
                last_t = now
            time.sleep(0.05)

    threading.Thread(target=feed, daemon=True).start()
    return srv, fake, stop


def login(page, srv):
    """POST the bootstrap token from the page's own context so the cookie lands in the browser context."""
    resp = page.request.post(srv.origin + "/bootstrap", form={"token": srv.token}, headers={"Origin": "null"})
    assert resp.status == 200, resp.status


if __name__ == "__main__":
    _srv, _fake, _stop = start(measure_delay=0.05, apply_delay=0.3)
    print(f"serving {_srv.origin}  (POST /bootstrap token={_srv.token} once, form field 'token'; fake helper, no hardware)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _stop.set()
        _srv.close()
        _fake.close()
