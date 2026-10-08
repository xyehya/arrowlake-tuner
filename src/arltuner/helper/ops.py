"""G1 op handlers (read-only): ``preflight``, ``vf.dump``, ``audit.tail``, ``telemetry.start`` / ``telemetry.stop``, ``cancel``.

Handlers are plain functions ``(helper, ctx, args) -> result dict`` that raise :class:`OpError`; the dispatcher in ``core`` owns framing,
state, threads and the one-response rule. Nothing here sends a mailbox request except through ``arltuner.guard.WriteGuard`` (``vf.dump``).
"""
from __future__ import annotations

import dataclasses
import json
import os
import secrets
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arltuner import vfdump
from arltuner.controls import predict
from arltuner.guard import (
    ENV_FLAG,
    AuditLog,
    AuditUnreadable,
    Confirmation,
    GuardRefused,
    PolicyError,
    WriteGuard,
    anchored_wall_clock,
    default_m1_policy,
    load_policy,
    read_state,
)
from arltuner.msr import MailboxTransport
from arltuner.platform.preflight import PreflightReport, Status
from arltuner.topology import TopologyError, UnknownCore

from .protocol import BAD_REQUEST, CANCELLED, INTERNAL, MAX_LINE, PREFLIGHT_FAILED, REFUSED, OpError
from .state import State
from .trust import TrustError, check_lock_file, check_trusted_path, open_checked

if TYPE_CHECKING:
    from .core import Helper

__all__ = ["OpContext", "OpSpec", "builtin_ops", "write_gate"]

AUDIT_TAIL_DEFAULT = 20
AUDIT_TAIL_MAX = 200
_TAIL_READ_BYTES = 1 << 20
_RESULT_SLACK = 2048  # envelope of the response line around the result
TELEMETRY_INTERVAL_RANGE = (0.2, 60.0)


@dataclass
class OpContext:
    """What a handler may use besides its args: its request id, the cancel flag (set by ``cancel``, EOF and the signals) and progress events."""

    req_id: str
    cancel: threading.Event
    emit: Callable[[dict[str, Any]], object]

    def progress(self, step: int, of: int, ops_used: int, ops_budget: int) -> None:
        self.emit({"event": "progress", "op_id": self.req_id, "step": step, "of": of, "ops_used": ops_used, "ops_budget": ops_budget})


@dataclass(frozen=True)
class OpSpec:
    """``long``: runs on the worker thread, one final response, state ``state`` while it runs. ``write``: needs the preflight write gate (D-027 c)."""

    name: str
    handler: Callable[[Helper, OpContext, dict[str, Any]], dict[str, Any]]
    validate: Callable[[dict[str, Any]], dict[str, Any]]
    long: bool = False
    write: bool = False
    state: State | None = None


# --------------------------------------------------------------------------------------------------------------
# Argument validation (unknown keys are BAD_REQUEST: nothing is silently ignored)
# --------------------------------------------------------------------------------------------------------------
def _only(args: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(args) - allowed)
    if extra:
        raise OpError(BAD_REQUEST, f"unknown arg(s): {', '.join(map(repr, extra))}")


def _no_args(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, set())
    return {}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_vf_dump(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"cpus", "all_e"})
    cpus, all_e = args.get("cpus"), args.get("all_e", False)
    if not isinstance(all_e, bool):
        raise OpError(BAD_REQUEST, "'all_e' must be a boolean")
    if isinstance(cpus, list):
        if not cpus or not all(_is_int(c) and c >= 0 for c in cpus):
            raise OpError(BAD_REQUEST, "'cpus' as a list must be non-empty non-negative integers")
        cpus = ",".join(str(c) for c in cpus)
    if cpus is not None:
        if not isinstance(cpus, str) or len(cpus) > 200:
            raise OpError(BAD_REQUEST, "'cpus' must be a CPU list string like \"0-7,16\" or a list of integers")
        try:
            vfdump.parse_cpu_list(cpus)
        except ValueError as exc:
            raise OpError(BAD_REQUEST, f"'cpus': {exc}") from None
    return {"cpus": cpus, "all_e": all_e}


def _validate_audit_tail(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"n"})
    n = args.get("n", AUDIT_TAIL_DEFAULT)
    if not _is_int(n) or not 1 <= n <= AUDIT_TAIL_MAX:
        raise OpError(BAD_REQUEST, f"'n' must be an integer 1..{AUDIT_TAIL_MAX}")
    return {"n": n}


def _validate_telemetry_start(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"interval_s"})
    interval = args.get("interval_s", 1.0)
    lo, hi = TELEMETRY_INTERVAL_RANGE
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not lo <= interval <= hi:
        raise OpError(BAD_REQUEST, f"'interval_s' must be a number {lo}..{hi}")
    return {"interval_s": float(interval)}


def _validate_cancel(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"op_id"})
    op_id = args.get("op_id")
    if op_id is not None and (not isinstance(op_id, str) or not 1 <= len(op_id) <= 64):
        raise OpError(BAD_REQUEST, "'op_id' must be a request id string")
    return {"op_id": op_id}


# --------------------------------------------------------------------------------------------------------------
# preflight (the helper's own run is the authority, D-027 c)
# --------------------------------------------------------------------------------------------------------------
def _run_preflight(helper: Helper) -> PreflightReport:
    try:
        report = helper.cfg.preflight_fn()
    except Exception as exc:  # fail closed
        raise OpError(PREFLIGHT_FAILED, f"preflight could not run: {type(exc).__name__}: {exc}") from None
    if not isinstance(report, PreflightReport):
        raise OpError(PREFLIGHT_FAILED, "preflight returned no report")
    return report


def _writes_allowed(report: PreflightReport) -> bool:
    return report.mode == "write" and not any(r.status is Status.UNKNOWN for r in report.results)


def write_gate(helper: Helper) -> None:
    """Refuse (``PREFLIGHT_FAILED``) unless the helper's own fresh preflight says mode ``write`` with no ``UNKNOWN`` check. Not applied to a simulator."""
    if helper.cfg.simulator:
        return
    report = _run_preflight(helper)
    if not _writes_allowed(report):
        blockers = [{"name": r.name, "status": r.status.value, "reason": r.reason} for r in report.results if r.status in (Status.READ_ONLY, Status.REFUSE, Status.UNKNOWN)]
        raise OpError(
            PREFLIGHT_FAILED,
            f"the helper's preflight does not allow writes (mode {report.mode!r}"
            + (", a check is UNKNOWN" if any(r.status is Status.UNKNOWN for r in report.results) else "")
            + "); nothing was sent",
            {"mode": report.mode, "blockers": blockers},
        )


def op_preflight(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    report = _run_preflight(helper)
    return {**report.to_dict(), "writes_allowed": _writes_allowed(report)}


# --------------------------------------------------------------------------------------------------------------
# audit.tail
# --------------------------------------------------------------------------------------------------------------
def _trust(helper: Helper, path: Path, *, want: str | None, must_exist: bool) -> None:
    if not helper.cfg.enforce_trust:
        return
    try:
        check_trusted_path(path, want=want, must_exist=must_exist, lstat_fn=helper.cfg.lstat_fn)
    except TrustError as exc:
        raise OpError(REFUSED, f"untrusted path (SPEC 4.4): {exc}", {"check": "TRUST_PATH", "path": exc.path, "reason": exc.reason}) from None


def _trust_refusal(exc: TrustError, check: str) -> OpError:
    return OpError(REFUSED, f"untrusted file: {exc}. Nothing was sent.", {"check": check, "path": exc.path, "reason": exc.reason})


def op_audit_tail(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    path = helper.cfg.audit_path
    _trust(helper, path, want="file", must_exist=False)
    try:
        # O_NOFOLLOW + fstat on the opened descriptor (regular file, ours, not writable by others): never open-then-check by name
        fd = open_checked(path, write=False, create=False, euid=helper.cfg.euid_fn(), what="audit log")
    except FileNotFoundError:
        return {"path": str(path), "records": [], "truncated": False}
    except TrustError as exc:
        raise _trust_refusal(exc, "TRUST_PATH") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - _TAIL_READ_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError as exc:
        raise OpError(INTERNAL, f"cannot read the audit log: {exc.strerror or exc}") from None
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line may be cut
    lines = [ln for ln in lines if ln.strip()]
    wanted = lines[-args["n"]:]
    records: list[Any] = []
    for ln in wanted:
        try:
            records.append(json.loads(ln.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            records.append({"unparsable": ln[:200].decode("utf-8", "replace")})
    truncated = len(lines) > len(wanted)
    while records and len(json.dumps(records, separators=(",", ":"))) > MAX_LINE - _RESULT_SLACK:  # the response line has a 64 KiB cap too
        records.pop(0)
        truncated = True
    return {"path": str(path), "records": records, "truncated": truncated}


# --------------------------------------------------------------------------------------------------------------
# vf.dump
# --------------------------------------------------------------------------------------------------------------
class _Cancel(KeyboardInterrupt):
    """Raised between two transactions when the cancel flag is set. ``vfdump.walk`` turns KeyboardInterrupt into a ``Stop`` without losing the curves read."""


class _VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _refuse(text: str, check: str, **detail: Any) -> OpError:
    return OpError(REFUSED, f"{text} Nothing was sent.", {"check": check, **detail})


def op_vf_dump(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """One guarded read session: every request is checked against the policy and budgeted before the first is sent; one at a time, dwell apart.
    Sends no write command whatever the policy says. Returns the ``vfdump.to_json`` document; a stop returns an error carrying it as ``detail.partial``."""
    cfg = helper.cfg
    try:
        topology = cfg.topology_fn()
    except TopologyError as exc:
        raise _refuse(f"this machine does not match the Core Ultra 9 285K map: {exc}.", "TOPOLOGY") from None
    try:
        cores = vfdump.select_cpus(topology, args["cpus"], args["all_e"])
    except UnknownCore as exc:
        raise _refuse(f"{exc} (valid CPUs: 0..{max(c.cpu for c in topology.cores)}).", "UNKNOWN_CORE") from None
    if not cores:
        raise OpError(BAD_REQUEST, "no CPU selected")
    # ---- trusted inputs only (SPEC 4.4) ----
    if cfg.policy_path is not None:
        _trust(helper, cfg.policy_path, want="file", must_exist=True)
    _trust(helper, cfg.audit_path, want="file", must_exist=False)
    try:
        policy = default_m1_policy() if cfg.policy_path is None else load_policy(cfg.policy_path)
    except (PolicyError, OSError) as exc:
        raise _refuse(f"policy error: {exc}.", "POLICY_INVALID") from None
    # ---- everything decidable before the first request: allowlist, read-only entries, budget ----
    requests = vfdump.plan_requests(cores, topology)
    for req in requests:
        shape = f"cmd {req.cmd:#04x} P1={req.p1:#x} P2={req.p2:#x}"
        entry = policy.match(req.cmd, req.p1, req.p2)
        if entry is None:
            raise _refuse(f"no allow entry of the policy matches {shape} (cpu {req.cpu}; default deny).", "NOT_ALLOWLISTED", cpu=req.cpu)
        if entry.kind != "read":
            raise _refuse(f"{shape} matches allow entry {entry.name!r} of kind {entry.kind!r}; vf.dump only uses kind = \"read\" entries.", "NOT_READ_ENTRY", cpu=req.cpu)
        verdict = predict(policy, req, None)
        if not verdict.allowed:
            raise _refuse(f"{verdict.reason}: {verdict.detail} (cpu {req.cpu}, {shape}).", verdict.reason, cpu=req.cpu)
    needed = len(requests)
    if needed > policy.max_operations_per_session:
        raise _refuse(
            f"this dump needs up to {needed} mailbox reads (every read is a WRMSR) but the policy allows {policy.max_operations_per_session} per session.",
            "SESSION_LIMIT", needed=needed, allowed=policy.max_operations_per_session,
        )
    euid = cfg.euid_fn()
    try:
        check_lock_file(cfg.lock_path, euid=euid)  # a foreign / symlinked lock file in a world-writable directory is refused, not followed or taken over
    except TrustError as exc:
        raise _trust_refusal(exc, "LOCK_UNTRUSTED") from None
    try:
        # create-or-validate the audit log with O_NOFOLLOW + fstat before AuditLog (which itself opens O_NOFOLLOW|O_APPEND) ever touches it
        os.close(open_checked(cfg.audit_path, write=True, create=True, mode=0o644, euid=euid, what="audit log"))
    except TrustError as exc:
        raise _trust_refusal(exc, "TRUST_PATH") from None
    except OSError as exc:
        raise _refuse(f"cannot open the audit log {cfg.audit_path}: {exc}.", "AUDIT_UNAVAILABLE") from None
    try:
        state = read_state(cfg.audit_path)
    except (AuditUnreadable, OSError) as exc:
        raise _refuse(f"the audit log {cfg.audit_path} is unavailable, so the per-boot budget cannot be checked: {exc}.", "AUDIT_UNAVAILABLE") from None
    try:
        boot = cfg.boot_id_fn()
    except Exception:
        boot = None
    boot_id = boot.strip() or None if isinstance(boot, str) else None
    used = state.attempts_by_boot.get(boot_id, 0)
    left = max(policy.max_operations_per_boot - used, 0)
    if needed > left:
        raise _refuse(
            f"this dump needs up to {needed} mailbox reads but only {left} of the policy's {policy.max_operations_per_boot} operations per boot are left.",
            "BOOT_LIMIT", needed=needed, left=left,
        )
    # ---- clocks: virtual for the simulator (dwell honoured instantly), real otherwise (cancel wakes the dwell wait) ----
    if cfg.simulator:
        # One virtual clock for the life of the process, so the audit timestamps of consecutive dumps stay consistent. Simulator only: the gap
        # between two operator-started sessions is taken to be at least the dwell (on hardware the guard refuses DWELL instead of waiting).
        if helper.sim_time is None:
            virtual = _VirtualClock()
            helper.sim_time = (virtual, anchored_wall_clock(virtual.clock, cfg.wall_clock))
        virtual, wall = helper.sim_time
        virtual.sleep(policy.min_dwell_s)
        clock, sleep = virtual.clock, virtual.sleep
    else:
        clock, wall = cfg.monotonic, cfg.wall_clock

        def sleep(seconds: float) -> None:
            ctx.cancel.wait(seconds)
    gate = cfg.health_gate_factory()
    if gate is None and not cfg.simulator:
        raise _refuse("no health gate is available (the health bracket is mandatory on hardware).", "NO_HEALTH_GATE")
    policy = dataclasses.replace(policy, max_operations_per_session=needed)  # exactly the budget; a policy can only lower it
    try:
        audit = AuditLog(cfg.audit_path)
    except OSError as exc:
        raise _refuse(f"cannot open the audit log {cfg.audit_path}: {exc}.", "AUDIT_UNAVAILABLE") from None
    port = cfg.port_factory()  # every refusal is above
    refused: list[GuardRefused] = []
    sent = 0
    try:
        token = secrets.token_hex(16)
        guard = WriteGuard(
            MailboxTransport(port, sleep=sleep, clock=clock),
            policy,
            audit,
            session_token=token,
            env={ENV_FLAG: "1"},  # SPEC 4.1: the polkit password replaced the exported flag; never os.environ
            health_gate=gate,
            lock_path=cfg.lock_path,
            clock=clock,
            wall_clock=wall,
            boot_id_fn=cfg.boot_id_fn,
            msr_port=port,
        )
        confirm = Confirmation(token, cfg.operator)

        def execute(req: Any) -> Any:
            nonlocal sent
            if ctx.cancel.is_set():
                raise _Cancel()
            result = guard.execute(req, confirm)
            sent += 1
            ctx.progress(sent, needed, used + sent, policy.max_operations_per_boot)
            return result

        def refusal_text(exc: GuardRefused) -> str:
            refused.append(exc)
            return f"refused by the guard: {exc}"

        pacer = vfdump.Pacer(policy.min_dwell_s, clock, sleep)
        curves, stop = vfdump.walk(cores, topology, execute, pacer, refusal_text)
        tripped = guard.tripped
        trip_reason = guard.trip_reason
    finally:
        close = getattr(port, "close", None)
        if callable(close):
            close()
    units = vfdump.units_of(curves, topology)
    document = vfdump.to_json(units, vfdump.find_flags(units), None, stop, sim=cfg.simulator)
    if ctx.cancel.is_set() and stop is not None:
        raise OpError(CANCELLED, "vf.dump was cancelled between transactions; the requests already sent are in the audit log", {"partial": document, "ops_done": sent})
    if stop is not None:
        if refused:
            detail: dict[str, Any] = {"check": refused[-1].reason.value, "partial": document, "ops_done": sent}
            if refused[-1].retry_after_s is not None:
                detail["retry_after_s"] = refused[-1].retry_after_s
            raise OpError(REFUSED, stop.text, detail)
        raise OpError(INTERNAL, stop.text, {"partial": document, "ops_done": sent})
    if tripped:
        raise OpError(INTERNAL, f"the guard tripped after the dump: {trip_reason}", {"partial": document, "ops_done": sent})
    return {**document, "ops_done": sent}


# --------------------------------------------------------------------------------------------------------------
# telemetry.start / telemetry.stop / cancel
# --------------------------------------------------------------------------------------------------------------
def op_telemetry_start(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    return helper.telemetry_start(ctx.req_id, args["interval_s"])


def op_telemetry_stop(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    return helper.telemetry_stop()


def op_cancel(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    return helper.cancel_running(args["op_id"])


def builtin_ops() -> dict[str, OpSpec]:
    specs = [
        OpSpec("preflight", op_preflight, _no_args),
        OpSpec("audit.tail", op_audit_tail, _validate_audit_tail),
        OpSpec("telemetry.start", op_telemetry_start, _validate_telemetry_start),
        OpSpec("telemetry.stop", op_telemetry_stop, _no_args),
        OpSpec("cancel", op_cancel, _validate_cancel),
        # a mailbox read is a WRMSR (docs/SAFETY.md), so vf.dump needs the write gate on hardware
        OpSpec("vf.dump", op_vf_dump, _validate_vf_dump, long=True, write=True, state=State.PLANNING),
    ]
    return {s.name: s for s in specs}
