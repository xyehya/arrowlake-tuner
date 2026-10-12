"""Test harness for the helper: real pipes, a real ``Helper.run`` thread, the simulator, temp paths. No hardware, no /run, no sudo, no real lock."""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arltuner.helper import Helper, HelperConfig
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.platform.preflight import CheckResult, PreflightReport, Status
from arltuner.telemetry import Sampler, TelemetryMsr
from arltuner.topology import arl_s_285k

from tests.telemetry._support import FakeMsrPort

POLICY_TOML = """\
cpus = ["0-23"]
max_operations_per_session = 300
max_operations_per_boot = 400
min_dwell_s = 60.0
max_ratio_step = 8
max_v_step_mv = 200
max_offset_step_mv = 200

[[allow]]
name = "vf_read"
cmd = 0x10
p1 = [0]
p2 = [0]
kind = "read"

[[allow]]
name = "vf_point_read"
cmd = 0x10
p1 = [0]
p2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
kind = "read"
"""

TIMEOUT = 10.0


def report(*statuses: tuple[str, Status]) -> PreflightReport:
    return PreflightReport("arl-s-285k", tuple(CheckResult(n, s, f"{n} is {s.value}") for n, s in statuses))


GOOD_REPORT = report(("cpuid", Status.PASS), ("msr_device", Status.PASS), ("msr_allow_writes", Status.PASS), ("tool:perf", Status.MISSING))
UNKNOWN_REPORT = report(("cpuid", Status.PASS), ("msr_allow_writes", Status.UNKNOWN))
READ_ONLY_REPORT = report(("cpuid", Status.PASS), ("lockdown", Status.READ_ONLY))


class BlockingSim(SimMsrPort):
    """A simulator that parks inside the ``n``-th mailbox request (after it was latched) until released: a deterministic 'transaction in flight'."""

    def __init__(self, config: SimConfig | None = None) -> None:
        super().__init__(config)
        self.park_at: int | None = None
        self.parked = threading.Event()
        self.release = threading.Event()

    def wrmsr(self, cpu: int, msr: int, value: int) -> None:
        super().wrmsr(cpu, msr, value)
        if self.park_at is not None and len(self.requests) >= self.park_at and not self.parked.is_set():
            self.parked.set()
            self.release.wait(TIMEOUT)


def dump_sim(cls: type[SimMsrPort] = SimMsrPort) -> SimMsrPort:
    return cls(SimConfig.measured(per_point=True, reject_zero_fields=True))


class HelperCase(unittest.TestCase):
    """Builds temp paths and a config; ``start()`` runs a helper on pipes. Everything is cleaned up."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.audit = self.tmp / "audit.jsonl"
        self.lock = self.tmp / "mailbox.lock"
        self.policy = self.tmp / "policy.toml"
        self.policy.write_text(POLICY_TOML, encoding="utf-8", newline="\n")
        self.sim = dump_sim()
        self.logs: list[str] = []
        self.msr_for_telemetry = FakeMsrPort(default={0xCE: 0x2500})
        self.harnesses: list[Harness] = []
        self.addCleanup(self._stop_all)

    def _stop_all(self) -> None:
        for h in self.harnesses:
            h.stop()

    def config(self, **overrides: Any) -> HelperConfig:
        values: dict[str, Any] = dict(
            lock_path=self.lock,
            audit_path=self.audit,
            policy_path=self.policy,
            port_factory=lambda: self.sim,
            topology_fn=arl_s_285k,
            preflight_fn=lambda: GOOD_REPORT,
            sampler_factory=lambda interval: Sampler(
                TelemetryMsr(self.msr_for_telemetry), hwmon_root=self.tmp / "h", powercap_root=self.tmp / "p", interval_s=interval, topology=arl_s_285k()
            ),
            simulator=True,
            heartbeat_s=0.05,
            log=self.logs.append,
            boot_id_fn=lambda: "11111111-2222-3333-4444-555555555555",
        )
        values.update(overrides)
        return HelperConfig(**values)

    def start(self, cfg: HelperConfig | None = None) -> Harness:
        h = Harness(cfg or self.config())
        self.harnesses.append(h)
        h.start()
        return h


class Harness:
    def __init__(self, cfg: HelperConfig) -> None:
        self.helper = Helper(cfg)
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self._to_helper = os.fdopen(in_w, "wb", buffering=0)
        self._helper_in = os.fdopen(in_r, "rb")
        self._helper_out = os.fdopen(out_w, "wb")
        self._from_helper = os.fdopen(out_r, "rb")
        self.exit_code: int | None = None
        self.messages: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._stdin_open = True
        self._run = threading.Thread(target=self._main, daemon=True)
        self._collect = threading.Thread(target=self._collector, daemon=True)

    def _main(self) -> None:
        try:
            self.exit_code = self.helper.run(self._helper_in, self._helper_out)
        finally:
            self._helper_out.close()

    def _collector(self) -> None:
        for raw in self._from_helper:
            msg = json.loads(raw)
            with self._cond:
                self.messages.append(msg)
                self._cond.notify_all()

    def start(self) -> None:
        self._collect.start()
        self._run.start()

    # ---- input
    def send(self, msg: dict[str, Any]) -> None:
        self.send_raw(json.dumps(msg).encode() + b"\n")

    def send_raw(self, data: bytes) -> None:
        self._to_helper.write(data)

    def close_stdin(self) -> None:
        if self._stdin_open:
            self._stdin_open = False
            self._to_helper.close()

    # ---- output
    def wait_for(self, pred: Callable[[dict[str, Any]], bool], timeout: float = TIMEOUT, start: int = 0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for m in self.messages[start:]:
                    if pred(m):
                        return m
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timed out; saw: {[_brief(m) for m in self.messages]}")
                self._cond.wait(left)

    def response(self, req_id: str | None, timeout: float = TIMEOUT) -> dict[str, Any]:
        return self.wait_for(lambda m: "id" in m and m["id"] == req_id, timeout)

    def call(self, req_id: str, op: str, args: dict[str, Any] | None = None, timeout: float = TIMEOUT) -> dict[str, Any]:
        self.send({"id": req_id, "op": op, **({} if args is None else {"args": args})})
        return self.response(req_id, timeout)

    def events(self, name: str) -> list[dict[str, Any]]:
        with self._cond:
            return [m for m in self.messages if m.get("event") == name]

    def join(self, timeout: float = TIMEOUT) -> int | None:
        self._run.join(timeout)
        if self._run.is_alive():
            raise AssertionError("the helper did not exit")
        # the helper's stdout is closed on exit; the collector sees EOF only after it has read everything the helper wrote. Without this wait a test
        # that reads h.messages right after join() could miss the last lines (the flake of the queue-bound test).
        self._collect.join(timeout)
        if self._collect.is_alive():
            raise AssertionError("the output collector did not reach EOF")
        return self.exit_code

    def stop(self) -> None:
        self.helper.request_shutdown("test cleanup")
        self.close_stdin()
        self._run.join(TIMEOUT)
        for f in (self._from_helper, self._helper_in):
            try:
                f.close()
            except OSError:
                pass


def _brief(m: dict[str, Any]) -> str:
    return json.dumps(m)[:160]


def error_code(msg: dict[str, Any]) -> str:
    assert msg.get("ok") is False, msg
    return msg["error"]["code"]
