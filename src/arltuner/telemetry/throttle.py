"""Throttle indicators: 10 Hz oversampling of the 0x64F status bits, the XTU-style groups, the event log, the kernel thermal counters.

Spec: docs/knowledge/throttling.md (section B mapping, C read strategy, E event log). Everything here is READ-ONLY with respect to the hardware: the word comes in through
``TelemetryMsr.rdmsr`` (or a scripted source for the simulator), the sticky log bits are never cleared, "reset" is an app counter only (throttling.md section C).
The only thing written is the app's own ``throttle.jsonl`` (0600, size-capped, ``O_NOFOLLOW``).

The group -> bit mapping is an inference from the SDM names (throttling.md section B, ``[UNVERIFIED]`` against XTU's own labels): Power = PL1 / PL2, Current / EDP =
"Other (electrical)" and VR TDC, Thermal = Thermal / PROCHOT / RATL, VR thermal = VR thermal alert, Max turbo = the multi-core turbo-ratio cap (normal under load, informational).
"""
from __future__ import annotations

import errno
import json
import os
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .limits import CORE_64F
from .readings import Reading, unavailable

STATUS_MASK = 0xFFFF  # 0x64F bits 15:0 are the live status; 31:16 are the sticky log and never feed an indicator

# key -> (label, status bits). Labels are exactly what the GUI shows. Order = chip order.
GROUPS: dict[str, tuple[str, tuple[int, ...]]] = {
    "power": ("Power limit", (10, 11)),
    "current": ("Current / EDP", (8, 7)),
    "thermal": ("Thermal", (1, 0, 5)),
    "vr_thermal": ("VR thermal", (6,)),
    "max_turbo": ("Max turbo cap", (12,)),
}
INFO_BITS = (13, 4)  # turbo transition attenuation, residency state regulation: popover only
UNLOGGED_GROUPS = frozenset({"max_turbo"})  # fires on every load: informational, never an event
CLOSE_AFTER_CLEAN = 2  # an event closes after this many consecutive clean windows (throttling.md section E)

LOG_NAME = "throttle.jsonl"
LOG_MAX_BYTES = 1 << 20
TAIL_DEFAULT = 20
TAIL_MAX = 200
_TAIL_READ_BYTES = 256 * 1024
_TAIL_RESULT_BYTES = 60 * 1024  # the helper's response line is capped at 64 KiB

_BIT_KEY = {b: CORE_64F[b][0] for b in CORE_64F}
_GROUP_BITS = {k: tuple(sorted(bits)) for k, (_, bits) in GROUPS.items()}
_KNOWN_STATUS = frozenset(CORE_64F)


def throttle_log_path(audit_path: Path) -> Path:
    """The event log sits beside the audit log (D-030 installed ``/var/lib/arltuner/``, D-041 dev ``$XDG_STATE_HOME/arltuner/``)."""
    return Path(audit_path).with_name(LOG_NAME)


# ---------------------------------------------------------------------------------------------------------------- event log
class EventLog:
    """Append-only JSONL, one record per closed throttle event. Never raises: a failure is kept in ``error`` and reported with the telemetry."""

    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES, euid: int | None = None) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._euid = os.geteuid() if euid is None and hasattr(os, "geteuid") else euid
        self.error: str | None = None

    def _rotate(self) -> None:
        try:
            if os.lstat(self.path).st_size >= self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))  # one generation: the log is a recent-history aid, not an archive
        except FileNotFoundError:
            pass

    def append(self, record: dict) -> bool:
        line = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._rotate()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or (self._euid is not None and st.st_uid != self._euid) or st.st_mode & 0o022:
                    raise OSError(errno.EPERM, "not a regular file of ours, or writable by others")
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError as e:
            self.error = f"{self.path}: {e.strerror or e}"
            return False
        self.error = None
        return True


def _read_tail_lines(path: Path) -> tuple[list[bytes], bool]:
    """The complete lines of the last 256 KiB of ``path`` ([] if absent), and whether older bytes were left unread."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return [], False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022:
            raise OSError(errno.EPERM, f"{path} is not a regular file or is writable by others")
        start = max(0, st.st_size - _TAIL_READ_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        data = b""
        while chunk := os.read(fd, 1 << 16):
            data += chunk
    finally:
        os.close(fd)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line may be cut
    return [ln for ln in lines if ln.strip()], start > 0


def validate_tail_n(args: dict) -> int:
    """``throttle.tail`` arguments: only ``n``, an integer 1..200 (default 20). Raises ValueError with the message for the client."""
    extra = set(args) - {"n"}
    if extra:
        raise ValueError(f"unknown argument(s): {', '.join(sorted(extra))}")
    n = args.get("n", TAIL_DEFAULT)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= TAIL_MAX:
        raise ValueError(f"'n' must be an integer 1..{TAIL_MAX}")
    return n


def tail(path: Path, n: int = TAIL_DEFAULT) -> dict:
    """The last ``n`` events (oldest first) from ``path``, falling back to the rotated ``.1`` file; bounded reads and a bounded result. Missing file = no events."""
    path = Path(path)
    lines, truncated = _read_tail_lines(path)
    if len(lines) < n:
        older, older_truncated = _read_tail_lines(path.with_name(path.name + ".1"))
        lines = older + lines
        truncated = truncated or older_truncated
    wanted = lines[-n:]
    truncated = truncated or len(lines) > len(wanted)
    records: list = []
    for ln in wanted:
        try:
            records.append(json.loads(ln.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            records.append({"unparsable": ln[:200].decode("utf-8", "replace")})
    while records and len(json.dumps(records, separators=(",", ":"))) > _TAIL_RESULT_BYTES:
        records.pop(0)
        truncated = True
    return {"path": str(path), "records": records, "truncated": truncated}


# ---------------------------------------------------------------------------------------------------------------- tracker
@dataclass
class _Open:
    t_on: float
    mono_on: float
    tuning_id: str | None
    bits: set[int]
    duty_sum: float = 0.0
    windows: int = 0
    gap: int = 0  # clean windows since the last active one (counted into the event only if it goes active again)
    last_end_wall: float = 0.0
    last_end_mono: float = 0.0
    word: int = 0
    peak_temp_c: float | None = None
    peak_pkg_w: float | None = None
    peak_mhz: float | None = None
    kernel_thermal_delta: int = 0


def _peak(old: float | None, new: object) -> float | None:
    if isinstance(new, bool) or not isinstance(new, (int, float)):
        return old
    return new if old is None else max(old, new)


class ThrottleTracker:
    """Collects 0x64F status words between ticks; ``drain`` turns the window into per-group ``{active, duty, bits}`` and runs the event debounce.

    Single-threaded (the sampler thread). ``clock`` is monotonic, ``wall`` is epoch seconds (both injectable)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time, log: EventLog | None = None,
                 tuning_id_fn: Callable[[], str | None] = lambda: None, close_after: int = CLOSE_AFTER_CLEAN) -> None:
        self._clock, self._wall, self._log, self._tuning_id = clock, wall, log, tuning_id_fn
        self._close_after = close_after
        self._words: list[int] = []
        self._failed = 0
        self._last_error = "no sample yet"
        self._t0, self._w0 = clock(), wall()
        self._open: dict[str, _Open] = {}

    @property
    def log(self) -> EventLog | None:
        return self._log

    def add(self, word: int) -> None:
        self._words.append(word & STATUS_MASK)

    def add_failure(self, reason: str) -> None:
        self._failed += 1
        self._last_error = reason

    def drain(self, ctx: dict | None = None) -> Reading:
        """Close the window (the time since the previous ``drain``) and return the ``throttle`` reading. ``ctx``: ``temp_c`` / ``pkg_w`` / ``mhz`` /
        ``kernel_thermal_delta`` of this tick, for the event record."""
        ctx = ctx or {}
        words, failed = self._words, self._failed
        self._words, self._failed = [], 0
        t1, w1 = self._clock(), self._wall()
        t0, w0 = self._t0, self._w0
        self._t0, self._w0 = t1, w1
        src = "msr 0x64F status bits, oversampled"
        if not words:
            return unavailable(src, self._last_error)
        n = len(words)
        or_word = 0
        for w in words:
            or_word |= w
        groups: dict[str, dict] = {}
        for key, (label, gbits) in GROUPS.items():
            mask = sum(1 << b for b in gbits)
            on = sum(1 for w in words if w & mask)
            seen = [b for b in sorted(gbits) if or_word >> b & 1]
            groups[key] = {"label": label, "active": on > 0, "duty": round(on / n, 4), "bits": [_BIT_KEY[b] for b in seen]}
            if key not in UNLOGGED_GROUPS:
                self._track(key, on > 0, on / n, set(seen), or_word, (t0, t1, w0, w1), ctx)
        bit_duty = {_BIT_KEY[b]: round(sum(1 for w in words if w >> b & 1) / n, 4) for b in sorted(_KNOWN_STATUS) if or_word >> b & 1}
        value = {
            "samples": n, "failed": failed, "window_s": round(max(0.0, t1 - t0), 3), "or_word": or_word, "groups": groups, "bit_duty": bit_duty,
            "info_bits": [_BIT_KEY[b] for b in sorted(INFO_BITS) if or_word >> b & 1],
            "unknown_bits": [b for b in range(16) if or_word >> b & 1 and b not in _KNOWN_STATUS],
        }
        if self._log is not None:
            value["log"] = {"path": str(self._log.path), "error": self._log.error}
        return Reading(value, "", src)

    def _track(self, key: str, active: bool, duty: float, seen: set[int], or_word: int, win: tuple[float, float, float, float], ctx: dict) -> None:
        t0, t1, w0, w1 = win
        ev = self._open.get(key)
        if active:
            if ev is None:
                ev = self._open[key] = _Open(t_on=w0, mono_on=t0, tuning_id=self._safe_tuning_id(), bits=set())
            ev.windows += ev.gap + 1
            ev.gap = 0
            ev.duty_sum += duty
            ev.bits |= seen
            ev.word |= or_word
            ev.last_end_wall, ev.last_end_mono = w1, t1
        elif ev is not None:
            ev.gap += 1
        if ev is None:
            return
        ev.peak_temp_c = _peak(ev.peak_temp_c, ctx.get("temp_c"))
        ev.peak_pkg_w = _peak(ev.peak_pkg_w, ctx.get("pkg_w"))
        ev.peak_mhz = _peak(ev.peak_mhz, ctx.get("mhz"))
        delta = ctx.get("kernel_thermal_delta")
        if isinstance(delta, int) and not isinstance(delta, bool):
            ev.kernel_thermal_delta += delta
        if ev.gap >= self._close_after:
            self._close(key)

    def _safe_tuning_id(self) -> str | None:
        try:
            return self._tuning_id()
        except Exception:  # the label of the applied state must never break sampling
            return None

    def _close(self, key: str) -> None:
        ev = self._open.pop(key)
        if self._log is None:
            return
        self._log.append({
            "group": key, "label": GROUPS[key][0], "bits": [_BIT_KEY[b] for b in sorted(ev.bits)], "word": ev.word,
            "t_on": round(ev.t_on, 3), "t_off": round(ev.last_end_wall, 3), "mono_on": round(ev.mono_on, 3), "mono_off": round(ev.last_end_mono, 3),
            "dur_ms": round((ev.last_end_mono - ev.mono_on) * 1000), "duty": round(ev.duty_sum / ev.windows, 4) if ev.windows else 0.0,
            "peak_temp_c": ev.peak_temp_c, "peak_pkg_w": ev.peak_pkg_w, "peak_mhz": ev.peak_mhz,
            "kernel_thermal_delta": ev.kernel_thermal_delta, "tuning_id": ev.tuning_id,
        })

    def flush(self) -> None:
        """Write the events still open (sampler stop): their end is the last active window, as if the clean windows had followed."""
        for key in list(self._open):
            self._close(key)


# ---------------------------------------------------------------------------------------------------------------- kernel thermal counters
class KernelThermal:
    """Root-free sysfs witness of thermal throttling: ``cpuN/thermal_throttle/{core,package}_throttle_count`` (+ ``total_time_ms``), diffed per tick.
    Thermal only: the power-limit counters exist just with boot option ``int_pln_enable`` (throttling.md section A) [MEASURED lnx 2026-10-18: files present, all 0]."""

    def __init__(self, cpus: Iterable[int], cpu_root: Path) -> None:
        self._cpus = list(cpus)
        self._root = Path(cpu_root)
        self._prev: tuple[int, int] | None = None

    @staticmethod
    def _int(path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def sample(self) -> Reading:
        src = "sysfs cpu*/thermal_throttle"
        core = pkg = core_ms = pkg_ms = 0
        files = 0
        for cpu in self._cpus:
            d = self._root / f"cpu{cpu}" / "thermal_throttle"
            c, p = self._int(d / "core_throttle_count"), self._int(d / "package_throttle_count")
            if c is None and p is None:
                continue
            files += 1
            core += c or 0
            pkg = max(pkg, p or 0)  # package counters repeat in every CPU of the package
            core_ms += self._int(d / "core_throttle_total_time_ms") or 0
            pkg_ms = max(pkg_ms, self._int(d / "package_throttle_total_time_ms") or 0)
        if not files:
            return unavailable(src, "no thermal_throttle counters in sysfs")
        prev, self._prev = self._prev, (core, pkg)
        core_d, pkg_d = (0, 0) if prev is None else (max(0, core - prev[0]), max(0, pkg - prev[1]))
        return Reading({"core_count": core, "package_count": pkg, "core_total_ms": core_ms, "package_total_ms": pkg_ms,
                        "core_delta": core_d, "package_delta": pkg_d}, "", src)


# ---------------------------------------------------------------------------------------------------------------- simulator knob
class ScriptedWords:
    """A 0x64F source for the simulator and tests: ``"0x400:3,0:5"`` = the word 0x400 for 3 s, then 0 for 5 s, repeating. Not hardware."""

    def __init__(self, steps: list[tuple[int, float]], clock: Callable[[], float] = time.monotonic) -> None:
        if not steps or any(not 0 <= w < 1 << 64 or s <= 0 for w, s in steps):
            raise ValueError("a throttle script needs steps of (word, seconds > 0)")
        self._steps, self._clock, self._t0 = steps, clock, clock()
        self._total = sum(s for _, s in steps)

    @classmethod
    def parse(cls, spec: str, clock: Callable[[], float] = time.monotonic) -> ScriptedWords:
        steps: list[tuple[int, float]] = []
        for part in spec.split(","):
            word, _, secs = part.strip().partition(":")
            steps.append((int(word, 0), float(secs)))
        return cls(steps, clock)

    def __call__(self) -> int:
        t = (self._clock() - self._t0) % self._total
        for word, secs in self._steps:
            if t < secs:
                return word
            t -= secs
        return self._steps[-1][0]
