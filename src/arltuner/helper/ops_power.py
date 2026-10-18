"""G-214: ``power.read``, ``power.plan``, ``power.apply`` -- the package power limits (PL1, PL2, PL1 time window) through the powercap files.

* ``power.read``: the powercap values, the guarded reads of MSR ``0x606`` / ``0x610`` / ``0x614`` (lock bit 63 -> read-only, with the reason), the ``[power]`` bounds of the policy and the
  boot values the journal holds. No write. MSRs the policy does not allow, or that cannot be read, leave their block ``null`` with a note; the powercap part still answers.
* ``power.plan``: ``{power: {pl1_w?, pl2_w?, tau_s?}}`` or ``{revert: true}`` (the boot values of the journal) -> the file writes in a safe order, the nonce, nothing written. Same plan-id /
  ``CONFIRM <nonce>`` / 600 s / single-use semantics as ``tune.plan`` (:class:`~arltuner.helper.ops_tune.PlanStore`). The policy's ``[power]`` table bounds every value; an absent key
  refuses the write and names itself (revert needs only the journal: it restores a recorded state).
* ``power.apply``: one powercap file per transaction. Before the first write the boot values are journaled (``power-journal.json``, root 0600, bound to the boot id, written only when absent
  or from another boot, never removed, never applied at start); each write is audited (``powercap_attempt``, fsync'd) before it is sent, under the guard's cross-process lock (``lock_path``,
  the same ``O_EXCL`` scheme), read back from the file **and** from MSR ``0x610`` (decoded), and a mismatch stops the apply (no retry, no auto-revert) and is audited as a trip.
  A tau is snapped down to the encodable window by the plan, and the read-back must show exactly that window.

Why this is not part of ``tune.plan`` / ``tune.apply``: those are profile-centred (``ops_tune``) and the guard (``WriteGuard``) only knows the OC mailbox and the plain MSRs; a powercap file
is neither. The MSR reads go through the guard (allowlist, health gate, audit ``msr_read``); the file writes are audited here with ``kind = "powercap"``: ``powercap_attempt`` before the write, then ``readback``, ``trip``, ``error`` --
deliberately **not** ``attempt``: that event counts against the mailbox budget and dwell (``read_state`` ignores ``powercap_attempt``).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arltuner.controls import power as pw
from arltuner.guard import AuditLog, GuardRefused, MsrRequest, Policy, Refusal
from arltuner.helper.config import INSTALLED_LOCK_DIR

from .ops import OpContext, OpSpec, _only, _refuse, _trust, _trust_refusal, helper_topology
from .ops_tune import _NONCE_ALPHABET, PLAN_EXPIRES_S, PlanStore, _guarded_session, _load_policy, _validate_tune_apply, cpu_limits_doc
from .protocol import BAD_REQUEST, CANCELLED, INTERNAL, REFUSED, PLAN_STALE, OpError
from .state import State
from .trust import TrustError, check_lock_file, open_checked

if TYPE_CHECKING:
    from .core import Helper

__all__ = ["PowerWiring", "power_ops"]

JOURNAL_NAME = "power-journal.json"
_JOURNAL_MAX = 4096
MSRS = (0x606, 0x610, 0x614)
_UW = 1_000_000
_LOCK = threading.Lock()


@dataclass
class PowerWiring:
    """Where the helper finds the powercap zone and keeps the journal: ``files`` (:class:`~arltuner.controls.power.PowerFiles` or the simulator) and ``journal_path``. Tests and
    the launcher may set ``helper.power_wiring``; otherwise ``--sim`` gets the simulated zone next to the audit log and hardware the installed paths."""

    files: pw.PowerFiles
    journal_path: Path


def _wiring(helper: Helper) -> PowerWiring:
    with _LOCK:
        w = getattr(helper, "power_wiring", None)
        if w is None:
            if helper.cfg.simulator:
                w = PowerWiring(pw.SimPowerFiles(), Path(helper.cfg.audit_path).parent / JOURNAL_NAME)
            else:
                w = PowerWiring(pw.PowerFiles(), INSTALLED_LOCK_DIR / JOURNAL_NAME)
            helper.power_wiring = w  # type: ignore[attr-defined]
        return w


def _plans(helper: Helper) -> PlanStore:
    with _LOCK:
        store = getattr(helper, "_power_plans", None)
        if store is None:
            store = PlanStore(helper.cfg.monotonic)
            helper._power_plans = store  # type: ignore[attr-defined]
        return store


@dataclass
class StoredPowerPlan:
    plan_id: str
    nonce: str
    created: float
    steps: list[pw.PowerStep]
    revert: bool
    intent: dict[str, Any]
    used: bool = False
    mismatches: int = 0


# --------------------------------------------------------------------------------------------------------------
# reads: powercap files and the guarded MSR reads
# --------------------------------------------------------------------------------------------------------------
def _boot_id(helper: Helper) -> str | None:
    try:
        boot = helper.cfg.boot_id_fn()
    except Exception:
        return None
    return boot.strip() or None if isinstance(boot, str) else None


def _read_state(w: PowerWiring) -> pw.State:
    try:
        return pw.read_state(w.files)
    except pw.PowerError as exc:
        raise OpError(REFUSED, f"{exc}. Nothing was written.", {"check": "POWER_UNAVAILABLE"}) from None


def _install_sim_sync(helper: Helper, files: Any, cpu: int) -> None:
    """``--sim`` only: every simulated port the helper creates gets the simulated zone's registers (``0x606`` / ``0x610`` / ``0x614``) put into it first -- what the kernel driver would
    have changed in the real ones. ``--sim`` builds a new port per session, so the port factory is wrapped once; a later call just retargets it (tests swap the zone)."""
    cfg = helper.cfg
    holder = getattr(cfg, "_power_sim", None)
    if holder is None:
        holder = types.SimpleNamespace(files=files, cpu=cpu)
        inner = cfg.port_factory

        def factory() -> Any:
            port = inner()
            if holder.files is not None:
                holder.files.sync_msrs(port, holder.cpu)
            return port

        cfg.port_factory = factory
        cfg._power_sim = holder  # type: ignore[attr-defined]
    holder.files, holder.cpu = files, cpu


def _msr_view(helper: Helper, ctx: OpContext, policy: Policy, msrs: tuple[int, ...], what: str) -> pw.MsrView:
    """The guarded plain ``rdmsr`` of ``msrs`` on the lowest CPU (the MSRs are package-wide), decoded. Never raises except for a cancel: whatever cannot be read is a note."""
    topology = helper_topology(helper)
    cpu = min(c.cpu for c in topology.cores)
    w = _wiring(helper)
    if helper.cfg.simulator and hasattr(w.files, "sync_msrs"):
        _install_sim_sync(helper, w.files, cpu)
    words: dict[int, int] = {}
    notes: list[str] = []
    try:
        # plain reads are not WRMSRs and are not counted by the guard; the session cap only has to be >= 1
        with _guarded_session(helper, ctx, policy, 1, what) as session:
            for msr in msrs:
                if ctx.cancel.is_set():
                    raise OpError(CANCELLED, f"{what} was cancelled between reads")
                try:
                    res = session.guard.execute(MsrRequest(cpu, msr, 0, f"{what}: read MSR {msr:#x}", "read"), session.confirm)
                except GuardRefused as exc:
                    notes.append(f"MSR {msr:#x}: refused by the guard ({exc})")
                    if exc.reason is Refusal.TRIPPED:
                        break
                except Exception as exc:  # MsrError: the guard has audited it and tripped
                    notes.append(f"MSR {msr:#x}: {type(exc).__name__}: {exc}")
                    break
                else:
                    words[msr] = res.value
    except OpError as exc:
        if exc.code == CANCELLED:
            raise
        notes.append(exc.message)
    units = pw.decode_units(words[0x606]) if 0x606 in words else None
    limit = pw.decode_limit(words[0x610], units) if units is not None and 0x610 in words else None
    info = pw.decode_info(words[0x614], units) if units is not None and 0x614 in words else None
    if 0x610 in words and units is None and not any("0x606" in n for n in notes):
        notes.append("MSR 0x610 was read but MSR 0x606 (its units) was not: it cannot be decoded")
    return pw.MsrView(units, limit, info, "; ".join(notes) or None)


def _msr_json(view: pw.MsrView) -> dict[str, Any] | None:
    if view.units is None and view.limit is None and view.info is None:
        return None
    out: dict[str, Any] = {"units": None, "limit": None, "info": None}
    if view.units is not None:
        out["units"] = {"raw": f"{view.units.raw:#x}", "power_unit_w": view.units.power_w, "time_unit_s": view.units.time_s}
    if view.limit is not None:
        lim = view.limit
        out["limit"] = {
            "raw": f"{lim.raw:#018x}", "pl1_w": lim.pl1_w, "pl2_w": lim.pl2_w, "tau_s": round(lim.tau_us / _UW, 6), "tau_us": lim.tau_us,
            "pl1_enabled": lim.pl1_enabled, "pl1_clamp": lim.pl1_clamp, "pl2_enabled": lim.pl2_enabled, "pl2_clamp": lim.pl2_clamp, "locked": lim.locked,
        }
    if view.info is not None:
        out["info"] = {"raw": f"{view.info.raw:#018x}", "tdp_w": view.info.tdp_w, "min_w": view.info.min_w, "max_w": view.info.max_w}
    return out


def _policy_json(policy: Policy) -> dict[str, Any]:
    """D-049: the policy no longer bounds a power value (no ``[power]`` table), so only where it was read from. The limits that apply are in ``cpu_limits`` (MSR 0x610 / 0x606)."""
    return {"source": policy.where}


# --------------------------------------------------------------------------------------------------------------
# the journal
# --------------------------------------------------------------------------------------------------------------
def _read_journal(helper: Helper, w: PowerWiring) -> dict[str, int | None] | None:
    """The boot values of this boot's journal, ``None`` when there is none (or it is from another boot). A journal that cannot be trusted or parsed is a refusal, never a guess."""
    _trust(helper, w.journal_path, want="file", must_exist=False)  # the parent chain is root-owned and not writable by others (SPEC 4.4)
    try:
        fd = open_checked(w.journal_path, write=False, create=False, euid=helper.cfg.euid_fn(), what="power journal")
    except FileNotFoundError:
        return None
    except TrustError as exc:
        raise _trust_refusal(exc, "JOURNAL_UNTRUSTED") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(_JOURNAL_MAX + 1)
    except OSError as exc:
        raise OpError(REFUSED, f"cannot read the power journal {w.journal_path}: {exc.strerror or exc}. Nothing was written.", {"check": "JOURNAL_UNAVAILABLE"}) from None
    try:
        if len(data) > _JOURNAL_MAX:
            raise ValueError("too large")
        return pw.parse_journal(data, _boot_id(helper))
    except ValueError as exc:
        raise OpError(REFUSED, f"the power journal {w.journal_path} is unusable ({exc}); remove it by hand once the powercap values are known to be right. Nothing was written.", {"check": "JOURNAL_UNUSABLE"}) from None


def _write_journal(helper: Helper, w: PowerWiring, state: pw.State, boot_id: str) -> None:
    doc = pw.journal_doc(state, boot_id, helper.cfg.wall_clock())
    data = json.dumps(doc, separators=(",", ":")).encode("ascii")
    path = w.journal_path
    _trust(helper, path, want="file", must_exist=False)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = open_checked(tmp, write=True, create=True, mode=0o600, euid=helper.cfg.euid_fn(), what="power journal")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except TrustError as exc:
        raise _trust_refusal(exc, "JOURNAL_UNTRUSTED") from None
    except OSError as exc:
        raise OpError(REFUSED, f"cannot write the power journal {path}: {exc.strerror or exc}. Nothing was written.", {"check": "JOURNAL_UNAVAILABLE"}) from None


# --------------------------------------------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------------------------------------------
def _open_audit(helper: Helper) -> AuditLog:
    """The one ``AuditLog`` of an apply. ``OSError`` when the log cannot be opened (the apply then sends nothing)."""
    cfg = helper.cfg
    _trust(helper, cfg.audit_path, want="file", must_exist=False)
    try:
        os.close(open_checked(cfg.audit_path, write=True, create=True, mode=0o600, euid=cfg.euid_fn(), what="audit log"))
    except TrustError as exc:
        raise OSError(str(exc)) from None
    return AuditLog(cfg.audit_path)


def _audit(helper: Helper, log: AuditLog, event: str, **fields: Any) -> None:
    """One audit record of a powercap action. ``OSError`` propagates."""
    cfg = helper.cfg
    log.append(event, ts=cfg.wall_clock(), operator=cfg.operator, kind="powercap", boot_id=_boot_id(helper), **fields)


def _audit_quiet(helper: Helper, log: AuditLog, event: str, **fields: Any) -> str | None:
    """After the first write: a record that cannot be written is reported, never allowed to hide the outcome."""
    try:
        _audit(helper, log, event, **fields)
    except OSError as exc:
        return f"could not write the audit record ({event}): {exc}"
    return None


# --------------------------------------------------------------------------------------------------------------
# the cross-process lock (the guard's scheme: an O_EXCL file at cfg.lock_path holding our pid; an existing file is never removed here)
# --------------------------------------------------------------------------------------------------------------
class _NotSent(Exception):
    """A step was refused before its write was sent. ``check`` names the reason."""

    def __init__(self, check: str, text: str) -> None:
        super().__init__(text)
        self.check = check


def _take_lock(helper: Helper) -> None:
    cfg = helper.cfg
    try:
        check_lock_file(cfg.lock_path, euid=cfg.euid_fn())
    except TrustError as exc:
        raise _NotSent("LOCK_UNTRUSTED", f"untrusted lock file: {exc}") from None
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(cfg.lock_path, flags, 0o600)
    except FileExistsError:
        raise _NotSent("LOCKED", f"lock file {cfg.lock_path} exists (another mailbox or power transaction is in progress, or a stale lock: clear it by hand)") from None
    except OSError as exc:
        raise _NotSent("LOCKED", f"cannot create lock file {cfg.lock_path}: {exc.strerror or exc}") from None
    try:
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError as exc:
        try:
            os.unlink(cfg.lock_path)  # created a moment ago by us
        except OSError:
            pass
        raise _NotSent("LOCKED", f"cannot write lock file {cfg.lock_path}: {exc.strerror or exc}") from None
    finally:
        os.close(fd)


def _release_lock(helper: Helper) -> str | None:
    """``None`` when released; else why the lock file is still there (every later transaction would refuse until it is cleared)."""
    try:
        os.unlink(helper.cfg.lock_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not remove the lock file {helper.cfg.lock_path}: {exc.strerror or exc}"
    return None


@dataclass
class _Sent:
    """What one sent step left: the file read-back (``None`` = unreadable), a failed write, a lock that could not be released."""

    file_value: int | None = None
    write_error: pw.PowerError | None = None
    lock_problem: str | None = None


def _send_step(helper: Helper, w: PowerWiring, log: AuditLog, step: pw.PowerStep, record: dict[str, Any]) -> _Sent:
    """Under the lock: the ``powercap_attempt`` record (written and fsync'd first), the one file write, the file read-back. :class:`_NotSent` before the write is sent."""
    _take_lock(helper)
    sent = _Sent()
    try:
        try:
            _audit(helper, log, "powercap_attempt", detail=f"powercap write {step.key}: {step.before} -> {step.value}", **record)
        except OSError as exc:
            raise _NotSent("AUDIT_UNAVAILABLE", f"the audit log cannot be written ({exc}); the write was not sent") from None
        try:
            w.files.write_int(step.file, step.value)
            try:
                sent.file_value = w.files.read_int(step.file)
            except pw.PowerError:
                sent.file_value = None
        except pw.PowerError as exc:
            sent.write_error = exc
    finally:
        sent.lock_problem = _release_lock(helper)
    return sent


# --------------------------------------------------------------------------------------------------------------
# validation (shape only)
# --------------------------------------------------------------------------------------------------------------
def _bad(text: str) -> OpError:
    return OpError(BAD_REQUEST, text)


def _validate_power_plan(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"power", "revert"})
    if ("power" in args) == ("revert" in args):
        raise _bad("power.plan needs exactly one of 'power' ({pl1_w, pl2_w, tau_s}) or 'revert' (true)")
    if "revert" in args:
        if args["revert"] is not True:
            raise _bad("'revert' must be true")
        return {"revert": True}
    raw = args["power"]
    if not isinstance(raw, dict):
        raise _bad("'power' must be an object {pl1_w?, pl2_w?, tau_s?}")
    extra = sorted(set(raw) - set(pw.KEYS))
    if extra:
        raise _bad(f"unknown power key(s): {', '.join(map(repr, extra))}")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
            raise _bad(f"power.{key} must be a number ({'seconds' if key == 'tau_s' else 'whole watts'})")
        out[key] = value
    if not out:
        raise OpError(BAD_REQUEST, "the power intent changes nothing (set pl1_w, pl2_w and/or tau_s)", {"check": "NOTHING_TO_DO"})
    return {"power": out}


def _refused(exc: pw.Refused) -> OpError:
    detail: dict[str, Any] = {"check": exc.check}
    if exc.key is not None:
        detail["key"] = exc.key
    return OpError(REFUSED, f"{exc}. Nothing was written.", detail)


# --------------------------------------------------------------------------------------------------------------
# power.read
# --------------------------------------------------------------------------------------------------------------
def op_power_read(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    policy = _load_policy(helper)
    w = _wiring(helper)
    result: dict[str, Any] = {
        "available": False, "powercap": None, "msr": None, "msr_note": None, "locked": None, "writable": False, "read_only_reason": None,
        "policy": _policy_json(policy), "boot": None, "journal": str(w.journal_path), "note": None,
        "cpu_limits": cpu_limits_doc(helper, power=pw.limits_json(None)),
    }
    try:
        state = pw.read_state(w.files)
    except pw.PowerError as exc:
        result["note"] = str(exc)
        result["read_only_reason"] = str(exc)
        return result
    result["available"] = True
    result["powercap"] = state.as_json()
    view = _msr_view(helper, ctx, policy, MSRS, "power.read")
    result["msr"] = _msr_json(view)
    result["msr_note"] = view.note
    result["cpu_limits"] = cpu_limits_doc(helper, power=pw.limits_json(view))  # D-049: the lock bit and the field width, read now
    if view.limit is not None:
        result["locked"] = view.limit.locked
        # the file and the register must agree on what is in force: a difference is reported, not resolved
        diffs = [
            f"{label}: powercap {a:g}, MSR 0x610 {b:g}"
            for label, a, b in (("PL1 W", state.pl1_w, view.limit.pl1_w), ("PL2 W", state.pl2_w, view.limit.pl2_w), ("tau s", state.tau_s, view.limit.tau_us / _UW))
            if a is not None and abs(a - b) > (0.125 if label != "tau s" else 0.01 * max(a, b))
        ]
        if diffs:
            result["msr_note"] = "; ".join(filter(None, [result["msr_note"], "the powercap files and MSR 0x610 disagree (" + "; ".join(diffs) + ")"]))
    if view.limit is None:
        result["read_only_reason"] = f"MSR 0x610 could not be read ({view.note or 'no read'}): the lock bit is unknown, so writes are not offered"
    elif view.limit.locked:
        result["read_only_reason"] = "MSR 0x610 bit 63 (lock) is set: the package power limits are read-only until the next reset"
    else:
        result["writable"] = True
    try:
        boot = _read_journal(helper, w)
    except OpError as exc:
        result["note"] = exc.message
        boot = None
    if boot is not None:
        result["boot"] = {"pl1_w": boot["pl1_uw"] / _UW, "pl2_w": None if boot["pl2_uw"] is None else boot["pl2_uw"] / _UW, "tau_s": boot["tau_us"] / _UW, "note": pw.BIOS_NOTE}
    return result


# --------------------------------------------------------------------------------------------------------------
# power.plan
# --------------------------------------------------------------------------------------------------------------
def _steps_for(helper: Helper, ctx: OpContext, policy: Policy, w: PowerWiring, args: dict[str, Any], what: str) -> tuple[list[pw.PowerStep], pw.State, pw.MsrView, dict[str, int | None] | None]:
    """``(steps, state, msr view, journal values of this boot or None)`` for a plan-shaped ``args``; raises the refusal."""
    state = _read_state(w)
    view = _msr_view(helper, ctx, policy, MSRS[:2], what)
    boot = _read_journal(helper, w)
    try:
        if args.get("revert"):
            if boot is None:
                raise OpError(REFUSED, "there is nothing to revert to: no power value was written in this boot (no journal for this boot). Nothing was written.", {"check": "NO_JOURNAL"})
            steps = pw.plan_revert(boot, state, view.units, view)
        else:
            steps = pw.plan(args["power"], policy, state, view.units, view)
    except pw.Refused as exc:
        raise _refused(exc) from None
    if not steps:
        raise OpError(REFUSED, "the values in force already are the ones asked: nothing to write. Nothing was written.", {"check": "NOTHING_TO_DO"})
    return steps, state, view, boot


def _after(state: pw.State, steps: list[pw.PowerStep]) -> dict[str, Any]:
    now = {s.key: s.value for s in steps}
    return {
        "pl1_w": now.get("pl1_w", state.pl1_uw) / _UW,
        "pl2_w": None if state.pl2_uw is None and "pl2_w" not in now else now.get("pl2_w", state.pl2_uw) / _UW,  # type: ignore[operator]
        "tau_s": now.get("tau_s", state.tau_us) / _UW,
    }


def op_power_plan(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    policy = _load_policy(helper)
    w = _wiring(helper)
    steps, state, view, boot = _steps_for(helper, ctx, policy, w, args, "power.plan")
    journal_new = boot is None and not args.get("revert")
    warnings = ["tau is rounded down to the window the register can encode (the step shows that window); the read-back must show exactly it"] if any(s.key == "tau_s" for s in steps) else []
    if not args.get("revert"):
        warnings.extend(f"{code}: {text}" for code, text in pw.warnings(args["power"], state))  # D-049: PL2_BELOW_PL1 / LARGE_CHANGE only warn
    if journal_new:
        warnings.append(f"the values now in force are journaled before the first write ({pw.BIOS_NOTE}); \"revert\" puts them back")
    warnings.append("package power limits are runtime state: a reboot restores the firmware values")
    nonce = "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(6))
    canon = json.dumps({"args": args, "steps": [s.as_json() for s in steps], "nonce": nonce}, sort_keys=True)
    plan_id = hashlib.sha256(canon.encode()).hexdigest()
    _plans(helper).add(StoredPowerPlan(plan_id, nonce, helper.cfg.monotonic(), steps, bool(args.get("revert")), dict(args)))  # type: ignore[arg-type]
    return {
        "plan_id": plan_id, "nonce": nonce, "revert": bool(args.get("revert")), "steps": [s.as_json() for s in steps], "writes": len(steps), "ops_needed": 0, "expires_s": int(PLAN_EXPIRES_S),
        "before": state.as_json(), "after": _after(state, steps), "locked": view.limit.locked if view.limit else None, "journal_new": journal_new, "warnings": warnings,
        "policy": _policy_json(policy), "cpu_limits": cpu_limits_doc(helper, power=pw.limits_json(view)),
    }


# --------------------------------------------------------------------------------------------------------------
# power.apply
# --------------------------------------------------------------------------------------------------------------
def op_power_apply(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    plan = _plans(helper).claim(args["plan_id"], args["typed"])
    policy = _load_policy(helper)
    w = _wiring(helper)
    boot_id = _boot_id(helper)
    if boot_id is None:
        raise OpError(REFUSED, "the boot id is unknown, so the power journal cannot be bound to this boot. Nothing was written.", {"check": "BOOT_ID_UNKNOWN"})
    # the plan was made from the values then in force: plan again from the values now; any difference is stale (nothing written)
    steps, state, view, boot = _steps_for(helper, ctx, policy, w, plan.intent, "power.apply")
    if [(s.key, s.before, s.value) for s in steps] != [(s.key, s.before, s.value) for s in plan.steps]:
        raise OpError(PLAN_STALE, "the power values changed since the plan was made (or the plan no longer fits the policy); nothing was written. Plan again.")
    try:
        log = _open_audit(helper)  # the one AuditLog of this apply
    except OSError as exc:
        raise OpError(REFUSED, f"the audit log cannot be opened ({exc}); nothing was written.", {"check": "AUDIT_UNAVAILABLE"}) from None
    if boot is None and not plan.revert:
        _write_journal(helper, w, state, boot_id)  # before the first write, only when this boot has none
    results: list[dict[str, Any]] = []
    readback: list[dict[str, Any]] = []
    warnings: list[str] = []
    stop: dict[str, Any] | None = None
    sent = 0
    table = [{"n": s.number, "key": s.key, "file": s.file, "before": s.before, "value": s.value, "status": "not_sent"} for s in steps]  # every step is listed, sent or not
    results.extend(table)
    for step, entry in zip(steps, table):
        if ctx.cancel.is_set():
            stop = {"kind": "cancelled", "reason": f"step {step.number}: cancelled between transactions; nothing was sent for it and the steps after it", "tripped": False}
            break
        record = dict(key=step.key, path=str(w.files.path(step.file)), before=step.before, written=step.value, plan_id=plan.plan_id, revert=plan.revert)
        try:
            outcome = _send_step(helper, w, log, step, record)
        except _NotSent as exc:
            entry["status"] = "refused"
            reason = f"step {step.number}: {exc}"
            if sent == 0:
                raise OpError(REFUSED, f"{reason}. Nothing was written.", {"check": exc.check}) from None
            stop = {"kind": "refused", "reason": reason, "tripped": False}
            break
        sent += 1  # a failed write may have reached the file
        if outcome.lock_problem:
            warnings.append(outcome.lock_problem)
        if outcome.write_error is not None:
            exc = outcome.write_error
            entry["status"] = "error"
            entry["detail"] = str(exc)
            warnings.extend(filter(None, [_audit_quiet(helper, log, "error", detail=str(exc), **record)]))
            stop = {"kind": "transport", "reason": f"step {step.number}: {exc}", "tripped": True}
            break
        file_value = outcome.file_value
        after_view = _msr_view(helper, ctx, policy, MSRS[:2], "power.apply read-back") if not ctx.cancel.is_set() else pw.MsrView(note="cancelled")
        problem = pw.readback_problem(step, file_value, after_view, after_view.units)
        lim = after_view.limit
        msr_value = None if lim is None else {"pl1_w": round(lim.pl1_w * _UW), "pl2_w": round(lim.pl2_w * _UW), "tau_s": lim.tau_us}[step.key]  # in the file's unit
        readback.append({
            "key": step.key, "expected": list(step.expect), "file": file_value, "msr": msr_value, "match": problem is None, **({} if problem is None else {"problem": problem}),
        })
        entry.update(status="ok" if problem is None else "unverified", file_value=file_value)
        warnings.extend(filter(None, [_audit_quiet(helper, log, "readback", detail=problem or "the file and MSR 0x610 show the value written", read_value=file_value, matches=problem is None, **record)]))
        if problem is not None:
            warnings.extend(filter(None, [_audit_quiet(helper, log, "trip", reason="TRIPPED", detail=problem, **record)]))
            stop = {"kind": "unverified", "reason": f"step {step.number}: read-back mismatch: {problem}; stopping (no retry, no automatic revert)", "tripped": True}
            break
        if outcome.lock_problem:  # the next transaction would refuse on the stale lock: stop here, loudly
            stop = {"kind": "refused", "reason": f"step {step.number}: {outcome.lock_problem}", "tripped": True}
            break
    try:
        after = pw.read_state(w.files).as_json()
    except pw.PowerError:
        after = None
    complete = stop is None
    out: dict[str, Any] = {
        "plan_id": plan.plan_id, "revert": plan.revert, "steps": results, "readback": readback, "complete": complete, "writes_sent": sent, "after": after, "journal": str(w.journal_path),
        "warnings": warnings,
    }
    if complete:
        return out
    if sent:
        out["stop"] = stop  # the hardware is partly changed: the table must reach the page, so this is a result, not an error
        return out
    assert stop is not None
    if stop["kind"] == "cancelled":
        raise OpError(CANCELLED, "power.apply was cancelled before the first write; nothing was changed")
    raise OpError(INTERNAL, str(stop["reason"]), {"tripped": stop["tripped"]})


# --------------------------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------------------------
def power_ops() -> list[OpSpec]:
    from .ops import _no_args

    return [
        # long: the guarded MSR reads take the mailbox lock, so no other long op may run at the same time; no hardware write
        OpSpec("power.read", op_power_read, _no_args, long=True, state=State.PLANNING),
        OpSpec("power.plan", op_power_plan, _validate_power_plan, long=True, state=State.PLANNING),
        # D-029: every write op is long and write-gated (the preflight write gate on hardware), state "applying"
        OpSpec("power.apply", op_power_apply, _validate_tune_apply, long=True, write=True, state=State.APPLYING),
    ]
