"""G-112 helper ops (contract 9, SPEC-v3 sections 4-6, D-032/D-033): ``tune.read``, ``tune.plan``, ``tune.apply``, ``profiles.list``, ``profiles.save``.

Design in one paragraph. ``tune.read`` is ``vf.dump`` plus decoded words (and a best-effort plain read of the turbo tables); it remembers the words in
force (``helper.last_words``: cpu -> [OC word, point 1, point 2, ...]). ``tune.plan`` is **pure** (no hardware, no mailbox, not write-gated): it turns the
UI's intent into a ``Profile`` for the existing controls planner, resolving "the point at ratio 53" from the words of the last read, checks every number
against the root policy (``REFUSED POLICY_RANGE``) and the controls pre-flight, and keeps the plan (id, nonce, expiry, the words it assumed) in memory.
``tune.apply`` is the existing ``controls.apply_profile`` engine run through the guard, one transaction at a time: it re-reads the baselines inside its
own guarded session (the guard demands a baseline read per session anyway), refuses with ``PLAN_STALE`` before any write when a word differs from what
the plan assumed, then writes and reads every write back. No new mailbox logic lives here: the guard stays the only path to the mailbox and
``controls`` builds every request.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arltuner import vfmodel
from arltuner.controls import (
    REVERT_NOTE,
    ApplyPlan,
    NeedsBootBudget,
    NeedsHigherLimit,
    NeedsRamp,
    PlanError,
    PlanRefused,
    PreflightError,
    StepKind,
    StepStatus,
    StopKind,
    apply_profile,
    count_operations,
    pack_turbo,
    plan_warnings,
    preflight,
    resolve_point_targets,
    resolve_targets,
)
from arltuner.guard import (
    ENV_FLAG,
    AuditLog,
    AuditUnreadable,
    Confirmation,
    GuardRefused,
    MsrRequest,
    Policy,
    PolicyError,
    Refusal,
    Request,
    WriteGuard,
    anchored_wall_clock,
    default_m1_policy,
    load_policy,
    read_state,
)
from arltuner.controls.planner import POINT_CAVEAT, kept_word
from arltuner.guard.policy import top_fused_ratio, turbo_bytes
from arltuner.mailbox import Cmd, VfWord, rc_name
from arltuner.msr import MailboxTransport
from arltuner.platform import default_platform
from arltuner.profiles import CoreSetting, PointSetting, Profile, ProfileError, TurboTables, load_profile
from arltuner.profiles.contents import intent_contents, profile_contents, summarize
from arltuner.profiles.lastread import LastReadError, LastReadStore
from arltuner.profiles.store import NAME_RE, ProfileStore, ProfileStoreError
from arltuner.topology import Topology, TopologyError, UnknownCore
from arltuner.vfmodel import CurvePoint, check_turbo_order, complete_baseline, effective_curve, predict_vccia, voltage_at

from .ops import (
    OpContext,
    OpSpec,
    _is_int,
    _no_args,
    _only,
    _refuse,
    _trust,
    _trust_refusal,
    _VirtualClock,
    accepted_match,
    op_vf_dump,
)
from .protocol import BAD_REQUEST, CANCELLED, CONFIRM_MISMATCH, INTERNAL, MAX_LINE, PLAN_STALE, REFUSED, OpError
from .state import State
from .trust import TrustError, check_lock_file, check_trusted_path, open_checked

if TYPE_CHECKING:
    from .core import Helper

__all__ = ["PLAN_EXPIRES_S", "PlanStore", "tune_ops", "unit_cpus", "unit_of_cpu", "vf_dump_tracked"]

PLAN_EXPIRES_S = 600.0  # contract 8: a plan older than this is stale
_MAX_PLANS = 16
_MAX_CONFIRM_MISMATCHES = 3
_MAX_INTENT_BYTES = 16 * 1024
_RESPONSE_SLACK = 4096  # envelope of the response line around the result (the 64 KiB cap includes it)
_ACCEPTED_FILE_MAX = 256 * 1024
_ACCEPTED_PATH_RE = re.compile(r"profiles/[A-Za-z0-9._-]{1,80}\Z")
_NONCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789"  # contract 8: [A-Z2-9]
_RATIO_RANGE = (1, 100)  # the profile schema's unit sanity range (the policy's ratio_max is the bound)

UNITS: tuple[str, ...] = tuple(f"P{i}" for i in range(8)) + tuple(f"EC{i}" for i in range(4))
GROUPS: dict[str, tuple[str, ...]] = {"P": UNITS[:8], "E": UNITS[8:]}
#: ``tune.plan`` echoes at most this many predicted write steps (the full count is ``writes``): the response line is capped at 64 KiB.
_MAX_STEPS_LISTED = 300


# --------------------------------------------------------------------------------------------------------------
# Units (contract 9): P0..P7 = CPU n, EC0..EC3 = the four CPUs of an E-cluster
# --------------------------------------------------------------------------------------------------------------
class Warn(str):
    """A plan warning with a machine-readable ``code`` (contract 9 addendum: the result carries ``{code, text}``). Everything untagged is ``NOTE``."""

    code: str

    def __new__(cls, code: str, text: str) -> "Warn":
        obj = super().__new__(cls, text)
        obj.code = code
        return obj


def warning_objects(items: Sequence[str]) -> list[dict[str, str]]:
    return [{"code": getattr(w, "code", "NOTE"), "text": str(w)} for w in items]


def unit_cpus(topology: Topology, unit: str) -> tuple[int, ...]:
    if unit.startswith("EC"):
        return topology.cluster_cpus(int(unit[2:]))
    return (topology.resolve("P", int(unit[1:])).cpu,)


def unit_of_cpu(topology: Topology, cpu: int) -> str:
    core = topology.by_cpu(cpu)
    return f"P{core.index}" if core.cluster is None else f"EC{core.cluster}"


def _kind_of(unit: str) -> str:
    return "P" if unit.startswith("P") else "E"


# --------------------------------------------------------------------------------------------------------------
# State kept on the helper: the words in force, the turbo tables, the stored plans
# --------------------------------------------------------------------------------------------------------------
@dataclass
class StoredPlan:
    plan_id: str
    nonce: str
    created: float
    profile: Profile
    refs: dict[tuple[int, int], int]  # (cpu, point) -> the word the plan assumed (point 0 = the OC word); empty = nothing to compare
    expected_ops: int
    used: bool = False
    mismatches: int = 0


class PlanStore:
    """Plans waiting for ``tune.apply``: in memory only (a helper restart forgets them: ``PLAN_STALE``). Single-use, 600 s, bounded."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._plans: dict[str, StoredPlan] = {}

    def add(self, plan: StoredPlan) -> None:
        with self._lock:
            self._prune()
            while len(self._plans) >= _MAX_PLANS:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan.plan_id] = plan

    def claim(self, plan_id: str, typed: str) -> StoredPlan:
        """The plan if ``typed`` is ``CONFIRM <nonce>``; marks it used (a replay is ``CONFIRM_MISMATCH``). ``PLAN_STALE`` if unknown or older than 600 s."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise OpError(PLAN_STALE, "no such plan: it was never made, it expired, or the helper restarted; plan again")
            if self._clock() - plan.created > PLAN_EXPIRES_S:
                del self._plans[plan_id]
                raise OpError(PLAN_STALE, f"the plan is older than {int(PLAN_EXPIRES_S)} s; plan again")
            if plan.used:
                raise OpError(CONFIRM_MISMATCH, "this plan was already applied; a nonce is single-use")
            expected = "CONFIRM " + plan.nonce
            if not secrets.compare_digest(typed.encode("utf-8"), expected.encode("utf-8")):
                plan.mismatches += 1
                if plan.mismatches >= _MAX_CONFIRM_MISMATCHES:
                    del self._plans[plan_id]
                raise OpError(CONFIRM_MISMATCH, "the confirmation does not match this plan's nonce")
            plan.used = True
            return plan

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, p in self._plans.items() if now - p.created > PLAN_EXPIRES_S]:
            del self._plans[key]


@dataclass
class TuneState:
    plans: PlanStore
    tables: dict[str, tuple[int, ...]] | None = None  # {"e": 8 buckets, "p": 8 buckets} from the last tune.read (plain rdmsr), or None
    tables_note: str | None = None
    fused: dict[int, dict[str, Any]] | None = None  # G-114 F7: cpu -> {word, ratio, v} of the last tune.read (cmd 0x09 P1=0x11 P2=0)
    turbo: dict[str, Any] | None = None  # G-114 F5: {"P": {ratios, counts}, "E": {...}, "favored": {cpu: ratio}} of the last tune.read
    lock: threading.Lock = field(default_factory=threading.Lock)


_STATE_LOCK = threading.RLock()  # re-entrant: the lazy restore of the last read (G-123) builds the state while holding it


def _state(helper: Helper) -> TuneState:
    with _STATE_LOCK:  # tune.plan runs on up to four slow threads: two first callers must not each build a state (one's plan would be lost)
        st = getattr(helper, "_tune_state", None)
        if st is None:
            st = TuneState(PlanStore(helper.cfg.monotonic))
            helper._tune_state = st  # type: ignore[attr-defined]
        return st


def _last_words(helper: Helper) -> dict[int, list[int]] | None:
    if not getattr(helper, "_last_restored", False):
        _restore_last(helper)  # G-123: the first tune.* call of a helper process picks up this boot's stored read
    words = getattr(helper, "last_words", None)
    return words if isinstance(words, dict) and words else None


def _set_last_words(helper: Helper, words: Mapping[int, Sequence[int]]) -> None:
    helper.last_words = {int(cpu): [int(w) for w in ws] for cpu, ws in words.items()}  # a new dict each time: readers never see a half-built one


# --------------------------------------------------------------------------------------------------------------
# Policy and topology (trusted inputs only, SPEC 4.4)
# --------------------------------------------------------------------------------------------------------------
def _load_policy(helper: Helper) -> Policy:
    cfg = helper.cfg
    if cfg.policy_path is not None:
        _trust(helper, cfg.policy_path, want="file", must_exist=True)
    try:
        return default_m1_policy() if cfg.policy_path is None else load_policy(cfg.policy_path)
    except (PolicyError, OSError) as exc:
        raise _refuse(f"policy error: {exc}.", "POLICY_INVALID") from None


def _topology(helper: Helper) -> Topology:
    try:
        return helper.cfg.topology_fn()
    except TopologyError as exc:
        raise _refuse(f"this machine does not match the Core Ultra 9 285K map: {exc}.", "TOPOLOGY") from None


# --------------------------------------------------------------------------------------------------------------
# Words: decode, remember
# --------------------------------------------------------------------------------------------------------------
def _decode(vf: VfWord) -> dict[str, Any]:
    """Contract 9 ``{ratio, offset_mv, v_target_v|null, mode}``. A ratio / V target field that reads 0 is *unset* (the OC word of 23 of 24 cores): null."""
    return {
        "ratio": vf.ratio or None,
        "offset_mv": round(vf.offset_mv, 3),
        "v_target_v": round(vf.v_target_v, 4) if vf.v_target_raw else None,
        "mode": "static" if vf.static else "adaptive",
    }


def _words_of_document(doc: Mapping[str, Any]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for unit in doc.get("units", []):
        for cpu in unit.get("cpus", []):
            oc = cpu.get("oc")
            if oc is None:
                continue
            out[int(cpu["cpu"])] = [int(oc["word"], 16)] + [int(p["word"], 16) for p in cpu.get("points", [])]
    return out


def _decode_document(doc: dict[str, Any], topology: Topology) -> dict[str, Any]:
    """Add ``units[*].id`` and ``tune`` (``{unit: {cpus: [{cpu, points: [{point, ratio, offset_mv, v_target_v, mode}]}]}}``; point 0 = the OC word)."""
    tune: dict[str, Any] = {}
    for unit in doc.get("units", []):
        first = unit["cpus"][0]["cpu"] if unit.get("cpus") else None
        if first is None:
            continue
        uid = unit_of_cpu(topology, first)
        unit["id"] = uid
        rows = []
        kind = _kind_of(uid)
        fused_n, oc_n = len(_POINT_RATIOS[kind]), len(_POINT_RATIOS[kind]) + 1  # P: fused 1..8, OC = point 9; E: fused 1..6, OC = point 7 (community naming)
        for cpu in unit["cpus"]:
            pts = []
            if cpu.get("oc") is not None:
                pts.append({"point": 0, "point_label": f"OC (point {oc_n})", **_decode(VfWord.from_u32(int(cpu["oc"]["word"], 16)))})
            for p in cpu.get("points", []):
                is_oc = p["point"] > fused_n  # a read that returns P2 = 9 (P) / 7 (E): the OC ratio is above the top fused ratio, so this IS the OC point
                label = f"OC (point {oc_n})" if is_oc else f"point {p['point']}"
                pts.append({"point": p["point"], "point_label": label, **({"oc_point": True} if is_oc else {}), **_decode(VfWord.from_u32(int(p["word"], 16)))})
            rows.append({"cpu": cpu["cpu"], "points": pts})
        tune[uid] = {"cpus": rows}
    return tune


def _fit(result: dict[str, Any]) -> dict[str, Any]:
    """Keep the result under the 64 KiB line (the Writer would otherwise replace it by an INTERNAL error): drop the decoded duplicates of the extra E
    CPUs, then the differences list, then the decoded copy altogether, re-measuring each time."""
    def size() -> int:
        return len(json.dumps(result, separators=(",", ":")))

    limit = MAX_LINE - _RESPONSE_SLACK
    if size() <= limit:
        return result
    tune = result.get("tune")
    if isinstance(tune, dict):
        for unit in tune.values():
            unit["cpus"] = unit["cpus"][:1]
        result["tune_truncated"] = True
    if size() > limit:
        result["differences"] = []
        result["differences_truncated"] = True
    if size() > limit:
        result["tune"] = None
    return result


# --------------------------------------------------------------------------------------------------------------
# A guarded session (the same set-up as vf.dump: trusted inputs, budget, lock, health gate, clocks, one guard)
# --------------------------------------------------------------------------------------------------------------
@dataclass
class _Session:
    guard: WriteGuard
    confirm: Confirmation
    policy: Policy  # with max_operations_per_session = exactly the budget of this session
    clock: Any
    sleep: Any
    used: int  # per-boot operations used before this session


@contextlib.contextmanager
def _guarded_session(helper: Helper, ctx: OpContext, policy: Policy, needed: int, what: str) -> Iterator[_Session]:
    """Everything decidable before the first request, then a ``WriteGuard`` over the helper's port. ``needed`` = the worst-case number of
    operations (the executor's own count); the guard gets exactly that as its session cap. ``SESSION_LIMIT`` / ``BOOT_LIMIT`` / ... are refusals
    with nothing sent."""
    cfg = helper.cfg
    _trust(helper, cfg.audit_path, want="file", must_exist=False)
    if needed > policy.max_operations_per_session:
        raise _refuse(
            f"{what} needs up to {needed} mailbox operations (every request is a WRMSR) but the policy allows {policy.max_operations_per_session} per session.",
            "SESSION_LIMIT", needed=needed, allowed=policy.max_operations_per_session,
        )
    euid = cfg.euid_fn()
    try:
        check_lock_file(cfg.lock_path, euid=euid)
    except TrustError as exc:
        raise _trust_refusal(exc, "LOCK_UNTRUSTED") from None
    try:
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
            f"{what} needs up to {needed} mailbox operations but only {left} of the policy's {policy.max_operations_per_boot} operations per boot are left.",
            "BOOT_LIMIT", needed=needed, left=left,
        )
    if cfg.simulator:
        if helper.sim_time is None:
            virtual = _VirtualClock()
            helper.sim_time = (virtual, anchored_wall_clock(virtual.clock, cfg.wall_clock))
        virtual, wall = helper.sim_time
        virtual.sleep(policy.min_dwell_s)
        clock, sleep = virtual.clock, virtual.sleep
        transport_sleep = sleep
    else:
        clock, wall = cfg.monotonic, cfg.wall_clock
        transport_sleep = cfg.sleep

        def sleep(seconds: float) -> None:  # the dwell between transactions: the cancel flag wakes it
            ctx.cancel.wait(seconds)
    try:
        gate = cfg.health_gate_factory()
    except TrustError as exc:
        raise _trust_refusal(exc, "TRUST_PATH") from None
    except (ValueError, OSError) as exc:
        raise _refuse(f"the helper's own configuration is invalid: {exc}.", "HELPER_CONFIG_INVALID") from None
    if gate is None and not cfg.simulator:
        raise _refuse("no health gate is available (the health bracket is mandatory on hardware).", "NO_HEALTH_GATE")
    policy = dataclasses.replace(policy, max_operations_per_session=needed)  # exactly the budget; a policy can only lower it
    try:
        audit = AuditLog(cfg.audit_path)
    except OSError as exc:
        raise _refuse(f"cannot open the audit log {cfg.audit_path}: {exc}.", "AUDIT_UNAVAILABLE") from None
    port = cfg.port_factory()  # every refusal is above
    try:
        token = secrets.token_hex(16)
        guard = WriteGuard(
            MailboxTransport(port, sleep=transport_sleep, clock=clock),
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
        yield _Session(guard, Confirmation(token, cfg.operator), policy, clock, sleep, used)
    finally:
        close = getattr(port, "close", None)
        if callable(close):
            close()


# --------------------------------------------------------------------------------------------------------------
# vf.dump / tune.read
# --------------------------------------------------------------------------------------------------------------
def vf_dump_tracked(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``vf.dump`` unchanged, plus: after a *complete* dump the words are kept in ``helper.last_words`` (cpu -> [OC word, point 1, ...])."""
    doc = op_vf_dump(helper, ctx, args)  # raises on a stop: a partial dump never becomes "the words in force"
    words = _words_of_document(doc)
    if words:
        merged = {cpu: list(ws) for cpu, ws in (_last_words(helper) or {}).items()}
        merged.update(words)
        _set_last_words(helper, merged)
    return doc


_LABEL = {"fused": "fused points", "favored": "favored ratios", "turbo": "turbo tables"}
_FUSED_CMD, _FUSED_P1 = 0x09, 0x11  # VoltageFloorRead P1 = 0x11 (domain floors), P2 = 0 (core): the fused V/F point per core, registers.md 0x09 [MEASURED win 2026-10-01]


def _fused_entry(word: int) -> dict[str, Any]:
    """Contract 9 addendum ``{word, ratio, v}``: ratio ``[7:0]``, voltage ``[19:8]`` S2.10 (platform-285k.md section 4)."""
    return {"word": f"0x{word & 0xFFFFFFFF:08x}", "ratio": word & 0xFF, "v": round(((word >> 8) & 0xFFF) / 1024 + 1e-9, 4)}  # half up: 0x4A0 = 1.15625 V -> 1.1563 as in platform-285k.md


def _read_extras(helper: Helper, ctx: OpContext, policy: Policy, topology: Topology) -> dict[str, Any]:
    """G-114: the guarded reads that make ``tune.read`` respect SPEC-v3 section 9, in ONE session of exactly ``12 + 8 + 4 = 24`` guard-counted requests (20 of them
    mailbox reads = WRMSR to 0x150, 4 plain rdmsr), one at a time:

    * ``fused`` (F7): cmd ``0x09 P1=0x11 P2=0`` on every P-core and on the first CPU of every E-cluster (12),
    * ``favored`` (F5): cmd ``0x1C`` on the 8 P-cores (low byte = the favored ratio; E clusters read ``0x2E30`` and follow ``0x650``) (8),
    * ``turbo`` (F5): plain ``rdmsr`` ``0x1AD`` / ``0x1AE`` / ``0x650`` / ``0x651`` on CPU 0 (4).

    Each group degrades alone (a refusal leaves that group ``None`` with a note); a guard trip or a cancel ends the session. Returns
    ``{fused, turbo, tables, notes}``."""
    p_cpus = sorted(c.cpu for c in topology.cores if c.kind == "P")
    e_cpus = [topology.cluster_cpus(k)[0] for k in sorted({c.cluster for c in topology.cores if c.cluster is not None})]
    out: dict[str, Any] = {"fused": None, "turbo": None, "tables": None, "notes": {}}
    notes: dict[str, str] = out["notes"]
    try:
        with _guarded_session(helper, ctx, policy, len(p_cpus) * 2 + len(e_cpus) + 4, "the fused-point / turbo-table read") as session:
            def mailbox(cpu: int, cmd: int, p1: int, what: str) -> int:
                if ctx.cancel.is_set():
                    raise OpError(CANCELLED, "tune.read was cancelled between transactions")
                res = session.guard.execute(Request(cpu, cmd, p1, 0, 0, f"tune.read: {what} of cpu {cpu}"), session.confirm)
                if res.response.rc != 0:
                    raise OpError(REFUSED, f"cpu {cpu}: mailbox completion code {res.response.rc} ({rc_name(res.response.rc)})")
                return res.response.data

            def plain(msr: int) -> int:
                if ctx.cancel.is_set():
                    raise OpError(CANCELLED, "tune.read was cancelled between transactions")
                return session.guard.execute(MsrRequest(p_cpus[0], msr, 0, f"tune.read: read {msr:#x} (plain rdmsr)", "read"), session.confirm).value

            def do_fused() -> None:
                out["fused"] = {cpu: _fused_entry(mailbox(cpu, _FUSED_CMD, _FUSED_P1, "read the fused V/F point (0x09)")) for cpu in p_cpus + e_cpus}

            favored: dict[int, int] = {}

            def do_favored() -> None:
                for cpu in p_cpus:
                    favored[cpu] = mailbox(cpu, int(Cmd.FAVORED_CORE_READ), 0, "read the favored ratio (0x1C)") & 0xFF

            def do_turbo() -> None:
                ratios = {msr: plain(msr) for msr in (0x1AD, 0x650)}
                out["tables"] = {"e": turbo_bytes(ratios[0x650]), "p": turbo_bytes(ratios[0x1AD])}
                counts: dict[int, list[int] | None] = {}
                for msr in (0x1AE, 0x651):  # an installed policy older than 2026-10-11 has no allow entry for the bucket counts: the ratios stand alone then
                    try:
                        counts[msr] = list(turbo_bytes(plain(msr)))
                    except GuardRefused as exc:
                        if exc.reason is Refusal.TRIPPED:
                            raise
                        counts[msr] = None
                        notes["counts"] = f"bucket counts not read: the guard refused ({exc.reason.value}); the policy has no msr_allow read entry for {msr:#x}"
                out["turbo"] = {
                    "P": {"ratios": list(turbo_bytes(ratios[0x1AD])), "counts": counts[0x1AE]},
                    "E": {"ratios": list(turbo_bytes(ratios[0x650])), "counts": counts[0x651]},
                    "favored": {},
                }

            for key, fn in (("fused", do_fused), ("favored", do_favored), ("turbo", do_turbo)):
                try:
                    fn()
                except OpError as exc:
                    if exc.code == CANCELLED:
                        raise
                    notes[key] = f"{_LABEL[key]} not read: {exc.message}"
                except GuardRefused as exc:
                    if exc.reason is Refusal.TRIPPED:
                        raise
                    notes[key] = f"{_LABEL[key]} not read: the guard refused ({exc.reason.value}); the policy has no allow entry for it"
            if out["turbo"] is not None:
                out["turbo"]["favored"] = {cpu: r for cpu, r in favored.items()}
            elif favored:
                out["turbo"] = {"P": None, "E": None, "favored": dict(favored)}
    except OpError as exc:
        if exc.code == CANCELLED:
            raise
        for key in _LABEL:
            notes.setdefault(key, f"{_LABEL[key]} not read: {exc.message}")
    except GuardRefused as exc:
        for key in _LABEL:
            notes.setdefault(key, f"{_LABEL[key]} not read: the guard refused ({exc.reason.value})")
    except Exception as exc:  # best effort: these reads must never fail the tuning-word read that already succeeded
        for key in _LABEL:
            notes.setdefault(key, f"{_LABEL[key]} not read: {type(exc).__name__}: {exc}")
    return out


def op_tune_read(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    doc = vf_dump_tracked(helper, ctx, args)
    topology = _topology(helper)
    result = dict(doc)
    result["units"] = [dict(u) for u in doc.get("units", [])]
    result["tune"] = _decode_document(result, topology)
    st = _state(helper)
    try:
        policy = _load_policy(helper)
        extras = _read_extras(helper, ctx, policy, topology)
    except OpError as exc:
        if exc.code == CANCELLED:
            raise
        extras = {"fused": None, "turbo": None, "tables": None, "notes": {k: f"{_LABEL[k]} not read: {exc.message}" for k in _LABEL}}
    notes = extras["notes"]
    tables = extras["tables"]
    note = notes.get("turbo")
    with st.lock:
        st.tables, st.tables_note = tables, note
        st.fused, st.turbo = extras["fused"], extras["turbo"]
    result["tables"] = None if tables is None else {k: list(v) for k, v in tables.items()}
    result["tables_note"] = None if tables is not None else (note or "turbo tables not read")
    fused, turbo = extras["fused"], extras["turbo"]
    result["fused"] = None if fused is None else {str(c): v for c, v in fused.items()}  # contract 9 addendum: {first cpu of the unit (string): {word, ratio, v}}
    result["turbo"] = None if turbo is None else {**turbo, "favored": {str(c): r for c, r in turbo["favored"].items()}}  # {P: {ratios, counts}, E: {...}, favored: {cpu (string): ratio}}
    result["extras_notes"] = notes
    result = _fit(result)
    _save_last_read(helper, result)
    return result


# --------------------------------------------------------------------------------------------------------------
# G-123: the last complete read of this boot (tune.last), kept root-owned next to the audit log
# --------------------------------------------------------------------------------------------------------------
def _word_json(w: int) -> dict[str, Any]:
    """The ``vfdump.to_json`` shape of one word (the stored result is patched with it after an apply)."""
    vf = VfWord.from_u32(w)
    return {"word": f"{w:#010x}", "ratio": vf.ratio, "v_target_raw": vf.v_target_raw, "v_target_v": vf.v_target_v, "static": vf.static,
            "offset_raw": vf.offset_raw, "offset_mv": round(vf.offset_mv, 3)}


def _last_store(helper: Helper) -> LastReadStore:
    cfg = helper.cfg
    return LastReadStore(Path(cfg.audit_path).parent / "state", euid=cfg.euid_fn())


def _current_boot(helper: Helper) -> str | None:
    boot = helper.cfg.boot_id_fn()
    return (boot.strip() or None) if isinstance(boot, str) else None


def _log(helper: Helper, text: str) -> None:
    try:
        helper.cfg.log(text)
    except Exception:  # noqa: BLE001 - logging must never fail an op
        pass


def _save_last_read(helper: Helper, result: dict[str, Any]) -> None:
    """Store a *complete* read (all 12 units) with the boot id. Best effort: a store problem is logged, never an error of the read that succeeded."""
    try:
        boot = _current_boot(helper)
        ids = {u.get("id") for u in result.get("units", []) if isinstance(u, dict)}
        if boot is None or result.get("complete") is False or not set(UNITS) <= ids:
            return
        words = _words_of_document(result)
        if not words:
            return
        record = {
            "version": 1, "boot_id": boot, "at": helper.cfg.wall_clock(), "applied_at": None,
            "words": {str(cpu): [f"{w:#010x}" for w in ws] for cpu, ws in words.items()}, "result": result,
        }
        _last_store(helper).save(record)
    except (LastReadError, OSError, ValueError, TypeError) as exc:
        _log(helper, f"last-read not stored: {exc}")


def _boot_record(helper: Helper) -> dict[str, Any] | None:
    """The stored record when it belongs to the current boot (both boot ids known and equal), else ``None``."""
    boot = _current_boot(helper)
    if boot is None:
        return None
    try:
        record = _last_store(helper).load()
    except LastReadError as exc:
        _log(helper, f"last-read not used: {exc}")
        return None
    return record if record is not None and record.get("boot_id") == boot else None


def _record_words(record: Mapping[str, Any]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    try:
        for cpu, ws in record["words"].items():
            if isinstance(ws, list) and ws:
                out[int(cpu)] = [int(w, 16) for w in ws]
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}
    return out


def _restore_last(helper: Helper) -> None:
    """Once per helper process (lazily, on the first tune.* call): this boot's stored read becomes ``helper.last_words`` and the turbo / fused facts of
    the tune state, so ``tune.plan`` works after a helper restart without a new read. A read made in this process wins (nothing is overwritten)."""
    with _STATE_LOCK:
        if getattr(helper, "_last_restored", False):
            return
        helper._last_restored = True  # type: ignore[attr-defined]
        try:
            if getattr(helper, "last_words", None):
                return
            record = _boot_record(helper)
            words = _record_words(record) if record is not None else {}
            if record is None or not words:
                return
            result = record["result"]
            st = _state(helper)
            with st.lock:
                tables = result.get("tables")
                if st.tables is None and isinstance(tables, dict) and all(isinstance(tables.get(k), list) for k in ("e", "p")):
                    st.tables = {k: tuple(int(x) for x in tables[k]) for k in ("e", "p")}
                    st.tables_note = None
                fused = result.get("fused")
                if st.fused is None and isinstance(fused, dict):
                    st.fused = {int(c): dict(v) for c, v in fused.items() if isinstance(v, dict)}
                turbo = result.get("turbo")
                if st.turbo is None and isinstance(turbo, dict):
                    st.turbo = {**turbo, "favored": {int(c): int(r) for c, r in (turbo.get("favored") or {}).items()}}
            _set_last_words(helper, words)
        except Exception as exc:  # noqa: BLE001 - a bad stored record must never break a tune op
            _log(helper, f"last-read not restored: {type(exc).__name__}: {exc}")


def _persist_applied_words(helper: Helper) -> None:
    """After an apply that wrote: the stored record's authoritative ``words`` follow ``helper.last_words`` (the verified read-backs)."""
    try:
        record = _boot_record(helper)
        last = _last_words(helper)
        if record is None or not last:
            return
        for cpu in list(record["words"]):
            if int(cpu) in last:
                record["words"][cpu] = [f"{w:#010x}" for w in last[int(cpu)]]
        record["applied_at"] = helper.cfg.wall_clock()
        _last_store(helper).save(record)
    except (LastReadError, OSError, ValueError, TypeError, KeyError) as exc:
        _log(helper, f"last-read not updated after the apply: {exc}")


def op_tune_last(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``{available: false}``, or ``{available: true, boot_id, at, applied_at, result}`` with ``result`` shaped like ``tune.read``'s. After an apply the
    stored words (the verified read-backs) are overlaid, the decoded copy rebuilt and the accepted comparison redone."""
    _restore_last(helper)
    record = _boot_record(helper)
    if record is None:
        return {"available": False}
    topology = _topology(helper)
    result = record["result"]
    applied_at = record.get("applied_at")
    if applied_at is not None:
        words = _record_words(record)
        for unit in result.get("units", []):
            for cpu in unit.get("cpus", []):
                ws = words.get(cpu.get("cpu"))
                if not ws:
                    continue
                if cpu.get("oc") is not None:
                    cpu["oc"] = _word_json(ws[0])
                for p in cpu.get("points", []):
                    if isinstance(p.get("point"), int) and 0 < p["point"] < len(ws):
                        p.update(_word_json(ws[p["point"]]))
        result["differences"] = []
        result["differences_stale"] = True
        matched, note = accepted_match(helper, result)
        result["accepted_match"], result["accepted_note"] = matched, note
    result["tune"] = _decode_document(result, topology)
    return {"available": True, "boot_id": record["boot_id"], "at": record.get("at"), "applied_at": applied_at, "result": _fit(result)}


# --------------------------------------------------------------------------------------------------------------
# Intent: validation (shape only), canonical form
# --------------------------------------------------------------------------------------------------------------
def _bad(text: str) -> OpError:
    return OpError(BAD_REQUEST, text)


def _int_mv(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or float(value) != int(value):
        raise _bad(f"{what} must be a whole number of millivolts")
    mv = int(value)
    if not -1000 <= mv <= 999:
        raise _bad(f"{what} {mv} mV is outside the V/F word's -1000..999 mV")
    return mv


def _canonical_row(raw: object, scope: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        raise _bad("each row must be an object")
    extra = sorted(set(raw) - {"target", "offset", "v_target_v", "ratio"})
    if extra:
        raise _bad(f"unknown row key(s): {', '.join(map(repr, extra))}")
    target = raw.get("target")
    allowed = ("P", "E") if scope == "simple" else UNITS
    if not isinstance(target, str) or target not in allowed:
        raise _bad(f"row target {target!r} is not one of {', '.join(allowed)} (scope {scope!r})")
    offset = raw.get("offset")
    canon_offset: dict[str, Any] | None = None
    if offset is not None:
        if not isinstance(offset, dict) or set(offset) != {"kind", "mv"}:
            raise _bad("offset must be null or {kind: 'curve', mv: int} or {kind: 'points', mv: {ratio: int}}")
        if offset["kind"] == "curve":
            canon_offset = {"kind": "curve", "mv": _int_mv(offset["mv"], "offset.mv")}
        elif offset["kind"] == "points":
            mvs = offset["mv"]
            if not isinstance(mvs, dict) or not mvs or len(mvs) > 12:
                raise _bad("offset.mv of kind 'points' must be a non-empty object {ratio: mV}")
            canon: dict[str, int] = {}
            for key, value in mvs.items():
                if not isinstance(key, str) or not key.isdigit() or not _RATIO_RANGE[0] <= int(key) <= _RATIO_RANGE[1]:
                    raise _bad(f"offset.mv key {key!r} is not a ratio (1..100)")
                canon[str(int(key))] = _int_mv(value, f"offset.mv[{key}]")
            canon_offset = {"kind": "points", "mv": canon}
        else:
            raise _bad(f"offset.kind {offset['kind']!r} is not 'curve' or 'points'")
    v = raw.get("v_target_v")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0.5 <= v <= 2.0):
        raise _bad("v_target_v must be null or a voltage in volts (0.5..2.0)")
    ratio = raw.get("ratio")
    if ratio is not None and (not _is_int(ratio) or not _RATIO_RANGE[0] <= ratio <= _RATIO_RANGE[1]):
        raise _bad(f"ratio must be null or an integer {_RATIO_RANGE[0]}..{_RATIO_RANGE[1]}")
    if canon_offset is None and v is None and ratio is None:
        return None  # a row that changes nothing is dropped, like an unchanged row of the UI
    return {"target": target, "offset": canon_offset, "v_target_v": None if v is None else float(v), "ratio": ratio}


def _canonical_turbo(raw: object) -> dict[str, list[int]]:
    """``{P: [8 ints], E: [8 ints]}`` (either key may be absent, not both): the bucket ratios of ``0x1AD`` / ``0x650``, bucket 0 = 1 active core."""
    if not isinstance(raw, dict) or not raw or not set(raw) <= {"P", "E"}:
        raise _bad("turbo must be {P: [8 ratios], E: [8 ratios]} (either key may be absent)")
    out: dict[str, list[int]] = {}
    for key, table in raw.items():
        if not isinstance(table, list) or len(table) != 8 or not all(_is_int(r) and _RATIO_RANGE[0] <= r <= _RATIO_RANGE[1] for r in table):
            raise _bad(f"turbo.{key} must be a list of 8 integer ratios {_RATIO_RANGE[0]}..{_RATIO_RANGE[1]}")
        out[key] = list(table)
    return out


def canonical_intent(raw: object, *, rows_only: bool = False) -> dict[str, Any]:
    """The intent in its canonical form, or ``BAD_REQUEST``. ``rows_only`` (profiles.save): a ``{scope, rows}`` intent is the only savable shape."""
    if not isinstance(raw, dict):
        raise _bad("intent must be an object")
    try:
        if len(json.dumps(raw)) > _MAX_INTENT_BYTES:
            raise _bad(f"intent is larger than {_MAX_INTENT_BYTES} bytes")
    except (TypeError, ValueError):
        raise _bad("intent is not JSON") from None
    keys = set(raw)
    if keys and keys <= {"scope", "rows", "turbo"} and keys & {"rows", "turbo"} and ("rows" in keys) == ("scope" in keys):
        if rows_only and "turbo" in keys:
            raise _bad("only a {scope, rows} intent can be saved as a profile")
        out: dict[str, Any] = {}
        turbo = _canonical_turbo(raw["turbo"]) if "turbo" in keys else None
        if "rows" in keys:
            scope, rows = raw["scope"], raw["rows"]
            if scope not in ("simple", "unit"):
                raise _bad("scope must be 'simple' or 'unit'")
            if not isinstance(rows, list) or len(rows) > 32:
                raise _bad("rows must be a list of at most 32 rows")
            canon_rows = [r for r in (_canonical_row(row, scope) for row in rows) if r is not None]
            targets = [r["target"] for r in canon_rows]
            if len(set(targets)) != len(targets):
                raise _bad("a target appears in two rows")
            if not canon_rows and turbo is None:
                raise OpError(BAD_REQUEST, "the intent changes nothing (every row is empty)", {"check": "NOTHING_TO_DO"})
            if canon_rows:
                out = {"scope": scope, "rows": canon_rows}
        if turbo is not None:
            out["turbo"] = turbo
        return out
    if rows_only:
        raise _bad("only a {scope, rows} intent can be saved as a profile")
    if keys == {"profile"}:
        name = raw["profile"]
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise _bad("profile must be a name of 1..64 characters of a-z 0-9 . _ -")
        return {"profile": name}
    if keys == {"reset"}:
        if raw["reset"] != "stock":
            raise _bad("reset must be 'stock'")
        return {"reset": "stock"}
    if keys == {"restore"}:
        if raw["restore"] != "accepted":
            raise _bad("restore must be 'accepted'")
        return {"restore": "accepted"}
    raise _bad("intent must be {scope, rows[, turbo]}, {turbo}, {profile}, {reset: 'stock'} or {restore: 'accepted'}")


# --------------------------------------------------------------------------------------------------------------
# Accepted state (/etc/arltuner/accepted/CURRENT chain) and saved profiles
# --------------------------------------------------------------------------------------------------------------
def _read_accepted_text(helper: Helper, relative: str, limit: int = _ACCEPTED_FILE_MAX) -> str:
    cfg = helper.cfg
    directory = getattr(cfg, "accepted_dir", None)
    if directory is None:
        raise OpError(REFUSED, "no accepted state directory is configured. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"})
    path = Path(directory) / relative
    if cfg.enforce_trust:
        try:
            check_trusted_path(path, want="file", must_exist=True, lstat_fn=cfg.lstat_fn)
        except TrustError as exc:
            raise OpError(REFUSED, f"accepted state untrusted: {exc}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None
    try:
        fd = open_checked(path, write=False, create=False, euid=cfg.euid_fn(), what="accepted state")
    except FileNotFoundError:
        raise OpError(REFUSED, f"no accepted state ({path} is absent). Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None
    except TrustError as exc:
        raise OpError(REFUSED, f"accepted state untrusted: {exc}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise OpError(REFUSED, f"accepted state unreadable: {exc.strerror or exc}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None
    if len(data) > limit:
        raise OpError(REFUSED, f"accepted state untrusted: {path} is larger than {limit} bytes. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"})
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise OpError(REFUSED, f"accepted state invalid: {path} is not UTF-8. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None


def _accepted_chain(helper: Helper) -> list[tuple[str, Profile]]:
    """``[(name, profile)]`` in apply order: the ``profile =`` lines of ``accepted/CURRENT`` (the name is the file stem)."""
    text = _read_accepted_text(helper, "CURRENT")
    out: list[tuple[str, Profile]] = []
    for line in text.splitlines():
        m = re.fullmatch(r"\s*profile\s*=\s*(\S+)\s*", line)
        if not m:
            continue
        rel = m.group(1)
        if not _ACCEPTED_PATH_RE.fullmatch(rel) or ".." in rel or not rel.endswith(".toml"):
            raise OpError(REFUSED, f"accepted CURRENT names an unacceptable profile path {rel!r}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"})
        try:
            out.append((Path(rel).stem, load_profile(_read_accepted_text(helper, rel))))
        except ProfileError as exc:
            raise OpError(REFUSED, f"accepted profile {rel} is invalid: {exc}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None
    if not out:
        raise OpError(REFUSED, "accepted CURRENT lists no profile. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"})
    return out


def _merge_profiles(name: str, chain: Sequence[Profile]) -> Profile:
    """The final state of applying ``chain`` in order: a later core entry overrides the fields it sets, a later point entry replaces the earlier one,
    a later turbo table replaces the earlier one."""
    cores: dict[tuple[str, int], dict[str, int | None]] = {}
    points: dict[tuple[str, int, int], PointSetting] = {}
    turbo: dict[str, tuple[int, ...] | None] = {"e": None, "p": None}
    for prof in chain:
        for c in prof.entries:
            cur = cores.setdefault((c.kind, c.index), {"ratio": None, "oc_voltage_mv": None, "offset_mv": None})
            for key in ("ratio", "oc_voltage_mv", "offset_mv"):
                value = getattr(c, key)
                if value is not None:
                    cur[key] = value
        for p in prof.points:
            points[(p.kind, p.index, p.point)] = p
        if prof.turbo is not None:
            for key in ("e", "p"):
                value = getattr(prof.turbo, key)
                if value is not None:
                    turbo[key] = tuple(value)
    entries = tuple(CoreSetting(kind, index, **vals) for (kind, index), vals in cores.items())  # type: ignore[arg-type]
    tables = TurboTables(e=turbo["e"], p=turbo["p"]) if (turbo["e"] or turbo["p"]) else None
    return Profile(name, entries, tables, tuple(points.values()))


def _profile_store(helper: Helper) -> ProfileStore:
    cfg = helper.cfg
    return ProfileStore(Path(cfg.audit_path).parent / "profiles", euid=cfg.euid_fn(), wall_clock=cfg.wall_clock)


# --------------------------------------------------------------------------------------------------------------
# Intent -> Profile
# --------------------------------------------------------------------------------------------------------------
@dataclass
class Resolved:
    profile: Profile
    refs: dict[tuple[int, int], int]
    warnings: list[str]
    units: list[str]


def _unit_reference(last: Mapping[int, Sequence[int]] | None, cpus: Sequence[int]) -> tuple[int, Sequence[int]] | None:
    if last:
        for cpu in cpus:
            if cpu in last and last[cpu]:
                return cpu, last[cpu]
    return None


def _no_read(unit: str, why: str) -> OpError:
    return OpError(REFUSED, f"{unit}: {why}; run 'Read tuning words' (tune.read) first. Nothing was sent.", {"check": "NO_READ"})


def _check_range(policy: Policy, unit: str, cpus: Sequence[int], row: Mapping[str, Any]) -> None:
    """``REFUSED POLICY_RANGE`` naming the field and the limit, before anything else is computed."""
    kind = _kind_of(unit)

    def refuse(text: str) -> OpError:
        return OpError(REFUSED, f"{text}. Nothing was sent.", {"check": "POLICY_RANGE"})

    offset = row["offset"]
    if offset is not None:
        values = [offset["mv"]] if offset["kind"] == "curve" else list(offset["mv"].values())
        floor_raw = min(policy.offset_floor_raw(c) for c in cpus)
        ceil_raw = VfWord.mv_to_offset_raw(policy.offset_ceiling_mv)
        for mv in values:
            raw = VfWord.mv_to_offset_raw(mv)
            if not floor_raw <= raw <= ceil_raw:
                raise refuse(
                    f"{unit}: offset {mv:+d} mV (raw {raw}) is outside the policy window {floor_raw * 1000 / 1024:+.1f}..{ceil_raw * 1000 / 1024:+.1f} mV "
                    f"(effective_offset_floor_mv {policy.effective_offset_floor_mv}, offset_ceiling_mv {policy.offset_ceiling_mv})"
                )
    v = row["v_target_v"]
    if v is not None:
        mv = round(v * 1000)
        cap = policy.v_target_max_mv[kind]
        if cap is not None and VfWord.v_to_raw(mv / 1000) > VfWord.v_to_raw(cap / 1000):
            raise refuse(f"{unit}: OC-point voltage {mv} mV is above the policy v_target_max_mv[{kind}] = {cap} mV")
    ratio = row["ratio"]
    if ratio is not None:
        cap = policy.ratio_max[kind]
        if cap is not None and ratio > cap:
            raise refuse(f"{unit}: ratio {ratio} is above the policy ratio_max[{kind}] = {cap}")


def _rows_to_profile(helper: Helper, intent: Mapping[str, Any], policy: Policy, topology: Topology, name: str) -> Resolved:
    last = _last_words(helper)
    warnings: list[str] = []
    cores: dict[tuple[str, int], dict[str, int | None]] = {}
    points: dict[tuple[str, int, int], PointSetting] = {}
    refs: dict[tuple[int, int], int] = {}
    units: list[str] = []
    asked_ratio: dict[str, int] = {}
    for row in intent.get("rows", ()):
        targets = GROUPS[row["target"]] if row["target"] in GROUPS else (row["target"],)
        for unit in targets:
            cpus = unit_cpus(topology, unit)
            _check_range(policy, unit, cpus, row)
            core = topology.by_cpu(cpus[0])
            known = {c: last[c] for c in cpus if last and c in last and last[c]}
            vals: dict[str, int | None] = {"ratio": row["ratio"], "oc_voltage_mv": None if row["v_target_v"] is None else round(row["v_target_v"] * 1000), "offset_mv": None}
            offset = row["offset"]
            if offset is not None and offset["kind"] == "curve":
                vals["offset_mv"] = offset["mv"]
            touched = False
            if any(v is not None for v in vals.values()):
                cores[(core.kind, core.index)] = vals
                touched = True
                if row["ratio"] is not None:
                    asked_ratio[core.kind] = max(asked_ratio.get(core.kind, 0), row["ratio"])
                for c, ws in known.items():
                    refs[(c, 0)] = ws[0]
            if offset is not None and offset["kind"] == "points":
                if not known:
                    raise _no_read(unit, "per-point offsets are addressed by ratio and no words of this unit were read")
                words = known[next(iter(known))]
                by_ratio = {VfWord.from_u32(w).ratio: n for n, w in enumerate(words[1 : len(_POINT_RATIOS[core.kind]) + 1], start=1)}  # the OC point (P2 9 / 7) is not a fused point
                missing = [r for r in offset["mv"] if int(r) not in by_ratio]
                for r, mv in offset["mv"].items():
                    n = by_ratio.get(int(r))
                    if n is None:
                        continue
                    points[(core.kind, core.index, n)] = PointSetting(core.kind, core.index, n, mv, int(r))
                    touched = True
                    for c, ws in known.items():
                        if len(ws) > n:
                            refs[(c, n)] = ws[n]
                if missing:
                    have = ", ".join(str(r) for r in sorted(by_ratio))
                    text = f"{unit} has no V/F point at ratio {', '.join(missing)} (points of the last read: {have})"
                    if intent["scope"] == "unit":
                        raise OpError(BAD_REQUEST, text)
                    warnings.append(text + "; skipped")
            if touched and unit not in units:
                units.append(unit)
    turbo = _turbo_tables(helper, asked_ratio, warnings)
    if "turbo" in intent:
        turbo = _explicit_turbo(helper, intent["turbo"], turbo, policy, warnings)
    try:
        entries = tuple(CoreSetting(k, i, **v) for (k, i), v in cores.items())  # type: ignore[arg-type]
        profile = Profile(name, entries, turbo, tuple(points.values()))  # G-119: a turbo-only plan has no entries and no points
    except ProfileError as exc:
        raise OpError(BAD_REQUEST, f"intent is not a valid profile: {exc}") from None
    return Resolved(profile, refs, warnings, units)


def _turbo_tables(helper: Helper, asked: Mapping[str, int], warnings: list[str]) -> TurboTables | None:
    """A ratio above a table's top bucket needs the table raised (T-023, D-017): bucket b becomes max(b, ratio). Never lowers a bucket, never invents a
    table: without a table read there is no table write and the planner's capped-raise warning explains the consequence."""
    if not asked:
        return None
    st = _state(helper)
    with st.lock:
        tables, note = st.tables, st.tables_note
    new: dict[str, tuple[int, ...]] = {}
    for kind, ratio in asked.items():
        key = kind.lower()
        if tables is None or key not in tables:
            warnings.append(f"{kind} turbo table (MSR {'0x650' if kind == 'E' else '0x1AD'}) was not read this boot ({note or 'no tune.read yet'}): no table write is planned, a raise above the table maximum will be capped")
            continue
        table = tables[key]
        if ratio > max(table):
            new[key] = tuple(max(b, ratio) for b in table)
            warnings.append(Warn("TURBO_RAISE", f"the {kind} turbo table is raised to {ratio} in every bucket below it (package-wide, MSR {'0x650' if kind == 'E' else '0x1AD'})"))
    return TurboTables(e=new.get("e"), p=new.get("p")) if new else None


def _explicit_turbo(helper: Helper, asked: Mapping[str, Sequence[int]], derived: TurboTables | None, policy: Policy, warnings: list[str]) -> TurboTables:
    """The tables the intent names (``turbo: {P, E}``), replacing what a row's OC ratio would have derived for that kind. Range-checked against the policy
    (``REFUSED POLICY_RANGE``); the order rule F5 is checked by :func:`_check_turbo_order` on the finished profile."""
    st = _state(helper)
    with st.lock:
        tables = st.tables
    out = {"e": None if derived is None else derived.e, "p": None if derived is None else derived.p}
    for kind, ratios in asked.items():
        cap = policy.ratio_max[kind]
        if cap is not None and max(ratios) > cap:
            raise OpError(REFUSED, f"turbo.{kind}: ratio {max(ratios)} is above the policy ratio_max[{kind}] = {cap}. Nothing was sent.", {"check": "POLICY_RANGE"})
        msr = "0x650" if kind == "E" else "0x1AD"
        current = None if tables is None else tables.get(kind.lower())
        out[kind.lower()] = tuple(ratios)
        if current is None:
            warnings.append(f"the {kind} turbo table (MSR {msr}) was not read this boot: run 'Read tuning words' so the apply can compare it")
        elif any(n > o for n, o in zip(ratios, current)):
            raised = ", ".join(str(k + 1) for k, (n, o) in enumerate(zip(ratios, current)) if n > o)
            warnings.append(Warn("TURBO_RAISE", f"the {kind} turbo table ({msr}) is RAISED in bucket(s) {raised} (package-wide)"))
    return TurboTables(e=out["e"], p=out["p"])


def _reset_profile(helper: Helper, topology: Topology) -> Resolved:
    last = _last_words(helper)
    cores: dict[tuple[str, int], dict[str, int | None]] = {}
    points: dict[tuple[str, int, int], PointSetting] = {}
    refs: dict[tuple[int, int], int] = {}
    units: list[str] = []
    warnings: list[str] = []
    for unit in UNITS:
        cpus = unit_cpus(topology, unit)
        ref = _unit_reference(last, cpus)
        if ref is None:
            raise _no_read(unit, "reset needs the words in force and none of this unit's CPUs were read")
        _, words = ref
        core = topology.by_cpu(cpus[0])
        changed = False
        if VfWord.from_u32(words[0]).offset_raw != 0:
            cores[(core.kind, core.index)] = {"ratio": None, "oc_voltage_mv": None, "offset_mv": 0}
            changed = True
        for n, w in enumerate(words[1:], start=1):
            vf = VfWord.from_u32(w)
            if vf.offset_raw != 0:
                points[(core.kind, core.index, n)] = PointSetting(core.kind, core.index, n, 0, vf.ratio)
                changed = True
        if changed:
            units.append(unit)
            for c in cpus:
                if last and c in last:
                    for n, w in enumerate(last[c]):
                        refs[(c, n)] = w
    if not cores and not points:
        raise OpError(BAD_REQUEST, "every offset of the last read is already 0: nothing to reset", {"check": "NOTHING_TO_DO"})
    warnings.append(
        "reset zeroes the OC-word offset and every V/F point offset; the OC ratio, the OC-point V target and the turbo tables are not changed "
        "(a zero field cannot be written back; a cold boot restores them)"
    )
    return Resolved(Profile("reset-stock", tuple(CoreSetting(k, i, **v) for (k, i), v in cores.items()), None, tuple(points.values())), refs, warnings, units)  # type: ignore[arg-type]


def _profile_resolved(helper: Helper, profile: Profile, topology: Topology, warnings: list[str]) -> Resolved:
    """A ready-made Profile (accepted state): no words are assumed, ``expect_ratio`` of every point guards the write."""
    units: list[str] = []
    for t in resolve_targets(profile, topology) if profile.entries else ():
        u = unit_of_cpu(topology, t.cpu)
        if u not in units:
            units.append(u)
    for p in resolve_point_targets(profile, topology):
        u = unit_of_cpu(topology, p.cpu)
        if u not in units:
            units.append(u)
    if _last_words(helper) is None:
        warnings.append("no tune.read this boot: 'before' is unknown; the apply re-reads every word and the points' expect_ratio guards the writes")
    return Resolved(profile, {}, warnings, units)


def resolve_intent(helper: Helper, intent: dict[str, Any], policy: Policy, topology: Topology) -> Resolved:
    if "rows" in intent or "turbo" in intent:
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()[:8]
        return _rows_to_profile(helper, intent, policy, topology, f"tune-{digest}")
    if "reset" in intent:
        return _reset_profile(helper, topology)
    if "restore" in intent:
        chain = _accepted_chain(helper)
        merged = _merge_profiles("accepted", [p for _, p in chain])
        return _profile_resolved(helper, merged, topology, [f"restore merges the accepted chain in order: {', '.join(n for n, _ in chain)}"])
    name = intent["profile"]
    try:
        chain = _accepted_chain(helper)
    except OpError:
        chain = []
    for stem, prof in chain:
        if stem == name:
            return _profile_resolved(helper, prof, topology, [f"accepted profile {name}"])
    try:
        record = _profile_store(helper).load(name)
    except ProfileStoreError as exc:
        raise OpError(BAD_REQUEST if exc.code == "NOT_FOUND" else REFUSED, exc.message, {"check": "PROFILE_" + exc.code}) from None
    saved = canonical_intent(record["intent"], rows_only=True)
    resolved = _rows_to_profile(helper, saved, policy, topology, name)
    resolved.warnings.insert(0, f"saved profile {name}")
    return resolved


# --------------------------------------------------------------------------------------------------------------
# before / after, predicted steps
# --------------------------------------------------------------------------------------------------------------
def _before_after(profile: Profile, topology: Topology, last: Mapping[int, Sequence[int]] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """``{unit: {point: {ratio, offset_mv, v_target_v}}}`` for the words the plan writes; point ``"0"`` is the OC word. ``before`` is ``null`` for a
    word that was not read; a field that reads 0 (unset) is ``null``."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    def show(vf: VfWord) -> dict[str, Any]:
        d = _decode(vf)
        return {"ratio": d["ratio"], "offset_mv": d["offset_mv"], "v_target_v": d["v_target_v"]}

    for setting in profile.expand(topology):
        unit = unit_of_cpu(topology, setting.cpu)
        ref = _unit_reference(last, setting.cpus)
        cur = VfWord.from_u32(ref[1][0]) if ref else None
        changes: dict[str, int] = {}
        if setting.ratio is not None:
            changes["ratio"] = setting.ratio
        if setting.oc_voltage_mv is not None:
            changes["v_target_raw"] = VfWord.v_to_raw(setting.oc_voltage_mv / 1000)
        if setting.offset_mv is not None:
            changes["offset_raw"] = VfWord.mv_to_offset_raw(setting.offset_mv)
        goal = (cur or VfWord(0, 0, False, 0)).with_(**changes)
        kind = _kind_of(unit)
        if goal.ratio:
            goal = kept_word(kind, goal)  # G-124: no OC point (ratio <= the top fused ratio) = the hardware keeps no V target, whatever the offset
        elif goal.v_target_raw:
            # a cold word (ratio field 0): the planner fills the ratio from 0x1C[7:0] when the plan sets an OC voltage (P 55/57 <= 57: no OC point; E 48 > 46: an OC
            # point) and caps it at the top fused ratio otherwise -- decide the target on that filled ratio, show the field as read (0 = unset)
            fill = top_fused_ratio(kind) + (1 if kind == "E" and setting.oc_voltage_mv is not None else 0)
            goal = kept_word(kind, goal.with_(ratio=fill)).with_(ratio=0)
        before.setdefault(unit, {})["0"] = None if cur is None else show(cur)
        after.setdefault(unit, {})["0"] = show(goal)
    for setting in profile.expand_points(topology):
        unit = unit_of_cpu(topology, setting.cpu)
        ref = _unit_reference(last, setting.cpus)
        cur = VfWord.from_u32(ref[1][setting.point]) if ref and len(ref[1]) > setting.point else None
        goal = (cur or VfWord(setting.expect_ratio or 0, 0, False, 0)).with_(offset_raw=VfWord.mv_to_offset_raw(setting.offset_mv))
        before.setdefault(unit, {})[str(setting.point)] = None if cur is None else show(cur)
        after.setdefault(unit, {})[str(setting.point)] = show(goal)
    return before, after


# --------------------------------------------------------------------------------------------------------------
# SPEC-v3 section 9: predictions (F4 clamp, F6 VccIA, F8 warnings) from the newest baseline curve
# --------------------------------------------------------------------------------------------------------------
_PLATFORM = default_platform()
_POINT_RATIOS = {"P": tuple(_PLATFORM.p_point_ratios), "E": tuple(_PLATFORM.e_point_ratios)}  # F1 [MEASURED lnx 2026-10-02]
_PLATFORM_TURBO = {  # stock tables [MEASURED lnx 2026-09-29], used only when tune.read could not read the live ones
    "P": list(turbo_bytes(_PLATFORM.turbo_defaults[0x1AD])),
    "E": list(turbo_bytes(_PLATFORM.turbo_defaults[0x650])),
}
_P_FAVORED_STOCK = 55  # the 0x1C low byte of a non-favored P-core [MEASURED win 2026-10-01] (0x3737); favored cores read 57 (0x3939)


def _baseline_record(helper: Helper) -> tuple[dict[str, Any] | None, str | None]:
    """The newest curve record flagged ``baseline`` of the measurement store, or ``(None, why)``."""
    from .ops_measure import _wiring  # imported lazily: both modules import .ops, neither imports the other at module level

    try:
        store = _wiring(helper).store
        for row in store.summaries():
            if row.get("baseline"):
                return store.get(row["id"]), None
    except OpError as exc:
        return None, exc.message
    except Exception as exc:  # StoreError and friends: a prediction is optional
        return None, f"{type(exc).__name__}: {exc}"
    return None, None


@dataclass
class _UnitModel:
    unit: str
    kind: str
    cpus: tuple[int, ...]
    ratios: tuple[int, ...]  # the fused point ratios (index n-1 = point n)
    cur_offsets: dict[int, float]  # ratio -> mV of the words last read (0 where nothing was read)
    new_offsets: dict[int, float]  # ... after the plan
    baseline: dict[int, float | None] | None
    oc_ratio: int | None = None  # the plan's OC ratio for this unit
    oc_v: float | None = None  # the plan's OC-point voltage target (V)
    cur_oc_ratio: int | None = None  # the ratio of the OC word of the last read (None = not read / 0)
    touched: bool = False


def _unit_models(
    profile: Profile, topology: Topology, last: Mapping[int, Sequence[int]] | None, record: dict[str, Any] | None, fused: Mapping[int, Mapping[str, Any]] | None
) -> dict[str, _UnitModel]:
    settings = {unit_of_cpu(topology, s.cpu): s for s in profile.expand(topology)}
    point_settings: dict[str, dict[int, Any]] = {}
    for ps in profile.expand_points(topology):
        point_settings.setdefault(unit_of_cpu(topology, ps.cpu), {})[ps.point] = ps
    models: dict[str, _UnitModel] = {}
    for unit in UNITS:
        kind = _kind_of(unit)
        cpus = unit_cpus(topology, unit)
        ref = _unit_reference(last, cpus)
        known = len(_POINT_RATIOS[kind])
        if ref is not None:
            words = [VfWord.from_u32(w) for w in ref[1][1 : known + 1]]  # past the table = the OC point (P2 9 / 7), not a fused point
            ratios = tuple(w.ratio for w in words)
            cur = {w.ratio: w.offset_mv for w in words}
        else:
            ratios, cur = _POINT_RATIOS[kind], {r: 0.0 for r in _POINT_RATIOS[kind]}
        new = dict(cur)
        st = settings.get(unit)
        explicit = point_settings.get(unit, {})
        if st is not None and st.offset_mv is not None:  # "whole curve": one offset on every point the plan does not set itself
            for n, r in enumerate(ratios, start=1):
                if n not in explicit:
                    new[r] = float(st.offset_mv)
        for n, ps in explicit.items():
            if 1 <= n <= len(ratios):
                new[ratios[n - 1]] = float(ps.offset_mv)
        baseline = None
        if record is not None:
            pts = ((record.get("units") or {}).get(unit) or {}).get("points") or []
            measured = {p["ratio"]: p["vid_v"] for p in pts if p.get("status") == "ok" and p.get("vid_v") is not None}
            fused_v = ((fused or {}).get(cpus[0]) or {}).get("v")
            if measured:
                baseline = complete_baseline(ratios, measured, fused_v)
        m = _UnitModel(unit, kind, cpus, ratios, cur, new, baseline, touched=st is not None or bool(explicit))
        if ref is not None:
            m.cur_oc_ratio = VfWord.from_u32(ref[1][0]).ratio or None
        if st is not None:
            m.oc_ratio = st.ratio
            m.oc_v = None if st.oc_voltage_mv is None else st.oc_voltage_mv / 1000
        models[unit] = m
    return models


def _allowed_ratios(m: _UnitModel, turbo: Mapping[str, Any] | None, plan: bool) -> tuple[int, int]:
    """(1-2 active cores, all cores): the highest ratio the unit is allowed at, from the live turbo tables (``0x1AD`` / ``0x650``) and the favored word (``0x1C``);
    with ``plan`` the plan's OC ratio raises both (a ratio above the table raises the table, ``_turbo_tables``). Stock tables when none were read."""
    ratios = ((turbo or {}).get(m.kind) or {}).get("ratios") or _PLATFORM_TURBO[m.kind]
    light, full = ratios[0], ratios[-1]
    if m.kind == "P":
        fav = ((turbo or {}).get("favored") or {}).get(m.cpus[0])
        light = min(light, fav if fav else (57 if m.unit in ("P0", "P3") else _P_FAVORED_STOCK))
    if plan and m.oc_ratio is not None:
        light, full = max(light, m.oc_ratio), max(full, m.oc_ratio)
    return light, full


def _point_view(curve: Sequence[CurvePoint]) -> dict[str, Any]:
    return {
        str(p.index): {
            "ratio": p.ratio, "base_v": None if p.base_v is None else round(p.base_v, 4), "offset_mv": round(p.offset_mv, 3),
            "effective_v": None if p.effective_v is None else round(p.effective_v, 4), "clamped_by": p.clamped_by,
        }
        for p in curve
    }


def _predict(
    helper: Helper, profile: Profile, topology: Topology, last: Mapping[int, Sequence[int]] | None, before: dict[str, Any], after: dict[str, Any], warnings: list[str]
) -> dict[str, Any]:
    """Fill ``effective_v`` / ``clamped_by`` into the ``before`` / ``after`` points the plan writes and return ``{predicted, vccia}`` (contract 9 addendum).
    ``effective_v`` = baseline VID + offset, then the F4 clamp up to V(n-1). Without a baseline curve both are null and one warning says so."""
    st = _state(helper)
    with st.lock:
        fused, turbo = st.fused, st.turbo
    record, why = _baseline_record(helper)
    models = _unit_models(profile, topology, last, record, fused)
    if record is None:
        warnings.append(Warn("NO_BASELINE", "measure a stock baseline for predictions" + (f" (the baseline curve could not be loaded: {why})" if why else " (no curve is flagged baseline)")))
    elif last is None:
        warnings.append("no tune.read this boot: the current offsets of every point are assumed to be 0 in the predictions")
    predicted: dict[str, Any] = {}
    curves_new: dict[str, list[CurvePoint]] = {}
    curves_cur: dict[str, list[CurvePoint]] = {}
    for unit, m in models.items():
        if m.baseline is None:
            continue
        curves_cur[unit] = effective_curve(m.ratios, m.baseline, m.cur_offsets)
        curves_new[unit] = effective_curve(m.ratios, m.baseline, m.new_offsets)
        if m.touched:
            predicted[unit] = _point_view(curves_new[unit])
    # ---- the written points get their numbers ----
    for side, curves in ((before, curves_cur), (after, curves_new)):
        for unit, items in side.items():
            curve = curves.get(unit)
            for point, entry in items.items():
                if entry is None:
                    continue
                n = int(point)
                if n == 0:
                    entry["effective_v"], entry["clamped_by"] = entry.get("v_target_v"), None
                    oc_model = models.get(unit)
                    if curve is not None and oc_model is not None and entry.get("v_target_v") is not None and (entry.get("ratio") or 0) > max(oc_model.ratios):
                        held = vfmodel.oc_point_voltage(curve, entry["v_target_v"])  # G-120 fix 3: max(target, V(top fused point))
                        if held is not None:
                            entry["effective_v"], entry["clamped_by"] = round(held[0], 4), (len(curve) if held[1] else None)
                elif curve is not None and 1 <= n <= len(curve):
                    ev = curve[n - 1].effective_v
                    entry["effective_v"], entry["clamped_by"] = (None if ev is None else round(ev, 4)), curve[n - 1].clamped_by
                else:
                    entry["effective_v"], entry["clamped_by"] = None, None
    # ---- warnings F4 / F8 ----
    for unit, m in models.items():
        if not m.touched:
            continue
        new, cur = curves_new.get(unit), curves_cur.get(unit)
        if new is not None and cur is not None:
            for p, c in zip(new, cur):
                # G-120 fix 2: only a point the plan newly clamps is worth a warning. A point clamped at stock already (the 56/57x of a core that cannot reach them sit at the
                # voltage of the point below) follows that point when the plan moves it: no new fact, and for a point the plan does not set an "offset +0.0 clamped" line.
                newly = c.clamped_by is None
                if newly and p.clamped_by is not None and p.effective_v is not None and p.base_v is not None:
                    landed = (p.effective_v - p.base_v) * 1000
                    warnings.append(Warn(
                        "CLAMPED",
                        f"{unit} point {p.index} ({p.ratio}x): offset {p.offset_mv:+.1f} mV is clamped up to the voltage of point {p.clamped_by} "
                        f"(the hardware keeps V(n) >= V(n-1)): effective {landed:+.1f} mV = {p.effective_v:.3f} V [MEASURED lnx 2026-10-02, F4]",
                    ))
        oc_ratio = m.oc_ratio or m.cur_oc_ratio
        if new is not None and m.oc_v is not None and oc_ratio and oc_ratio > max(m.ratios):  # G-120 fix 2: an OC-point target below the top fused point is held up to it
            held = vfmodel.oc_point_voltage(new, m.oc_v)
            if held is not None and held[1]:
                top = new[-1]
                warnings.append(Warn(
                    "CLAMPED",
                    f"{unit} OC point ({oc_ratio}x): the adaptive target {m.oc_v:.4f} V is below the voltage of the top fused point ({top.ratio}x, point {top.index}) "
                    f"{held[0]:.4f} V and is held up to it (the hardware keeps V(n) >= V(n-1)): effective {held[0]:.4f} V [MEASURED lnx 2026-10-11, F4]",
                ))
        if m.kind == "E":
            at40 = m.new_offsets.get(vfmodel.E_NO_OFFSET_RATIO, 0.0)
            if at40 != 0.0 and at40 != m.cur_offsets.get(vfmodel.E_NO_OFFSET_RATIO, 0.0):
                warnings.append(Warn("E40_OFFSET", f"{unit}: an offset of {at40:+.1f} mV on the 40x point (point 5): never offset E point 5, -20 mV there caused OCCT errors [MEASURED lnx 2026-10-02, F8]"))
            if m.oc_v is not None and m.oc_v > vfmodel.E_WHEA_V:
                warnings.append(Warn("E_TARGET_WHEA", f"{unit}: E-core voltage target {m.oc_v:.3f} V is above ~1.2 V: WHEA reports are known above it [COMMUNITY skatterbencher.com/2025/02/14, F8]"))
            elif new is not None:
                hi = max((p.effective_v for p in new if p.effective_v is not None), default=None)
                if hi is not None and hi > vfmodel.E_WHEA_V:
                    warnings.append(Warn("E_TARGET_WHEA", f"{unit}: the predicted E-core voltage {hi:.3f} V is above ~1.2 V: WHEA reports are known above it [COMMUNITY, F8]"))
    # ---- F6: VccIA ----
    vccia = _predict_vccia(models, curves_new, curves_cur, turbo)
    if vccia is not None:
        warnings.append(
            "VccIA is a prediction (highest allowed VID of any unit or the ring + ~0.105 V; a hypothesis on 3+ points [MEASURED lnx 2026-10-03]): "
            "lowering a unit that does not set the maximum does not lower Vcore"
        )
    return {"predicted": predicted if record is not None else None, "vccia": vccia}


def _vccia_scenario(models: Mapping[str, _UnitModel], curves: Mapping[str, list[CurvePoint]], turbo: Mapping[str, Any] | None, which: int, plan: bool) -> dict[str, Any] | None:
    rows: list[tuple[str, int, float | None]] = []
    for unit, m in models.items():
        curve = curves.get(unit)
        if curve is None:
            continue
        ratio = _allowed_ratios(m, turbo, plan)[which]
        oc = (m.oc_ratio, m.oc_v) if plan and m.oc_ratio is not None and m.oc_v is not None else None
        rows.append((unit, ratio, voltage_at(curve, ratio, oc)))
    out = predict_vccia(rows)
    if out is None:
        return None
    best = {k: max((r for r in rows if r[0].startswith(k) and r[2] is not None), key=lambda r: r[2], default=None) for k in ("P", "E")}  # type: ignore[arg-type,return-value]
    out["contributors"] = {  # the unit of each group with the highest allowed VID, and the ring floor
        **{k: (None if r is None else {"unit": r[0], "ratio": r[1], "v": round(r[2], 4)}) for k, r in best.items()},
        "ring": {"unit": "ring", "ratio": 40, "v": vfmodel.RING_V},
    }
    return out


def _predict_vccia(models: Mapping[str, _UnitModel], new: Mapping[str, list[CurvePoint]], cur: Mapping[str, list[CurvePoint]], turbo: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """F6 for the all-core bucket (the one that matters for sustained load: 55x points set VccIA for every multi-core load, 57x only for 1-2 active cores), plus the
    1-2 core case as ``light_load`` and the current state as ``before_v``."""
    full = _vccia_scenario(models, new, turbo, 1, True)
    if full is None:
        return None
    light = _vccia_scenario(models, new, turbo, 0, True)
    was = _vccia_scenario(models, cur, turbo, 1, False)
    return {
        "predicted_v": full["predicted_v"], "set_by": full["set_by"], "label": "prediction", "active_cores": "all", "margin_v": full["margin_v"],
        "contributors": full["contributors"], "before_v": None if was is None else was["predicted_v"],
        "light_load": None if light is None else {"predicted_v": light["predicted_v"], "set_by": light["set_by"], "active_cores": "1-2"},
    }


def _predicted_steps(profile: Profile, topology: Topology) -> tuple[list[dict[str, Any]], int, int]:
    """The write steps the plan will contain, in the planner's order (conditional 0x15, point offsets, per-core triples, turbo tables), without data
    words (those need the hardware baselines and are built at apply time). Returns ``(listed steps, total writes, read-only requests)``."""
    targets = resolve_targets(profile, topology) if profile.entries else ()
    pts = resolve_point_targets(profile, topology)
    steps: list[dict[str, Any]] = [{"kind": StepKind.WRITE_PERCORE_ENABLE.value, "conditional": True}] if (targets or pts) else []  # G-117: none for a table-only plan
    for p in pts:
        steps.append({"kind": StepKind.WRITE_VF_POINT.value, "unit": unit_of_cpu(topology, p.cpu), "cpu": p.cpu, "point": p.point, "offset_mv": p.offset_mv})
    for t in targets:
        u = unit_of_cpu(topology, t.cpu)
        # G-124 (as G-122 in the planner): a target that sets no OC ratio sends the 0x11 alone -- no 0x1D (favored) and no 0x27 (granular)
        kinds = (StepKind.WRITE_VF, StepKind.WRITE_FAVORED, StepKind.WRITE_GRANULAR) if t.ratio is not None else (StepKind.WRITE_VF,)
        for kind in kinds:
            steps.append({"kind": kind.value, "unit": u, "cpu": t.cpu, "point": 0})
    if profile.turbo is not None:
        for key in ("e", "p"):
            if getattr(profile.turbo, key) is not None:
                steps.append({"kind": StepKind.WRITE_MSR.value, "table": key})
    reads = (1 if (targets or pts) else 0) + 4 * len(targets) + sum(1 for t in targets if t.oc_voltage_mv is not None) + len(pts)
    total = len(steps)
    listed = [{"n": i, **s} for i, s in enumerate(steps[:_MAX_STEPS_LISTED], start=1)]
    return listed, total, reads


# --------------------------------------------------------------------------------------------------------------
# tune.plan
# --------------------------------------------------------------------------------------------------------------
def _validate_tune_plan(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"intent"})
    if "intent" not in args:
        raise _bad("tune.plan needs an 'intent'")
    return {"intent": canonical_intent(args["intent"])}


_REFUSAL_CHECK = {"CHECK_FAILED": "POLICY_RANGE"}


def _preflight_error(exc: PreflightError) -> OpError:
    if isinstance(exc, NeedsHigherLimit):
        return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": "SESSION_LIMIT"})
    if isinstance(exc, NeedsBootBudget):
        return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": "BOOT_LIMIT"})
    if isinstance(exc, NeedsRamp):
        return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": "STEP_LIMIT"})
    if isinstance(exc, PlanRefused):
        first = exc.refusals[0]
        return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": _REFUSAL_CHECK.get(first.reason, first.reason)})
    return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": "PREFLIGHT"})


def _check_turbo_order(profile: Profile) -> None:
    """F5: bucket ratios must not RISE with the active-core count. ``REFUSED TURBO_ORDER`` before the planner (which would refuse the same table later as a
    generic policy failure). Only a profile can carry a table (the contract 9 row intent has no table field): accepted / saved profiles and ``restore``."""
    if profile.turbo is None:
        return
    for key, label in (("p", "P (MSR 0x1AD)"), ("e", "E (MSR 0x650)")):
        table = getattr(profile.turbo, key)
        if table is None:
            continue
        bad = check_turbo_order(table)
        if bad is not None:
            raise OpError(
                REFUSED,
                f"the {label} turbo table is not non-increasing: bucket {bad + 1} ({table[bad]}x) is above bucket {bad} ({table[bad - 1]}x); more active cores must never raise the ratio "
                f"[MEASURED lnx 2026-10-01, F5]. Nothing was sent.",
                {"check": "TURBO_ORDER", "table": key.upper(), "bucket": bad, "ratios": list(table)},
            )


# G-125: the GUI plan's own caveats, worded from what the hardware showed (docs/knowledge/tuning-results.md); the planner's constants stay as the CLI prints them.
_RAISE_NOTE = (
    "this plan RAISES a ratio, V target or offset: OC ratio 48 and an adaptive target above the fused curve were accepted and read back on this machine "
    "[MEASURED lnx 2026-10-11]; the numbers come from the reviewed policy (D-016); take a stability check between steps"
)
_POINT_NOTE = (
    "per-point offsets change one V/F point (0x11 with P2 = the point) and read back as written [MEASURED lnx 2026-10-11]; the hardware keeps V(n) >= V(n-1) "
    "[MEASURED lnx 2026-10-11, F4]: arltuner models it (CLAMPED warnings) but cannot read the lower fused voltages"
)


def _words_assumed(last: Mapping[int, Sequence[int]] | None, topology: Topology, targets: Sequence[Any], points: Sequence[Any]) -> bool:
    """G-125: the plan assumed words only when a unit it writes has no CPU in ``last`` (an E cluster is read on one CPU, the others share its words)."""
    cpus_by_unit: dict[str, list[int]] = {}
    for item in (*targets, *points):
        cpus_by_unit.setdefault(unit_of_cpu(topology, item.cpu), []).append(item.cpu)
    return any(not any(c in (last or {}) for c in unit_cpus(topology, unit)) for unit in cpus_by_unit)


def op_tune_plan(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    intent = args["intent"]
    topology = _topology(helper)
    policy = _load_policy(helper)
    resolved = resolve_intent(helper, intent, policy, topology)
    profile = resolved.profile
    _check_turbo_order(profile)
    used, budget = helper.budget.snapshot()
    ops_left = None if used is None or budget is None else max(budget - used, 0)
    try:
        targets = preflight(profile, topology, policy, boot_ops_used=used or 0)
    except PreflightError as exc:
        raise _preflight_error(exc) from None
    except (PlanError, ProfileError) as exc:
        raise OpError(BAD_REQUEST, str(exc)) from None
    points = resolve_point_targets(profile, topology)
    tables = sum(1 for key in ("e", "p") if profile.turbo is not None and getattr(profile.turbo, key) is not None)
    ops_needed = count_operations(targets, None, tables, len(points)) if (targets or points or tables) else 0  # G-117: a table-only plan is plain-MSR writes only
    last = _last_words(helper)
    before, after = _before_after(profile, topology, last)
    steps, writes, reads = _predicted_steps(profile, topology)
    warnings = [_POINT_NOTE if w == POINT_CAVEAT else w for w in plan_warnings(targets, policy, points)]
    warnings.extend(resolved.warnings)
    if any(t.ratio is not None or t.oc_voltage_mv is not None for t in targets):
        warnings.append(_RAISE_NOTE)
    for unit, items in after.items():
        for point, now in items.items():
            was = (before.get(unit) or {}).get(point)
            if now["offset_mv"] is not None and now["offset_mv"] > 0 and (was is None or was["offset_mv"] <= 0):
                warnings.append(Warn("POSITIVE_OFFSET", f"positive offset {now['offset_mv']:+.1f} mV on {unit} {'OC word' if point == '0' else 'point ' + point}"))
    prediction = _predict(helper, profile, topology, last, before, after, warnings)
    warnings.append(REVERT_NOTE)
    nonce = "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(6))
    canon = json.dumps(
        {"profile": _profile_canon(profile), "refs": sorted(f"{c}:{p}:{w}" for (c, p), w in resolved.refs.items()), "nonce": nonce},
        sort_keys=True,
    )
    plan_id = hashlib.sha256(canon.encode()).hexdigest()
    _state(helper).plans.add(StoredPlan(plan_id, nonce, helper.cfg.monotonic(), profile, resolved.refs, ops_needed))
    result = {
        "plan_id": plan_id,
        "nonce": nonce,
        "name": profile.name,
        "steps": steps,
        "writes": writes,
        "reads": reads,
        "ops_needed": ops_needed,
        "ops_left": ops_left,
        "expires_s": int(PLAN_EXPIRES_S),
        "units": resolved.units,
        "before": before,
        "after": after,  # per written point also effective_v / clamped_by (contract 9 addendum, SPEC-v3 F4)
        "predicted": prediction["predicted"],  # G-114 (additive): {unit: {point: {ratio, base_v, offset_mv, effective_v, clamped_by}}} for every point of the touched units
        "vccia": prediction["vccia"],  # contract 9 addendum (F6): {predicted_v, set_by: {unit, ratio}} + label / contributors / before_v / light_load, null without a baseline
        "warnings": warning_objects(warnings),  # [{code, text}]: CLAMPED E40_OFFSET E_TARGET_WHEA POSITIVE_OFFSET NO_BASELINE TURBO_RAISE, everything else NOTE
        "words_assumed": _words_assumed(last, topology, targets, points),  # G-125: true only when a written unit has no word from this boot's read (tune.last)
    }
    while len(json.dumps(result, separators=(",", ":"))) > MAX_LINE - _RESPONSE_SLACK and result["steps"]:
        del result["steps"][len(result["steps"]) // 2 :]
        result["steps_truncated"] = True
    return result


def _profile_canon(profile: Profile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "core": [dataclasses.asdict(c) for c in profile.entries],
        "point": [dataclasses.asdict(p) for p in profile.points],
        "turbo": None if profile.turbo is None else dataclasses.asdict(profile.turbo),
    }


# --------------------------------------------------------------------------------------------------------------
# tune.apply
# --------------------------------------------------------------------------------------------------------------
def _validate_tune_apply(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"plan_id", "typed"})
    plan_id, typed = args.get("plan_id"), args.get("typed")
    if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise _bad("'plan_id' must be the 64-hex plan id")
    if not isinstance(typed, str) or not re.fullmatch(r"CONFIRM [A-Z2-9]{6}", typed):
        raise _bad("'typed' must be \"CONFIRM \" followed by the plan's nonce")
    return {"plan_id": plan_id, "typed": typed}


def _stale_check(plan: StoredPlan, topology: Topology):  # type: ignore[no-untyped-def]
    """The ``plan_check`` hook of the executor: the baselines it just read must be the words the plan assumed (``PLAN_STALE`` before any write)."""

    def check(applied: ApplyPlan) -> None:
        if not plan.refs:
            return
        seen: dict[tuple[int, int], int] = {}
        for step in applied.steps:
            if step.kind is StepKind.WRITE_VF and step.before is not None:
                seen[(step.cpu, 0)] = step.before.to_u32()
            elif step.kind is StepKind.WRITE_VF_POINT and step.before is not None:
                seen[(step.cpu, step.p2)] = step.before.to_u32()
        diffs = [
            f"{unit_of_cpu(topology, cpu)} cpu {cpu} {'OC word' if point == 0 else 'point ' + str(point)}: planned from {plan.refs[(cpu, point)]:#010x}, now {word:#010x}"
            for (cpu, point), word in sorted(seen.items())
            if (cpu, point) in plan.refs and plan.refs[(cpu, point)] != word
        ]
        if diffs:
            more = f" (+{len(diffs) - 3} more)" if len(diffs) > 3 else ""
            raise OpError(PLAN_STALE, "a word changed since the plan was made; nothing was written. " + "; ".join(diffs[:3]) + more + ". Read the tuning words again and re-plan.")

    return check


def _step_unit(topology: Topology, step: Any) -> str:
    return unit_of_cpu(topology, step.cpu)


def _report_rows(report: Any, topology: Topology) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(steps, readback)`` of an ApplyReport: the write steps only (compact), and the read-back comparison of the V/F words and turbo tables."""
    steps: list[dict[str, Any]] = []
    readback: list[dict[str, Any]] = []
    done = {r.step.number: r for r in report.steps}
    plan = report.plan
    planned = [s for s in plan.steps if s.kind.is_write] if plan is not None else [r.step for r in report.steps if r.step.kind.is_write]
    for s in planned:
        r = done.get(s.number)
        status = "not_sent" if r is None else r.status.value
        entry: dict[str, Any] = {"n": s.number, "kind": s.kind.value, "cpu": s.cpu, "status": status}
        if s.kind in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT):
            entry["unit"] = _step_unit(topology, s)
            entry["point"] = s.p2
            expected = s.data
            actual = None if r is None else r.readback_data
            match = r is not None and r.verified is True and actual == expected
            readback.append(
                {
                    "unit": entry["unit"], "point": s.p2, "cpu": s.cpu,
                    "expected": None if expected is None else f"{expected:#010x}", "actual": None if actual is None else f"{actual:#010x}", "match": match,
                }
            )
        elif s.kind is StepKind.WRITE_MSR:
            entry["table"] = "e" if s.msr == 0x650 else "p"
            actual = None if r is None else r.readback_data
            readback.append(
                {
                    "unit": entry["table"].upper(), "point": "turbo", "cpu": s.cpu,
                    "expected": f"{s.data:#018x}", "actual": None if actual is None else f"{actual:#018x}",
                    "match": r is not None and r.verified is True and actual == s.data,
                }
            )
        steps.append(entry)
    return steps, readback


def _merge_applied_words(helper: Helper, report: Any) -> None:
    """The words verified (or seen differing) by the read-backs of this apply update ``helper.last_words`` if there was a complete read; the OC word is
    index 0, point n index n. Nothing is invented for a CPU that was not read."""
    last = _last_words(helper)
    if last is None:
        return
    merged = {cpu: list(ws) for cpu, ws in last.items()}
    for r in report.steps:
        s = r.step
        if s.kind in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT) and r.readback_data is not None and s.cpu in merged and s.p2 < len(merged[s.cpu]):
            merged[s.cpu][s.p2] = r.readback_data
    _set_last_words(helper, merged)


# --------------------------------------------------------------------------------------------------------------
# G-126: an OC ratio raise must lift the OS frequency ceiling
# --------------------------------------------------------------------------------------------------------------
#: ``cpuinfo_max_freq`` follows HWP highest a moment after the 0x1D: re-read this many times, this far apart, while it has not moved (hardware only).
CPUINFO_POLL = (4, 0.25)


def _khz(value: str | None) -> int | None:
    return int(value) if value is not None and re.fullmatch(r"[0-9]{1,10}", value) else None


def _cpufreq_before(helper: Helper, cpus: Sequence[int]) -> dict[int, tuple[int | None, int | None]]:
    """``{cpu: (cpuinfo_max_freq, scaling_max_freq)}`` in kHz, read before the first write of an apply. Nothing without the measurement wiring."""
    from .ops_measure import CAP_ATTR, MAX_ATTR

    w = getattr(helper, "measure_wiring", None)
    out: dict[int, tuple[int | None, int | None]] = {}
    if w is None:
        return out
    for cpu in cpus:
        try:
            out[cpu] = (_khz(w.cpufreq.read(cpu, MAX_ATTR)), _khz(w.cpufreq.read(cpu, CAP_ATTR)))
        except OSError:
            out[cpu] = (None, None)
    return out


def _oc_ratio_cpus(report: Any) -> list[int]:
    """The CPUs whose OC ratio (0x1D) an apply wrote successfully."""
    return sorted({r.step.cpu for r in report.steps if r.step.kind is StepKind.WRITE_FAVORED and r.status is StepStatus.OK})


def _lift_cpufreq(helper: Helper, ctx: OpContext, before: Mapping[int, tuple[int | None, int | None]], cpus: Sequence[int], warnings: list[str]) -> list[dict[str, Any]]:
    """G-126: after an apply that changed an OC ratio, ``cpuinfo_max_freq`` follows HWP highest but ``scaling_max_freq`` keeps the old value, so the new ratio is never
    used (``[MEASURED lnx 2026-10-12]``). For each CPU whose OC ratio changed: a cap that sat AT the old cpuinfo max (no user cap) follows the new max (up or down); a
    cap the user set below the old max is never touched; a cap above a lowered max is lowered to it. Writes go through the measurement wiring's cpufreq IO to
    ``scaling_max_freq`` only, never while a measure/stress journal exists (those own the caps). Never raises: the hardware is already changed, the result must reach
    the page. Returns ``[{cpu, before_khz, after_khz}]``."""
    from .ops_measure import CAP_ATTR, MAX_ATTR

    w = getattr(helper, "measure_wiring", None)
    if w is None or not cpus:
        return []
    owned = Path(w.journal_path).exists()
    if owned:
        warnings.append("a measure/stress cpufreq journal exists: scaling_max_freq was not touched (restart the helper to recover it, then re-check the frequency cap)")
    rows: list[dict[str, Any]] = []
    tries, interval = (1, 0.0) if helper.cfg.simulator else CPUINFO_POLL
    for cpu in cpus:
        old_max, old_cap = before.get(cpu, (None, None))
        try:
            now_max = _khz(w.cpufreq.read(cpu, MAX_ATTR))
            for _ in range(tries - 1):  # the kernel lags the 0x1D by a moment: wait while nothing moved
                if now_max is not None and now_max != old_max:
                    break
                ctx.cancel.wait(interval)
                now_max = _khz(w.cpufreq.read(cpu, MAX_ATTR))
            cap = _khz(w.cpufreq.read(cpu, CAP_ATTR))
            if old_cap is None:
                old_cap = cap
            after = cap
            if not owned and now_max is not None and cap is not None and old_max is not None and now_max != old_max:
                if cap == old_max or (now_max < old_max and cap > now_max):
                    if cap != now_max:
                        w.cpufreq.write(cpu, CAP_ATTR, str(now_max))
                        after = _khz(w.cpufreq.read(cpu, CAP_ATTR))  # read back: the kernel may clamp
            rows.append({"cpu": cpu, "before_khz": old_cap, "after_khz": after})
        except (OSError, ValueError) as exc:
            warnings.append(f"scaling_max_freq of cpu {cpu} could not be adjusted: {type(exc).__name__}: {exc}")
            rows.append({"cpu": cpu, "before_khz": old_cap, "after_khz": old_cap})
    return rows


def op_tune_apply(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    st = _state(helper)
    plan = st.plans.claim(args["plan_id"], args["typed"])
    topology = _topology(helper)
    policy = _load_policy(helper)
    profile = plan.profile
    try:
        targets = preflight(profile, topology, policy)  # nothing sent: the numbers against the policy as it is NOW
    except PreflightError as exc:
        raise _preflight_error(exc) from None
    except (PlanError, ProfileError) as exc:
        raise OpError(BAD_REQUEST, str(exc)) from None
    points = resolve_point_targets(profile, topology)
    tables = sum(1 for key in ("e", "p") if profile.turbo is not None and getattr(profile.turbo, key) is not None)
    needed = count_operations(targets, None, tables, len(points))
    progress_steps = 0
    freq_before = _cpufreq_before(helper, sorted({t.cpu for t in targets}))  # G-126: the old cpuinfo / cap, before any write

    def progress(line: str) -> None:
        nonlocal progress_steps
        if line.startswith("step "):
            progress_steps += 1
            ctx.progress(progress_steps, needed, session.used + progress_steps, session.policy.max_operations_per_boot)

    with _guarded_session(helper, ctx, policy, needed, "this apply") as session:
        try:
            report = apply_profile(
                profile,
                guard=session.guard,
                topology=topology,
                confirm=session.confirm,
                policy=session.policy,
                sleep=session.sleep,
                clock=session.clock,
                progress=progress,
                plan_check=_stale_check(plan, topology),
                cancelled=ctx.cancel.is_set,
            )
        except PreflightError as exc:
            raise _preflight_error(exc) from None
        except BaseException:
            helper.tuned_this_boot = True  # type: ignore[attr-defined]  # G-120: an apply that died midway may have written; "stock" is never claimed after it
            raise
    _merge_applied_words(helper, report)
    steps, readback = _report_rows(report, topology)
    cpufreq_warnings: list[str] = []
    cpufreq = _lift_cpufreq(helper, ctx, freq_before, _oc_ratio_cpus(report), cpufreq_warnings)
    sent_writes = sum(1 for r in report.steps if r.step.kind.is_write and r.status is not StepStatus.REFUSED and r.status is not StepStatus.CANCELLED)
    if sent_writes:
        helper.tuned_this_boot = True  # type: ignore[attr-defined]  # G-120 fix 4: any tune.apply that wrote ends "stock" for this boot (ops_measure._tuning_state)
        _persist_applied_words(helper)  # G-123: the stored last read follows the verified read-backs
    result: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "steps": steps,
        "readback": readback,
        "ops_used": report.operations + sum(1 for r in report.steps if r.step.kind.is_write and not r.step.kind.is_msr and r.status is StepStatus.OK) + report.msr_writes,
        "msr_writes": report.msr_writes,   # G-119: the plain wrmsr (turbo tables) inside ops_used, so a page can show them apart from the mailbox ones
        "complete": report.ok,
        "writes_sent": sent_writes,
        "warnings": [*report.warnings, *cpufreq_warnings],
        "cpufreq": cpufreq,  # G-126: [{cpu, before_khz, after_khz}] of scaling_max_freq for the CPUs whose OC ratio changed
        "revert_plan": len(report.revert_plan),
    }
    if report.ok:
        return result
    stop = {"kind": report.stop_kind.value, "reason": report.stopped_reason, "tripped": report.tripped}
    if sent_writes:
        # the hardware is partly changed: the read-back table must reach the page, so this is a result (complete: false), not an error
        result["stop"] = stop
        return result
    if report.stop_kind is StopKind.CANCELLED:
        raise OpError(CANCELLED, "tune.apply was cancelled before the first write; nothing was changed", {"ops_done": result["ops_used"]})
    last_detail = report.steps[-1].detail if report.steps else ""
    if report.stop_kind is StopKind.REFUSED:
        check = last_detail.split(":", 1)[0] if ":" in last_detail else "REFUSED"
        raise OpError(REFUSED, f"{report.stopped_reason}. Nothing was written.", {"check": check, "ops_done": result["ops_used"]})
    raise OpError(INTERNAL, f"{report.stopped_reason}", {"ops_done": result["ops_used"], "tripped": report.tripped})


# --------------------------------------------------------------------------------------------------------------
# profiles.list / profiles.save
# --------------------------------------------------------------------------------------------------------------
def _validate_profiles_save(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"name", "intent"})
    name = args.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise _bad("'name' must be 1..64 characters of a-z 0-9 . _ -")
    return {"name": name, "intent": canonical_intent(args.get("intent"), rows_only=True)}


def op_profiles_list(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    note: str | None = None
    try:
        text = _read_accepted_text(helper, "CURRENT")
        created = None
        for line in text.splitlines():
            m = re.fullmatch(r"\s*profile\s*=\s*(\S+)\s*", line)
            if m and _ACCEPTED_PATH_RE.fullmatch(m.group(1)) and m.group(1).endswith(".toml"):
                out.append({"name": Path(m.group(1)).stem, "source": "accepted", "created": created})
    except OpError as exc:
        note = exc.message
    try:
        out.extend(_profile_store(helper).list())
    except ProfileStoreError as exc:
        note = (note + "; " if note else "") + exc.message
    result: dict[str, Any] = {"profiles": out}
    if note:
        result["note"] = note
    return result


def op_profiles_save(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    try:
        accepted = {n for n, _ in _accepted_chain(helper)}
    except OpError:
        accepted = set()
    if name in accepted:
        raise OpError(REFUSED, f"{name!r} is the name of an accepted profile; profiles are never overwritten", {"check": "EXISTS"})
    try:
        record = _profile_store(helper).save(name, args["intent"])
    except ProfileStoreError as exc:
        raise OpError(REFUSED if exc.code in ("EXISTS", "UNTRUSTED") else BAD_REQUEST if exc.code in ("BAD_NAME", "TOO_BIG") else INTERNAL, exc.message, {"check": exc.code}) from None
    return {"name": record["name"], "source": "saved", "created": record["created"]}


def _validate_profiles_get(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"name"})
    name = args.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise _bad("'name' must be 1..64 characters of a-z 0-9 . _ -")
    return {"name": name}


def op_profiles_get(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """Contract 9 (G-123): ``{name, source, created, contents: {rows, turbo}, summary}``; an accepted member also ``position`` / ``of`` / ``chain`` and
    ``cumulative`` (the state after applying the chain up to and including it: what loading it into Tune means)."""
    name = args["name"]
    topology = _topology(helper)

    def contents_of(profile: Profile) -> dict[str, Any]:
        return profile_contents(profile, topology, lambda cpu: unit_of_cpu(topology, cpu), _POINT_RATIOS)

    try:
        chain = _accepted_chain(helper)
    except OpError:
        chain = []
    for i, (stem, prof) in enumerate(chain):
        if stem == name:
            own = contents_of(prof)
            cumulative = contents_of(_merge_profiles(name, [p for _, p in chain[: i + 1]]))
            return {"name": name, "source": "accepted", "created": None, "position": i + 1, "of": len(chain), "chain": [n for n, _ in chain],
                    "contents": own, "cumulative": cumulative, "summary": summarize(own), "cumulative_summary": summarize(cumulative)}
    try:
        record = _profile_store(helper).load(name)
    except ProfileStoreError as exc:
        raise OpError(BAD_REQUEST if exc.code == "NOT_FOUND" else REFUSED, exc.message, {"check": "PROFILE_" + exc.code}) from None
    own = intent_contents(canonical_intent(record["intent"], rows_only=True))
    created = record.get("created")
    return {"name": name, "source": "saved", "created": created if isinstance(created, str) else None, "contents": own, "summary": summarize(own)}


# --------------------------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------------------------
def tune_ops() -> list[OpSpec]:
    from .ops import _validate_vf_dump

    return [
        # a mailbox read is a WRMSR (docs/SAFETY.md): write-gated on hardware, long (state machine begin/end), one at a time
        OpSpec("vf.dump", vf_dump_tracked, _validate_vf_dump, long=True, write=True, state=State.PLANNING),
        OpSpec("tune.read", op_tune_read, _validate_vf_dump, long=True, write=True, state=State.PLANNING),
        # pure: policy + last read + files, no hardware; own thread because it touches the filesystem
        OpSpec("tune.plan", op_tune_plan, _validate_tune_plan, slow=True),
        # D-029: every write op is long, so it goes through machine.begin (state "applying") and the dispatcher's busy check
        OpSpec("tune.apply", op_tune_apply, _validate_tune_apply, long=True, write=True, state=State.APPLYING),
        OpSpec("profiles.list", op_profiles_list, _no_args, slow=True),
        OpSpec("profiles.save", op_profiles_save, _validate_profiles_save, slow=True),
        OpSpec("profiles.get", op_profiles_get, _validate_profiles_get, slow=True),
        # G-123: the stored last read of this boot (a file read, no hardware); also restores the plan state after a helper restart
        OpSpec("tune.last", op_tune_last, _no_args, slow=True),
    ]
