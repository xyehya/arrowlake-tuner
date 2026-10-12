"""``python -m arltuner``: ``topology`` / ``plan`` / ``apply`` / ``read-vf`` / ``read`` / ``vf-dump`` (spec: ``docs/tasks/T-014-controls-cli.md``, ``T-026-vf-dump.md``).

Every mailbox request - reads included, because each is a WRMSR to MSR 0x150 (``docs/SAFETY.md`` "OC mailbox rule") - is sent
through ``arltuner.guard.WriteGuard``; this module never calls a transport itself. The CLI never sets ``ARLTUNER_ALLOW_MAILBOX``
(the human exports it) and never accepts the confirmation token as an argument: it prints a random token and the operator must
type ``CONFIRM <token>``.

``--device`` uses ``DevCpuMsrPort(writable=True)`` (``/dev/cpu/N/msr``). Every refusal - env flag missing, wrong confirmation,
policy / CPU / topology refusal, pre-flight failure - happens **before** that port is constructed.
``--device-win`` is the same on Windows 11 (Administrator): ``WinThrottleStopMsrPort`` (ThrottleStop's driver, thread pinned per access) with the
WHEA-Logger event-log health gate; every guard step is unchanged. It needs the accepted state's ``fingerprint.json`` (``--fingerprint``, G-138): the Linux CPU ->
Windows LP map is derived at run time from Windows' own data and settled by the guarded identity reads (cmd 0x09) that precede every write.

Exit codes: 0 ok - 1 usage / parse errors - 2 refused, plan not allowed, needs a higher limit - 3 hardware error, timeout,
non-zero completion code, unverified read-back or tripped guard.

All I/O is injectable (``main`` parameters) so the tests run against the simulator with temp files and never touch a device.
"""
from __future__ import annotations

import argparse
import dataclasses
import hmac
import json
import math
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO

from arltuner import vfdump
from arltuner.controls import (
    ApplyPlan,
    ApplyReport,
    ControlsError,
    CoreReadings,
    PlanError,
    PreflightError,
    StepKind,
    StopKind,
    apply_profile,
    build_plan,
    describe_word,
    format_plan,
    format_preview,
    format_revert_plan,
    predict,
    predict_msr,
    preflight,
    read_steps,
    resolve_point_targets,
    resolve_targets,
)
from arltuner.guard import (
    ENV_FLAG,
    POINT_P2_MAX,
    AuditLog,
    Confirmation,
    GuardRefused,
    Policy,
    PolicyError,
    Refusal,
    MsrRequest,
    Request,
    WriteGuard,
    anchored_wall_clock,
    default_m1_policy,
    load_policy,
    AuditUnreadable,
    read_boot_id,
    read_state,
)
from arltuner.mailbox import Cmd, VfWord, rc_name
from arltuner.msr import (
    DevCpuMsrPort,
    IdentityError,
    MailboxTransport,
    MsrError,
    MsrPort,
    SimConfig,
    SimMsrPort,
    WinThrottleStopMsrPort,
    identity_cpus,
    resolve_identity,
    verify_machine,
    windows_boot_id,
)
from arltuner.profiles import Profile, ProfileError, load_profile
from arltuner.profiles.fingerprint import FINGERPRINT_FILE, Fingerprint, FingerprintError, load_fingerprint
from arltuner.topology import Topology, TopologyError, TopologyMismatch, UnknownCore, detect
from arltuner.topology.reference import arl_s_285k

__all__ = [
    "EXIT_HARDWARE",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "InstalledStateRefused",
    "default_audit_path",
    "default_lock_path",
    "installed_state",
    "main",
]

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2
EXIT_HARDWARE = 3

HealthGate = Callable[[str], tuple[bool, str]]
WIN_HELP = "Windows 11 as Administrator via ThrottleStop's kernel driver (hardware; WHEA event-log health gate; --allow-taint-bit is ignored)"


# --------------------------------------------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------------------------------------------
class InstalledStateRefused(Exception):
    """``/etc/arltuner/policy.toml`` exists but fails the helper's trust check (D-030): refuse, never fall back to the legacy paths."""


_TMPFILES_CONF = "/usr/lib/tmpfiles.d/arltuner.conf"


def installed_state(lstat_fn: Callable[[str], object] = os.lstat) -> bool:
    """D-030: the machine is *installed* when ``/etc/arltuner/policy.toml`` exists and passes the helper's own trust check
    (``arltuner.helper.trust``: root-owned, not group/world-writable, no symlink, root-owned parents). Absent => ``False`` (legacy paths
    unchanged). Present but untrusted => :class:`InstalledStateRefused` (no fallback: a planted file must not silently route the
    guard's lock and audit log elsewhere)."""
    from arltuner.helper.config import INSTALLED_POLICY  # lazy: the CLI must not import the helper package at module load
    from arltuner.helper.trust import TrustError, check_trusted_path

    try:
        lstat_fn(os.fspath(INSTALLED_POLICY))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        pass  # exists but cannot be examined: the trust check below refuses it by name
    try:
        check_trusted_path(INSTALLED_POLICY, want="file", lstat_fn=lstat_fn)
    except TrustError as exc:
        raise InstalledStateRefused(
            f"{INSTALLED_POLICY} is present but not trusted ({exc.path}: {exc.reason}); the CLI will not guess between the installed and the "
            "legacy lock/audit paths (D-030). Repair the install (re-run install.sh as root) or pass explicit --audit and --lock. "
        ) from None
    return True


def default_audit_path(
    env: Mapping[str, str], windows: bool = os.name == "nt", lstat_fn: Callable[[str], object] = os.lstat, consider_install: bool = True
) -> Path:
    """Installed machine (D-030, not Windows): ``/var/lib/arltuner/audit.jsonl``. Otherwise ``~/.local/state/linuxmon/audit.jsonl`` (home from
    ``env`` HOME / USERPROFILE, else the OS home); on Windows ``%LOCALAPPDATA%\\linuxmon\\audit.jsonl`` when LOCALAPPDATA is set.
    ``consider_install=False`` (simulator runs, which never touch hardware) skips the rule: always the legacy per-user path.
    Raises :class:`InstalledStateRefused` when the installed policy exists but is untrusted."""
    if windows and env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "linuxmon" / "audit.jsonl"
    if consider_install and not windows and installed_state(lstat_fn):
        from arltuner.helper.config import INSTALLED_AUDIT

        return INSTALLED_AUDIT
    home = env.get("HOME") or env.get("USERPROFILE")
    base = Path(home) if home else Path.home()
    return base / ".local" / "state" / "linuxmon" / "audit.jsonl"


def default_lock_path(
    isdir: Callable[[str], bool] = os.path.isdir,
    lstat_fn: Callable[[str], object] = os.lstat,
    windows: bool = os.name == "nt",
    consider_install: bool = True,
) -> Path:
    """Installed machine (D-030, not Windows): ``/run/arltuner/mailbox.lock``. Otherwise ``/run/lock/linuxmon-mailbox.lock`` when ``/run/lock``
    exists, else the temp directory. ``consider_install=False`` (simulator runs): never the installed path. Raises :class:`InstalledStateRefused` when the installed policy exists but is untrusted."""
    if consider_install and not windows and installed_state(lstat_fn):
        from arltuner.helper.config import INSTALLED_LOCK

        return INSTALLED_LOCK
    if isdir("/run/lock"):
        return Path("/run/lock") / "linuxmon-mailbox.lock"
    return Path(tempfile.gettempdir()) / "linuxmon-mailbox.lock"


def _default_device_health_gate_factory(taint_bits: frozenset[int]) -> HealthGate:
    """The real read-only bracket (``arltuner.health``: taint word + PCI set + module set) over the live /proc and /sys.

    Used only for ``--device``. ``taint_bits`` are the ``--allow-taint-bit`` values (the expected first-write taint)."""
    from arltuner.health import make_health_gate

    return make_health_gate(Path("/"), allowed_new_taint_bits=taint_bits)


def _default_device_port() -> MsrPort:
    """The only place a real device port is built (a mailbox read is a WRMSR, so it is writable). Looked up at call time."""
    return DevCpuMsrPort(writable=True)


def _default_win_health_gate_factory(taint_bits: frozenset[int]) -> HealthGate:
    """The Windows bracket (new WHEA-Logger events in the System log). ``taint_bits`` is a Linux notion and is ignored."""
    from arltuner.health import make_whea_health_gate
    from arltuner.wevtutil import run_wevtutil

    return make_whea_health_gate(run_wevtutil)


def _default_win_port(fingerprint: Fingerprint) -> MsrPort:
    """The only place a ThrottleStop port is built (writable, like the /dev/cpu one). It derives and verifies the LP map before it exists."""
    return WinThrottleStopMsrPort(writable=True, fingerprint=fingerprint)


def _default_win_detect(fingerprint: Fingerprint) -> Topology:
    """Windows' stand-in for ``detect``: the Linux topology the accepted state was recorded on, provided Windows' CPU and layout match it."""
    try:
        verify_machine(None, fingerprint)
    except OSError as exc:
        raise TopologyMismatch(str(exc)) from exc
    return fingerprint.topology()


def _default_sim_port() -> MsrPort:
    """The simulator for ``--sim``: the simulated reference CPU, as MEASURED on Windows (``SimConfig.measured()``, ``[MEASURED win 2026-10-01]``: per-core
    0x10 word 0x0 except CPU 0, fused points, favored words, ...) with the one unmeasured rule switched on -- a 0x11 with a zero ratio or
    V target is rejected (``reject_zero_fields``) -- so a plan that would send one fails here too. T-025: ``per_point`` is on (P2 1..6 E / 1..8 P, past that rc 0x11)."""
    return SimMsrPort(SimConfig.measured(reject_zero_fields=True, per_point=True))


class _VirtualClock:
    """Simulator runs: ``sleep`` advances a virtual clock instantly, so dwell is honoured without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------------------------------------------
class _UsageError(Exception):
    pass


class _ArgparseExit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _Done(Exception):
    """Internal early exit of a command with an exit code (the message has already been written)."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    """argparse that neither exits the process nor writes to ``sys.std*``: usage errors become ``_UsageError`` (exit code 1)."""

    def __init__(self, *args: object, out: TextIO, err: TextIO, **kwargs: object) -> None:
        super().__init__(*args, allow_abbrev=False, **kwargs)  # type: ignore[arg-type]
        self._out = out
        self._err = err

    def error(self, message: str) -> NoReturn:
        raise _UsageError(f"{self.prog}: {message}")

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            self._err.write(message)
        raise _ArgparseExit(status)

    def print_help(self, file: TextIO | None = None) -> None:
        super().print_help(self._out if file is None else file)

    def print_usage(self, file: TextIO | None = None) -> None:
        super().print_usage(self._err if file is None else file)


def _taint_bit(text: str) -> int:
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if not 0 <= value <= 63:
        raise argparse.ArgumentTypeError(f"taint bit {value} is outside 0..63")
    return value


def _uint(text: str, top: int | None = None) -> int:
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer (decimal or 0x hex)") from None
    if value < 0 or (top is not None and value > top):
        raise argparse.ArgumentTypeError(f"{text} is outside 0..{top if top is not None else 'unbounded'}")
    return value


def _cmd_byte(text: str) -> int:
    return _uint(text, 0xFF)


def _build_parser(out: TextIO, err: TextIO) -> _Parser:
    parser = _Parser(
        prog="python -m arltuner",
        description="arltuner: plan and apply per-core tuning through the guarded OC mailbox (simulator or /dev/cpu).",
        out=out,
        err=err,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{topology,plan,apply,read-vf,read,vf-dump,point-offset}")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--policy",
            metavar="FILE",
            help="reviewed policy TOML (default: the built-in conservative M1 policy: no raise, step limits, D-015 offset floor); "
            "ratio / voltage ceilings, the offset window and step limits are this file's numbers (D-016, D-039): editable up to the CPU's own limits "
            "(cmd 0x01 ratio, cmd 0x28 voltage, read from the CPU) and the field encodings",
        )
        p.add_argument("--audit", metavar="FILE", help="audit log (default: /var/lib/arltuner/audit.jsonl on an installed machine (D-030); else ~/.local/state/linuxmon/audit.jsonl, on Windows %%LOCALAPPDATA%%\\linuxmon\\audit.jsonl)")
        p.add_argument("--lock", metavar="FILE", help="lock file (default: /run/arltuner/mailbox.lock on an installed machine (D-030); else /run/lock/linuxmon-mailbox.lock, else the temp dir)")
        p.add_argument("--operator", metavar="NAME", help="name recorded in the audit log (default: $USER)")
        p.add_argument(
            "--fingerprint",
            metavar="FILE",
            help="--device-win only: the accepted state's fingerprint.json (written by 'Set as accepted' on Linux; default: %%LOCALAPPDATA%%\\arltuner\\accepted\\fingerprint.json)",
        )
        p.add_argument(
            "--allow-taint-bit",
            metavar="N",
            type=_taint_bit,
            action="append",
            default=[],
            help="kernel taint bit the health gate may ignore (repeatable; used once the health gate is wired)",
        )

    p_top = sub.add_parser("topology", out=out, err=err, help="print the core map derived from sysfs (XTU name, Linux CPU, cluster, favored)")
    p_top.add_argument("--json", action="store_true", help="machine-readable output")

    p_plan = sub.add_parser("plan", out=out, err=err, help="dry-run a profile against the simulator's state; sends nothing")
    p_plan.add_argument("profile", metavar="PROFILE.toml")
    p_plan.add_argument("--sim", action="store_true", help="use the simulator's state as baseline (required)")
    p_plan.add_argument(
        "--policy", metavar="FILE", help="reviewed policy TOML (default: the built-in conservative M1 policy; a raise needs numbers in this file)"
    )

    p_apply = sub.add_parser("apply", out=out, err=err, help="apply a profile through the write guard (needs a typed confirmation)")
    p_apply.add_argument("profile", metavar="PROFILE.toml")
    mode = p_apply.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sim", action="store_true", help="run against the software simulator")
    mode.add_argument("--device", action="store_true", help="run against /dev/cpu/N/msr (hardware; a mailbox read is a WRMSR)")
    mode.add_argument("--device-win", action="store_true", help=WIN_HELP)
    common(p_apply)

    p_read = sub.add_parser("read-vf", out=out, err=err, help="ONE guarded cmd 0x10 read on one CPU (the T-007 instrument)")
    p_read.add_argument("--cpu", type=int, required=True, metavar="N", help="Linux CPU number")
    mode = p_read.add_mutually_exclusive_group()
    mode.add_argument("--sim", action="store_true", help="simulator (the default)")
    mode.add_argument("--device", action="store_true", help="/dev/cpu/N/msr (hardware; a mailbox read is a WRMSR)")
    mode.add_argument("--device-win", action="store_true", help=WIN_HELP)
    common(p_read)

    p_rd = sub.add_parser("read", out=out, err=err, help="ONE guarded mailbox READ of any allowlisted read command (kind = \"read\" entries only)")
    p_rd.add_argument("--cpu", type=int, required=True, metavar="N", help="Linux CPU number")
    p_rd.add_argument("--cmd", type=_cmd_byte, required=True, metavar="C", help="mailbox command, decimal or 0x hex (e.g. 0x1C)")
    p_rd.add_argument("--p1", type=_uint, default=0, metavar="N", help="parameter 1 (default 0)")
    p_rd.add_argument("--p2", type=_uint, default=0, metavar="N", help="parameter 2 (default 0)")
    mode = p_rd.add_mutually_exclusive_group()
    mode.add_argument("--sim", action="store_true", help="simulator (the default)")
    mode.add_argument("--device", action="store_true", help="/dev/cpu/N/msr (hardware; a mailbox read is a WRMSR)")
    mode.add_argument("--device-win", action="store_true", help=WIN_HELP)
    common(p_rd)

    p_po = sub.add_parser(
        "point-offset",
        out=out,
        err=err,
        help="ONE guarded session: read a V/F point (cmd 0x10), then write only its offset field (cmd 0x11) -- core (P1 0) or ring (P1 2)",
    )
    p_po.add_argument("--cpu", type=int, required=True, metavar="N", help="Linux CPU 0..23 (the ring is package-level: use 0)")
    p_po.add_argument("--p1", type=_uint, choices=(0, 2), default=0, metavar="0|2", help="domain: 0 = core (default), 2 = ring")
    p_po.add_argument("--p2", type=_uint, required=True, metavar="N", help="V/F point 1..15")
    p_po.add_argument("--offset-mv", type=float, required=True, metavar="X", help="new offset of the point in mV (rounded to 1/1024 V like profiles)")
    p_po.add_argument("--expect-ratio", type=_uint, metavar="R", help="refuse before any write unless the point's ratio field reads R")
    mode = p_po.add_mutually_exclusive_group()
    mode.add_argument("--sim", action="store_true", help="simulator (the default)")
    mode.add_argument("--device", action="store_true", help="/dev/cpu/N/msr (hardware; a mailbox read is a WRMSR)")
    mode.add_argument("--device-win", action="store_true", help=WIN_HELP)
    common(p_po)

    p_dump = sub.add_parser(
        "vf-dump", out=out, err=err, help="ONE guarded session that reads the V/F curve (OC word + every point) of the P-cores and E-clusters; --measure adds the voltages"
    )
    p_dump.add_argument("--cpus", metavar="LIST", help="CPUs to dump, e.g. 0-7,8,12 (default: all 8 P-cores + one CPU per E-cluster: 8, 12, 16, 20)")
    p_dump.add_argument("--all-e", action="store_true", help="dump all 16 E CPUs instead of one per cluster (ignored when --cpus is given)")
    p_dump.add_argument(
        "--measure",
        action="store_true",
        help="also measure the requested VID of every point ratio per P-core / E-cluster: caps cpufreq scaling_max_freq (always restored), runs stress-ng, "
        "reads MSR 0x198 through the guard (needs --device and a policy msr_allow entry for 0x198)",
    )
    p_dump.add_argument("--json", metavar="FILE", help="also write the same data as JSON")
    mode = p_dump.add_mutually_exclusive_group()
    mode.add_argument("--sim", action="store_true", help="simulator (the default)")
    mode.add_argument("--device", action="store_true", help="/dev/cpu/N/msr (hardware; a mailbox read is a WRMSR)")
    mode.add_argument("--device-win", action="store_true", help=WIN_HELP)
    common(p_dump)
    return parser


@dataclass
class _Ctx:
    env: Mapping[str, str]
    stdin: TextIO
    out: TextIO
    err: TextIO
    clock: Callable[[], float] | None
    sleep: Callable[[float], object] | None
    wall_clock: Callable[[], float]
    boot_id_fn: Callable[[], str | None]
    confirm_fn: Callable[[str], str] | None
    token_fn: Callable[[], str]
    health_gate: HealthGate | None
    health_gate_factory: Callable[[frozenset[int]], HealthGate | None] | None
    device_health_gate_factory: Callable[[frozenset[int]], HealthGate | None]
    sim_factory: Callable[[], MsrPort]
    device_port_factory: Callable[[], MsrPort]
    detect_fn: Callable[[], Topology]
    win: bool = False  # --device-win: the ThrottleStop port / WHEA gate / Windows LP-map check replace the /dev/cpu + /proc ones
    win_port_factory: Callable[[Fingerprint], MsrPort] = _default_win_port
    win_health_gate_factory: Callable[[frozenset[int]], HealthGate | None] = _default_win_health_gate_factory
    win_detect_fn: Callable[[Fingerprint], Topology] = _default_win_detect
    fingerprint_path: str | None = None  # --fingerprint (default: %LOCALAPPDATA%\\arltuner\\accepted\\fingerprint.json)
    fingerprint: Fingerprint | None = None  # loaded by _topology_for on --device-win
    identity_cpus: tuple[int, ...] = ()  # the CPUs of the identity reads (--device-win): every P CPU + the first CPU of each E cluster
    measure_io_factory: Callable[[], vfdump.MeasureIO] | None = None  # T-026: None -> the real cpufreq/stress-ng I/O for --device, a refusal for --sim
    hardware: bool = False  # --device / --device-win: only hardware runs apply the D-030 installed paths (a simulator keeps the per-user defaults)
    lstat_fn: Callable[[str], Any] = os.lstat  # D-030 installed-detection (tests inject fake stat results)

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def warn(self, text: str) -> None:
        self.err.write(text + "\n")

    def fail(self, code: int, text: str) -> NoReturn:
        self.err.write(text + "\n")
        raise _Done(code)

    def refuse(self, text: str) -> NoReturn:
        self.fail(EXIT_REFUSED, f"refused: {text}")


# --------------------------------------------------------------------------------------------------------------
# Shared steps
# --------------------------------------------------------------------------------------------------------------
def _load_profile_or_exit(ctx: _Ctx, path: str) -> Profile:
    try:
        return load_profile(Path(path))
    except ProfileError as exc:
        ctx.fail(EXIT_USAGE, f"profile error: {exc}")


def _load_policy_or_exit(ctx: _Ctx, path: str | None, topology: Topology) -> Policy:
    """The policy bound to the topology of this run (live on hardware; ``"auto"`` entries and the CPU sets are derived from it, D-036)."""
    if path is None:
        return default_m1_policy(topology)
    try:
        return load_policy(Path(path), topology)
    except PolicyError as exc:
        ctx.fail(EXIT_USAGE, f"policy error: {exc}")


def _require_env_flag(ctx: _Ctx) -> None:
    """The human must have exported ``ARLTUNER_ALLOW_MAILBOX=1``; this code only ever *reads* the environment."""
    if ctx.env.get(ENV_FLAG) != "1":
        ctx.refuse(
            f"environment variable {ENV_FLAG} is not '1'. Mailbox traffic (reads included: each is a WRMSR) needs the human to "
            f"export {ENV_FLAG}=1; this program never sets it. Nothing was sent."
        )


def _load_fingerprint(ctx: _Ctx) -> Fingerprint:
    """--device-win: the accepted state's ``fingerprint.json`` (nothing is read from Windows or sent before it is judged restorable); refuses otherwise."""
    path = ctx.fingerprint_path
    if path is None:
        base = ctx.env.get("LOCALAPPDATA")
        if not base:
            ctx.refuse(f"--device-win needs the accepted state's {FINGERPRINT_FILE}: pass --fingerprint FILE (%LOCALAPPDATA% is not set). Nothing was sent.")
        path = str(Path(base) / "arltuner" / "accepted" / FINGERPRINT_FILE)
    try:
        fingerprint = load_fingerprint(path)
        fingerprint.require_restorable()
    except FingerprintError as exc:
        ctx.refuse(f"{exc}. Nothing was sent.")
    ctx.fingerprint = fingerprint
    return fingerprint


def _topology_for(ctx: _Ctx, device: bool) -> Topology:
    """The simulated reference CPU for ``--sim``; on hardware the topology derived live from sysfs (D-036), refusing when it cannot be."""
    if not device:
        return arl_s_285k()
    try:
        if ctx.win:
            fingerprint = _load_fingerprint(ctx)
            topology = ctx.win_detect_fn(fingerprint)
            ctx.identity_cpus = identity_cpus(topology)
            return topology
        return ctx.detect_fn()
    except TopologyError as exc:
        ctx.refuse(f"this system's CPU topology could not be derived or is not an Arrow Lake-S layout: {exc}. Nothing was sent.")


def _operator(ctx: _Ctx, name: str | None) -> str:
    for candidate in (name, ctx.env.get("USER"), ctx.env.get("USERNAME"), ctx.env.get("LOGNAME")):
        if candidate and candidate.strip():
            return candidate.strip()
    return "operator"


def _confirm(ctx: _Ctx, what: str) -> tuple[str, str]:
    """Print the session token and require ``CONFIRM <token>``. Returns ``(token, typed_token)``; anything else is a refusal."""
    token = ctx.token_fn()
    if not isinstance(token, str) or not token.strip() or len(token.split()) != 1:
        ctx.fail(EXIT_USAGE, "internal error: the session token must be one non-empty word")
    prompt = f"{what}\nTo proceed type exactly:  CONFIRM {token}\n> "
    ctx.out.write(prompt)
    ctx.out.flush()
    line = ctx.confirm_fn(prompt) if ctx.confirm_fn is not None else ctx.stdin.readline()
    parts = line.split() if isinstance(line, str) else []
    if len(parts) != 2 or parts[0] != "CONFIRM" or not hmac.compare_digest(
        parts[1].encode("utf-8", "surrogatepass"), token.encode("utf-8", "surrogatepass")
    ):
        ctx.say()
        ctx.refuse("the confirmation does not match (type CONFIRM followed by the token shown). Nothing was sent.")
    ctx.say()
    return token, parts[1]


def _clock_sleep(ctx: _Ctx, device: bool) -> tuple[Callable[[], float], Callable[[float], object]]:
    if ctx.clock is None and ctx.sleep is None:
        if device:
            return time.monotonic, time.sleep
        virtual = _VirtualClock()  # simulator: honour the dwell in virtual time
        return virtual.clock, virtual.sleep
    return ctx.clock or time.monotonic, ctx.sleep or time.sleep


def _device_port(ctx: _Ctx) -> MsrPort:
    """The hardware port of this run: the ThrottleStop port built from the accepted state's fingerprint (--device-win) or the /dev/cpu one."""
    if ctx.win:
        assert ctx.fingerprint is not None  # _topology_for loaded it before any port is built
        return ctx.win_port_factory(ctx.fingerprint)
    return ctx.device_port_factory()


def _health_gate(ctx: _Ctx, taint_bits: Sequence[int], device: bool) -> HealthGate | None:
    gate = ctx.health_gate
    if gate is None and ctx.health_gate_factory is not None:
        gate = ctx.health_gate_factory(frozenset(taint_bits))
    if gate is None and device:
        gate = (ctx.win_health_gate_factory if ctx.win else ctx.device_health_gate_factory)(frozenset(taint_bits))
    if gate is None and device:
        # D-011 / docs/SAFETY.md: the health bracket is mandatory for real hardware. Fail closed.
        ctx.refuse("no health gate is available for --device (the health bracket is mandatory). Nothing was sent.")
    return gate


# --device-win: Windows cannot be asked for the Linux CPU numbering and cannot tell the P-cores apart, but the fused V/F point (cmd 0x09 P1=0x11 P2=0) is
# distinct per physical P-core ([MEASURED lnx 2026-10-02]) and shared inside an E cluster. The accepted state's fingerprint.json records it per Linux CPU; the
# identity check reads it through the guard on every P LP of the provisional map (and the first LP of each E cluster), matches the words and settles the map.
def _identity_ops(ctx: _Ctx) -> int:
    """Guard operations the identity check spends (0 without --device-win)."""
    return len(ctx.identity_cpus) if ctx.win else 0


def _identity_note(ctx: _Ctx) -> str:
    n = _identity_ops(ctx)
    return (
        f"Identity check first (--device-win): {n} guarded reads of cmd 0x09 P1=0x11 P2=0 (every P-core and the first CPU of each E cluster), matched with the fused V/F "
        "words recorded in the accepted state's fingerprint.json; any mismatch refuses the whole session before a write."
    )


_NO_IDENTITY_NOTE = (
    "note: --device-win read-vf / read send exactly one request and run no identity check: a P-core CPU number is the first guess of the Linux CPU -> Windows LP "
    "map and may be another physical P-core (E clusters and the P/E split are verified). apply, point-offset and vf-dump settle it first."
)


def _identity_policy_check(ctx: _Ctx, policy: Policy) -> None:
    """Refuse (nothing sent) unless the policy allows the 0x09 identity reads on every identity CPU and has room for them."""
    for cpu in ctx.identity_cpus:
        req = Request(cpu, int(Cmd.VOLTAGE_FLOOR_READ), 0x11, 0, 0, "identity check")
        entry = policy.match(req.cmd, req.p1, req.p2)
        if entry is None or entry.kind != "read":
            ctx.refuse("--device-win needs the identity check (cmd 0x09 P1=0x11 P2=0 on the P-cores and E clusters) and no read allow entry of the policy matches it "
                       "(default deny; see policies/default.toml 'voltage_floor_read'). Nothing was sent.")
        verdict = predict(policy, req, None)
        if not verdict.allowed:
            ctx.refuse(f"identity check: {verdict.reason}: {verdict.detail} (cpu {cpu}). Nothing was sent.")
    if policy.max_operations_per_session <= _identity_ops(ctx):
        ctx.refuse(f"--device-win spends {_identity_ops(ctx)} guard operations on the identity check but the policy allows {policy.max_operations_per_session} per session. Nothing was sent.")


def _identity_check(ctx: _Ctx, guard: WriteGuard, confirm: Confirmation, sleep: Callable[[float], object], dwell: float, audit_path: Path, port: MsrPort) -> None:
    """Read the fused V/F word of every identity CPU through the guard, one request at a time, then settle the Linux CPU -> Windows LP map from the recorded words
    (``resolve_identity``) and rebind the port's P order. Refuses (no write sent) unless every P word is distinct and matches exactly one recorded P-core and every
    E cluster reads its recorded word. The reads go to the provisional slots, so ``cpu`` in these audit records is a slot, not yet a verified Linux CPU."""
    fingerprint = ctx.fingerprint
    assert fingerprint is not None
    lps = getattr(port, "lp_of_cpu", None)
    read: dict[int, int] = {}
    for cpu in ctx.identity_cpus:
        where = f", provisional Windows LP {lps[cpu]}" if lps is not None and cpu in lps else ""
        req = Request(cpu, int(Cmd.VOLTAGE_FLOOR_READ), 0x11, 0, 0, f"identity check: fused V/F point of slot cpu {cpu}{where} (Windows LP map, provisional until matched)")
        try:
            result = guard.execute(req, confirm)
        except GuardRefused as exc:
            ctx.fail(EXIT_HARDWARE if exc.reason is Refusal.TRIPPED else EXIT_REFUSED, _refusal_text(exc, audit_path))
        except Exception as exc:  # MailboxTimeout / MsrError: the guard has audited it and tripped
            ctx.fail(EXIT_HARDWARE, f"identity check: hardware error: {type(exc).__name__}: {exc}")
        if result.response.rc != 0:
            ctx.refuse(f"identity check failed on slot cpu {cpu}: completion code {result.response.rc} ({rc_name(result.response.rc)}). No write was sent.")
        read[cpu] = result.response.data
        if dwell > 0:
            sleep(dwell)  # the guard enforces the dwell between operations; waiting is ours
    try:
        permutation = resolve_identity(fingerprint, read)
    except IdentityError as exc:
        ctx.refuse(f"identity check failed: {exc}. The Linux CPU -> Windows LP map cannot be established from what the CPU reports. No write was sent.")
    moved = {cpu: slot for cpu, slot in permutation.items() if cpu != slot}
    rebind = getattr(port, "apply_identity", None)
    if moved and rebind is None:
        ctx.refuse("identity check: the P-cores read in a different order than the accepted state recorded and this port cannot rebind its map. No write was sent.")
    if rebind is not None:
        try:
            rebind(permutation)
        except MsrError as exc:
            ctx.refuse(f"identity check: {exc}. No write was sent.")
    ctx.say(f"identity check passed: {len(read)} guarded reads matched the accepted state's fused V/F words ({len(moved)} P-cores were in a different order than the first guess).")


def _build_guard(
    ctx: _Ctx, port: MsrPort, policy: Policy, audit: AuditLog, token: str, gate: HealthGate | None, lock: str | None, device: bool
) -> tuple[WriteGuard, Callable[[], float], Callable[[float], object]]:
    clock, sleep = _clock_sleep(ctx, device)
    transport = MailboxTransport(port, sleep=sleep, clock=clock)
    lock_path = Path(lock) if lock else _default_lock(ctx)
    # T-016: the guard also enforces dwell across processes from the audit log's wall-clock timestamps. On hardware that is
    # the real wall clock. A simulator run waits on a virtual (or injected) clock, so its wall clock must move with it.
    wall_clock = ctx.wall_clock if device else anchored_wall_clock(clock, ctx.wall_clock)
    guard = WriteGuard(
        transport,
        policy,
        audit,
        session_token=token,
        env=ctx.env,
        health_gate=gate,
        lock_path=lock_path,
        clock=clock,
        wall_clock=wall_clock,
        boot_id_fn=ctx.boot_id_fn,
        msr_port=port,  # T-023: plain rdmsr/wrmsr of the turbo tables go through the same guard, on the same port
    )
    return guard, clock, sleep


def _default_lock(ctx: _Ctx) -> Path:
    try:
        lock = default_lock_path(lstat_fn=ctx.lstat_fn, consider_install=ctx.hardware)
    except InstalledStateRefused as exc:
        ctx.refuse(f"{exc}Nothing was sent.")
    from arltuner.helper.config import INSTALLED_LOCK_DIR  # lazy, as in installed_state

    if ctx.hardware and lock.parent == INSTALLED_LOCK_DIR:  # the installed lock: its directory comes from tmpfiles.d, never created here
        try:
            ctx.lstat_fn(os.fspath(INSTALLED_LOCK_DIR))
        except (FileNotFoundError, NotADirectoryError):
            ctx.refuse(
                f"the installed lock directory {INSTALLED_LOCK_DIR} does not exist (/etc/arltuner/policy.toml is present, so the CLI uses it and never falls back to the legacy lock). "
                f"Create it with: sudo systemd-tmpfiles --create {_TMPFILES_CONF}. Nothing was sent."
            )
        except OSError:
            pass  # exists but cannot be examined: the guard's own open reports it
    return lock


def _default_audit(ctx: _Ctx) -> Path:
    try:
        return default_audit_path(ctx.env, lstat_fn=ctx.lstat_fn, consider_install=ctx.hardware)
    except InstalledStateRefused as exc:
        ctx.refuse(f"{exc}Nothing was sent.")


def _open_audit(ctx: _Ctx, path: str | None) -> AuditLog:
    target = Path(path) if path else _default_audit(ctx)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        return AuditLog(target)
    except OSError as exc:
        ctx.fail(EXIT_USAGE, f"cannot open the audit log {target}: {exc}")


def _boot_ops_used(ctx: _Ctx, path: str | None) -> int:
    """Operations the audit log already records for this boot (every process, reads included). An unreadable log refuses,
    exactly as the guard would (``AUDIT_UNAVAILABLE``): a budget that cannot be evaluated is not waived."""
    target = Path(path) if path else _default_audit(ctx)
    try:
        state = read_state(target)
    except (AuditUnreadable, OSError) as exc:
        ctx.refuse(f"the audit log {target} is unavailable, so the per-boot budget cannot be checked: {exc}. Nothing was sent.")
    try:
        boot = ctx.boot_id_fn()
    except Exception:
        boot = None
    boot_id = boot.strip() or None if isinstance(boot, str) else None
    return state.attempts_by_boot.get(boot_id, 0)


def _refusal_text(exc: GuardRefused, audit_path: Path) -> str:
    """One clear message for a guard refusal. T-016 adds the limits that survive process boundaries (SAFETY.md
    "Mailbox-session discipline", D-011): say what to do next, and for ``DWELL`` how long to wait. Nothing was sent."""
    lines = [f"refused by the guard: {exc}"]
    if exc.reason is Refusal.DWELL and exc.retry_after_s is not None:
        lines.append(
            f"DWELL: wait about {math.ceil(exc.retry_after_s)} s before the next mailbox operation (this program never waits by itself)."
        )
    elif exc.reason is Refusal.BOOT_LIMIT:
        lines.append(
            "BOOT_LIMIT: no further mailbox operation is allowed until the next reboot, or until the human raises "
            "max_operations_per_boot in a reviewed policy."
        )
    elif exc.reason is Refusal.AUDIT_UNAVAILABLE:
        lines.append(
            f"AUDIT_UNAVAILABLE: the audit log {audit_path} must be readable for the cross-process limits; "
            "the human inspects and repairs it."
        )
    lines.append("Nothing was sent.")
    return "\n".join(lines)


def _close(port: object) -> None:
    close = getattr(port, "close", None)
    if callable(close):
        close()


def _sim_plan(ctx: _Ctx, sim: MsrPort, profile: Profile, topology: Topology, policy: Policy) -> ApplyPlan:
    """Exact plan from the simulator's own state (a test-double peek, not a mailbox request; nothing is sent)."""
    if not isinstance(sim, SimMsrPort):
        ctx.fail(EXIT_USAGE, "internal error: the simulator factory did not return a SimMsrPort")
    cfg = sim.config

    def vmax(cpu: int) -> int:  # what a 0x28 read would answer ([11:0]); the planner reads it only where an OC-point voltage is set
        return cfg.max_voltage_cpu.get(cpu, cfg.max_voltage_raw) & 0xFFF

    baseline = {
        t.cpu: CoreReadings(
            vf=sim.vf(t.cpu),
            favored=sim.favored_word(t.cpu),
            fused=sim.fused_word(t.cpu),
            granular=sim.granular_word(t.cpu),
            vmax=None if t.oc_voltage_mv is None else vmax(t.cpu),
        )
        for t in resolve_targets(profile, topology)
    }
    msr_words: dict[int, int] = {}  # T-023: the plain MSRs the plan reads (0xCE, the turbo tables), as the simulator holds them
    point_words = {  # T-025: the V/F point words, as the simulator holds them (a peek, not a mailbox request)
        (pt.cpu, pt.point): sim.vf(pt.cpu, 0, pt.point).to_u32() for pt in resolve_point_targets(profile, topology)
    }
    for step in read_steps(resolve_targets(profile, topology), policy, profile, topology):
        if step.kind is StepKind.READ_MSR and step.msr is not None:
            try:
                msr_words[step.msr] = sim.plain_msr(step.cpu, step.msr)
            except ValueError:
                pass  # not modelled: the plan then has no baseline for it (a turbo table of the profile is a PlanError)
    return build_plan(
        profile,
        topology,
        baseline,
        per_core_enabled=sim.per_core_enabled,
        misc_word=sim.misc_global_config,
        policy=policy,
        msr_words=msr_words,
        point_words=point_words,
    )


def _exit_code_of(report: ApplyReport) -> int:
    if report.stop_kind is StopKind.NONE:
        return EXIT_OK
    return EXIT_REFUSED if report.stop_kind is StopKind.REFUSED else EXIT_HARDWARE


# --------------------------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------------------------
def _cmd_topology(ctx: _Ctx, args: argparse.Namespace) -> int:
    topo = _topology_for(ctx, True)
    if args.json:
        rows = [
            {
                "xtu_name": topo.xtu_name(c),
                "kind": c.kind,
                "index": c.index,
                "cpu": c.cpu,
                "cluster": c.cluster,
                "favored": c.favored,
            }
            for c in topo.cores
        ]
        ctx.say(json.dumps(rows, indent=2))
        return EXIT_OK
    ctx.say(f"{'XTU name':<10} {'Linux CPU':>9} {'Cluster':>8} {'Favored':>8}")
    for c in topo.cores:
        cluster = "-" if c.cluster is None else str(c.cluster)
        ctx.say(f"{topo.xtu_name(c):<10} {c.cpu:>9} {cluster:>8} {'yes' if c.favored else '':>8}")
    return EXIT_OK


def _cmd_plan(ctx: _Ctx, args: argparse.Namespace) -> int:
    if not args.sim:
        ctx.fail(
            EXIT_USAGE,
            "plan needs --sim: a dry run takes its baseline from the simulator; a real baseline needs guarded mailbox reads (use apply).",
        )
    profile = _load_profile_or_exit(ctx, args.profile)
    topology = arl_s_285k()
    policy = _load_policy_or_exit(ctx, args.policy, topology)
    try:
        plan = _sim_plan(ctx, ctx.sim_factory(), profile, topology, policy)
    except (ProfileError, PlanError) as exc:
        ctx.fail(EXIT_USAGE, f"plan error: {exc}")
    ctx.say(format_plan(plan))
    ctx.say("Dry run against the simulator's state: nothing was sent.")
    return EXIT_OK if plan.allowed else EXIT_REFUSED


def _cmd_apply(ctx: _Ctx, args: argparse.Namespace) -> int:
    device = bool(args.device)
    profile = _load_profile_or_exit(ctx, args.profile)
    topology = _topology_for(ctx, device)
    policy = _load_policy_or_exit(ctx, args.policy, topology)
    plan_policy = policy  # what the profile may spend: --device-win keeps the identity reads' share of the session / boot budget
    extra_ops = 0
    if ctx.win:
        _identity_policy_check(ctx, policy)
        plan_policy = dataclasses.replace(policy, max_operations_per_session=policy.max_operations_per_session - _identity_ops(ctx))
        extra_ops = _identity_ops(ctx)
    try:
        preflight(profile, topology, plan_policy, boot_ops_used=_boot_ops_used(ctx, args.audit) + extra_ops)
    except PreflightError as exc:
        ctx.refuse(f"{exc}")
    except (ProfileError, PlanError) as exc:
        ctx.fail(EXIT_USAGE, f"plan error: {exc}")
    _require_env_flag(ctx)
    sim: MsrPort | None = None
    try:
        if device:
            targets = resolve_targets(profile, topology)
            ctx.say(format_preview(profile.name, targets, policy, profile.turbo, resolve_point_targets(profile, topology)))
        else:
            sim = ctx.sim_factory()
            plan = _sim_plan(ctx, sim, profile, topology, policy)
            ctx.say(format_plan(plan))
            if not plan.allowed:
                ctx.refuse("the plan would not be allowed (see above). Nothing was sent.")
    except (ProfileError, PlanError) as exc:
        ctx.fail(EXIT_USAGE, f"plan error: {exc}")
    token, typed = _confirm(ctx, "About to apply the plan above through the write guard." + (f"\n{_identity_note(ctx)}" if ctx.win else ""))
    audit = _open_audit(ctx, args.audit)
    gate = _health_gate(ctx, args.allow_taint_bit, device)
    port = _device_port(ctx) if device else sim  # the only place a real port is constructed; every refusal is above
    assert port is not None
    try:
        try:
            guard, clock, sleep = _build_guard(ctx, port, policy, audit, token, gate, args.lock, device)
        except (ValueError, TypeError) as exc:
            ctx.fail(EXIT_USAGE, f"cannot build the guard: {exc}")
        confirm = Confirmation(typed, _operator(ctx, args.operator))
        if ctx.win:
            _identity_check(ctx, guard, confirm, sleep, policy.min_dwell_s, audit.path, port)
        try:
            report = apply_profile(
                profile,
                guard=guard,
                topology=topology,
                confirm=confirm,
                policy=plan_policy,
                sleep=sleep,
                clock=clock,
                progress=ctx.say,
            )
        except PreflightError as exc:
            partial = exc.report
            if partial is not None:
                ctx.say(partial.format_report())
            ctx.refuse(f"{exc}")
        except ControlsError as exc:
            ctx.fail(EXIT_USAGE, f"plan error: {exc}")
        except KeyboardInterrupt:
            # Ctrl-C between two operations (e.g. during a dwell wait): say what was already sent, never continue or auto-revert.
            ctx.warn(f"interrupted: the run was stopped; requests already sent are in the audit log ({audit.path}).")
            revert = guard.revert_plan()
            ctx.warn("\n".join(format_revert_plan(revert) if revert else ["No write had been executed, so there is nothing to revert."]))
            raise _Done(EXIT_HARDWARE) from None
    finally:
        _close(port)
    ctx.say(report.format_report())
    return _exit_code_of(report)


def _cmd_read_vf(ctx: _Ctx, args: argparse.Namespace) -> int:
    device = bool(args.device)
    cpu: int = args.cpu
    topology = _topology_for(ctx, device)
    policy = _load_policy_or_exit(ctx, args.policy, topology)
    policy = dataclasses.replace(policy, max_operations_per_session=1)  # exactly one request; a policy can only lower it
    try:
        core = topology.by_cpu(cpu)
    except UnknownCore:
        ctx.refuse(f"cpu {cpu} is not a CPU of this system (valid: 0..{max(c.cpu for c in topology.cores)}). Nothing was sent.")
    if ctx.win:
        ctx.warn(_NO_IDENTITY_NOTE)
    req = Request(cpu, int(Cmd.VOLTAGE_FREQ_READ), 0, 0, 0, f"read-vf: single V/F read of {topology.xtu_name(core)} (T-007 instrument)")
    verdict = predict(policy, req, None)
    if not verdict.allowed:
        ctx.refuse(f"{verdict.reason}: {verdict.detail}. Nothing was sent.")
    _require_env_flag(ctx)
    where = ("the ThrottleStop driver (Windows)" if ctx.win else "the /dev/cpu device") if device else "the simulator (no hardware)"
    token, typed = _confirm(
        ctx,
        f"read-vf: exactly ONE guard-mediated mailbox request on {where}: cpu {cpu} ({topology.xtu_name(core)}), "
        "cmd 0x10 P1=0 P2=0.\nA mailbox read is a WRMSR to MSR 0x150 (hardware-write tier, docs/SAFETY.md).",
    )
    audit = _open_audit(ctx, args.audit)
    gate = _health_gate(ctx, args.allow_taint_bit, device)
    port = _device_port(ctx) if device else ctx.sim_factory()  # every refusal is above
    try:
        try:
            guard, _, _ = _build_guard(ctx, port, policy, audit, token, gate, args.lock, device)
        except (ValueError, TypeError) as exc:
            ctx.fail(EXIT_USAGE, f"cannot build the guard: {exc}")
        try:
            result = guard.execute(req, Confirmation(typed, _operator(ctx, args.operator)))
        except GuardRefused as exc:
            ctx.fail(EXIT_HARDWARE if exc.reason is Refusal.TRIPPED else EXIT_REFUSED, _refusal_text(exc, audit.path))
        except Exception as exc:  # MailboxTimeout / MsrError: the guard has audited it and tripped
            sent = getattr(exc, "write_sent", None)
            extra = "" if sent is None else f" (request written: {sent})"
            ctx.fail(EXIT_HARDWARE, f"hardware error: {type(exc).__name__}: {exc}{extra}")
    finally:
        _close(port)
    if result.response.rc != 0:
        ctx.fail(EXIT_HARDWARE, f"mailbox completion code {result.response.rc} ({rc_name(result.response.rc)})")
    word = VfWord.from_u32(result.response.data)
    ctx.say(f"read-vf cpu {cpu} ({topology.xtu_name(core)}){'' if device else '  [simulator: no hardware was accessed]'}")
    ctx.say(f"  ratio    : {word.ratio}x")
    ctx.say(f"  V target : {word.v_target_v:.3f} V (raw {word.v_target_raw:#05x})")
    ctx.say(f"  mode     : {'static (override)' if word.static else 'adaptive'}")
    ctx.say(f"  offset   : {word.offset_mv:+.2f} mV (raw {word.offset_raw})")
    ctx.say(f"  raw word : {result.response.data:#010x}   ({describe_word(word)})")
    if guard.tripped:  # e.g. the after-gate saw a new taint bit: the word is still reported, but the session did not end clean
        ctx.fail(EXIT_HARDWARE, f"the guard tripped after the request: {guard.trip_reason} (audit: {audit.path})")
    return EXIT_OK


def _decode_read(cmd: int, word: int) -> list[str]:
    """Decode lines for a raw read word; empty where the layout is not known."""
    if cmd in (int(Cmd.VOLTAGE_FREQ_READ), int(Cmd.VOLTAGE_FLOOR_READ)):
        vf = VfWord.from_u32(word)
        return [
            f"  ratio    : {vf.ratio}x",
            f"  V target : {vf.v_target_v:.3f} V (raw {vf.v_target_raw:#05x})",
            f"  mode     : {'static (override)' if vf.static else 'adaptive'}",
            f"  offset   : {vf.offset_mv:+.2f} mV (raw {vf.offset_raw})",
            f"  word     : {describe_word(vf)}",
        ]
    if cmd == int(Cmd.FAVORED_CORE_READ):
        return [f"  favored ratio [7:0]  : {word & 0xFF}x", f"  fused/limit [15:8]   : {(word >> 8) & 0xFF}  (meaning partly unverified)"]
    if cmd == int(Cmd.GRANULAR_RATIO_READ):
        return [f"  granular index [2:0] : {word & 0x7}", f"  bins [15:8]          : {(word >> 8) & 0xFF}"]
    if cmd == int(Cmd.MISC_GLOBAL_CONFIG_READ):
        return [f"  per-core enable [3]  : {(word >> 3) & 1}"]
    return []


def _cmd_read(ctx: _Ctx, args: argparse.Namespace) -> int:
    device = bool(args.device)
    cpu: int = args.cpu
    cmd: int = args.cmd
    p1: int = args.p1
    p2: int = args.p2
    topology = _topology_for(ctx, device)
    policy = _load_policy_or_exit(ctx, args.policy, topology)
    policy = dataclasses.replace(policy, max_operations_per_session=1)  # exactly one request; a policy can only lower it
    try:
        core = topology.by_cpu(cpu)
    except UnknownCore:
        ctx.refuse(f"cpu {cpu} is not a CPU of this system (valid: 0..{max(c.cpu for c in topology.cores)}). Nothing was sent.")
    if ctx.win:
        ctx.warn(_NO_IDENTITY_NOTE)
    shape = f"cmd {cmd:#04x} P1={p1:#x} P2={p2:#x}"
    entry = policy.match(cmd, p1, p2)
    if entry is None:
        ctx.refuse(f"no allow entry of the policy matches {shape} (default deny). Nothing was sent.")
    if entry.kind != "read":  # `read` can never send a write command, whatever the policy says
        ctx.refuse(f"{shape} matches allow entry {entry.name!r} of kind {entry.kind!r}; `read` only sends kind = \"read\" entries. Nothing was sent.")
    req = Request(cpu, cmd, p1, p2, 0, f"read: single {shape} read of {topology.xtu_name(core)}")
    verdict = predict(policy, req, None)
    if not verdict.allowed:
        ctx.refuse(f"{verdict.reason}: {verdict.detail}. Nothing was sent.")
    _require_env_flag(ctx)
    where = ("the ThrottleStop driver (Windows)" if ctx.win else "the /dev/cpu device") if device else "the simulator (no hardware)"
    token, typed = _confirm(
        ctx,
        f"read: exactly ONE guard-mediated mailbox request on {where}: cpu {cpu} ({topology.xtu_name(core)}), "
        f"{shape} (allow entry {entry.name!r}).\nA mailbox read is a WRMSR to MSR 0x150 (hardware-write tier, docs/SAFETY.md).",
    )
    audit = _open_audit(ctx, args.audit)
    gate = _health_gate(ctx, args.allow_taint_bit, device)
    port = _device_port(ctx) if device else ctx.sim_factory()  # every refusal is above
    try:
        try:
            guard, _, _ = _build_guard(ctx, port, policy, audit, token, gate, args.lock, device)
        except (ValueError, TypeError) as exc:
            ctx.fail(EXIT_USAGE, f"cannot build the guard: {exc}")
        try:
            result = guard.execute(req, Confirmation(typed, _operator(ctx, args.operator)))
        except GuardRefused as exc:
            ctx.fail(EXIT_HARDWARE if exc.reason is Refusal.TRIPPED else EXIT_REFUSED, _refusal_text(exc, audit.path))
        except Exception as exc:  # MailboxTimeout / MsrError: the guard has audited it and tripped
            sent = getattr(exc, "write_sent", None)
            extra = "" if sent is None else f" (request written: {sent})"
            ctx.fail(EXIT_HARDWARE, f"hardware error: {type(exc).__name__}: {exc}{extra}")
    finally:
        _close(port)
    if result.response.rc != 0:
        ctx.fail(EXIT_HARDWARE, f"mailbox completion code {result.response.rc} ({rc_name(result.response.rc)})")
    word = result.response.data
    ctx.say(f"read cpu {cpu} ({topology.xtu_name(core)}) {shape}{'' if device else '  [simulator: no hardware was accessed]'}")
    for line in _decode_read(cmd, word):
        ctx.say(line)
    ctx.say(f"  raw word : {word:#010x}")
    if guard.tripped:  # the word is still reported, but the session did not end clean
        ctx.fail(EXIT_HARDWARE, f"the guard tripped after the request: {guard.trip_reason} (audit: {audit.path})")
    return EXIT_OK


def _cmd_point_offset(ctx: _Ctx, args: argparse.Namespace) -> int:
    """T-028: read ``0x10 P1/P2`` (baseline), check ``--expect-ratio``, write ``0x11`` with only ``[31:21]`` changed (the guard reads it back). Two operations."""
    device = bool(args.device)
    cpu: int = args.cpu
    p1: int = args.p1
    p2: int = args.p2
    topology = _topology_for(ctx, device)
    policy = _load_policy_or_exit(ctx, args.policy, topology)
    if ctx.win:
        _identity_policy_check(ctx, policy)
    ops = 2 + _identity_ops(ctx)
    policy = dataclasses.replace(policy, max_operations_per_session=min(policy.max_operations_per_session, ops))  # [identity reads +] read + write; a policy can only lower it
    try:
        core = topology.by_cpu(cpu)
    except UnknownCore:
        ctx.refuse(f"cpu {cpu} is not a CPU of this system (valid: 0..{max(c.cpu for c in topology.cores)}). Nothing was sent.")
    if p1 not in (0, 2) or not 1 <= p2 <= POINT_P2_MAX:
        ctx.refuse(f"P1 {p1} / P2 {p2}: point-offset needs P1 0 or 2 and P2 1..{POINT_P2_MAX}. Nothing was sent.")
    try:
        offset_raw = VfWord.mv_to_offset_raw(args.offset_mv)
    except ValueError as exc:
        ctx.fail(EXIT_USAGE, f"bad --offset-mv {args.offset_mv!r}: {exc}")
    shape = f"P1={p1} P2={p2}"
    read_entry = policy.match(int(Cmd.VOLTAGE_FREQ_READ), p1, p2)
    if read_entry is None or read_entry.kind != "read":
        ctx.refuse(f"no read allow entry of the policy matches cmd 0x10 {shape} (default deny). Nothing was sent.")
    write_entry = policy.match(int(Cmd.VOLTAGE_FREQ_WRITE), p1, p2)
    if write_entry is None or write_entry.kind != "write" or write_entry.check != "vf_point_offset_bounded":
        ctx.refuse(f"no vf_point_offset_bounded write entry of the policy matches cmd 0x11 {shape} (default deny). Nothing was sent.")
    read_req = Request(cpu, int(Cmd.VOLTAGE_FREQ_READ), p1, p2, 0, f"point-offset: baseline read of {shape} on {topology.xtu_name(core)}")
    verdict = predict(policy, read_req, None)
    if not verdict.allowed:
        ctx.refuse(f"{verdict.reason}: {verdict.detail}. Nothing was sent.")
    _require_env_flag(ctx)
    where = ("the ThrottleStop driver (Windows)" if ctx.win else "the /dev/cpu device") if device else "the simulator (no hardware)"
    token, typed = _confirm(
        ctx,
        f"point-offset: ONE guarded session on {where}: cpu {cpu} ({topology.xtu_name(core)}), V/F point {shape}: read cmd 0x10, then write cmd 0x11 "
        f"with only the offset field set to {args.offset_mv:+g} mV (raw {offset_raw}), then a read-back (2 operations).\n"
        "A mailbox read is a WRMSR to MSR 0x150 (hardware-write tier, docs/SAFETY.md)." + (f"\n{_identity_note(ctx)}" if ctx.win else ""),
    )
    audit = _open_audit(ctx, args.audit)
    gate = _health_gate(ctx, args.allow_taint_bit, device)
    port = _device_port(ctx) if device else ctx.sim_factory()  # every refusal is above
    try:
        try:
            guard, _, sleep = _build_guard(ctx, port, policy, audit, token, gate, args.lock, device)
        except (ValueError, TypeError) as exc:
            ctx.fail(EXIT_USAGE, f"cannot build the guard: {exc}")
        confirm = Confirmation(typed, _operator(ctx, args.operator))

        def send(req: Request):
            try:
                return guard.execute(req, confirm)
            except GuardRefused as exc:
                ctx.fail(EXIT_HARDWARE if exc.reason is Refusal.TRIPPED else EXIT_REFUSED, _refusal_text(exc, audit.path))
            except Exception as exc:  # MailboxTimeout / MsrError: the guard has audited it and tripped
                sent = getattr(exc, "write_sent", None)
                extra = "" if sent is None else f" (request written: {sent})"
                ctx.fail(EXIT_HARDWARE, f"hardware error: {type(exc).__name__}: {exc}{extra}")

        if ctx.win:
            _identity_check(ctx, guard, confirm, sleep, policy.min_dwell_s, audit.path, port)
        before = send(read_req)
        if before.response.rc != 0:
            ctx.fail(EXIT_HARDWARE, f"baseline read: mailbox completion code {before.response.rc} ({rc_name(before.response.rc)}). Nothing was written.")
        base_word = before.response.data
        base = VfWord.from_u32(base_word)
        if args.expect_ratio is not None and base.ratio != args.expect_ratio:
            ctx.refuse(f"point {shape} reads ratio {base.ratio}x, not the expected {args.expect_ratio}x. Nothing was written.")
        new_word = (base_word & 0x1FFFFF) | ((offset_raw & 0x7FF) << 21)  # only [31:21] changes
        if policy.min_dwell_s > 0:
            sleep(policy.min_dwell_s)  # the guard never waits by itself; the dwell between its two operations is ours to honour
        after = send(Request(cpu, int(Cmd.VOLTAGE_FREQ_WRITE), p1, p2, new_word, f"point-offset: offset of {shape} on {topology.xtu_name(core)} to {args.offset_mv:+g} mV"))
        revert = guard.revert_plan()
    finally:
        _close(port)
    tag = "" if device else "  [simulator: no hardware was accessed]"
    ctx.say(f"point-offset cpu {cpu} ({topology.xtu_name(core)}) cmd 0x11 {shape}{tag}")
    ctx.say(f"  before : {base_word:#010x}   ratio {base.ratio}x, offset {base.offset_mv:+.2f} mV (raw {base.offset_raw})")
    if after.response.rc != 0:
        ctx.fail(EXIT_HARDWARE, f"write: mailbox completion code {after.response.rc} ({rc_name(after.response.rc)}); the point was not changed (revert word {base_word:#010x})")
    word = VfWord.from_u32(new_word)
    ctx.say(f"  after  : {new_word:#010x}   ratio {word.ratio}x, offset {word.offset_mv:+.2f} mV (raw {word.offset_raw})")
    if after.readback is not None:
        ctx.say(f"  readback: {after.readback.data:#010x}   verified: {after.verified}")
    undo = [r for r in revert if isinstance(r, Request) and (r.cpu, int(r.cmd), r.p1, r.p2) == (cpu, int(Cmd.VOLTAGE_FREQ_WRITE), p1, p2)]
    ctx.say(f"  revert : cmd 0x11 {shape} data {(undo[-1].data if undo else base_word):#010x} (a reboot also restores the BIOS value)")
    if guard.tripped or after.verified is not True:
        ctx.fail(EXIT_HARDWARE, f"the guard tripped: {guard.trip_reason or 'read-back did not verify'} (audit: {audit.path})")
    return EXIT_OK


def _default_measure_io() -> vfdump.MeasureIO:
    """The only place real cpufreq sysfs / stress-ng I/O is built (``--device --measure``). Looked up at call time."""
    return vfdump.SysfsMeasureIO()


def _cmd_vf_dump(ctx: _Ctx, args: argparse.Namespace) -> int:
    """T-026: one guarded session, one CONFIRM, every read budgeted before anything is sent. Never sends a write command."""
    device = bool(args.device)
    measure = bool(args.measure)
    topology = _topology_for(ctx, device)
    policy = _load_policy_or_exit(ctx, args.policy, topology)
    try:
        cores = vfdump.select_cpus(topology, args.cpus, bool(args.all_e))
    except UnknownCore as exc:
        ctx.refuse(f"{exc} (valid CPUs: 0..{max(c.cpu for c in topology.cores)}). Nothing was sent.")
    except ValueError as exc:
        ctx.fail(EXIT_USAGE, f"--cpus: {exc}")
    if not cores:
        ctx.fail(EXIT_USAGE, "--cpus: no CPU selected")
    if args.cpus is not None and args.all_e:
        ctx.warn("note: --all-e is ignored because --cpus was given.")
    if args.json is not None and not Path(args.json).resolve().parent.is_dir():
        ctx.fail(EXIT_USAGE, f"--json: the directory of {args.json} does not exist")
    if measure and ctx.win and ctx.measure_io_factory is None:
        ctx.refuse("--measure needs Linux cpufreq and stress-ng; it is not available with --device-win. Nothing was sent.")
    if measure and not device and ctx.measure_io_factory is None:
        ctx.refuse("--measure needs --device: the simulator has no cpufreq and no MSR 0x198 (and this program never writes a real cpufreq file in a simulator run). Nothing was sent.")
    # ---- everything that can be decided before the first request: allowlist, read-only entries, budget ----
    cap = vfdump.walk_cap(policy)
    requests = vfdump.plan_requests(cores, topology, cap)
    for req in requests:
        shape = f"cmd {req.cmd:#04x} P1={req.p1:#x} P2={req.p2:#x}"
        entry = policy.match(req.cmd, req.p1, req.p2)
        if entry is None:
            ctx.refuse(f"no allow entry of the policy matches {shape} (cpu {req.cpu}; default deny). Nothing was sent.")
        if entry.kind != "read":  # a dump can never send a write command, whatever the policy says
            ctx.refuse(f"{shape} matches allow entry {entry.name!r} of kind {entry.kind!r}; vf-dump only uses kind = \"read\" entries. Nothing was sent.")
        verdict = predict(policy, req, None)
        if not verdict.allowed:
            ctx.refuse(f"{verdict.reason}: {verdict.detail} (cpu {req.cpu}, {shape}). Nothing was sent.")
    if ctx.win:
        _identity_policy_check(ctx, policy)
    needed = len(requests) + _identity_ops(ctx)  # --device-win: + the identity reads
    # The guard refuses a plain rdmsr (SESSION_LIMIT / BOOT_LIMIT) once the caps are *full*, although a plain read does not count: --measure needs one spare.
    spare = 1 if measure else 0
    why = " (+1 spare: the guard refuses even a plain 0x198 read once the cap is full)" if measure else ""
    if needed + spare > policy.max_operations_per_session:
        ctx.refuse(
            f"this dump needs up to {needed} mailbox reads{why} (every read is a WRMSR) but the policy allows {policy.max_operations_per_session} per session: raise "
            "max_operations_per_session in a reviewed policy, or dump fewer CPUs (--cpus). Nothing was sent."
        )
    left = max(policy.max_operations_per_boot - _boot_ops_used(ctx, args.audit), 0)
    if needed + spare > left:
        ctx.refuse(
            f"this dump needs up to {needed} mailbox reads{why} but only {left} of the policy's {policy.max_operations_per_boot} operations per boot are left "
            "(the audit log counts every process): raise max_operations_per_boot in a reviewed policy, reboot, or dump fewer CPUs. Nothing was sent."
        )
    shells = [vfdump.CpuCurve(c.cpu, c.kind, c.cluster, topology.xtu_name(c)) for c in cores]
    reps = [u.curves[0].cpu for u in vfdump.units_of(shells, topology)]
    if measure:
        for cpu in reps:
            verdict = predict_msr(policy, MsrRequest(cpu, vfdump.MSR_PERF_STATUS, 0, "vf-dump: preflight", kind="read"), None)
            if not verdict.allowed:
                ctx.refuse(
                    f"--measure reads MSR {vfdump.MSR_PERF_STATUS:#x} through the guard, and the policy does not allow it for cpu {cpu}: {verdict.reason}: "
                    f"{verdict.detail}. Add a kind = \"read\" msr_allow entry for {vfdump.MSR_PERF_STATUS:#x}. Nothing was sent."
                )
    _require_env_flag(ctx)
    measure_io: vfdump.MeasureIO | None = None
    if measure:
        measure_io = ctx.measure_io_factory() if ctx.measure_io_factory is not None else _default_measure_io()
        problem = measure_io.check()
        if problem is not None:
            ctx.refuse(f"{problem}. Nothing was sent.")
        try:  # read-only: every CPU we would cap must have a readable scaling_max_freq
            for unit in vfdump.units_of(shells, topology):
                for cpu in unit.cluster_cpus:
                    measure_io.read_cap(cpu)
        except (OSError, ValueError) as exc:
            ctx.refuse(f"cannot read cpufreq scaling_max_freq for --measure: {exc}. Nothing was sent.")
    where = ("the ThrottleStop driver (Windows)" if ctx.win else "the /dev/cpu device") if device else "the simulator (no hardware)"
    extra = ""
    if measure:
        extra = (
            f"\n--measure: plain rdmsr of MSR {vfdump.MSR_PERF_STATUS:#x} on CPUs {', '.join(map(str, reps))} (audited, not WRMSRs), and, per P-core / E-cluster, "
            "temporary cpufreq scaling_max_freq caps plus a stress-ng load on those CPUs (the original caps are restored afterwards)."
        )
    token, typed = _confirm(
        ctx,
        f"vf-dump: up to {needed} guard-mediated mailbox READ requests on {where}, CPUs {', '.join(str(c.cpu) for c in cores)} (cmd 0x10 P1=0, P2 = 0 and each point until the first rc 0x11), "
        f"one at a time, {policy.min_dwell_s:g} s apart; normally one per point of each CPU (the budget is the worst case: P2 <= {cap}, as far as the policy allows).\nA mailbox read is a WRMSR to MSR 0x150 (hardware-write tier, docs/SAFETY.md). No write command is sent.{extra}" + (f"\n{_identity_note(ctx)}" if ctx.win else ""),
    )
    audit = _open_audit(ctx, args.audit)
    gate = _health_gate(ctx, args.allow_taint_bit, device)
    port = _device_port(ctx) if device else ctx.sim_factory()  # every refusal is above
    policy = dataclasses.replace(policy, max_operations_per_session=needed + spare)  # exactly the budget (+ the spare a --measure needs); a policy can only lower it
    curves: list[vfdump.CpuCurve] = []
    stop: vfdump.Stop | None = None
    measured: dict[str, list[vfdump.RatioMeasurement]] | None = None
    problems: list[str] = []
    try:
        try:
            guard, clock, sleep = _build_guard(ctx, port, policy, audit, token, gate, args.lock, device)
        except (ValueError, TypeError) as exc:
            ctx.fail(EXIT_USAGE, f"cannot build the guard: {exc}")
        confirm = Confirmation(typed, _operator(ctx, args.operator))
        if ctx.win:
            _identity_check(ctx, guard, confirm, sleep, policy.min_dwell_s, audit.path, port)
        pacer = vfdump.Pacer(policy.min_dwell_s, clock, sleep)
        curves, stop = vfdump.walk(cores, topology, lambda req: guard.execute(req, confirm), pacer, lambda exc: _refusal_text(exc, audit.path), cap)
        units = vfdump.units_of(curves, topology)
        if stop is None and measure_io is not None:
            measured = {}

            def read_perf(cpu: int) -> int:
                pacer.wait()  # the first plain read after the last mailbox read must also respect the dwell (the guard checks it for MsrRequests too)
                res = guard.execute(MsrRequest(cpu, vfdump.MSR_PERF_STATUS, 0, f"vf-dump: measure requested VID (MSR 0x198) on cpu {cpu}", kind="read"), confirm)
                return res.value

            try:
                for group in vfdump.measure_groups(units):
                    measured[group.name] = vfdump.measure_group(measure_io, group, read_perf, sleep, problems, progress=ctx.say)
            except GuardRefused as exc:
                stop = vfdump.Stop(3 if exc.reason is Refusal.TRIPPED else 2, _refusal_text(exc, audit.path))
            except KeyboardInterrupt:
                stop = vfdump.Stop(3, "interrupted: the measurement was stopped (the cpufreq caps were restored).")
            except Exception as exc:  # MsrError / OSError (sysfs) / ...: partial results are still printed
                stop = vfdump.Stop(3, f"measurement error: {type(exc).__name__}: {exc}")
        if stop is None and guard.tripped:  # e.g. the after-gate saw a new taint bit: the data is still reported, but the session did not end clean
            stop = vfdump.Stop(3, f"the guard tripped: {guard.trip_reason} (audit: {audit.path})")
    finally:
        _close(port)
    flags = vfdump.find_flags(units)
    sent = sum(1 + len(c.points) + (1 if c.end.startswith("rc 0x11") else 0) for c in curves)
    ctx.say(f"vf-dump{'' if device else '  [simulator: no hardware was accessed]'}: {sent} mailbox reads, {sum(1 + len(c.points) for c in curves)} words, {len(curves)} CPU(s)")
    for line in vfdump.format_dump(units, flags, measured, stop):
        ctx.say(line)
    for text in problems:
        ctx.warn(f"WARNING: {text}")
    if args.json is not None:
        try:
            Path(args.json).write_text(json.dumps(vfdump.to_json(units, flags, measured, stop, sim=not device, problems=problems), indent=2) + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            ctx.warn(f"cannot write {args.json}: {exc}")
            return EXIT_HARDWARE if stop is None else stop.code
    if stop is not None:
        ctx.warn(stop.text)
        return stop.code
    return EXIT_HARDWARE if problems else EXIT_OK


_COMMANDS: dict[str, Callable[[_Ctx, argparse.Namespace], int]] = {
    "topology": _cmd_topology,
    "plan": _cmd_plan,
    "apply": _cmd_apply,
    "read-vf": _cmd_read_vf,
    "read": _cmd_read,
    "vf-dump": _cmd_vf_dump,
    "point-offset": _cmd_point_offset,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], object] | None = None,
    wall_clock: Callable[[], float] = time.time,
    boot_id_fn: Callable[[], str | None] = read_boot_id,
    confirm_fn: Callable[[str], str] | None = None,
    token_fn: Callable[[], str] | None = None,
    health_gate: HealthGate | None = None,
    health_gate_factory: Callable[[frozenset[int]], HealthGate | None] | None = None,
    device_health_gate_factory: Callable[[frozenset[int]], HealthGate | None] | None = None,
    sim_factory: Callable[[], MsrPort] | None = None,
    device_port_factory: Callable[[], MsrPort] | None = None,
    detect_fn: Callable[[], Topology] | None = None,
    measure_io_factory: Callable[[], vfdump.MeasureIO] | None = None,
    win_port_factory: Callable[[], MsrPort] | None = None,
    win_health_gate_factory: Callable[[frozenset[int]], HealthGate | None] | None = None,
    win_detect_fn: Callable[[], Topology] | None = None,
    lstat_fn: Callable[[str], Any] | None = None,
) -> int:
    """Run the CLI and return the exit code (0 ok, 1 usage, 2 refused, 3 hardware/guard trouble). Nothing is read from
    ``sys``/``os`` when the matching parameter is given.

    ``boot_id_fn`` supplies the boot id the guard records and counts per boot (default: ``/proc/sys/kernel/random/boot_id``).
    ``confirm_fn(prompt) -> line`` replaces reading the confirmation from ``stdin``; ``token_fn`` supplies the session token
    (default: a random one). ``health_gate`` is used as is; else ``health_gate_factory(allowed_taint_bits)`` builds it from the
    ``--allow-taint-bit`` values (wired after T-015). ``clock`` / ``sleep`` default to real time for ``--device`` and to a
    virtual clock for ``--sim``. ``sim_factory`` / ``device_port_factory`` / ``detect_fn`` build the MSR port(s) and validate the
    machine; for ``--device-win`` the ``win_*`` counterparts (ThrottleStop port, WHEA health gate, LP-map check; ``boot_id_fn`` defaults to the Windows boot id). ``measure_io_factory`` (T-026, ``vf-dump --measure``) builds the cpufreq / load-generator I/O; unset, only ``--device`` gets the real one.
    ``lstat_fn`` (default ``os.lstat``) feeds the D-030 installed-detection that picks the default lock / audit paths (tests inject fake stat results).
    """
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    ctx = _Ctx(
        env=os.environ if env is None else env,
        stdin=sys.stdin if stdin is None else stdin,
        out=out,
        err=err,
        clock=clock,
        sleep=sleep,
        wall_clock=wall_clock,
        boot_id_fn=boot_id_fn,
        confirm_fn=confirm_fn,
        token_fn=token_fn if token_fn is not None else (lambda: secrets.token_hex(4)),
        health_gate=health_gate,
        health_gate_factory=health_gate_factory,
        device_health_gate_factory=(
            device_health_gate_factory if device_health_gate_factory is not None else _default_device_health_gate_factory
        ),
        sim_factory=sim_factory if sim_factory is not None else _default_sim_port,
        device_port_factory=device_port_factory if device_port_factory is not None else _default_device_port,
        detect_fn=detect_fn if detect_fn is not None else detect,
        measure_io_factory=measure_io_factory,
        **{k: v for k, v in (("win_port_factory", win_port_factory), ("win_health_gate_factory", win_health_gate_factory), ("win_detect_fn", win_detect_fn), ("lstat_fn", lstat_fn)) if v is not None},
    )
    parser = _build_parser(out, err)
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except _ArgparseExit as done:
        return done.status
    except _UsageError as exc:
        parser.print_usage()
        err.write(f"error: {exc}\n")
        return EXIT_USAGE
    if getattr(args, "fingerprint", None) is not None and not getattr(args, "device_win", False):
        parser.print_usage()
        err.write("error: --fingerprint only applies with --device-win\n")
        return EXIT_USAGE
    ctx.fingerprint_path = getattr(args, "fingerprint", None)
    if getattr(args, "device_win", False):
        args.device = True  # every command treats it as --device; ctx.win swaps the port, gate, topology check and boot id
        ctx.win = True
        if boot_id_fn is read_boot_id:
            ctx.boot_id_fn = windows_boot_id
        if args.allow_taint_bit:
            err.write("note: --allow-taint-bit is a Linux kernel-taint notion and is ignored with --device-win.\n")
    ctx.hardware = bool(getattr(args, "device", False))
    try:
        return _COMMANDS[args.command](ctx, args)
    except _Done as done:
        return done.code
    except Exception as exc:  # fail closed: an unexpected error is never reported as success
        err.write(f"internal error: {type(exc).__name__}: {exc}\n")
        return EXIT_HARDWARE
