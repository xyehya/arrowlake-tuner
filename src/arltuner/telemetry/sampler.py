"""The telemetry sampler: one background thread, 1 Hz default, read-only, never near the OC mailbox.

It receives a ``TelemetryMsr`` (read-only, allowlisted, no ``wrmsr``) or ``None`` (non-root: MSR-backed values come back
unavailable with reason "no MSR access"). It imports nothing from ``arltuner.mailbox`` / ``arltuner.guard`` and holds no
mailbox lock; tests/telemetry/test_isolation.py enforces both. A failing source degrades only its own values.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from arltuner.topology import cpumap

from . import cpu as cpumod
from .hwmon import DEFAULT_HWMON_ROOT, read_coretemp, read_vcore
from .imc import ImcStat
from .msrio import TelemetryMsr
from .rapl import DEFAULT_POWERCAP_ROOT, MsrPackagePower, PowercapPower
from .readings import Reading, unavailable

NO_MSR = "no MSR access (needs root)"


class Sampler:
    def __init__(self, msr: TelemetryMsr | None, topology: cpumap.Topology | None = None,
                 hwmon_root: Path = DEFAULT_HWMON_ROOT, powercap_root: Path = DEFAULT_POWERCAP_ROOT,
                 imc: ImcStat | None = None, clock: Callable[[], float] = time.monotonic,
                 interval_s: float = 1.0) -> None:
        self._msr = msr
        self._topo = topology or cpumap.arl_s_285k()
        self._cpus = sorted(c.cpu for c in self._topo.cores)
        self._hwmon, self._imc, self._interval = Path(hwmon_root), imc, interval_s
        self._freq: cpumod.FreqSampler | None = None
        self._tsc = unavailable("msr 0xCE", NO_MSR, "MHz")
        if msr is not None:
            self._tsc = cpumod.tsc_mhz(msr)
            if self._tsc.available:
                self._freq = cpumod.FreqSampler(msr, self._tsc.value, clock)
        self._pkg_power = PowercapPower("intel-rapl:0", powercap_root, clock)
        self._core_power = PowercapPower("intel-rapl:0:0", powercap_root, clock)  # subdomain name checked in collect()
        self._msr_power = MsrPackagePower(msr, 0, clock) if msr is not None else None
        self._lock = threading.Lock()
        self._snapshot: dict = {"ts": None, "status": "not sampled yet"}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def collect(self) -> dict:
        """One synchronous sample -> JSON-able dict. Every leaf is ``Reading.to_json()``."""
        msr, no = self._msr, lambda src, unit="": unavailable(src, NO_MSR, unit)
        cores: dict[int, dict] = {}
        freq = self._freq.sample(self._cpus) if self._freq else {}
        tjmax = cpumod.read_tjmax(msr) if msr else no("msr 0x1A2", "C")
        for core in self._topo.cores:
            mhz, busy = freq.get(core.cpu, (unavailable("msr 0xE8/0xE7", self._tsc.reason or NO_MSR, "MHz"),) * 2)
            vid, ratio = cpumod.read_vid(msr, core.cpu) if msr else (no("msr 0x198", "V"), no("msr 0x198", "x"))
            cores[core.cpu] = {"name": self._topo.xtu_name(core), "mhz": mhz.to_json(), "busy": busy.to_json(),
                               "vid": vid.to_json(), "ratio": ratio.to_json()}
        ct = read_coretemp(self._hwmon)
        temps = {k: v.to_json() for k, v in ct.items()}
        if not ct and msr:  # privileged fallback when coretemp hwmon is absent
            temps = {"Package": cpumod.read_package_temp_msr(msr, 0, tjmax).to_json()}
            for core in self._topo.cores:
                temps[f"cpu{core.cpu}"] = cpumod.read_core_temp_msr(msr, core.cpu, tjmax).to_json()
        elif not ct:
            temps = {"Package": unavailable("hwmon coretemp", "no coretemp device and " + NO_MSR, "C").to_json()}
        pkg = self._pkg_power.sample()
        if not pkg.available and self._msr_power is not None:
            pkg = self._msr_power.sample()
        core_w = self._core_power.sample() if self._core_power.name() == "core" else \
            unavailable("powercap intel-rapl:0:0", "no 'core' subdomain", "W")
        rd, wr = self._imc.latest() if self._imc else (unavailable("perf stat", "IMC sampling not enabled", "GB/s"),) * 2
        return {
            "ts": time.time(), "cores": cores, "temps": temps, "vcore": read_vcore(self._hwmon).to_json(),
            "power": {"package": pkg.to_json(), "core": core_w.to_json()},
            "dram": {"read": rd.to_json(), "write": wr.to_json()},
            "limit_reasons_raw": (cpumod.read_limit_reasons_raw(msr).to_json() if msr else no("msr 0x6B0", "raw").to_json()),
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self.collect()
            except Exception as e:  # a bug must not kill the thread; report, keep going
                snap = {"ts": time.time(), "status": f"sampler error: {type(e).__name__}: {e}"}
            with self._lock:
                self._snapshot = snap
            self._stop.wait(self._interval)

    def start(self) -> None:
        if self._thread is None:
            if self._imc:
                self._imc.start()
            self._thread = threading.Thread(target=self._loop, name="telemetry", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._imc:
            self._imc.stop()
