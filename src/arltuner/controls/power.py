"""G-214: the package power limits (PL1, PL2, PL1 time window "tau") as one more control -- reads, decoding, the plan and the read-back rule. Pure apart from the files it is given.

Facts (``docs/knowledge/registers.md`` "Power limits"): the Linux powercap zone ``intel-rapl:0`` exposes ``constraint_0`` = ``long_term`` = PL1 (``..._power_limit_uw``,
``..._time_window_us`` = tau) and ``constraint_1`` = ``short_term`` = PL2 (``[MEASURED lnx 2026-10-18]``: PL1 250 W, tau 127926272 us, PL2 295 W, no ``constraint_2``).
MSR ``0x610`` holds the same limits (PL1 ``[14:0]``, PL2 ``[46:32]``, tau ``[23:17]``, lock ``[63]``) in the units of ``0x606``; ``0x614`` is the CPU's own power info.
**Writes go to the powercap files only, never to MSR ``0x610``**; the MSRs are read (through the guard) to see the lock bit and to check what the kernel wrote.

Nothing here has a default bound: ``policy.power`` (``[power]`` in the policy file) says what may be written, and an absent key refuses the write naming itself.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arltuner.guard.policy import ENCODING_PL_MAX_W, ENCODING_TAU_MAX_S, Policy

__all__ = [
    "BIOS_NOTE",
    "DEFAULT_ROOT",
    "JOURNAL_VERSION",
    "ZONE",
    "FILES",
    "Info",
    "Limit",
    "MsrView",
    "PowerError",
    "PowerFiles",
    "PowerStep",
    "Refused",
    "State",
    "Units",
    "decode_info",
    "decode_limit",
    "decode_units",
    "journal_doc",
    "SimPowerFiles",
    "parse_journal",
    "plan",
    "plan_revert",
    "read_state",
    "readback_problem",
    "tau_from_raw",
    "tau_snap",
    "tau_to_raw",
]

DEFAULT_ROOT = Path("/sys/class/powercap")
ZONE = "intel-rapl:0"
JOURNAL_VERSION = 1
BIOS_NOTE = "the values at the first power write of this boot (the journal), not necessarily the BIOS setup values"

#: file of each key, relative to the zone directory. ``constraint_N_name`` is read to check which constraint is PL1 / PL2 (a kernel that numbers them otherwise is refused).
FILES: Mapping[str, str] = {
    "pl1_w": "constraint_0_power_limit_uw",
    "tau_s": "constraint_0_time_window_us",
    "pl2_w": "constraint_1_power_limit_uw",
}
NAMES: Mapping[str, str] = {"constraint_0_name": "long_term", "constraint_1_name": "short_term"}
#: the order steps are listed in (not the write order: see :func:`plan`)
KEYS = ("pl1_w", "pl2_w", "tau_s")

_UW_PER_W = 1_000_000
_PL_MASK = 0x7FFF
_LOCK_BIT = 63


class PowerError(Exception):
    """A power file cannot be read / written, or a value is outside what the encoding can hold."""


class Refused(PowerError):
    """The plan or the write is refused. ``check`` names the reason (``POWER_POLICY_MISSING`` ...), ``key`` the ``[power]`` key / field it is about."""

    def __init__(self, check: str, text: str, key: str | None = None) -> None:
        super().__init__(text)
        self.check = check
        self.key = key


# --------------------------------------------------------------------------------------------------------------
# MSR decoding (0x606 units, 0x610 limits, 0x614 info)
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Units:
    """``0x606``: power unit 2^-PU W (``[3:0]``), energy unit ``[12:8]``, time unit 2^-TU s (``[19:16]``). ``[MEASURED lnx 2026-09-29]`` 0.125 W / 2^-14 J / 2^-10 s on the reference machine."""

    power_w: float
    time_s: float
    raw: int


def decode_units(raw: int) -> Units:
    return Units(power_w=1.0 / (1 << (raw & 0xF)), time_s=1.0 / (1 << ((raw >> 16) & 0xF)), raw=raw)


def tau_from_raw(field7: int, time_unit_us: int) -> int:
    """The window in us of a 7-bit time field ``Y = [4:0]``, ``Z = [6:5]``: ``2^Y * (1 + Z/4) * time unit`` -- integer arithmetic like the kernel's, the unit rounded to whole us
    (1000000 / 1024 -> 976, which is why ``[MEASURED lnx 2026-10-18]`` reads 127926272 us = 2^17 * 976)."""
    y, z = field7 & 0x1F, (field7 >> 5) & 0x3
    return (1 << y) * (4 + z) * time_unit_us // 4


def tau_to_raw(window_us: int, time_unit_us: int) -> int:
    """The 7-bit field the kernel writes for ``window_us``: ``Y = floor(log2(window / unit))``, ``Z = floor(4 * (window/unit - 2^Y) / 2^Y)`` -- the request is rounded **down** to
    an encodable window ``[UNVERIFIED]`` (the kernel source says so; the write sitting H4 confirms or refutes it, and a read-back that differs from the planned window trips)."""
    if window_us < time_unit_us:
        raise PowerError(f"a time window of {window_us} us is below the time unit ({time_unit_us} us)")
    n = window_us // time_unit_us
    y = n.bit_length() - 1
    if y > 0x1F:
        raise PowerError("the time window is too large for the 5-bit exponent")
    z = (4 * (n - (1 << y))) >> y
    return y | ((z & 0x3) << 5)


def tau_snap(window_us: int, time_unit_us: int) -> int:
    """The encodable window (us) at or below ``window_us`` -- what the kernel leaves in force after a write of ``window_us``. Idempotent. ``PowerError`` below one time unit."""
    return tau_from_raw(tau_to_raw(window_us, time_unit_us), time_unit_us)


@dataclass(frozen=True)
class Limit:
    """``0x610``: PL1 / PL2 in watts, tau in seconds, the enable / clamp bits and the lock."""

    pl1_w: float
    pl2_w: float
    tau_s: float
    pl1_enabled: bool
    pl1_clamp: bool
    pl2_enabled: bool
    pl2_clamp: bool
    locked: bool
    raw: int
    tau_us: int


def decode_limit(raw: int, units: Units) -> Limit:
    """``0x610`` layout ``[DOC SDM Vol.4 table 2-? / MEASURED lnx 2026-09-29: PL1 [14:0], PL2 [46:32], lock [63]]``; enable ``[15]`` / ``[47]``, clamp ``[16]`` / ``[48]``, tau ``[23:17]`` ``[DOC SDM]``."""
    unit_us = int(units.time_s * 1_000_000)
    tau_us = tau_from_raw((raw >> 17) & 0x7F, unit_us)
    return Limit(
        pl1_w=(raw & _PL_MASK) * units.power_w,
        pl2_w=((raw >> 32) & _PL_MASK) * units.power_w,
        tau_s=tau_us / 1_000_000,
        pl1_enabled=bool(raw >> 15 & 1),
        pl1_clamp=bool(raw >> 16 & 1),
        pl2_enabled=bool(raw >> 47 & 1),
        pl2_clamp=bool(raw >> 48 & 1),
        locked=bool(raw >> _LOCK_BIT & 1),
        raw=raw,
        tau_us=tau_us,
    )


@dataclass(frozen=True)
class Info:
    """``0x614`` PKG_POWER_INFO: thermal spec power ``[14:0]``, minimum ``[30:16]``, maximum ``[46:32]`` (watts, ``0`` = not reported), maximum time window ``[53:48]``. Information only:
    never a bound (``[UNVERIFIED]`` on Arrow Lake: the register is read and shown as is)."""

    tdp_w: float
    min_w: float
    max_w: float
    raw: int


def decode_info(raw: int, units: Units) -> Info:
    return Info(
        tdp_w=(raw & _PL_MASK) * units.power_w,
        min_w=((raw >> 16) & _PL_MASK) * units.power_w,
        max_w=((raw >> 32) & _PL_MASK) * units.power_w,
        raw=raw,
    )


@dataclass(frozen=True)
class MsrView:
    """What the guarded reads of ``0x606`` / ``0x610`` / ``0x614`` showed: ``units`` and ``limit`` are ``None`` when ``0x606`` / ``0x610`` could not be read (``note`` says why)."""

    units: Units | None = None
    limit: Limit | None = None
    info: Info | None = None
    note: str | None = None


# --------------------------------------------------------------------------------------------------------------
# the powercap files
# --------------------------------------------------------------------------------------------------------------
class PowerFiles:
    """The powercap zone under ``root`` (``/sys/class/powercap``): ``intel-rapl:0/constraint_N_*``. ``read`` of an absent file gives ``None``."""

    def __init__(self, root: Path | str = DEFAULT_ROOT, zone: str = ZONE) -> None:
        self.root = Path(root)
        self.zone = zone

    def path(self, name: str) -> Path:
        return self.root / self.zone / name

    def read_text(self, name: str) -> str | None:
        try:
            return self.path(name).read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise PowerError(f"cannot read {self.path(name)}: {getattr(exc, 'strerror', None) or exc}") from None

    def read_int(self, name: str) -> int | None:
        text = self.read_text(name)
        if text is None:
            return None
        try:
            return int(text, 10)
        except ValueError:
            raise PowerError(f"{self.path(name)} holds {text[:40]!r}, not a number") from None

    def write_int(self, name: str, value: int) -> None:
        """One write of one file (the caller audits before and reads back after)."""
        data = str(int(value)).encode("ascii")
        try:
            fd = os.open(self.path(name), os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))  # the leaf is a kernel attribute file, never a link
            try:
                if os.write(fd, data) != len(data):  # a sysfs attribute takes its whole value in one write()
                    raise OSError("short write")
            finally:
                os.close(fd)
        except OSError as exc:
            raise PowerError(f"cannot write {self.path(name)}: {exc.strerror or exc}") from None

    def present(self) -> bool:
        return self.path("constraint_0_power_limit_uw").exists()


@dataclass(frozen=True)
class State:
    """The powercap values in force, in the units of the files (uW, us) and as the intent speaks (W, s)."""

    pl1_uw: int
    pl2_uw: int | None
    tau_us: int
    pl2_tau_us: int | None = None
    max_power_uw: int | None = None  # constraint_0_max_power_uw: information, never a PL1 ceiling
    names: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def pl1_w(self) -> float:
        return self.pl1_uw / _UW_PER_W

    @property
    def pl2_w(self) -> float | None:
        return None if self.pl2_uw is None else self.pl2_uw / _UW_PER_W

    @property
    def tau_s(self) -> float:
        return self.tau_us / _UW_PER_W

    def value(self, key: str) -> int | None:
        """``key`` in the file's unit: uW for the PLs, us for tau."""
        return {"pl1_w": self.pl1_uw, "pl2_w": self.pl2_uw, "tau_s": self.tau_us}[key]

    def as_json(self) -> dict[str, Any]:
        return {
            "pl1_w": self.pl1_w, "pl2_w": self.pl2_w, "tau_s": round(self.tau_s, 6),
            "pl1_uw": self.pl1_uw, "pl2_uw": self.pl2_uw, "tau_us": self.tau_us, "pl2_tau_us": self.pl2_tau_us, "max_power_uw": self.max_power_uw,
        }


def read_state(files: PowerFiles) -> State:
    """The values in force from the powercap files. ``PowerError`` when PL1 or its window cannot be read; a missing PL2 is ``None`` (PL2 then cannot be planned)."""
    if not files.present():
        raise PowerError(f"no powercap zone at {files.root / files.zone} (is the intel_rapl driver loaded?)")
    names = {n: files.read_text(n) for n in NAMES}
    wrong = {n: v for n, v in names.items() if v is not None and v != NAMES[n]}
    if wrong:
        raise PowerError(f"the powercap constraints are not the expected ones ({', '.join(f'{n}={v!r}' for n, v in wrong.items())}; expected long_term / short_term): nothing is assumed")
    pl1, tau = files.read_int(FILES["pl1_w"]), files.read_int(FILES["tau_s"])
    if pl1 is None or tau is None:
        raise PowerError("constraint_0 (PL1) has no power limit / time window file")
    return State(pl1, files.read_int(FILES["pl2_w"]), tau, files.read_int("constraint_1_time_window_us"), files.read_int("constraint_0_max_power_uw"), names)


# --------------------------------------------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PowerStep:
    """One powercap file write. ``key`` is the intent key; values in the file's unit (uW / us); ``value`` is already encodable (a tau is snapped down); ``expect`` is the ``(low, high)``
    a read-back may show -- both ends equal ``value``: the read-back is exact."""

    number: int
    key: str
    file: str
    before: int
    value: int
    expect: tuple[int, int]

    def as_json(self) -> dict[str, Any]:
        return {"n": self.number, "key": self.key, "file": self.file, "before": self.before, "value": self.value, "expect": list(self.expect)}


def _policy_range(policy: Policy, key: str) -> tuple[float, float]:
    where = policy.where
    if policy.power is None:
        raise Refused("POWER_POLICY_MISSING", f"the policy has no [power] table ({where}): no package power limit may be written. Add [power] {key} = [min, max] to the policy to allow it", key)
    rng = getattr(policy.power, key)
    if rng is None:
        raise Refused("POWER_POLICY_MISSING", f"the policy has no [power] {key} ({where}): this value may not be written. Add [power] {key} = [min, max] to the policy to allow it", key)
    return rng


def _whole_watts(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or float(value) != int(value):
        raise Refused("POWER_VALUE", f"{key} must be a whole number of watts", key)
    return int(value)


def _intent_values(intent: Mapping[str, Any]) -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    for key in KEYS:
        if intent.get(key) is None:
            continue
        v = intent[key]
        if key == "tau_s":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                raise Refused("POWER_VALUE", "tau_s must be a positive number of seconds", key)
            out[key] = float(v)
        else:
            out[key] = _whole_watts(key, v)
    return out


def _snap_tau(window_us: int, units: Units) -> int:
    """``window_us`` snapped down to the window the register can hold; ``Refused`` below one time unit / above the exponent."""
    try:
        return tau_snap(window_us, int(units.time_s * 1_000_000))
    except PowerError as exc:
        raise Refused("POWER_VALUE", str(exc), "tau_s") from None


def _writable(state: State, units: Units | None, msr: MsrView | None, keys: Mapping[str, Any]) -> Units:
    """The lock / readability gate shared by a set and a revert: ``Units`` of ``0x606``, or :class:`Refused`."""
    if msr is None or msr.limit is None:
        raise Refused("POWER_MSR_UNREADABLE", f"MSR 0x610 could not be read ({msr.note if msr and msr.note else 'no read'}), so the lock bit is unknown: a write that may silently not take is not sent")
    if msr.limit.locked:
        raise Refused("POWER_LOCKED", "MSR 0x610 bit 63 (lock) is set: the package power limits are read-only until the next reset; writing the powercap files would not take")
    if units is None:
        raise Refused("POWER_MSR_UNREADABLE", "MSR 0x606 (units) could not be read: the read-back cannot be checked")
    for key in keys:
        if state.value(key) is None:
            raise Refused("POWER_NO_FILE", f"the powercap zone has no file for {key}", key)
    return units


def _steps(new_uw: Mapping[str, int], state: State, units: Units, max_step_w: int | None, where: str) -> list[PowerStep]:
    """Order and list the writes of ``new_uw`` (values in file units) over ``state``. ``max_step_w`` ``None`` = no step limit (a revert)."""
    unit_uw = int(round(units.power_w * _UW_PER_W))
    for key in ("pl1_w", "pl2_w"):
        if key in new_uw and new_uw[key] % unit_uw:
            raise Refused("POWER_VALUE", f"{key} is not a multiple of the power unit ({units.power_w:g} W)", key)
    pl1 = new_uw.get("pl1_w", state.pl1_uw)
    pl2 = new_uw.get("pl2_w", state.pl2_uw)
    if ("pl1_w" in new_uw or "pl2_w" in new_uw) and pl2 is None:
        raise Refused("POWER_NO_FILE", "the powercap zone has no PL2 file (constraint_1): PL2 >= PL1 cannot be checked", "pl2_w")
    if pl2 is not None and pl2 < pl1:
        raise Refused("POWER_ORDER", f"PL2 ({pl2 / _UW_PER_W:g} W) would be below PL1 ({pl1 / _UW_PER_W:g} W): PL2 must be >= PL1", "pl2_w" if "pl2_w" in new_uw else "pl1_w")
    changing = {k: v for k, v in new_uw.items() if v != state.value(k)}
    if max_step_w is not None:
        step_uw = max_step_w * _UW_PER_W
        for key in ("pl1_w", "pl2_w"):
            if key in changing and abs(changing[key] - state.value(key)) > step_uw:  # type: ignore[operator]
                raise Refused(
                    "POWER_STEP",
                    f"{key} would change by {abs(changing[key] - state.value(key)) / _UW_PER_W:g} W (from {state.value(key) / _UW_PER_W:g} to {changing[key] / _UW_PER_W:g}); "  # type: ignore[operator]
                    f"[power] max_step_w = {max_step_w} W ({where})",
                    key,
                )
    order = ["pl1_w", "pl2_w"] if state.pl2_uw is None or pl1 <= state.pl2_uw else ["pl2_w", "pl1_w"]
    order.append("tau_s")
    steps: list[PowerStep] = []
    for key in order:
        if key not in changing:
            continue
        value = changing[key]
        steps.append(PowerStep(len(steps) + 1, key, FILES[key], state.value(key), value, (value, value)))  # type: ignore[arg-type]
    return steps


def plan(intent: Mapping[str, Any], policy: Policy, state: State, units: Units | None, msr: MsrView | None) -> list[PowerStep]:
    """The write steps for ``intent`` (``{pl1_w?, pl2_w?, tau_s?}``) over the values in force, or :class:`Refused` -- nothing is written by planning.

    Checks, in this order (each refusal names its check): the policy keys (``POWER_POLICY_MISSING``: names the key), the lock bit of ``0x610`` and its readability (``POWER_LOCKED`` /
    ``POWER_MSR_UNREADABLE``: a write that may silently not take is not sent), the ranges (``POWER_RANGE``; a tau is snapped down to the encodable window first and *that* value is checked), the step (``POWER_STEP``; an absent ``max_step_w`` refuses like a missing
    range), PL2 >= PL1 against the *final* pair (``POWER_ORDER``). Steps that change nothing are dropped. Write order: if the final PL1 does not exceed the current PL2
    (``new PL1 <= old PL2``) PL1 goes first, else PL2 goes first -- the pair PL2 >= PL1 then holds after every single write (a PL1 that goes down before a PL2 that goes down; a PL2
    that goes up before a PL1 that goes up); tau last.
    """
    want = _intent_values(intent)
    if not want:
        raise Refused("NOTHING_TO_DO", "the power intent changes nothing (set pl1_w, pl2_w and/or tau_s)")
    ranges = {key: _policy_range(policy, key) for key in want}  # an absent key refuses first, by name: the operator's next step is the policy file, not the register
    units = _writable(state, units, msr, want)
    new_uw: dict[str, int] = {}
    for key, v in want.items():
        lo, hi = ranges[key]
        value_uw = int(round(v * _UW_PER_W))
        shown = f"{v:g}"
        if key == "tau_s":  # the kernel rounds the window down: the value that will be in force is the one the policy bounds
            snapped = _snap_tau(value_uw, units)
            if snapped != value_uw:
                shown = f"{v:g} (snapped down to the encodable {snapped / _UW_PER_W:g})"
            value_uw = snapped
        if not lo <= value_uw / _UW_PER_W <= hi:
            raise Refused("POWER_RANGE", f"{key} = {shown} is outside [power] {key} = [{lo:g}, {hi:g}] ({policy.where})", key)
        new_uw[key] = value_uw
    max_step = None
    if any(k in new_uw and new_uw[k] != state.value(k) for k in ("pl1_w", "pl2_w")):
        if policy.power is None or policy.power.max_step_w is None:
            raise Refused("POWER_POLICY_MISSING", f"the policy has no [power] max_step_w ({policy.where}): a power limit may not be changed without a step limit. Add [power] max_step_w = <W>", "max_step_w")
        max_step = policy.power.max_step_w
    return _steps(new_uw, state, units, max_step, policy.where)


def plan_revert(values: Mapping[str, int | None], state: State, units: Units | None, msr: MsrView | None) -> list[PowerStep]:
    """The writes that put the journaled boot values back (``values`` = pl1_uw / pl2_uw / tau_us). The lock and readability gate and PL2 >= PL1 apply; the policy's ranges and
    step do not: this restores a recorded state, it does not choose a new one."""
    asked = {"pl1_w": values["pl1_uw"], "pl2_w": values["pl2_uw"], "tau_s": values["tau_us"]}
    new_uw = {k: v for k, v in asked.items() if v is not None}
    units = _writable(state, units, msr, new_uw)
    if "tau_s" in new_uw:
        new_uw["tau_s"] = _snap_tau(new_uw["tau_s"], units)
    return _steps(new_uw, state, units, None, "the journal")


# --------------------------------------------------------------------------------------------------------------
# the read-back rule
# --------------------------------------------------------------------------------------------------------------
def readback_problem(step: PowerStep, file_value: int | None, msr: MsrView | None, units: Units | None) -> str | None:
    """``None`` when the write took: the file reads back an encodable value ``step.expect`` allows (exactly ``step.value``: the PL as asked, the tau as snapped by the plan) **and**
    the ``0x610`` field, decoded, agrees with the file (PL: within one power unit; tau: ``[23:17]`` decodes to exactly the same window). Otherwise the reason (a mismatch trips the apply)."""
    if file_value is None:
        return f"{step.file} cannot be read back"
    lo, hi = step.expect
    if not lo <= file_value <= hi:
        return f"{step.file} reads {file_value}, expected {lo if lo == hi else f'{lo} to {hi}'}"
    if msr is None or msr.limit is None or units is None:
        return "MSR 0x610 could not be read back, so the write is not confirmed by the register"
    if msr.limit.locked:
        return "MSR 0x610 reports the lock bit (63) set after the write"
    if step.key == "tau_s":
        msr_us = msr.limit.tau_us
        if msr_us != file_value:
            return f"MSR 0x610 [23:17] decodes to {msr_us} us, the file reads {file_value}"
        return None
    msr_w = msr.limit.pl1_w if step.key == "pl1_w" else msr.limit.pl2_w
    if abs(msr_w * _UW_PER_W - file_value) > units.power_w * _UW_PER_W:
        return f"MSR 0x610 decodes to {msr_w:g} W, the file reads {file_value / _UW_PER_W:g} W"
    return None


# --------------------------------------------------------------------------------------------------------------
# the journal (boot values)
# --------------------------------------------------------------------------------------------------------------
def journal_doc(state: State, boot_id: str | None, created: float, pid: int | None = None) -> dict[str, Any]:
    """The journal: the values in force before the first power write of this boot -- what ``revert`` puts back. Bound to the boot id."""
    return {
        "version": JOURNAL_VERSION, "boot_id": boot_id, "created": created, "pid": os.getpid() if pid is None else pid,
        "values": {"pl1_uw": state.pl1_uw, "pl2_uw": state.pl2_uw, "tau_us": state.tau_us},
        "note": BIOS_NOTE,
    }


def parse_journal(data: bytes, boot_id: str | None) -> dict[str, int | None] | None:
    """The boot values of a journal, or ``None`` when it is for another boot (a stale file from before a reboot: ignored, replaced by the next write). ``ValueError`` when it is not a journal."""
    doc = json.loads(data.decode("ascii"))
    if not isinstance(doc, dict) or doc.get("version") != JOURNAL_VERSION or not isinstance(doc.get("values"), dict):
        raise ValueError("not a power journal")
    values = doc["values"]
    if set(values) != {"pl1_uw", "pl2_uw", "tau_us"} or not all(v is None or (isinstance(v, int) and not isinstance(v, bool) and v > 0) for v in values.values()):
        raise ValueError("bad power journal values")
    if values["pl1_uw"] is None or values["tau_us"] is None:
        raise ValueError("bad power journal values")
    pl_max_uw, tau_max_us = ENCODING_PL_MAX_W * _UW_PER_W, ENCODING_TAU_MAX_S * _UW_PER_W
    if any(values[k] is not None and values[k] > pl_max_uw for k in ("pl1_uw", "pl2_uw")) or values["tau_us"] > tau_max_us:
        raise ValueError("power journal values outside what the registers can hold")
    if doc.get("boot_id") != boot_id or boot_id is None:
        return None
    return values


# --------------------------------------------------------------------------------------------------------------
# the simulator: a kernel + MSR model of the zone (``--sim``, tests)
# --------------------------------------------------------------------------------------------------------------
class SimPowerFiles(PowerFiles):
    """An in-memory ``intel-rapl:0`` that behaves like the kernel driver and keeps the three MSRs coherent (``sync_msrs`` puts them into a simulated MSR port): a PL write is
    stored in whole power units, a window write is rounded **down** to an encodable window (the rule ``tau_to_raw`` assumes), a locked zone ignores the write silently (the kernel
    reports success; ``[UNVERIFIED]``: whether a locked write errors instead). Fault switches for tests: ``drop_writes`` (the kernel accepts and ignores), ``msr_skew_w`` (``0x610`` shows
    a PL1 and PL2 off by that many watts, ``0`` = coherent), ``unreadable_msr`` (``sync_msrs`` leaves ``0x610`` unconfigured: the simulated read fails). ``writes`` lists ``(file, value)``.
    Defaults are the reference machine's values ``[MEASURED lnx 2026-10-18]`` (units ``0x606`` ``[MEASURED lnx 2026-09-29]``, TDP ``0x614`` ``[MEASURED]`` platform.md)."""

    UNITS_RAW = 0x000A0E03
    INFO_RAW = 0x3E8  # 125 W thermal spec power; no min / max reported

    def __init__(
        self, *, pl1_w: float = 250, pl2_w: float = 295, tau_us: int = 127926272, pl2_tau_us: int = 2440, locked: bool = False,
        drop_writes: bool = False, msr_skew_w: float = 0, unreadable_msr: bool = False,
    ) -> None:
        super().__init__(Path("/nonexistent/powercap"), ZONE)
        self.units = decode_units(self.UNITS_RAW)
        self.unit_uw = int(round(self.units.power_w * _UW_PER_W))
        self.unit_us = int(self.units.time_s * 1_000_000)
        self.pl1_uw = int(pl1_w * _UW_PER_W)
        self.pl2_uw = int(pl2_w * _UW_PER_W)
        self.tau_raw = tau_to_raw(tau_us, self.unit_us)
        self.pl2_tau_us = pl2_tau_us
        self.locked, self.drop_writes, self.msr_skew_w, self.unreadable_msr = locked, drop_writes, msr_skew_w, unreadable_msr
        self.writes: list[tuple[str, int]] = []

    def present(self) -> bool:
        return True

    def read_text(self, name: str) -> str | None:
        values: dict[str, str] = {
            "constraint_0_name": "long_term", "constraint_1_name": "short_term",
            FILES["pl1_w"]: str(self.pl1_uw), FILES["pl2_w"]: str(self.pl2_uw), FILES["tau_s"]: str(tau_from_raw(self.tau_raw, self.unit_us)),
            "constraint_1_time_window_us": str(self.pl2_tau_us), "constraint_0_max_power_uw": "125000000",
        }
        return values.get(name)

    def write_int(self, name: str, value: int) -> None:
        self.writes.append((name, int(value)))
        if self.locked or self.drop_writes:
            return
        if name == FILES["pl1_w"]:
            self.pl1_uw = int(value) // self.unit_uw * self.unit_uw
        elif name == FILES["pl2_w"]:
            self.pl2_uw = int(value) // self.unit_uw * self.unit_uw
        elif name == FILES["tau_s"]:
            self.tau_raw = tau_to_raw(int(value), self.unit_us)
        else:
            raise PowerError(f"cannot write {self.path(name)}: No such file or directory")

    def msr_values(self) -> dict[int, int]:
        skew = int(self.msr_skew_w / self.units.power_w)
        pl1 = ((self.pl1_uw // self.unit_uw) + skew) & _PL_MASK
        pl2 = ((self.pl2_uw // self.unit_uw) + skew) & _PL_MASK
        raw = pl1 | (1 << 15) | (1 << 16) | (self.tau_raw << 17) | (pl2 << 32) | (1 << 47) | (int(self.locked) << _LOCK_BIT)
        return {0x606: self.UNITS_RAW, 0x610: raw, 0x614: self.INFO_RAW}

    def sync_msrs(self, port: Any, cpu: int) -> None:
        """Put the model's MSR words into a simulated port (``SimMsrPort.set_msr``); a port without ``set_msr`` (hardware) is left alone."""
        setter = getattr(port, "set_msr", None)
        if setter is None:
            return
        for msr, word in self.msr_values().items():
            if msr == 0x610 and self.unreadable_msr:
                continue
            setter(cpu, msr, word)
