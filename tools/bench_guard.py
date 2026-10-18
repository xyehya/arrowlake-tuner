#!/usr/bin/env python3
"""G-211: where does the guard's per-operation bookkeeping time go? (host: lnx, offline)

Runs N mailbox reads through the REAL ``WriteGuard`` on the simulator (``SimMsrPort``) with a synthetic audit log of ``--log-mb`` MB
already in place, a fake ``/proc`` + ``/sys`` tree for the health gate, real ``fsync`` on the audit log and the real lock file, and prints
the median / mean wall time per operation split into: audit-state read (``read_state`` and friends), lock take + release, audit appends
(incl. fsync), health snapshots, the executor's pacer, the transaction itself, and the rest (guard checks, request building).

Touches no hardware and no real ``/proc`` / ``/sys`` / audit log / lock: everything lives in a temp directory (``--dir``, default
``tempfile``; give a disk-backed directory so ``fsync`` costs what it costs). Read-only mailbox requests only; nothing is ever written to a CPU.

  python3 tools/bench_guard.py [--ops 300] [--log-mb 16] [--dir DIR] [--no-fsync] [--json]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from arltuner.controls.executor import _Pacer  # noqa: E402
from arltuner.guard import ENV_FLAG, AuditLog, Confirmation, WriteGuard, default_m1_policy  # noqa: E402
from arltuner.guard import guard as guard_mod  # noqa: E402
from arltuner.guard import audit as audit_module  # noqa: E402
from arltuner.health import make_health_gate  # noqa: E402
from arltuner.msr import MailboxTransport, SimConfig, SimMsrPort  # noqa: E402
from arltuner.topology import arl_s_285k  # noqa: E402

BOOT_ID = "bench-boot-0000-0000-0000-000000000000"
PARTS = ("audit_state", "lock", "audit_append", "health", "pacer", "transact")


def make_fake_root(base: Path, cpus: int) -> Path:
    """A ``/proc`` + ``/sys`` tree shaped like the reference machine's (about 40 PCI devices, 200 modules, thermal zones, MCE banks)."""
    root = base / "fakeroot"
    (root / "proc" / "sys" / "kernel").mkdir(parents=True)
    (root / "proc" / "sys" / "kernel" / "tainted").write_text("0\n", encoding="ascii")
    pci = root / "sys" / "bus" / "pci" / "devices"
    pci.mkdir(parents=True)
    for n in range(40):
        (pci / f"0000:{n:02x}:00.0").mkdir()
    (root / "proc" / "modules").write_text(
        "".join(f"mod_{n} 61440 3 dep_a,dep_b, Live 0x0000000000000000\n" for n in range(200)), encoding="ascii"
    )
    for z in range(12):
        zone = root / "sys" / "class" / "thermal" / f"thermal_zone{z}"
        zone.mkdir(parents=True)
        (zone / "temp").write_text("45000\n", encoding="ascii")
    for c in range(cpus):
        for b in range(10):
            bank = root / "sys" / "devices" / "system" / "machinecheck" / f"machinecheck{c}"
            bank.mkdir(parents=True, exist_ok=True)
            (bank / f"bank{b}").write_text("0xbe00000000000000\n", encoding="ascii")
    return root


def make_audit_log(path: Path, mb: int) -> int:
    """About ``mb`` MB of audit records of the shapes the guard writes (attempt / result / readback / refused), many boots, one line each."""
    target = mb * 1_000_000
    size = 0
    seq = 0
    boots = [f"boot-{n:04d}-0000-0000-0000-000000000000" for n in range(40)]
    with open(path, "wb") as handle:
        while size < target:
            ts = f"2026-10-0{1 + seq % 9}T0{seq % 10}:{seq // 60 % 60:02d}:{seq % 60:02d}.{seq % 1000:03d}+00:00"
            boot = boots[seq // 1600 % len(boots)]
            cpu = seq % 24
            lines = [
                {"ts": ts, "seq": seq, "event": "attempt", "cpu": cpu, "cmd": "0x10", "p1": "0x00", "p2": "0x00", "req_data": "0x00000000",
                 "purpose": "bench: synthetic history of the real log's shape", "operator": "yk", "boot_id": boot, "health_gate": {"before": "snapshot taken: taint=4096 pci=36 modules=180"}},
                {"ts": ts, "seq": seq + 1, "event": "result", "cpu": cpu, "cmd": "0x10", "p1": "0x00", "p2": "0x00", "req_data": "0x00000000",
                 "purpose": "bench: synthetic history of the real log's shape", "operator": "yk", "rc": 0, "rc_name": "SUCCESS", "data": "0xFEC00039"},
            ]
            for rec in lines:
                data = json.dumps(rec, separators=(",", ":")).encode("ascii") + b"\n"
                handle.write(data)
                size += len(data)
            seq += 2
    return size


class Timers:
    def __init__(self) -> None:
        self.cur = dict.fromkeys(PARTS, 0.0)
        self.per_op: list[dict[str, float]] = []

    def wrap(self, part: str, fn):
        def timed(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.cur[part] += time.perf_counter() - t0

        return timed

    def end_op(self, total: float) -> None:
        row = dict(self.cur)
        row["total"] = total
        row["other"] = total - sum(self.cur.values())
        self.per_op.append(row)
        self.cur = dict.fromkeys(PARTS, 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ops", type=int, default=300)
    ap.add_argument("--log-mb", type=int, default=16)
    ap.add_argument("--dir", default=None, help="working directory (default: a fresh temp dir; use a disk-backed one for real fsync cost)")
    ap.add_argument("--no-fsync", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="bench-guard-", dir=args.dir) as tmp_name:
        tmp = Path(tmp_name)
        topo = arl_s_285k()
        cpus = [core.cpu for core in topo.cores]
        audit_path, lock_path = tmp / "audit.jsonl", tmp / "mailbox.lock"
        log_bytes = make_audit_log(audit_path, args.log_mb)
        root = make_fake_root(tmp, len(cpus))

        sim = SimMsrPort(SimConfig.measured())
        timers = Timers()
        transport = MailboxTransport(sim)
        transport.transact = timers.wrap("transact", transport.transact)  # type: ignore[method-assign]
        policy = dataclasses.replace(
            default_m1_policy(), max_operations_per_session=args.ops + 10, max_operations_per_boot=10**9, min_dwell_s=0.0
        )
        t0 = time.perf_counter()
        audit = AuditLog(audit_path, fsync=not args.no_fsync)
        open_s = time.perf_counter() - t0
        gate = make_health_gate(root)
        guard = WriteGuard(
            transport, policy, audit, session_token="bench", env={ENV_FLAG: "1"}, health_gate=timers.wrap("health", gate),
            lock_path=lock_path, boot_id_fn=lambda: BOOT_ID, msr_port=sim,
        )
        # time-slice hooks: names exist before and after G-211 (the bench measures the same seams in both)
        for name in ("_audit_state",):
            setattr(guard, name, timers.wrap("audit_state", getattr(guard, name)))
        for name in ("_take_lock", "_release_lock"):
            setattr(guard, name, timers.wrap("lock", getattr(guard, name)))
        audit.append = timers.wrap("audit_append", audit.append)  # type: ignore[method-assign]
        pacer = _Pacer(0.0, time.monotonic, time.sleep)
        wait, mark = timers.wrap("pacer", pacer.wait), timers.wrap("pacer", pacer.mark)
        confirm = Confirmation("bench", "bench")
        request_cls = guard_mod.Request

        for n in range(args.ops):
            req = request_cls(cpus[n % len(cpus)], 0x10, 0, 0, 0, "bench read")
            t = time.perf_counter()
            wait(None)
            guard.execute(req, confirm)
            mark()
            timers.end_op(time.perf_counter() - t)

        rows = timers.per_op
        keys = (*PARTS, "other", "total")
        med = {k: statistics.median(r[k] for r in rows) * 1000 for k in keys}
        mean = {k: statistics.fmean(r[k] for r in rows) * 1000 for k in keys}
        overhead = [(r["total"] - r["transact"]) * 1000 for r in rows]
        result = {
            "ops": args.ops, "log_bytes": log_bytes, "fsync": not args.no_fsync, "audit_open_ms": open_s * 1000,
            "median_ms": med, "mean_ms": mean, "overhead_median_ms": statistics.median(overhead), "overhead_p90_ms": sorted(overhead)[int(0.9 * len(overhead))],
            "tripped": guard.tripped, "operations": guard.operations,
            "audit_scan": dict(getattr(audit_module, "SCAN_STATS", {})),  # G-211 incremental reader (absent before it)
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"G-211 guard bench: {args.ops} reads, audit log {log_bytes / 1e6:.1f} MB, fsync={'on' if not args.no_fsync else 'off'}, dir={tmp.parent}")
            print(f"AuditLog() open (once): {open_s * 1000:.1f} ms")
            print(f"{'part':<14}{'median ms':>11}{'mean ms':>10}")
            for k in keys:
                print(f"{k:<14}{med[k]:>11.3f}{mean[k]:>10.3f}")
            if result["audit_scan"]:
                print(f"audit parse: {result['audit_scan']}")
            print(f"overhead per op (total - transact): median {result['overhead_median_ms']:.2f} ms, p90 {result['overhead_p90_ms']:.2f} ms")
        return 1 if guard.tripped else 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
