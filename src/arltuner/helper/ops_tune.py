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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arltuner.controls import (
    RAISE_CAVEAT,
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
    WriteGuard,
    anchored_wall_clock,
    default_m1_policy,
    load_policy,
    read_state,
)
from arltuner.guard.policy import turbo_bytes
from arltuner.mailbox import VfWord
from arltuner.msr import MailboxTransport
from arltuner.profiles import CoreSetting, PointSetting, Profile, ProfileError, TurboTables, load_profile
from arltuner.profiles.store import NAME_RE, ProfileStore, ProfileStoreError
from arltuner.topology import Topology, TopologyError, UnknownCore

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
    lock: threading.Lock = field(default_factory=threading.Lock)


_STATE_LOCK = threading.Lock()


def _state(helper: Helper) -> TuneState:
    with _STATE_LOCK:  # tune.plan runs on up to four slow threads: two first callers must not each build a state (one's plan would be lost)
        st = getattr(helper, "_tune_state", None)
        if st is None:
            st = TuneState(PlanStore(helper.cfg.monotonic))
            helper._tune_state = st  # type: ignore[attr-defined]
        return st


def _last_words(helper: Helper) -> dict[int, list[int]] | None:
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
        for cpu in unit["cpus"]:
            pts = []
            if cpu.get("oc") is not None:
                pts.append({"point": 0, **_decode(VfWord.from_u32(int(cpu["oc"]["word"], 16)))})
            for p in cpu.get("points", []):
                pts.append({"point": p["point"], **_decode(VfWord.from_u32(int(p["word"], 16)))})
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


def _read_tables(helper: Helper, ctx: OpContext, policy: Policy, topology: Topology) -> tuple[dict[str, tuple[int, ...]] | None, str | None]:
    """Best effort: plain ``rdmsr`` of the E (0x650) and P (0x1AD) turbo tables through the guard (not WRMSRs). ``(None, why)`` if the policy does not
    allow it or anything goes wrong: a ratio intent then carries no table raise and says so."""
    try:
        cpus = sorted(c.cpu for c in topology.cores if c.kind == "P")
        cpu = cpus[0]
        with _guarded_session(helper, ctx, policy, 3, "the turbo table read") as session:
            out: dict[str, tuple[int, ...]] = {}
            for key, msr in (("e", 0x650), ("p", 0x1AD)):
                res = session.guard.execute(MsrRequest(cpu, msr, 0, f"tune.read: read turbo table {msr:#x} (plain rdmsr)", "read"), session.confirm)
                out[key] = turbo_bytes(res.value)
        return out, None
    except OpError as exc:
        return None, f"turbo tables not read: {exc.message}"
    except GuardRefused as exc:
        return None, f"turbo tables not read: the guard refused ({exc.reason.value}); the policy has no msr_allow read entry for them"
    except Exception as exc:  # best effort: a table read must never fail the tuning-word read that already succeeded
        return None, f"turbo tables not read: {type(exc).__name__}: {exc}"


def op_tune_read(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    doc = vf_dump_tracked(helper, ctx, args)
    topology = _topology(helper)
    result = dict(doc)
    result["units"] = [dict(u) for u in doc.get("units", [])]
    result["tune"] = _decode_document(result, topology)
    st = _state(helper)
    try:
        policy = _load_policy(helper)
        tables, note = _read_tables(helper, ctx, policy, topology)
    except OpError as exc:
        tables, note = None, f"turbo tables not read: {exc.message}"
    with st.lock:
        st.tables, st.tables_note = tables, note
    result["tables"] = None if tables is None else {k: list(v) for k, v in tables.items()}
    result["tables_note"] = note
    return _fit(result)


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
    if keys == {"scope", "rows"}:
        scope, rows = raw["scope"], raw["rows"]
        if scope not in ("simple", "unit"):
            raise _bad("scope must be 'simple' or 'unit'")
        if not isinstance(rows, list) or len(rows) > 32:
            raise _bad("rows must be a list of at most 32 rows")
        canon_rows = [r for r in (_canonical_row(row, scope) for row in rows) if r is not None]
        targets = [r["target"] for r in canon_rows]
        if len(set(targets)) != len(targets):
            raise _bad("a target appears in two rows")
        if not canon_rows:
            raise OpError(BAD_REQUEST, "the intent changes nothing (every row is empty)", {"check": "NOTHING_TO_DO"})
        return {"scope": scope, "rows": canon_rows}
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
    raise _bad("intent must be {scope, rows}, {profile}, {reset: 'stock'} or {restore: 'accepted'}")


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
    for row in intent["rows"]:
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
                by_ratio = {VfWord.from_u32(w).ratio: n for n, w in enumerate(words[1:], start=1)}
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
    try:
        profile = Profile(name, tuple(CoreSetting(k, i, **v) for (k, i), v in cores.items()), turbo, tuple(points.values()))  # type: ignore[arg-type]
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
            warnings.append(f"the {kind} turbo table is raised to {ratio} in every bucket below it (package-wide, MSR {'0x650' if kind == 'E' else '0x1AD'})")
    return TurboTables(e=new.get("e"), p=new.get("p")) if new else None


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
    if "rows" in intent:
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


def _predicted_steps(profile: Profile, topology: Topology) -> tuple[list[dict[str, Any]], int, int]:
    """The write steps the plan will contain, in the planner's order (conditional 0x15, point offsets, per-core triples, turbo tables), without data
    words (those need the hardware baselines and are built at apply time). Returns ``(listed steps, total writes, read-only requests)``."""
    steps: list[dict[str, Any]] = [{"kind": StepKind.WRITE_PERCORE_ENABLE.value, "conditional": True}]
    targets = resolve_targets(profile, topology) if profile.entries else ()
    pts = resolve_point_targets(profile, topology)
    for p in pts:
        steps.append({"kind": StepKind.WRITE_VF_POINT.value, "unit": unit_of_cpu(topology, p.cpu), "cpu": p.cpu, "point": p.point, "offset_mv": p.offset_mv})
    for t in targets:
        u = unit_of_cpu(topology, t.cpu)
        for kind in (StepKind.WRITE_VF, StepKind.WRITE_FAVORED, StepKind.WRITE_GRANULAR):
            steps.append({"kind": kind.value, "unit": u, "cpu": t.cpu, "point": 0})
    if profile.turbo is not None:
        for key in ("e", "p"):
            if getattr(profile.turbo, key) is not None:
                steps.append({"kind": StepKind.WRITE_MSR.value, "table": key})
    reads = 1 + 4 * len(targets) + sum(1 for t in targets if t.oc_voltage_mv is not None) + len(pts)
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


def op_tune_plan(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    intent = args["intent"]
    topology = _topology(helper)
    policy = _load_policy(helper)
    resolved = resolve_intent(helper, intent, policy, topology)
    profile = resolved.profile
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
    ops_needed = count_operations(targets, None, tables, len(points)) if (targets or points) else 0
    last = _last_words(helper)
    before, after = _before_after(profile, topology, last)
    steps, writes, reads = _predicted_steps(profile, topology)
    warnings = list(plan_warnings(targets, policy, points))
    warnings.extend(resolved.warnings)
    if any(t.ratio is not None or t.oc_voltage_mv is not None for t in targets):
        warnings.append(RAISE_CAVEAT)
    for unit, items in after.items():
        for point, now in items.items():
            was = (before.get(unit) or {}).get(point)
            if now["offset_mv"] is not None and now["offset_mv"] > 0 and (was is None or was["offset_mv"] <= 0):
                warnings.append(f"positive offset {now['offset_mv']:+.1f} mV on {unit} {'OC word' if point == '0' else 'point ' + point}")
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
        "after": after,
        "warnings": warnings,
        "words_assumed": bool(resolved.refs),
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
    _merge_applied_words(helper, report)
    steps, readback = _report_rows(report, topology)
    sent_writes = sum(1 for r in report.steps if r.step.kind.is_write and r.status is not StepStatus.REFUSED and r.status is not StepStatus.CANCELLED)
    result: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "steps": steps,
        "readback": readback,
        "ops_used": report.operations + sum(1 for r in report.steps if r.step.kind.is_write and not r.step.kind.is_msr and r.status is StepStatus.OK),
        "complete": report.ok,
        "writes_sent": sent_writes,
        "warnings": list(report.warnings),
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
    ]
