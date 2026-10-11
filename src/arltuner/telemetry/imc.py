"""DRAM read/write bandwidth from the IMC free-running PMU via ONE long-lived ``perf stat -I`` process.

Command (root or perf_event_paranoid <= -1): ``perf stat -a -I <ms> -x , -e <read-event>,<write-event>``. perf stat prints to
*stderr* by default, so the process is started with stderr merged into the pipe; lines that are not counter rows are ignored.

Row format with ``-I`` and ``-x ,`` [DOC perf-stat(1) CSV output]: ``timestamp,count,unit,event,run-time,pct-running[,metric...]``
where an unusable counter prints ``<not counted>`` / ``<not supported>`` in the count field. The free-running IMC events report
MiB [MEASURED lnx 2026-10-03, linux-path.md s7: "units MiB; x1.0486 = MB"]. The event spelling ``uncore_imc_free_running/data_read/`` and
``/data_write/`` (perf merges the ``_0`` / ``_1`` instances) is [MEASURED lnx 2026-10-11] (root perf; sample CSV in docs/knowledge/telemetry-sources.md);
it stays configurable. Nothing here runs perf at import or in tests.

Hardening (the helper runs this as root): perf is the absolute ``/usr/bin/perf``, started with a fixed minimal environment (nothing inherited: no
``PATH`` / ``LD_*`` / ``PERF_*`` / ``HOME``), ``stdin`` = /dev/null and ``cwd`` = ``/``. An optional ``trust_check(path)`` runs before every spawn; if it
raises :class:`PerfRefused` no process is started and the readings are unavailable with that reason. If the helper dies, perf's next write hits a
closed pipe (SIGPIPE) and it exits; a clean stop terminates it.
"""
from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .readings import Reading, unavailable

DEFAULT_EVENTS = ("uncore_imc_free_running/data_read/", "uncore_imc_free_running/data_write/")  # [MEASURED lnx 2026-10-11]
PERF_PATH = "/usr/bin/perf"
PERF_ENV = {"PATH": "/usr/sbin:/usr/bin", "LC_ALL": "C"}  # fixed, nothing inherited; LC_ALL=C keeps the CSV decimal point a '.'
MIB_TO_GB = 1.048576 / 1000.0  # MiB -> GB (decimal) [MEASURED lnx 2026-10-03: "x1.0486 = MB"]


class PerfRefused(OSError):
    """perf is not trusted (missing, not root-owned, group/world-writable, ...): it is not started."""


@dataclass(frozen=True)
class ImcRow:
    timestamp: float
    count: float | None  # None for <not counted>/<not supported>
    unit: str
    event: str


def parse_row(line: str) -> ImcRow | None:
    """Parse one perf CSV row; ``None`` for anything that is not a counter row (blank, comment, error text)."""
    parts = line.strip().split(",")
    if len(parts) < 4:
        return None
    try:
        ts = float(parts[0])
    except ValueError:
        return None
    raw = parts[1].strip()
    try:
        count: float | None = float(raw)
    except ValueError:
        if not raw.startswith("<"):
            return None
        count = None
    return ImcRow(ts, count, parts[2].strip(), parts[3].strip())


def _kind(event: str) -> str | None:
    if "data_read" in event:
        return "read"
    if "data_write" in event:
        return "write"
    return None


class ImcAggregator:
    """Folds rows (all IMC instances, one timestamp per interval) into (read GB/s, write GB/s) Readings."""

    def __init__(self, interval_s: float) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._interval = interval_s
        self._ts: float | None = None
        self._acc: dict[str, float] = {}
        self._bad: set[str] = set()

    def feed(self, row: ImcRow) -> tuple[Reading, Reading] | None:
        """Add a row; returns the finished previous interval when the timestamp advances, else ``None``."""
        done = None
        if self._ts is not None and row.timestamp != self._ts:
            done = self._finish()
        self._ts = row.timestamp
        kind = _kind(row.event)
        if kind is not None:
            if row.count is None or row.unit != "MiB":
                self._bad.add(kind)  # not counted, or a unit we do not know how to scale -> unavailable
            else:
                self._acc[kind] = self._acc.get(kind, 0.0) + row.count
        return done

    def flush(self) -> tuple[Reading, Reading] | None:
        return self._finish() if self._ts is not None else None

    def _finish(self) -> tuple[Reading, Reading]:
        out = []
        for kind in ("read", "write"):
            src = f"perf stat -I uncore_imc_free_running data_{kind}"
            if kind in self._bad:
                out.append(unavailable(src, "counter not counted/supported or unexpected unit", "GB/s"))
            elif kind not in self._acc:
                out.append(unavailable(src, "event absent from perf output", "GB/s"))
            else:
                out.append(Reading(self._acc[kind] * MIB_TO_GB / self._interval, "GB/s", src))
        self._ts, self._acc, self._bad = None, {}, set()
        return out[0], out[1]


def parse_stream(lines: Iterable[str], interval_s: float) -> list[tuple[Reading, Reading]]:
    """Parse a whole recorded perf output (used by tests and offline analysis)."""
    agg, out = ImcAggregator(interval_s), []
    for line in lines:
        row = parse_row(line)
        if row is not None and (res := agg.feed(row)) is not None:
            out.append(res)
    if (res := agg.flush()) is not None:
        out.append(res)
    return out


class ImcStat:
    """One long-lived perf process + reader thread; ``latest()`` is the newest finished interval or unavailable."""

    def __init__(self, interval_ms: int = 1000, events: tuple[str, ...] = DEFAULT_EVENTS, perf: str = PERF_PATH,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 trust_check: Callable[[str], None] | None = None) -> None:
        self.interval_s = interval_ms / 1000.0
        self.command = [perf, "stat", "-a", "-I", str(interval_ms), "-x", ",", "-e", ",".join(events)]
        self._popen, self._perf, self._trust_check = popen, perf, trust_check
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest = (unavailable("perf stat", "not started", "GB/s"), unavailable("perf stat", "not started", "GB/s"))

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            if self._trust_check is not None:
                self._trust_check(self._perf)  # raises PerfRefused: no process is started
            self._proc = self._popen(self.command, env=dict(PERF_ENV), cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:  # perf not installed / not trusted / not permitted
            self._set(unavailable("perf stat", f"cannot start perf: {e}", "GB/s"))
            return
        self._thread = threading.Thread(target=self._run, args=(self._proc,), name="imc-perf", daemon=True)
        self._thread.start()

    def _set(self, reason_reading: Reading) -> None:
        with self._lock:
            self._latest = (reason_reading, reason_reading)

    def _run(self, proc: subprocess.Popen) -> None:
        agg = ImcAggregator(self.interval_s)
        for line in proc.stdout or ():
            row = parse_row(line)
            if row is None:
                continue
            res = agg.feed(row)
            if res is not None:
                with self._lock:
                    self._latest = res
        res = agg.flush()
        with self._lock:
            self._latest = res if res is not None else (unavailable("perf stat", "perf exited without data", "GB/s"),) * 2

    def latest(self) -> tuple[Reading, Reading]:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=2)
