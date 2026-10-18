"""G-112 helper ops (contract 9, SPEC-v3 sections 4-6, D-032/D-033): ``tune.read``, ``tune.plan``, ``tune.apply``, ``profiles.list``, ``profiles.save``.

Design in one paragraph. ``tune.read`` is ``vf.dump`` plus decoded words (and a best-effort plain read of the turbo tables); it remembers the words in
force (``helper.last_words``: cpu -> [OC word, point 1, point 2, ...]). ``tune.plan`` is **pure** (no hardware, no mailbox, not write-gated): it turns the
UI's intent into a ``Profile`` for the existing controls planner, resolving "the point at ratio 53" from the words of the last read, checks every number
against the root policy (``REFUSED POLICY_RANGE``) and the controls pre-flight, and keeps the plan (id, nonce, expiry, the words it assumed) in memory.
``tune.apply`` is the existing ``controls.apply_profile`` engine run through the guard, one transaction at a time: it re-reads the baselines inside its
own guarded session (the guard demands a baseline read per session anyway), refuses with ``PLAN_STALE`` before any write when a word differs from what
the plan assumed, then writes and reads every write back. No new mailbox logic lives here: the guard stays the only path to the mailbox and
``controls`` builds every request.

G-148 (D-044): profiles are complete and independent. ``profiles.save`` stores this boot's read with the editor's rows laid over it (a complete profile);
the accepted state is exactly one profile (``accepted/CURRENT`` has one ``profile =`` line; an older chain is refused with a pointer to ``accepted-migrate``);
Restore accepted, and the apply of any complete profile, writes only the words that differ from what the CPU holds, keeps the G-147 residual rule, and
splits a change larger than the policy's step limits into guarded rounds within the one apply (``controls.executor``), judged before the first write.

G-154 (D-045): the ring (cache, mailbox domain P1 = 2) is one more unit, ``ring``. ``tune.read`` reads it (``ring_words``: its OC word and points, one guarded read per word, in
its own session of at most ``RING_WALK_CAP + 1`` operations) and returns ``ring: {cpu, oc, points, end}`` in the shape of a ``cpus[]`` entry; ``helper.last_ring`` keeps
``[OC word, point 1, ...]`` beside ``helper.last_words`` and the last-read record stores it as ``ring_words``. ``accepted_match`` gets a ``ring`` entry when the accepted state has a
``ring`` line; ``profiles.save`` / ``profiles.accept`` record it; a plan of a profile with a ``[ring]`` reads and writes the ring points that differ and refuses the ring unit alone,
with the reason, when the live ring OC word is not the profile's (it is BIOS-owned and never written).
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

from arltuner import vfdump, vfmodel
from arltuner.controls import (
    READS_PER_CPU,
    REVERT_NOTE,
    SPLIT_EXTRA_WRITES,
    VMAX_EXTRA_READS,
    WRITES_PER_CPU,
    ApplyPlan,
    NeedsBootBudget,
    NeedsHigherLimit,
    NeedsRamp,
    NeedsRounds,
    PlanError,
    PlanRefused,
    PreflightError,
    StepKind,
    StepStatus,
    StopKind,
    apply_profile,
    check_rounds,
    count_operations,
    pack_turbo,
    plan_warnings,
    preflight,
    ramp_rounds,
    resolve_point_targets,
    resolve_targets,
    resolve_ring_targets,
    CoreReadings,
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
    default_m1_policy,
    load_policy,
    read_state,
)
from arltuner.controls import cpufreq as cpufreq_rules
from arltuner.controls import residual
from arltuner.controls import ring as ringmod
from arltuner.controls.planner import POINT_CAVEAT, build_plan, _same_state, _goal_word, current_word, kept_word, oc_cap_reads_of
from arltuner.guard.policy import ENCODING_OFFSET_CEILING_MV, ENCODING_OFFSET_FLOOR_MV, fused_top_ratio, hw_ratio_max, hw_v_max_mv, ratio_limit, turbo_bytes, v_target_limit, v_target_limit_mv
from arltuner.mailbox import Cmd, VfWord, rc_name
from arltuner.mailbox.encoding import RATIO_RANGE, RATIO_SOURCE, TURBO_RATIO_RANGE, V_TARGET_RANGE_MV, V_TARGET_SOURCE
from arltuner.msr import MailboxTransport
from arltuner.curves import is_unit_id
from arltuner.profiles import BuildError, CoreSetting, POINT_RANGE, PointSetting, Profile, ProfileError, RingPoint, RingSetting, TurboTables, complete_from_words, load_profile, overlay
from arltuner.profiles.contents import intent_contents, profile_contents, summarize
from arltuner.profiles.expected import parse_expected
from arltuner.profiles.migrate import current_profile_lines
from arltuner.profiles.serialize import dump_profile
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
    NO_ACCEPTED_NOTE,
    accepted_match as _core_accepted_match,
    helper_topology,
    op_vf_dump,
    sim_clocks,
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
_RATIO_RANGE = RATIO_RANGE  # G-141 (D-039): the field encodings of arltuner.mailbox.encoding, shared with the profile schema; the policy and the CPU's limits are the bounds


def groups_of(topology: Topology) -> dict[str, tuple[str, ...]]:
    """The ``P`` / ``E`` groups of a topology's units (a ``simple`` row addresses a whole group)."""
    ids = topology.unit_ids()
    return {"P": tuple(u for u in ids if u.startswith("P")), "E": tuple(u for u in ids if u.startswith("EC"))}


#: ``tune.plan`` echoes at most this many predicted write steps (the full count is ``writes``): the response line is capped at 64 KiB.
_MAX_STEPS_LISTED = 300


# --------------------------------------------------------------------------------------------------------------
# Units (contract 9): P<n> = the n-th P CPU, EC<n> = the CPUs of the n-th E-cluster (live topology)
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
    return topology.unit_cpus(unit)


def unit_of_cpu(topology: Topology, cpu: int) -> str:
    return topology.unit_of_cpu(cpu)


def _kind_of(unit: str) -> str:
    return "P" if unit.startswith("P") else "E"


_RING_STEP_KINDS = (StepKind.READ_RING_OC, StepKind.READ_RING_POINT, StepKind.WRITE_RING_POINT)


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
    restore: bool = False  # G-147: the restore of the accepted state (G-148: or any complete profile): the apply scans for a residual OC point and neutralises it
    rounds: int = 1  # G-148: guarded rounds the last read says the apply needs within the step limits (the budget reserves them; the apply re-judges from its own reads)


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
    favored_words: dict[int, int] | None = None  # cpu -> the whole 0x1C word of the last tune.read (P-cores): the data an "auto" ratio ceiling is derived from
    fused: dict[int, dict[str, Any]] | None = None  # G-114 F7: cpu -> {word, ratio, v} of the last tune.read (cmd 0x09 P1=0x11 P2=0)
    turbo: dict[str, Any] | None = None  # G-114 F5: {"P": {ratios, counts}, "E": {...}, "favored": {cpu: ratio}} of the last tune.read
    hw_limits: dict[str, dict[str, Any]] | None = None  # G-137 (D-039): the CPU's own limits per kind from the last tune.read (0x01 ratio, 0x28 V target); see _hw_limits_doc
    audit_mark: int | None = None  # G-141: byte size of the audit log when the words held were read (or last verified by an apply); a write after it makes them stale; None = unknown
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


def _last_ring(helper: Helper) -> list[int] | None:
    """G-154: ``[OC word, point 1, ...]`` of the ring as this boot's last read (and the verified read-backs of the applies since) found it, or ``None`` (not read)."""
    if not getattr(helper, "_last_restored", False):
        _restore_last(helper)
    words = getattr(helper, "last_ring", None)
    return list(words) if isinstance(words, list) and len(words) >= 2 else None


def _set_last_ring(helper: Helper, words: Sequence[int] | None) -> None:
    helper.last_ring = None if not words else [int(w) for w in words]  # type: ignore[attr-defined]


def _ring_word_map(ring: Sequence[int] | None) -> dict[int, int] | None:
    """The ring channel of the planner: ``{0: OC word, k: point k}``; ``None`` without a read."""
    return None if not ring else {n: int(w) for n, w in enumerate(ring)}


def _accepted_ring(helper: Helper) -> list[int] | None:
    """The ``ring`` line of the accepted ``EXPECTED-words.txt`` (G-154), or ``None`` when the accepted state has none (or cannot be read: the compare then has its own note)."""
    try:
        return parse_expected(_read_accepted_text(helper, "EXPECTED-words.txt"))[1]
    except (OpError, ValueError):
        return None


def _hexw(word: int | None) -> str | None:
    return None if word is None else f"{word:#010x}"


def accepted_match(
    helper: Helper, document: dict[str, Any], fused: Mapping[int, int] | None = None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``helper.ops.accepted_match`` plus (G-154) the ring: when the accepted state has a ``ring`` line, one more entry ``{cpu: "ring", unit: "ring", match, diffs}`` compares the
    ring of ``document["ring"]`` (a ``tune.read`` result; a ``vf.dump`` has none, so the ring then differs as "not read") word for word -- the OC word as point 0 -- with no
    equivalence rules: a ring that differs is a difference. An accepted state without a ``ring`` line gets no entry (nothing about the ring is compared)."""
    matched, note = _core_accepted_match(helper, document, fused)
    if matched is None:
        return matched, note
    want = _accepted_ring(helper)
    if want is None:
        return matched, note
    got = ringmod.from_json(document.get("ring"))
    diffs = [
        {"point": n, "expected": _hexw(want[n] if n < len(want) else None), "actual": _hexw(got[n] if got is not None and n < len(got) else None)}
        for n in range(max(len(want), len(got or [])))
        if (want[n] if n < len(want) else None) != (got[n] if got is not None and n < len(got) else None)
    ]
    return [*matched, {"cpu": ringmod.UNIT, "unit": ringmod.UNIT, "match": not diffs, "diffs": diffs}], note


# --------------------------------------------------------------------------------------------------------------
# Policy and topology (trusted inputs only, SPEC 4.4)
# --------------------------------------------------------------------------------------------------------------
def _load_policy(helper: Helper) -> Policy:
    """The root policy, bound to the live topology (``cpus`` / ``e_cpus`` / ``"auto"`` ceilings are derived from it, D-036)."""
    cfg = helper.cfg
    topology = helper_topology(helper)
    if cfg.policy_path is not None:
        _trust(helper, cfg.policy_path, want="file", must_exist=True)
    try:
        policy = default_m1_policy(topology) if cfg.policy_path is None else load_policy(cfg.policy_path, topology)
    except (PolicyError, OSError) as exc:
        raise _refuse(f"policy error: {exc}.", "POLICY_INVALID") from None
    return _bind_hw_limits(helper, policy)


def _bind_hw_limits(helper: Helper, policy: Policy) -> Policy:
    """G-137 (D-039): the policy bound to the CPU's own limits of this boot's ``tune.read`` (ratio ``0x01``, V target ``0x28``). Every ratio / V target ceiling is
    then the policy value clamped by them (planner, guard and plan-time checks alike); none read = no raise."""
    _last_words(helper)  # the lazy restore of this boot's stored read brings the limits back after a helper restart
    st = _state(helper)
    with st.lock:
        limits = {kind: dict(v) for kind, v in (st.hw_limits or {}).items()}
    return policy.with_hw_limits(
        {kind: v.get("ratio_max_oc") for kind, v in limits.items()},
        {kind: v.get("v_target_max_mv") for kind, v in limits.items()},
    )


def _topology(helper: Helper) -> Topology:
    return helper_topology(helper)


def _ratio_limit(policy: Policy, kind: str, favored_words: Mapping[int, int] | None) -> int | None:
    """The effective ratio ceiling of a kind (G-137, D-039): the policy's number, or for ``"auto"`` the fused ``0x1C`` values of that kind in ``favored_words`` plus 3,
    clamped by the CPU's own ``0x01`` limit that ``policy`` is bound to (``None`` = none / not read: no raise)."""
    return ratio_limit(policy, kind, [w for cpu, w in (favored_words or {}).items() if policy.core_kind(cpu) == kind])[0]


def _limits(policy: Policy, topology: Topology, favored_words: Mapping[int, int] | None) -> dict[str, Any]:
    """G-133 / D-036: the limits of the bound policy, ``"auto"`` resolved, so the page offers exactly what the guard would accept and no literal. Per kind ``P`` / ``E``:

    * ``offset_mv`` ``{min, max}``: the runtime floor of the kind (the lowest ``offset_floor_raw`` over its CPUs, in mV) and ``offset_ceiling_mv``,
    * ``v_target_max_v``: ``v_target_max_mv`` / 1000 clamped by the CPU's own ``0x28`` maximum read by ``tune.read`` (G-137); for ``"auto"`` that live value (``None`` when it was
      not read: no raise); ``None`` = the policy sets no ceiling (no raise),
    * ``ratio_max``: the policy number, or the ``"auto"`` ceiling from the ``0x1C`` words read this boot; ``None`` = none (no raise / not read),
    * ``source``: ``{ratio_max, v_target_max}`` -> ``"policy"`` | ``"auto"`` | ``"none"`` (what the number comes from), for the page's one-line reasons."""
    out: dict[str, Any] = {"offset_mv": {}, "v_target_max_v": {}, "ratio_max": {}, "source": {"ratio_max": {}, "v_target_max": {}}}
    for kind in ("P", "E"):
        cpus = [c.cpu for c in topology.cores if (c.kind == "E") == (kind == "E")]
        floor_raw = min((policy.offset_floor_raw(c) for c in cpus), default=0)
        out["offset_mv"][kind] = {"min": min(0, round(floor_raw * 1000 / 1024)), "max": policy.offset_ceiling_mv}
        vmax = v_target_limit_mv(policy, kind, None)[0]
        out["v_target_max_v"][kind] = None if vmax is None else round(vmax / 1000, 4)
        out["source"]["v_target_max"][kind] = "auto" if kind in policy.v_target_auto else "none" if policy.v_target_max_mv[kind] is None else "policy"
        out["ratio_max"][kind] = _ratio_limit(policy, kind, favored_words)
        out["source"]["ratio_max"][kind] = "auto" if kind in policy.ratio_auto else "none" if policy.ratio_max[kind] is None else "policy"
    return out


def _add_limits(helper: Helper, result: dict[str, Any], topology: Topology, favored_words: Mapping[int, int] | None) -> None:
    """``result["limits"]`` from the policy as it is now; ``None`` plus ``limits_note`` when the policy cannot be loaded (never an error of a read)."""
    try:
        result["limits"] = _limits(_load_policy(helper), topology, favored_words)
        result.pop("limits_note", None)
    except OpError as exc:
        result["limits"], result["limits_note"] = None, f"policy limits not available: {exc.message}"


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


def _decode_document(doc: dict[str, Any], topology: Topology, fused: Mapping[int, int] | None = None) -> dict[str, Any]:
    """Add ``units[*].id`` and ``tune`` (``{unit: {cpus: [{cpu, points: [{point, ratio, offset_mv, v_target_v, mode}]}]}}``; point 0 = the OC word). ``fused``: cpu ->
    its live ``0x09`` word; a point whose ratio is above the core's top fused ratio ``[7:0]`` IS the OC point (the OC ratio is above the top fused ratio)."""
    fused = fused or {}
    tune: dict[str, Any] = {}
    for unit in doc.get("units", []):
        first = unit["cpus"][0]["cpu"] if unit.get("cpus") else None
        if first is None:
            continue
        uid = unit_of_cpu(topology, first)
        unit["id"] = uid
        rows = []
        for cpu in unit["cpus"]:
            top = fused_top_ratio(fused.get(cpu["cpu"]))
            ratios = [VfWord.from_u32(int(p["word"], 16)).ratio for p in cpu.get("points", [])]
            fused_n = sum(1 for r in ratios if top is None or r <= top)  # fused points of this core; with no 0x09 read nothing is called the OC point
            oc_n = fused_n + 1  # community naming: the OC point follows the last fused point
            pts = []
            if cpu.get("oc") is not None:
                pts.append({"point": 0, "point_label": f"OC (point {oc_n})", **_decode(VfWord.from_u32(int(cpu["oc"]["word"], 16)))})
            for p in cpu.get("points", []):
                is_oc = top is not None and ratios[p["point"] - 1] > top  # the OC ratio is above the top fused ratio, so this point IS the OC point
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
    used: int | None  # per-boot operations used before this session; None = the boot id is unknown (G-141: not claimed as 0)


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
        os.close(open_checked(cfg.audit_path, write=True, create=True, mode=0o600, euid=euid, what="audit log"))  # G-141: private, like install.sh creates it
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
    counted = state.attempts_by_boot.get(boot_id, 0)  # the guard caps an unknown boot as its own bucket, so the cap check uses it either way
    used = counted if boot_id is not None else None  # ... but it is not "operations of this boot": the display says unknown
    left = max(policy.max_operations_per_boot - counted, 0)
    if needed > left:
        raise _refuse(
            f"{what} needs up to {needed} mailbox operations but only {left} of the policy's {policy.max_operations_per_boot} operations per boot are left.",
            "BOOT_LIMIT", needed=needed, left=left,
        )
    if cfg.simulator:
        virtual, wall = sim_clocks(helper, state.last_attempt_wall)  # G-141: starts at the newest audit record when a previous process left it ahead of the real clock
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
            env={ENV_FLAG: "1"},  # D-043: the allowlisted socket peer replaced the exported flag; never os.environ
            health_gate=gate,
            lock_path=cfg.lock_path,
            clock=clock,
            wall_clock=wall,
            boot_id_fn=cfg.boot_id_fn,
            msr_port=port,
            session_favored_words=_state(helper).favored_words,
        )
        yield _Session(guard, Confirmation(token, cfg.operator), policy, clock, sleep, used)
    finally:
        close = getattr(port, "close", None)
        if callable(close):
            close()


# --------------------------------------------------------------------------------------------------------------
# vf.dump / tune.read
# --------------------------------------------------------------------------------------------------------------
def audit_mark(helper: Helper) -> int | None:
    """G-141: the byte size of the audit log now (0 = no log yet), a position every later record lies after; ``None`` when the log cannot be read or trusted."""
    cfg = helper.cfg
    try:
        _trust(helper, cfg.audit_path, want="file", must_exist=False)
        fd = open_checked(cfg.audit_path, write=False, create=False, euid=cfg.euid_fn(), what="audit log")
    except FileNotFoundError:
        return 0
    except (OpError, TrustError, OSError):
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            return os.fstat(handle.fileno()).st_size
    except OSError:
        return None


_AUDIT_LINE_MAX = 1 << 20
_MAILBOX_WRITE_CMDS = frozenset({0x11, 0x13, 0x15, 0x17, 0x19, 0x1B, 0x1D, 0x27})  # arltuner.mailbox.WRITE_COMMANDS as plain ints (a record's cmd is "0x11")


def stale_since_read(helper: Helper) -> str | None:
    """G-141: ``None`` when no hardware write happened since the words held (``helper.last_words``) were read or last verified by an apply, else why they may be stale.
    Judged from the audit log: a mailbox write command (``0x11`` ...) or a plain MSR write ``attempt`` after the byte position ``audit_mark`` taken when the read began
    (or when the last complete apply ended). Unknown = stale: an unreadable log, a log shorter than the mark, or a read that has no mark (stored by an older version)."""
    _last_words(helper)  # the lazy restore brings the stored mark back after a helper restart
    st = _state(helper)
    with st.lock:
        mark = st.audit_mark
    if mark is None:
        return "when the words in force were read is not known (an unreadable audit log, or a read stored by an older version)"
    cfg = helper.cfg
    try:
        _trust(helper, cfg.audit_path, want="file", must_exist=False)
        fd = open_checked(cfg.audit_path, write=False, create=False, euid=cfg.euid_fn(), what="audit log")
    except FileNotFoundError:
        return None if mark == 0 else "the audit log is gone since the read"
    except (OpError, TrustError, OSError) as exc:
        return f"the audit log cannot be read to prove nothing was written since the read ({getattr(exc, 'message', None) or exc})"
    try:
        with os.fdopen(fd, "rb") as handle:
            if os.fstat(handle.fileno()).st_size < mark:
                return "the audit log is shorter than when the words were read"
            if mark > 0:
                handle.seek(mark - 1)
                if handle.read(1) != b"\n":
                    handle.readline(_AUDIT_LINE_MAX + 1)  # the mark landed inside a record another process was still writing: start at the next line
            else:
                handle.seek(0)
            while True:
                raw = handle.readline(_AUDIT_LINE_MAX + 1)
                if not raw:
                    return None
                text = raw.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    if not raw.endswith(b"\n"):
                        continue  # a record cut short at the end of the file: ignored, as the guard's read_state does
                    return "an audit record after the read cannot be parsed"
                if not isinstance(record, dict) or record.get("event") != "attempt":
                    continue
                cmd = record.get("cmd")
                try:
                    wrote = record.get("msr_kind") == "write" or (isinstance(cmd, str) and int(cmd, 16) in _MAILBOX_WRITE_CMDS)
                except ValueError:
                    wrote = True
                if wrote:
                    what = f"MSR {record.get('msr')}" if record.get("msr_kind") == "write" else f"mailbox command {cmd}"
                    return f"the audit log shows a write ({what} on cpu {record.get('cpu')}, {record.get('ts')}) after the words were read"
    except OSError as exc:
        return f"the audit log cannot be read to prove nothing was written since the read ({exc.strerror or exc})"


def _advance_mark(helper: Helper) -> None:
    """After an apply that completed and whose verified read-backs were folded into the words held: they are current as of now."""
    mark = audit_mark(helper)
    st = _state(helper)
    with st.lock:
        st.audit_mark = mark


def _covers_all_units(topology: Topology, words: Mapping[int, Sequence[int]]) -> bool:
    return all(any(c in words for c in topology.unit_cpus(u)) for u in topology.unit_ids())


def vf_dump_tracked(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``vf.dump`` unchanged, plus: after a *complete* dump the words are kept in ``helper.last_words`` (cpu -> [OC word, point 1, ...]) together with the audit position
    the dump began at (``TuneState.audit_mark``, G-141): a dump of every unit replaces it, a dump of some CPUs keeps the older of the two (the rest of the words are as old)."""
    _last_words(helper)  # restore first: the mark below must not be overwritten by a later lazy restore
    before = audit_mark(helper)
    doc = op_vf_dump(helper, ctx, args)  # raises on a stop: a partial dump never becomes "the words in force"
    words = _words_of_document(doc)
    if words:
        prior = _last_words(helper)
        merged = {cpu: list(ws) for cpu, ws in (prior or {}).items()}
        merged.update(words)
        _set_last_words(helper, merged)
        st = _state(helper)
        with st.lock:
            if prior is None or _covers_all_units(helper_topology(helper), words):
                st.audit_mark = before
            elif st.audit_mark is None or before is None:
                st.audit_mark = None
            else:
                st.audit_mark = min(st.audit_mark, before)
    return doc


_LABEL = {"fused": "fused points", "favored": "favored ratios", "limits": "CPU limits", "turbo": "turbo tables"}
_FUSED_CMD, _FUSED_P1 = 0x09, 0x11  # VoltageFloorRead P1 = 0x11 (domain floors), P2 = 0 (core): the fused V/F point per core, registers.md 0x09 [MEASURED win 2026-10-01]


def _fused_entry(word: int) -> dict[str, Any]:
    """Contract 9 addendum ``{word, ratio, v}``: ratio ``[7:0]``, voltage ``[19:8]`` S2.10 (registers.md 0x09)."""
    return {"word": f"0x{word & 0xFFFFFFFF:08x}", "ratio": word & 0xFF, "v": round(((word >> 8) & 0xFFF) / 1024 + 1e-9, 4)}  # half up: 0x4A0 = 1.15625 V -> 1.1563


def _read_extras(helper: Helper, ctx: OpContext, policy: Policy, topology: Topology) -> dict[str, Any]:
    """G-114: the guarded reads that make ``tune.read`` respect SPEC-v3 section 9, in ONE session of exactly ``units + P-cores + E-clusters + 4 + 4`` guard-counted requests (four
    are plain rdmsr, the rest mailbox reads = WRMSR to 0x150), one at a time:

    * ``fused`` (F7): cmd ``0x09 P1=0x11 P2=0`` on every P-core and on the first CPU of every E-cluster (one per unit),
    * ``favored`` (F5): cmd ``0x1C`` on every P-core (low byte = the favored ratio; E clusters follow ``0x650``) and (G-137) on the first CPU of every E-cluster (word kept for an "auto" ceiling only),
    * ``limits`` (G-137, D-039): cmd ``0x01 P1=0`` (fused max OC ratio) and ``0x28 P1=0`` (max voltage) on the first P CPU and the first CPU of the first E cluster (4),
    * ``turbo`` (F5): plain ``rdmsr`` ``0x1AD`` / ``0x1AE`` / ``0x650`` / ``0x651`` on the first P CPU (4).

    Each group degrades alone (a refusal leaves that group ``None`` with a note); a guard trip or a cancel ends the session. The mailbox requests are paced like
    ``vf.dump`` / the executor (G-141): the first waits out the dwell since the dump that just ended, every later one the dwell since the previous one (a policy with
    ``min_dwell_s > 0`` would otherwise refuse every group with DWELL). Returns ``{fused, turbo, tables, hw_limits, notes, ops}``; ``ops`` = the operations this session
    executed (the guard's own count: the mailbox requests, not the plain ``rdmsr`` reads)."""
    p_cpus = sorted(c.cpu for c in topology.cores if c.kind == "P")
    e_cpus = [topology.cluster_cpus(k)[0] for k in sorted({c.cluster for c in topology.cores if c.cluster is not None})]
    out: dict[str, Any] = {"fused": None, "turbo": None, "tables": None, "hw_limits": None, "notes": {}, "ops": 0}
    notes: dict[str, str] = out["notes"]
    limit_cpus = {kind: cpus[0] for kind, cpus in (("P", p_cpus), ("E", e_cpus)) if cpus}  # one CPU per kind answers for its kind
    guards: list[WriteGuard] = []
    try:
        with _guarded_session(helper, ctx, policy, len(p_cpus) * 2 + len(e_cpus) * 2 + 4 + 2 * len(limit_cpus), "the fused-point / CPU-limit / turbo-table read") as session:
            guards.append(session.guard)
            pacer = vfdump.Pacer(policy.min_dwell_s, session.clock, session.sleep)
            if not helper.cfg.simulator:
                pacer.mark()  # the dump that just ended was a mailbox session too; the simulator's session already waited the dwell out (virtual clock)

            def mailbox(cpu: int, cmd: int, p1: int, what: str) -> int:
                if ctx.cancel.is_set():
                    raise OpError(CANCELLED, "tune.read was cancelled between transactions")
                pacer.wait()
                if ctx.cancel.is_set():  # the dwell wait is woken by the cancel flag
                    raise OpError(CANCELLED, "tune.read was cancelled between transactions")
                try:
                    res = session.guard.execute(Request(cpu, cmd, p1, 0, 0, f"tune.read: {what} of cpu {cpu}"), session.confirm)
                finally:
                    pacer.mark()
                if res.response.rc != 0:
                    raise OpError(REFUSED, f"cpu {cpu}: mailbox completion code {res.response.rc} ({rc_name(res.response.rc)})")
                return res.response.data

            def plain(msr: int) -> int:
                if ctx.cancel.is_set():
                    raise OpError(CANCELLED, "tune.read was cancelled between transactions")
                pacer.wait()  # a plain rdmsr starts no dwell of its own (not a WRMSR) but meets the guard's dwell check since the last mailbox request
                if ctx.cancel.is_set():
                    raise OpError(CANCELLED, "tune.read was cancelled between transactions")
                return session.guard.execute(MsrRequest(p_cpus[0], msr, 0, f"tune.read: read {msr:#x} (plain rdmsr)", "read"), session.confirm).value

            def do_fused() -> None:
                out["fused"] = {cpu: _fused_entry(mailbox(cpu, _FUSED_CMD, _FUSED_P1, "read the fused V/F point (0x09)")) for cpu in p_cpus + e_cpus}

            favored: dict[int, int] = {}
            favored_full: dict[int, int] = {}

            def do_favored() -> None:
                for cpu in p_cpus:
                    word = mailbox(cpu, int(Cmd.FAVORED_CORE_READ), 0, "read the favored ratio (0x1C)")
                    favored[cpu], favored_full[cpu] = word & 0xFF, word
                for cpu in e_cpus:  # G-137: the E fused word ([15:8]) and E ratio ([7:0]) of each cluster: the data of an "auto" E ratio ceiling; not a favored ratio (E clusters follow 0x650)
                    favored_full[cpu] = mailbox(cpu, int(Cmd.FAVORED_CORE_READ), 0, "read the fused/E ratio word (0x1C)")

            def do_limits() -> None:
                limits: dict[str, dict[str, Any]] = {}
                out["hw_limits"] = limits  # filled kind by kind: a read that fails later leaves the earlier ones
                for kind, cpu in limit_cpus.items():
                    entry: dict[str, Any] = {"ratio_max_oc": None, "v_target_max_mv": None, "offset_mv": [ENCODING_OFFSET_FLOOR_MV, ENCODING_OFFSET_CEILING_MV]}
                    limits[kind] = entry
                    for what, cmd, fill in (
                        ("the CPU's ratio limit (0x01)", int(Cmd.OC_CAPABILITY_READ), _fill_oc_cap),
                        ("the CPU's maximum voltage (0x28)", int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ), _fill_v_max),
                    ):
                        try:
                            fill(entry, mailbox(cpu, cmd, 0, f"read {what}"))
                        except (OpError, GuardRefused) as exc:  # each read degrades alone: a refused 0x01 leaves the 0x28 limit readable
                            if isinstance(exc, OpError) and exc.code == CANCELLED or isinstance(exc, GuardRefused) and exc.reason is Refusal.TRIPPED:
                                raise
                            why = exc.message if isinstance(exc, OpError) else _refused_text(exc)
                            notes["limits"] = (notes.get("limits", "") + f" {kind}: {what} not read: {why}.").strip()

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
                        notes["counts"] = f"bucket counts not read: {_refused_text(exc, f'the policy has no msr_allow read entry for {msr:#x}')}"
                out["turbo"] = {
                    "P": {"ratios": list(turbo_bytes(ratios[0x1AD])), "counts": counts[0x1AE]},
                    "E": {"ratios": list(turbo_bytes(ratios[0x650])), "counts": counts[0x651]},
                    "favored": {},
                }

            for key, fn in (("fused", do_fused), ("favored", do_favored), ("limits", do_limits), ("turbo", do_turbo)):
                try:
                    fn()
                except OpError as exc:
                    if exc.code == CANCELLED:
                        raise
                    notes[key] = f"{_LABEL[key]} not read: {exc.message}"
                except GuardRefused as exc:
                    if exc.reason is Refusal.TRIPPED:
                        raise
                    notes[key] = f"{_LABEL[key]} not read: {_refused_text(exc)}"
            if out["turbo"] is not None:
                out["turbo"]["favored"] = {cpu: r for cpu, r in favored.items()}
            elif favored:
                out["turbo"] = {"P": None, "E": None, "favored": dict(favored)}
            out["favored_words"] = favored_full
    except OpError as exc:
        if exc.code == CANCELLED:
            raise
        for key in _LABEL:
            notes.setdefault(key, f"{_LABEL[key]} not read: {exc.message}")
    except GuardRefused as exc:
        for key in _LABEL:
            notes.setdefault(key, f"{_LABEL[key]} not read: {_refused_text(exc)}")
    except Exception as exc:  # best effort: these reads must never fail the tuning-word read that already succeeded
        for key in _LABEL:
            notes.setdefault(key, f"{_LABEL[key]} not read: {type(exc).__name__}: {exc}")
    finally:
        if guards:
            out["ops"] = guards[0].operations  # whatever ended the session, the operations it executed were counted by the guard
    return out


def _read_ring(helper: Helper, ctx: OpContext, policy: Policy, topology: Topology) -> tuple[dict[str, Any] | None, str | None, int]:
    """G-154 (D-045): the ring unit of ``tune.read``, in ITS OWN guarded session of at most ``RING_WALK_CAP + 1`` mailbox reads (``cmd 0x10 P1=2``, ``P2 = 0`` then the points until the first
    rc ``0x11``; one at a time, paced like the other reads). ``(ring document, note, operations executed)``: a ring the policy cannot read, a refusal or a stop leaves ``None`` and a note --
    the cores' read stands. A cancel raises ``CANCELLED``. The ring is package-level: it is read through the first CPU."""
    cap = ringmod.walk_cap(policy)
    if cap is None:
        return None, "ring not read: the policy has no read entry for cmd 0x10 P1=2 (the ring is left out; the cores' read stands)", 0
    cpu = ringmod.ring_cpu(topology)
    needed = ringmod.requests_worst(cap)
    ops = 0
    try:
        with _guarded_session(helper, ctx, policy, needed, "the ring read") as session:
            pacer = vfdump.Pacer(policy.min_dwell_s, session.clock, session.sleep)
            if not helper.cfg.simulator:
                pacer.mark()  # the reads that just ended were mailbox traffic too; the simulator's session already waited the dwell out (virtual clock)

            def execute(req: Any) -> Any:
                if ctx.cancel.is_set():
                    raise KeyboardInterrupt  # ringmod.walk turns it into a Stop; the cancel is raised below
                return session.guard.execute(req, session.confirm)

            curve, stop = ringmod.walk(cpu, execute, pacer, lambda exc: _refused_text(exc), cap)
            ops = session.guard.operations
    except OpError as exc:
        if exc.code == CANCELLED:
            raise
        return None, f"ring not read: {exc.message}", ops
    if ctx.cancel.is_set():
        raise OpError(CANCELLED, "tune.read was cancelled between transactions")
    if stop is not None:
        return None, f"ring not read: {stop.text}", ops
    if curve.oc is None:
        return None, "ring not read: the OC word was not read", ops
    return ringmod.to_json(curve), None, ops


def _refused_text(exc: GuardRefused, no_entry: str = "the policy has no allow entry for it") -> str:
    """What a guard refusal of a ``tune.read`` extra says, by its reason (G-141): only ``NOT_ALLOWLISTED`` is "no allow entry"; a ``DWELL``, a ``CHECK_FAILED`` ... says itself."""
    head = f"the guard refused ({exc.reason.value})"
    return f"{head}; {no_entry}" if exc.reason is Refusal.NOT_ALLOWLISTED else f"{head}: {exc.detail}"


def _fill_oc_cap(entry: dict[str, Any], word: int) -> None:
    """``0x01 P1=0`` word -> ``ratio_max_oc`` (``[7:0]`` fused max OC ratio); ratio overclocking not ok (``[8]`` clear) or 0 is no limit (``None`` = no raise)."""
    entry["ratio_max_oc"] = hw_ratio_max([word])


def _fill_v_max(entry: dict[str, Any], word: int) -> None:
    """``0x28 P1=0`` word -> ``v_target_max_mv`` (``[11:0] - 1 LSB`` in whole mV, XTU's rule)."""
    entry["v_target_max_mv"] = hw_v_max_mv(word)


def _merge_hw_limits(old: Mapping[str, Mapping[str, Any]] | None, new: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, Any]] | None:
    """A new read replaces the fields it read; a field it could not read keeps the value of the earlier read of this boot (the fused limits do not change at runtime)."""
    if not new:
        return None if old is None else {k: dict(v) for k, v in old.items()}
    out = {k: dict(v) for k, v in (old or {}).items()}
    for kind, entry in new.items():
        merged = out.setdefault(kind, {})
        for key, value in entry.items():
            if value is not None or key not in merged:
                merged[key] = value
    return out


def _read_at(epoch: float) -> str:
    """ISO-8601 UTC, second resolution (``2026-10-12T09:49:05Z``): the time a read was taken, as the page shows it."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _fused_map(topology: Topology, fused: Mapping[int, Mapping[str, Any]] | None) -> dict[int, int]:
    """cpu -> live ``0x09`` word for every CPU of every unit read (an E cluster is read on its first CPU; the other CPUs share its word)."""
    out: dict[int, int] = {}
    for cpu, entry in (fused or {}).items():
        try:
            word = int(entry["word"], 16)
            cpus = topology.unit_cpus(topology.unit_of_cpu(int(cpu)))
        except (KeyError, TypeError, ValueError, UnknownCore):
            continue
        for c in cpus:
            out[c] = word
    return out


def _favored_crosscheck(topology: Topology, turbo: Mapping[str, Any] | None) -> str | None:
    """After a read: the P-cores whose ``0x1C[7:0]`` ratio is the highest (none when all are equal) against the favored set sysfs reported. A note when they differ;
    nothing when ``0x1C`` was not read."""
    favored = (turbo or {}).get("favored") if isinstance(turbo, Mapping) else None
    if not isinstance(favored, Mapping) or not favored:
        return None
    ratios = {int(c): int(r) for c, r in favored.items()}
    by_1c = {c for c, r in ratios.items() if r == max(ratios.values())} if len(set(ratios.values())) > 1 else set()
    by_sysfs = {c.cpu for c in topology.cores if c.favored}
    if by_1c == by_sysfs:
        return None

    def names(cpus: set[int]) -> str:
        return ", ".join(sorted((topology.unit_of_cpu(c) for c in cpus), key=lambda u: int(u[1:]))) or "none"

    return (
        f"favored cores differ: sysfs ({topology.favored_source or 'no source'}) says {names(by_sysfs)}; the 0x1C favored ratios say {names(by_1c)}. "
        "Both are shown as read; nothing was changed."
    )


def op_tune_read(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    doc = vf_dump_tracked(helper, ctx, args)
    topology = _topology(helper)
    result = dict(doc)
    result["units"] = [dict(u) for u in doc.get("units", [])]
    st = _state(helper)
    policy: Policy | None = None
    try:
        policy = _load_policy(helper)
        extras = _read_extras(helper, ctx, policy, topology)
    except OpError as exc:
        if exc.code == CANCELLED:
            raise
        extras = {"fused": None, "turbo": None, "tables": None, "hw_limits": None, "ops": 0, "notes": {k: f"{_LABEL[k]} not read: {exc.message}" for k in _LABEL}}
    notes = extras["notes"]
    partial = args.get("cpus") is not None  # a read of some CPUs only is not a read of the tuning state: it leaves the ring (and its kept words) alone
    if partial:
        ring_doc, ring_note, ring_ops = None, "ring not read: this read named CPUs", 0
    elif policy is not None:  # G-154: the ring, in its own session after the cores' extras
        ring_doc, ring_note, ring_ops = _read_ring(helper, ctx, policy, topology)
    else:
        ring_doc, ring_note, ring_ops = None, "ring not read: the policy could not be loaded", 0
    if ring_note:
        notes["ring"] = ring_note
    if not partial:
        _set_last_ring(helper, ringmod.from_json(ring_doc))
    tables = extras["tables"]
    note = notes.get("turbo")
    with st.lock:
        st.tables, st.tables_note = tables, note
        st.fused, st.turbo = extras["fused"], extras["turbo"]
        st.favored_words = extras.get("favored_words") or None
        st.hw_limits = _merge_hw_limits(st.hw_limits, extras.get("hw_limits"))
        hw_limits = None if st.hw_limits is None else {k: dict(v) for k, v in st.hw_limits.items()}
    fused_words = _fused_map(topology, extras["fused"])
    if fused_words:
        helper.fused_words = fused_words
    result["tune"] = _decode_document(result, topology, helper.fused_words)
    result["intent_features"] = list(INTENT_FEATURES)  # G-212 (additive): the optional intent keys tune.plan understands ("ring" = ring points by P2 number); a front-end gates its editor on it
    result["ring"] = ring_doc  # G-154: {unit, cpu, oc, points, end} in the shape of a cpus[] entry; None = not read (see extras_notes.ring); before the compare, which judges it
    matched, accepted_note = accepted_match(helper, result, helper.fused_words)  # redone with the fused words just read (the dump's own compare ran before them)
    result["accepted_match"], result["accepted_note"] = matched, accepted_note
    result["tables"] = None if tables is None else {k: list(v) for k, v in tables.items()}
    result["tables_note"] = None if tables is not None else (note or "turbo tables not read")
    fused, turbo = extras["fused"], extras["turbo"]
    result["fused"] = None if fused is None else {str(c): v for c, v in fused.items()}  # contract 9 addendum: {first cpu of the unit (string): {word, ratio, v}}
    result["turbo"] = None if turbo is None else {**turbo, "favored": {str(c): r for c, r in turbo["favored"].items()}}  # {P: {ratios, counts}, E: {...}, favored: {cpu (string): ratio}}
    result["hw_limits"] = hw_limits  # G-137 (D-039): {P|E: {ratio_max_oc, v_target_max_mv, offset_mv: [-1000, 999]}}, the CPU's own limits; None = not read
    result["extras_notes"] = notes
    # G-141: ops_done = every mailbox operation of this read (the page shows "N mailbox operations used"): the vf.dump words plus the guarded extras (0x09 / 0x1C / 0x01 / 0x28).
    result["ops_words"], result["ops_extras"], result["ops_ring"] = int(doc.get("ops_done", 0)), int(extras.get("ops", 0)), int(ring_ops)
    result["ops_done"] = result["ops_words"] + result["ops_extras"] + result["ops_ring"]
    cross = _favored_crosscheck(topology, turbo)
    if cross:
        result["problems"] = [*result.get("problems", []), cross]
    result["read_at"] = _read_at(helper.cfg.wall_clock())
    _add_limits(helper, result, topology, extras.get("favored_words"))
    result = _fit(result)
    _save_last_read(helper, result, topology)
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


def _save_last_read(helper: Helper, result: dict[str, Any], topology: Topology) -> None:
    """Store a *complete* read (every unit of the topology) with the boot id. Best effort: a store problem is logged, never an error of the read that succeeded."""
    try:
        boot = _current_boot(helper)
        ids = {u.get("id") for u in result.get("units", []) if isinstance(u, dict)}
        if boot is None or result.get("complete") is False or not set(topology.unit_ids()) <= ids:
            return
        words = _words_of_document(result)
        if not words:
            return
        record = {
            "version": 1, "boot_id": boot, "at": helper.cfg.wall_clock(), "applied_at": None,
            "words": {str(cpu): [f"{w:#010x}" for w in ws] for cpu, ws in words.items()}, "result": result,
        }
        ring_words = ringmod.from_json(result.get("ring"))
        if ring_words:
            record["ring_words"] = [f"{w:#010x}" for w in ring_words]  # G-154: authoritative like "words"; an apply updates it
        st = _state(helper)
        with st.lock:
            favored = dict(st.favored_words or {})
        if favored:
            record["favored_words"] = {str(cpu): f"{w:#010x}" for cpu, w in favored.items()}  # G-133: the "auto" ratio ceiling after a helper restart
        with st.lock:
            record["audit_mark"] = st.audit_mark  # G-141: where in the audit log these words are valid from (profiles.accept refuses a stale read)
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
        if getattr(helper, "_last_restored", False) or getattr(helper, "_restoring", False):
            return  # done, or this very thread is inside the restore already (it holds the lock; another thread waits here until the restore has finished)
        helper._restoring = True  # type: ignore[attr-defined]
        try:
            _restore_last_locked(helper)
        finally:
            helper._restoring = False  # type: ignore[attr-defined]
            helper._last_restored = True  # type: ignore[attr-defined]  # G-141: set when the restore is DONE, so a concurrent tune.plan never sees "restored" before the words are there


def _restore_last_locked(helper: Helper) -> None:
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
                helper.fused_words = _fused_map(_topology(helper), st.fused)
            limits = result.get("hw_limits")
            if st.hw_limits is None and isinstance(limits, dict):
                st.hw_limits = {str(k): dict(v) for k, v in limits.items() if isinstance(v, dict)}
            turbo = result.get("turbo")
            if st.turbo is None and isinstance(turbo, dict):
                st.turbo = {**turbo, "favored": {int(c): int(r) for c, r in (turbo.get("favored") or {}).items()}}
            favored = record.get("favored_words")  # G-141: the 0x1C words of the read: plan-time checks and the apply session's seed match the ones before the restart
            if st.favored_words is None and isinstance(favored, dict) and favored:
                st.favored_words = {int(c): int(w, 16) for c, w in favored.items()}
            mark = record.get("audit_mark")
            if st.audit_mark is None and isinstance(mark, int) and not isinstance(mark, bool) and mark >= 0:
                st.audit_mark = mark
        _set_last_words(helper, words)
        stored = record.get("ring_words")  # G-154
        try:
            ring_words = [int(w, 16) for w in stored] if isinstance(stored, list) and stored else ringmod.from_json(result.get("ring"))
        except (TypeError, ValueError):
            ring_words = None
        if getattr(helper, "last_ring", None) is None:
            _set_last_ring(helper, ring_words)
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
        ring = _last_ring(helper)
        if ring:  # G-154
            record["ring_words"] = [f"{w:#010x}" for w in ring]
        st = _state(helper)
        with st.lock:
            tables = None if st.tables is None else {k: list(v) for k, v in st.tables.items()}
            turbo = None if st.turbo is None else {**st.turbo, "favored": {str(c): r for c, r in (st.turbo.get("favored") or {}).items()}}
            record["audit_mark"] = st.audit_mark
        result = record.get("result")
        if isinstance(result, dict):  # G-141: tune.last and a restart must show the turbo tables the apply wrote
            if tables is not None:
                result["tables"] = tables
            if turbo is not None:
                result["turbo"] = turbo
        record["applied_at"] = helper.cfg.wall_clock()
        _last_store(helper).save(record)
    except (LastReadError, OSError, ValueError, TypeError, KeyError) as exc:
        _log(helper, f"last-read not updated after the apply: {exc}")


def op_tune_last(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``{available: false}``, or ``{available: true, boot_id, at, applied_at, result}`` with ``result`` shaped like ``tune.read``'s. After an apply the
    stored words (the verified read-backs) are overlaid and the decoded copy rebuilt; the accepted comparison is always redone against the accepted state as it is now."""
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
        stored = record.get("ring_words")  # G-154: the ring follows the verified read-backs too
        if isinstance(result.get("ring"), dict) and isinstance(stored, list) and stored:
            try:
                ring_words = [int(w, 16) for w in stored]
                result["ring"] = {**result["ring"], "oc": ringmod.word_json(ring_words[0]), "points": [{"point": n, **ringmod.word_json(w)} for n, w in enumerate(ring_words[1:], start=1)]}
            except (TypeError, ValueError):
                pass
    result["intent_features"] = list(INTENT_FEATURES)  # G-212: set at response time, so a record stored by an older helper still advertises it
    matched, note = accepted_match(helper, result, helper.fused_words)  # G-129: always redone (a file read, no hardware): the accepted state can change after the read (profiles.accept)
    result["accepted_match"], result["accepted_note"] = matched, note
    result["tune"] = _decode_document(result, topology, helper.fused_words)
    st = _state(helper)
    with st.lock:
        favored = dict(st.favored_words or {})
    if not favored:  # a helper restarted since the read: the 0x1C words stored with it
        try:
            favored = {int(c): int(w, 16) for c, w in (record.get("favored_words") or {}).items()}
        except (AttributeError, TypeError, ValueError):
            favored = {}
    _add_limits(helper, result, topology, favored)  # G-133: the policy as it is now, not as it was at the read
    if not isinstance(result.get("read_at"), str) and isinstance(record.get("at"), (int, float)):
        result["read_at"] = _read_at(float(record["at"]))  # a record stored before read_at existed: the time it was stored
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
    if not isinstance(target, str) or not (target in ("P", "E") if scope == "simple" else is_unit_id(target)):
        raise _bad(f"row target {target!r} is not {'P or E' if scope == 'simple' else 'a unit (P<n> / EC<n>)'} (scope {scope!r})")
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
                    raise _bad(f"offset.mv key {key!r} is not a ratio ({_RATIO_RANGE[0]}..{_RATIO_RANGE[1]}, {RATIO_SOURCE})")
                canon[str(int(key))] = _int_mv(value, f"offset.mv[{key}]")
            canon_offset = {"kind": "points", "mv": canon}
        else:
            raise _bad(f"offset.kind {offset['kind']!r} is not 'curve' or 'points'")
    v = raw.get("v_target_v")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not V_TARGET_RANGE_MV[0] <= round(v * 1000) <= V_TARGET_RANGE_MV[1]):
        raise _bad(f"v_target_v must be null or a voltage in volts ({V_TARGET_RANGE_MV[0] / 1000:g}..{V_TARGET_RANGE_MV[1] / 1000:g}, {V_TARGET_SOURCE})")
    ratio = raw.get("ratio")
    if ratio is not None and (not _is_int(ratio) or not _RATIO_RANGE[0] <= ratio <= _RATIO_RANGE[1]):
        raise _bad(f"ratio must be null or an integer {_RATIO_RANGE[0]}..{_RATIO_RANGE[1]} ({RATIO_SOURCE})")
    if canon_offset is None and v is None and ratio is None:
        return None  # a row that changes nothing is dropped, like an unchanged row of the UI
    return {"target": target, "offset": canon_offset, "v_target_v": None if v is None else float(v), "ratio": ratio}


def _canonical_turbo(raw: object) -> dict[str, list[int]]:
    """``{P: [8 ints], E: [8 ints]}`` (either key may be absent, not both): the bucket ratios of ``0x1AD`` / ``0x650``, bucket 0 = 1 active core."""
    if not isinstance(raw, dict) or not raw or not set(raw) <= {"P", "E"}:
        raise _bad("turbo must be {P: [8 ratios], E: [8 ratios]} (either key may be absent)")
    out: dict[str, list[int]] = {}
    for key, table in raw.items():
        if not isinstance(table, list) or len(table) != 8 or not all(_is_int(r) and TURBO_RATIO_RANGE[0] <= r <= TURBO_RATIO_RANGE[1] for r in table):
            raise _bad(f"turbo.{key} must be a list of 8 integer ratios {TURBO_RATIO_RANGE[0]}..{TURBO_RATIO_RANGE[1]} ({RATIO_SOURCE}, one byte per bucket)")
        out[key] = list(table)
    return out


#: G-212: the intent features this helper understands beyond ``{scope, rows[, turbo]}``; ``tune.read`` / ``tune.last`` carry it as ``intent_features`` so a front-end can detect support.
INTENT_FEATURES = ("ring",)


def _canonical_ring(raw: object) -> dict[str, Any]:
    """G-212 (D-045, D-046): ``{points: {"<P2>": mV}}`` -- the ring (cache, P1 = 2) points to move, keyed by the **P2 point number** (1..15; ``0`` is the BIOS-owned ring OC word and is
    never written), offset-only. Nothing else is accepted: the ring has no OC ratio, V target or whole-curve offset to set here."""
    if not isinstance(raw, dict) or set(raw) != {"points"}:
        raise _bad("ring must be {points: {P2: mV}}")
    points = raw["points"]
    if not isinstance(points, dict) or not points or len(points) > POINT_RANGE[1]:
        raise _bad("ring.points must be a non-empty object {P2 point number: mV}")
    out: dict[str, int] = {}
    for key, value in points.items():
        if not isinstance(key, str) or not key.isdigit() or not POINT_RANGE[0] <= int(key) <= POINT_RANGE[1]:
            raise _bad(f"ring.points key {key!r} is not a ring point number ({POINT_RANGE[0]}..{POINT_RANGE[1]}; P2 = 0 is the ring OC word, which is never written)")
        out[str(int(key))] = _int_mv(value, f"ring.points[{key}]")
    return {"points": dict(sorted(out.items(), key=lambda kv: int(kv[0])))}


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
    if keys and keys <= {"scope", "rows", "turbo", "ring"} and keys & {"rows", "turbo", "ring"} and ("rows" in keys) == ("scope" in keys):
        if rows_only and keys & {"turbo", "ring"}:
            raise _bad("only a {scope, rows} intent can be saved as a profile")
        out: dict[str, Any] = {}
        turbo = _canonical_turbo(raw["turbo"]) if "turbo" in keys else None
        ring = _canonical_ring(raw["ring"]) if "ring" in keys else None
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
            if not canon_rows and turbo is None and ring is None:
                raise OpError(BAD_REQUEST, "the intent changes nothing (every row is empty)", {"check": "NOTHING_TO_DO"})
            if canon_rows:
                out = {"scope": scope, "rows": canon_rows}
        if turbo is not None:
            out["turbo"] = turbo
        if ring is not None:
            out["ring"] = ring
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
    raise _bad("intent must be {scope, rows[, turbo][, ring]}, {turbo}, {ring}, {profile}, {reset: 'stock'} or {restore: 'accepted'}")


# --------------------------------------------------------------------------------------------------------------
# Accepted state (/etc/arltuner/accepted/CURRENT: one profile since D-044) and saved profiles
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
            if exc.reason == "does not exist" and exc.path in (str(path), str(directory)):
                raise OpError(REFUSED, f"{NO_ACCEPTED_NOTE}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None
            raise OpError(REFUSED, f"accepted state untrusted: {exc}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None
    try:
        fd = open_checked(path, write=False, create=False, euid=cfg.euid_fn(), what="accepted state")
    except FileNotFoundError:
        raise OpError(REFUSED, f"{NO_ACCEPTED_NOTE}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None
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


#: G-148 (D-044): what to do with an accepted state that is still a chain (several ``profile =`` lines, applied in order before D-044).
CHAIN_HINT = (
    "run 'sudo python3 -I tools/arltuner.py accepted-migrate' once: it builds one complete profile from EXPECTED-words.txt (never from a read) and keeps a backup"
)


def _accepted_paths(helper: Helper) -> list[str]:
    """The ``profile =`` paths of ``accepted/CURRENT`` (validated; ``NO_ACCEPTED_STATE`` when there is none or a path is unacceptable). D-044: one is the accepted
    state; several are an older chain that only ``accepted-migrate`` reads."""
    text = _read_accepted_text(helper, "CURRENT")
    out: list[str] = []
    for rel in current_profile_lines(text):
        if not _ACCEPTED_PATH_RE.fullmatch(rel) or ".." in rel or not rel.endswith(".toml"):
            raise OpError(REFUSED, f"accepted CURRENT names an unacceptable profile path {rel!r}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"})
        out.append(rel)
    if not out:
        raise OpError(REFUSED, "accepted CURRENT lists no profile. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"})
    return out


def _accepted_profile(helper: Helper) -> tuple[str, Profile]:
    """``(name, profile)`` of the one accepted profile (the name is the file stem). An older chain is refused (``ACCEPTED_CHAIN``): D-044 never replays a chain."""
    paths = _accepted_paths(helper)
    if len(paths) > 1:
        names = ", ".join(Path(p).stem for p in paths)
        raise OpError(
            REFUSED,
            f"the accepted state is an older chain of {len(paths)} profiles ({names}); the accepted state is now one complete profile (D-044): {CHAIN_HINT}. Nothing was sent.",
            {"check": "ACCEPTED_CHAIN"},
        )
    rel = paths[0]
    try:
        return Path(rel).stem, load_profile(_read_accepted_text(helper, rel))
    except ProfileError as exc:
        raise OpError(REFUSED, f"accepted profile {rel} is invalid: {exc}. Nothing was sent.", {"check": "NO_ACCEPTED_STATE"}) from None


def _accepted_names(helper: Helper) -> list[str]:
    """The names ``CURRENT`` lists (one, or the members of an older chain); empty when there is no accepted state."""
    try:
        return [Path(p).stem for p in _accepted_paths(helper)]
    except OpError:
        return []


def _check_complete(helper: Helper, profile: Profile, topology: Topology) -> None:
    """G-148: a complete profile is refused on another CPU (``PROFILE_TOPOLOGY``): every unit of the live topology must be in it, and each unit's recorded top fused ratio
    must be the one this boot's read found (``0x09``; units not read are judged by the planner from the apply's own read)."""
    if not profile.complete:
        return
    try:
        profile.check_complete(topology)
    except ProfileError as exc:
        raise OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": "PROFILE_TOPOLOGY"}) from None
    fused = getattr(helper, "fused_words", None) or {}
    for e in profile.entries:
        cpu = topology.resolve(e.kind, e.index).cpu
        unit = topology.unit_of_cpu(cpu)
        word = next((fused[c] for c in topology.unit_cpus(unit) if c in fused), None)
        top = fused_top_ratio(word)
        if top is not None and e.fused_top_ratio is not None and top != e.fused_top_ratio:
            raise OpError(
                REFUSED,
                f"{unit} reads top fused ratio {top} (0x09) but the complete profile was made where it was {e.fused_top_ratio}: it belongs to another CPU. Nothing was sent.",
                {"check": "PROFILE_TOPOLOGY"},
            )


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


def _ratio_cap(helper: Helper, policy: Policy, kind: str) -> tuple[int | None, str]:
    """The ratio ceiling of a kind at plan time and where it comes from (G-137, D-039): the policy value (for ``"auto"`` the fused ``0x1C`` values of that kind read by
    this boot's ``tune.read`` plus 3) clamped by the CPU's own ``0x01`` limit that ``policy`` is bound to. ``None`` = not known: the guard judges it when the plan is applied."""
    st = _state(helper)
    with st.lock:
        words = dict(st.favored_words or {})
    return ratio_limit(policy, kind, [w for cpu, w in words.items() if policy.core_kind(cpu) == kind])


def _check_range(policy: Policy, unit: str, cpus: Sequence[int], row: Mapping[str, Any], ratio_cap: tuple[int | None, str] | None = None) -> None:
    """``REFUSED POLICY_RANGE`` naming the field, the limit and where the limit comes from (policy file / CPU / field encoding), before anything else is computed."""
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
                    f"(policy effective_offset_floor_mv = {policy.effective_offset_floor_mv}, offset_ceiling_mv = {policy.offset_ceiling_mv}; {policy.where}; "
                    f"the field encoding allows {ENCODING_OFFSET_FLOOR_MV}..{ENCODING_OFFSET_CEILING_MV} mV)"
                )
    v = row["v_target_v"]
    if v is not None:
        mv = round(v * 1000)
        cap_raw, text = v_target_limit(policy, kind, None)  # the 0x28 limit comes from the policy binding (tune.read); the guard judges the word of the session when applied
        if cap_raw is not None and VfWord.v_to_raw(mv / 1000) > cap_raw:
            raise refuse(f"{unit}: OC-point voltage {mv} mV is above {text}")
    ratio = row["ratio"]
    if ratio is not None and ratio_cap is not None:
        cap, text = ratio_cap
        if cap is not None and ratio > cap:
            raise refuse(f"{unit}: ratio {ratio} is above {text}")


def _rows_to_profile(helper: Helper, intent: Mapping[str, Any], policy: Policy, topology: Topology, name: str) -> Resolved:
    last = _last_words(helper)
    warnings: list[str] = []
    cores: dict[tuple[str, int], dict[str, int | None]] = {}
    points: dict[tuple[str, int, int], PointSetting] = {}
    refs: dict[tuple[int, int], int] = {}
    units: list[str] = []
    asked_ratio: dict[str, int] = {}
    groups = groups_of(topology)
    for row in intent.get("rows", ()):
        targets = groups[row["target"]] if row["target"] in groups else (row["target"],)
        for unit in targets:
            try:
                cpus = unit_cpus(topology, unit)
            except UnknownCore:
                raise OpError(BAD_REQUEST, f"{unit} is not a unit of this system (units: {', '.join(topology.unit_ids())})") from None
            _check_range(policy, unit, cpus, row, _ratio_cap(helper, policy, _kind_of(unit)))
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
                first = next(iter(known))
                words = known[first]
                top = fused_top_ratio(helper.fused_words.get(first))
                by_ratio = {
                    VfWord.from_u32(w).ratio: n for n, w in enumerate(words[1:], start=1) if top is None or VfWord.from_u32(w).ratio <= top
                }  # the OC point (a ratio above the top fused ratio) is not a fused point
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
    ring = _ring_setting(helper, intent["ring"], policy, topology) if "ring" in intent else None  # G-212
    if ring is not None:
        units.append(ringmod.UNIT)
    try:
        entries = tuple(CoreSetting(k, i, **v) for (k, i), v in cores.items())  # type: ignore[arg-type]
        profile = Profile(name, entries, turbo, tuple(points.values()), ring=ring)  # G-119: a turbo-only plan has no entries and no points; G-212: a ring-only plan has neither
    except ProfileError as exc:
        raise OpError(BAD_REQUEST, f"intent is not a valid profile: {exc}") from None
    return Resolved(profile, refs, warnings, units)


def _check_ring_range(policy: Policy, topology: Topology, points: Mapping[str, int]) -> None:
    """``REFUSED POLICY_RANGE`` before anything else is computed (G-212): every ring point must have a policy ``0x11 P1=2`` write entry, and its offset must sit in the window the
    guard evaluates for a point offset write (``_check_vf_point_offset_bounded``: the runtime floor / ceiling of the policy, tightened by the entry's ``offset_raw_min`` / ``_max``)."""
    cpu = ringmod.ring_cpu(topology)
    floor_raw = policy.offset_floor_raw(cpu)
    ceil_raw = VfWord.mv_to_offset_raw(policy.offset_ceiling_mv)

    def refuse(text: str) -> OpError:
        return OpError(REFUSED, f"{text}. Nothing was sent.", {"check": "POLICY_RANGE"})

    for key, mv in points.items():
        p2 = int(key)
        entry = policy.match(int(Cmd.VOLTAGE_FREQ_WRITE), ringmod.RING_P1, p2)
        if entry is None or entry.kind != "write":
            raise refuse(f"ring point {p2}: the policy has no write entry for cmd 0x11 P1=2 P2={p2} ({policy.where})")
        low = max(floor_raw, entry.params.get("offset_raw_min", -1024))
        high = min(ceil_raw, entry.params.get("offset_raw_max", 1023))
        raw = VfWord.mv_to_offset_raw(mv)
        if not low <= raw <= high:
            raise refuse(
                f"ring point {p2}: offset {mv:+d} mV (raw {raw}) is outside the policy window {low * 1000 / 1024:+.1f}..{high * 1000 / 1024:+.1f} mV "
                f"(policy effective_offset_floor_mv = {policy.effective_offset_floor_mv}, offset_ceiling_mv = {policy.offset_ceiling_mv}, entry {entry.name!r}; {policy.where}; "
                f"the field encoding allows {ENCODING_OFFSET_FLOOR_MV}..{ENCODING_OFFSET_CEILING_MV} mV)"
            )


def _ring_setting(helper: Helper, asked: Mapping[str, Any], policy: Policy, topology: Topology) -> RingSetting:
    """G-212 (D-045): the ``[ring]`` of an intent's ``ring: {points: {P2: mV}}``, from this boot's last ring read: the ring OC word as read (compared by the planner with the live
    word and **never written**: a changed word refuses the ring unit alone) and, for every asked point, its offset with ``expect_ratio`` = the ratio the read found there. Offset-only; keyed
    by the P2 point number (not by ratio like a core's points)."""
    points: Mapping[str, int] = asked["points"]
    _check_ring_range(policy, topology, points)
    last = _last_ring(helper)
    if not last:
        raise _no_read(ringmod.UNIT, "the ring points are addressed by number and the ring was not read this boot")
    missing = [k for k in points if int(k) >= len(last)]
    if missing:
        raise OpError(BAD_REQUEST, f"the ring has no point {', '.join(missing)} (ring points of the last read: 1..{len(last) - 1})")
    try:
        return RingSetting(last[0], tuple(RingPoint(int(k), mv, VfWord.from_u32(last[int(k)]).ratio) for k, mv in points.items()))
    except ProfileError as exc:
        raise OpError(BAD_REQUEST, f"intent is not a valid profile: {exc}") from None


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
        cap, text = _ratio_cap(helper, policy, kind)
        if cap is not None and max(ratios) > cap:
            raise OpError(REFUSED, f"turbo.{kind}: ratio {max(ratios)} is above {text}. Nothing was sent.", {"check": "POLICY_RANGE"})
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
    for unit in topology.unit_ids():
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
    if resolve_ring_targets(profile, topology):  # G-154
        units.append(ringmod.UNIT)
    if _last_words(helper) is None:
        warnings.append("no tune.read this boot: 'before' is unknown; the apply re-reads every word and the points' expect_ratio guards the writes")
    return Resolved(profile, {}, warnings, units)


def resolve_intent(helper: Helper, intent: dict[str, Any], policy: Policy, topology: Topology) -> Resolved:
    if "rows" in intent or "turbo" in intent or "ring" in intent:
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()[:8]
        return _rows_to_profile(helper, intent, policy, topology, f"tune-{digest}")
    if "reset" in intent:
        return _reset_profile(helper, topology)
    if "restore" in intent:
        name, prof = _accepted_profile(helper)
        _check_complete(helper, prof, topology)
        how = "complete: only the words that differ from what the CPU holds are written" if prof.complete else "partial (accepted before G-148): its own entries only"
        return _profile_resolved(helper, prof, topology, [f"restore applies the accepted profile {name} ({how})"])
    name = intent["profile"]
    try:
        acc_name, acc = _accepted_profile(helper)
    except OpError:
        acc_name, acc = None, None
    if acc is not None and acc_name == name:
        _check_complete(helper, acc, topology)
        return _profile_resolved(helper, acc, topology, [f"accepted profile {name}"])
    try:
        record = _profile_store(helper).load(name)
    except ProfileStoreError as exc:
        raise OpError(BAD_REQUEST if exc.code == "NOT_FOUND" else REFUSED, exc.message, {"check": "PROFILE_" + exc.code}) from None
    prof = _saved_profile(record)
    if prof is not None:
        _check_complete(helper, prof, topology)
        return _profile_resolved(helper, prof, topology, [f"saved profile {name} (complete: only the words that differ from what the CPU holds are written)"])
    saved = canonical_intent(record["intent"], rows_only=True)
    resolved = _rows_to_profile(helper, saved, policy, topology, name)
    resolved.warnings.insert(0, f"saved profile {name} (partial, saved before G-148: its rows are applied over what the CPU holds)")
    return resolved


def _saved_profile(record: Mapping[str, Any]) -> Profile | None:
    """The complete profile of a saved record (G-148), or ``None`` for a legacy ``{intent}`` record; ``REFUSED PROFILE_INVALID`` when the stored text does not load."""
    text = record.get("profile")
    if not isinstance(text, str):
        return None
    try:
        return load_profile(text if "\n" in text else text + "\n")
    except ProfileError as exc:
        raise OpError(REFUSED, f"saved profile {record.get('name')!r} is invalid: {exc}. Nothing was sent.", {"check": "PROFILE_INVALID"}) from None


# --------------------------------------------------------------------------------------------------------------
# before / after, predicted steps
# --------------------------------------------------------------------------------------------------------------
def _fill_ratio(turbo: Mapping[str, Any] | None, kind: str, cpu: int) -> int | None:
    """The ratio the planner's XTU fill takes for a cold OC word (``0x1C[7:0]``): a P-core's own favored ratio; an E core follows the first bucket of the E table."""
    if not turbo:
        return None
    if kind == "P":
        return (turbo.get("favored") or {}).get(cpu) or None
    ratios = (turbo.get("E") or {}).get("ratios")
    return ratios[0] if ratios else None


def _before_after(
    profile: Profile,
    topology: Topology,
    last: Mapping[int, Sequence[int]] | None,
    fused: Mapping[int, int] | None = None,
    turbo: Mapping[str, Any] | None = None,
    last_ring: Sequence[int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``{unit: {point: {ratio, offset_mv, v_target_v}}}`` for the words the plan writes; point ``"0"`` is the OC word. ``before`` is ``null`` for a
    word that was not read; a field that reads 0 (unset) is ``null``. G-154: the ring points of the profile are the unit ``"ring"`` (its OC word is compared, never written: no
    entry for point ``"0"``), ``last_ring`` = ``[OC word, point 1, ...]`` of the last read."""
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
        top = fused_top_ratio((fused or {}).get(setting.cpu))  # the core's own top fused ratio, from this boot's 0x09 read (None = not read: nothing is dropped)
        if goal.ratio:
            goal = kept_word(top, goal)  # G-124: no OC point (ratio <= the top fused ratio) = the hardware keeps no V target, whatever the offset
        elif goal.v_target_raw:
            # a cold word (ratio field 0): the planner fills the ratio from 0x1C[7:0] (the plan sets an OC voltage, so the fill is not capped) -- decide the target on
            # that filled ratio, show the field as read (0 = unset). The fill comes from this boot's read (P: 0x1C of the core; E: the first bucket of the E table).
            fill = _fill_ratio(turbo, kind, setting.cpu)
            if fill is not None:
                goal = kept_word(top, goal.with_(ratio=fill)).with_(ratio=0)
        before.setdefault(unit, {})["0"] = None if cur is None else show(cur)
        after.setdefault(unit, {})["0"] = show(goal)
    for setting in profile.expand_points(topology):
        unit = unit_of_cpu(topology, setting.cpu)
        ref = _unit_reference(last, setting.cpus)
        cur = VfWord.from_u32(ref[1][setting.point]) if ref and len(ref[1]) > setting.point else None
        goal = (cur or VfWord(setting.expect_ratio or 0, 0, False, 0)).with_(offset_raw=VfWord.mv_to_offset_raw(setting.offset_mv))
        before.setdefault(unit, {})[str(setting.point)] = None if cur is None else show(cur)
        after.setdefault(unit, {})[str(setting.point)] = show(goal)
    if profile.ring is not None:
        for rp in profile.ring.points:
            cur = VfWord.from_u32(last_ring[rp.point]) if last_ring and len(last_ring) > rp.point else None
            goal = (cur or VfWord(rp.expect_ratio, 0, False, 0)).with_(offset_raw=VfWord.mv_to_offset_raw(rp.offset_mv))
            before.setdefault(ringmod.UNIT, {})[str(rp.point)] = None if cur is None else show(cur)
            after.setdefault(ringmod.UNIT, {})[str(rp.point)] = show(goal)
    return before, after


# --------------------------------------------------------------------------------------------------------------
# SPEC-v3 section 9: predictions (F4 clamp, F6 VccIA, F8 warnings) from the newest baseline curve
# --------------------------------------------------------------------------------------------------------------
def _fused_ratios(words: Sequence[int], top: int | None) -> tuple[int, ...]:
    """The fused V/F point ratios of a core from its words (``words[0]`` = the OC word, ``words[n]`` = point ``n``): every point, without the one that sits above the
    core's top fused ratio ``top`` (live ``0x09`` ``[7:0]``) -- that one is the OC point. With ``top`` unknown (``0x09`` not read) every point counts."""
    ratios = [VfWord.from_u32(w).ratio for w in words[1:]]
    return tuple(r for r in ratios if r and (top is None or r <= top))


def _unit_point_ratios(helper: Helper, topology: Topology) -> dict[str, tuple[int, ...]]:
    """unit -> its fused point ratios from this boot's kept read (units not read are absent)."""
    last = _last_words(helper) or {}
    out: dict[str, tuple[int, ...]] = {}
    for unit in topology.unit_ids():
        ref = _unit_reference(last, topology.unit_cpus(unit))
        if ref is not None:
            out[unit] = _fused_ratios(ref[1], fused_top_ratio(helper.fused_words.get(ref[0])))
    return out



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
    ratios: tuple[int, ...]  # the fused point ratios (index n-1 = point n); empty = the unit was not read, nothing can be predicted
    cur_offsets: dict[int, float]  # ratio -> mV of the words last read (0 where nothing was read)
    new_offsets: dict[int, float]  # ... after the plan
    baseline: dict[int, float | None] | None
    oc_ratio: int | None = None  # the plan's OC ratio for this unit
    oc_v: float | None = None  # the plan's OC-point voltage target (V)
    cur_oc_ratio: int | None = None  # the ratio of the OC word of the last read (None = not read / 0)
    touched: bool = False


def _unit_models(
    profile: Profile,
    topology: Topology,
    last: Mapping[int, Sequence[int]] | None,
    record: dict[str, Any] | None,
    fused: Mapping[int, Mapping[str, Any]] | None,
    fused_words: Mapping[int, int] | None = None,
) -> dict[str, _UnitModel]:
    settings = {unit_of_cpu(topology, s.cpu): s for s in profile.expand(topology)}
    point_settings: dict[str, dict[int, Any]] = {}
    for ps in profile.expand_points(topology):
        point_settings.setdefault(unit_of_cpu(topology, ps.cpu), {})[ps.point] = ps
    models: dict[str, _UnitModel] = {}
    for unit in topology.unit_ids():
        kind = _kind_of(unit)
        cpus = unit_cpus(topology, unit)
        ref = _unit_reference(last, cpus)
        if ref is not None:
            top = fused_top_ratio((fused_words or {}).get(ref[0]))
            words = [VfWord.from_u32(w) for w in ref[1][1:] if VfWord.from_u32(w).ratio and (top is None or VfWord.from_u32(w).ratio <= top)]  # past the table = the OC point
            ratios = tuple(w.ratio for w in words)
            cur = {w.ratio: w.offset_mv for w in words}
        else:
            ratios, cur = (), {}
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
            if measured and ratios:
                baseline = complete_baseline(ratios, measured, fused_v)
        m = _UnitModel(unit, kind, cpus, ratios, cur, new, baseline, touched=st is not None or bool(explicit))
        if ref is not None:
            m.cur_oc_ratio = VfWord.from_u32(ref[1][0]).ratio or None
        if st is not None:
            m.oc_ratio = st.ratio
            m.oc_v = None if st.oc_voltage_mv is None else st.oc_voltage_mv / 1000
        models[unit] = m
    return models


def _allowed_ratios(m: _UnitModel, turbo: Mapping[str, Any] | None, plan: bool) -> tuple[int, int] | None:
    """(1-2 active cores, all cores): the highest ratio the unit is allowed at, from the live turbo tables (``0x1AD`` / ``0x650``) and the favored word (``0x1C``);
    with ``plan`` the plan's OC ratio raises both (a ratio above the table raises the table, ``_turbo_tables``). ``None`` when the tables were not read this boot
    (nothing is assumed)."""
    ratios = ((turbo or {}).get(m.kind) or {}).get("ratios")
    if not ratios:
        return None
    light, full = ratios[0], ratios[-1]
    if m.kind == "P":
        fav = ((turbo or {}).get("favored") or {}).get(m.cpus[0])
        if fav:
            light = min(light, fav)
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
    models = _unit_models(profile, topology, last, record, fused, helper.fused_words)
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
                        f"(the hardware keeps V(n) >= V(n-1)): effective {landed:+.1f} mV = {p.effective_v:.3f} V",
                    ))
        oc_ratio = m.oc_ratio or m.cur_oc_ratio
        if new is not None and m.oc_v is not None and oc_ratio and oc_ratio > max(m.ratios):  # G-120 fix 2: an OC-point target below the top fused point is held up to it
            held = vfmodel.oc_point_voltage(new, m.oc_v)
            if held is not None and held[1]:
                top = new[-1]
                warnings.append(Warn(
                    "CLAMPED",
                    f"{unit} OC point ({oc_ratio}x): the adaptive target {m.oc_v:.4f} V is below the voltage of the top fused point ({top.ratio}x, point {top.index}) "
                    f"{held[0]:.4f} V and is held up to it (the hardware keeps V(n) >= V(n-1)): effective {held[0]:.4f} V",
                ))
        if m.kind == "E":
            at40 = m.new_offsets.get(vfmodel.E_NO_OFFSET_RATIO, 0.0)
            if at40 != 0.0 and at40 != m.cur_offsets.get(vfmodel.E_NO_OFFSET_RATIO, 0.0):
                warnings.append(Warn("E40_OFFSET", f"{unit}: an offset of {at40:+.1f} mV on the 40x point. Community report: a negative offset on an E-core 40x point has caused OCCT errors"))
            if m.oc_v is not None and m.oc_v > vfmodel.E_WHEA_V:
                warnings.append(Warn("E_TARGET_WHEA", f"{unit}: E-core voltage target {m.oc_v:.3f} V is above ~1.2 V. Community report: WHEA errors have been seen above it"))
            elif new is not None:
                hi = max((p.effective_v for p in new if p.effective_v is not None), default=None)
                if hi is not None and hi > vfmodel.E_WHEA_V:
                    warnings.append(Warn("E_TARGET_WHEA", f"{unit}: the predicted E-core voltage {hi:.3f} V is above ~1.2 V. Community report: WHEA errors have been seen above it"))
    # ---- F6: VccIA ----
    vccia = _predict_vccia(models, curves_new, curves_cur, turbo)
    if vccia is not None:
        warnings.append(
            "VccIA is a prediction: the highest requested VID among the units. Lowering a unit that does not set the maximum does not lower Vcore"
        )
    return {"predicted": predicted if record is not None else None, "vccia": vccia}


def _vccia_scenario(models: Mapping[str, _UnitModel], curves: Mapping[str, list[CurvePoint]], turbo: Mapping[str, Any] | None, which: int, plan: bool) -> dict[str, Any] | None:
    rows: list[tuple[str, int, float | None]] = []
    for unit, m in models.items():
        curve = curves.get(unit)
        allowed = _allowed_ratios(m, turbo, plan)
        if curve is None or allowed is None:
            continue
        ratio = allowed[which]
        oc = (m.oc_ratio, m.oc_v) if plan and m.oc_ratio is not None and m.oc_v is not None else None
        rows.append((unit, ratio, voltage_at(curve, ratio, oc)))
    out = predict_vccia(rows)
    if out is None:
        return None
    best = {k: max((r for r in rows if r[0].startswith(k) and r[2] is not None), key=lambda r: r[2], default=None) for k in ("P", "E")}  # type: ignore[arg-type,return-value]
    out["contributors"] = {  # the unit of each group with the highest allowed VID, and the ring floor
        **{k: (None if r is None else {"unit": r[0], "ratio": r[1], "v": round(r[2], 4)}) for k, r in best.items()},
        "ring": None,  # the ring ratio is not read by tune.read: no number is shown for it
    }
    return out


def _predict_vccia(models: Mapping[str, _UnitModel], new: Mapping[str, list[CurvePoint]], cur: Mapping[str, list[CurvePoint]], turbo: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """F6 for the all-core bucket (the one that matters for sustained load: the top points of the favored cores only count for 1-2 active cores), plus the
    1-2 core case as ``light_load`` and the current state as ``before_v``. The value is the highest requested VID among the units (no ring, no margin)."""
    full = _vccia_scenario(models, new, turbo, 1, True)
    if full is None:
        return None
    light = _vccia_scenario(models, new, turbo, 0, True)
    was = _vccia_scenario(models, cur, turbo, 1, False)
    return {
        "predicted_v": full["predicted_v"], "set_by": full["set_by"], "label": "highest requested VID among units", "active_cores": "all", "margin_v": None,
        "contributors": full["contributors"], "before_v": None if was is None else was["predicted_v"],
        "light_load": None if light is None else {"predicted_v": light["predicted_v"], "set_by": light["set_by"], "active_cores": "1-2"},
    }


_Known = tuple[dict[int, CoreReadings], dict[tuple[int, int], int], "dict[int, int] | None"]


def _last_baseline(helper: Helper, profile: Profile, topology: Topology) -> _Known | None:
    """G-148: the planner's inputs as this boot's last read gives them (an E cluster read on one CPU stands for all four; ``0x26`` / ``0x28`` are not kept: 0 / unknown),
    for the round estimate and the preview of a complete profile. ``None`` without a read. G-154: the third item is the ring channel (``{0: OC word, k: point k}``, ``None`` when
    the ring was not read)."""
    last = _last_words(helper)
    if not last:
        return None
    fused = getattr(helper, "fused_words", None) or {}
    st = _state(helper)
    with st.lock:
        favored = dict(st.favored_words or {})
    baseline: dict[int, CoreReadings] = {}
    point_words: dict[tuple[int, int], int] = {}
    cpus = {t.cpu for t in (resolve_targets(profile, topology) if profile.entries else ())} | {p.cpu for p in resolve_point_targets(profile, topology)}
    for cpu in cpus:
        unit = unit_of_cpu(topology, cpu)
        ref = _unit_reference(last, (cpu, *unit_cpus(topology, unit)))
        if ref is None:
            continue
        ref_cpu, words = ref
        fused_word = fused.get(cpu, fused.get(ref_cpu))
        if fused_word is None:
            continue
        baseline[cpu] = CoreReadings(vf=VfWord.from_u32(words[0]), favored=favored.get(cpu, favored.get(ref_cpu, 0)), fused=fused_word, granular=0, vmax=None)
        for n, w in enumerate(words[1:], start=1):
            point_words[(cpu, n)] = w
    return baseline, point_words, _ring_word_map(_last_ring(helper))


def _rounds_from_last(helper: Helper, profile: Profile, topology: Topology, policy: Policy) -> tuple[int, str | None]:
    """G-148 (decision 5): the guarded rounds the last read says a restore / complete apply needs within the step limits (1 without a read: the apply judges from its own)."""
    known = _last_baseline(helper, profile, topology)
    if known is None:
        return 1, None
    return ramp_rounds(profile, topology, known[0], known[1], policy, only_changed=profile.complete, ring_words=known[2])


_ORDER_NOTE = "the apply orders the writes so that no ratio's voltage dips (G-151); the order is decided from its own reads"


def _apply_order(
    profile: Profile, topology: Topology, policy: Policy, known: _Known | None
) -> tuple[dict[tuple[str, int, int], int] | None, tuple[str, ...]]:
    """G-153: the place of every point / per-core write in the order the apply will send them, from the planner itself (``build_plan`` on the last read's words, so
    ``reorder_writes`` is the one ordering rule) -- ``{(kind, cpu, point): place}``; ``None`` without a read that covers every unit (the static order stays). A unit the order model
    refuses (``write_order``) raises the apply's own refusal. The turbo tables are left out (their MSR words are not in the last read; they are listed last)."""
    if known is None:
        return None, ()
    try:
        plan = build_plan(
            dataclasses.replace(profile, turbo=None), topology, known[0], per_core_enabled=None, policy=policy, point_words=known[1], only_changed=profile.complete,
            ring_words=known[2],
        )
    except (PlanError, ProfileError, ValueError):  # a word the last read does not hold: the apply decides from its own reads
        return None, ()
    refused = tuple(r for r in plan.refusals if r.reason == "CHECK_FAILED" and r.detail.startswith("write_order"))
    if refused:
        raise _preflight_error(PlanRefused(refused, plan))
    place: dict[tuple[str, int, int], int] = {}
    for st in plan.steps:
        if st.kind.is_write and st.msr is None and st.kind is not StepKind.WRITE_PERCORE_ENABLE:
            place.setdefault((st.kind.value, st.cpu, st.p2), len(place))
    return place, (plan.ring_refused if known[2] is not None else ())


def _predicted_steps(
    profile: Profile,
    topology: Topology,
    known: _Known | None = None,
    order: Mapping[tuple[str, int, int], int] | None = None,
    ring_refused: bool = False,
) -> tuple[list[dict[str, Any]], int, int]:
    """The write steps the plan will contain, in the planner's order (conditional 0x15, point offsets, per-core triples, turbo tables), without data
    words (those need the hardware baselines and are built at apply time). Returns ``(listed steps, total writes, read-only requests)``. G-148: for a complete
    profile with ``known`` (the last read, ``_last_baseline``) a word the CPU already holds is not listed (the apply writes only what differs). G-153: ``order``
    (``_apply_order``) puts the point and per-core steps in the apply's own order."""
    targets = resolve_targets(profile, topology) if profile.entries else ()
    pts = resolve_point_targets(profile, topology)
    rings = resolve_ring_targets(profile, topology)
    reads = (1 if (targets or pts) else 0) + 4 * len(targets) + sum(1 for t in targets if t.oc_voltage_mv is not None) + len(pts) + (len(rings) + 1 if rings else 0)  # every word is read, also the unchanged ones
    if profile.complete and known is not None:
        baseline, words, ring_known = known

        def unchanged_point(p: Any) -> bool:
            w = words.get((p.cpu, p.point))
            return w is not None and VfWord.from_u32(w).offset_raw == VfWord.mv_to_offset_raw(p.offset_mv) and (p.expect_ratio is None or VfWord.from_u32(w).ratio == p.expect_ratio)

        def unchanged_core(t: Any) -> bool:
            rd = baseline.get(t.cpu)
            if rd is None or (t.fused_top_ratio is not None and fused_top_ratio(rd.fused) != t.fused_top_ratio):
                return False
            top = fused_top_ratio(rd.fused)
            try:
                goal = _goal_word(t, current_word(rd, t.ratio is not None or t.oc_voltage_mv is not None), top)
            except PlanError:
                return False
            return _same_state(t, rd, goal, top)

        pts = tuple(p for p in pts if not unchanged_point(p))
        targets = tuple(t for t in targets if not unchanged_core(t))
        if ring_known is not None:  # G-154: a ring point the CPU already holds is not listed

            def unchanged_ring(r: Any) -> bool:
                w = ring_known.get(r.point)
                return w is not None and VfWord.from_u32(w).offset_raw == VfWord.mv_to_offset_raw(r.offset_mv) and VfWord.from_u32(w).ratio == r.expect_ratio

            rings = tuple(r for r in rings if not unchanged_ring(r))
    if ring_refused:  # G-154: the plan refuses the ring unit alone: no ring step is listed (the reason is a plan warning)
        rings = ()
    steps: list[dict[str, Any]] = [{"kind": StepKind.WRITE_PERCORE_ENABLE.value, "conditional": True}] if (targets or pts) else []  # G-117 / G-148: none without a mailbox write
    for p in pts:
        steps.append({"kind": StepKind.WRITE_VF_POINT.value, "unit": unit_of_cpu(topology, p.cpu), "cpu": p.cpu, "point": p.point, "offset_mv": p.offset_mv})
    for t in targets:
        u = unit_of_cpu(topology, t.cpu)
        # G-124 (as G-122 in the planner): a target that sets no OC ratio sends the 0x11 alone -- no 0x1D (favored) and no 0x27 (granular)
        kinds = (StepKind.WRITE_VF, StepKind.WRITE_FAVORED, StepKind.WRITE_GRANULAR) if t.ratio is not None else (StepKind.WRITE_VF,)
        for kind in kinds:
            steps.append({"kind": kind.value, "unit": u, "cpu": t.cpu, "point": 0})
    for r in rings:  # G-154: one ring-point write each (the ring OC word is never written)
        steps.append({"kind": StepKind.WRITE_RING_POINT.value, "unit": ringmod.UNIT, "cpu": r.cpu, "point": r.point, "offset_mv": r.offset_mv})
    if order is not None:  # the 0x15 (first) and the tables (last) keep their places; the writes between them follow the apply
        first = 1 if steps and steps[0]["kind"] == StepKind.WRITE_PERCORE_ENABLE.value else 0
        steps[first:] = sorted(steps[first:], key=lambda s: order.get((s["kind"], s["cpu"], s.get("point", 0)), len(order)))
    if profile.turbo is not None:
        for key in ("e", "p"):
            if getattr(profile.turbo, key) is not None:
                steps.append({"kind": StepKind.WRITE_MSR.value, "table": key})
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
    if isinstance(exc, (NeedsRamp, NeedsRounds)):
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
                f"the {label} turbo table is not non-increasing: bucket {bad + 1} ({table[bad]}x) is above bucket {bad} ({table[bad - 1]}x); more active cores must never raise the ratio. "
                f"Nothing was sent.",
                {"check": "TURBO_ORDER", "table": key.upper(), "bucket": bad, "ratios": list(table)},
            )


# G-125: the GUI plan's own caveats, worded from what the hardware showed (docs/knowledge/tuning-results.md); the planner's constants stay as the CLI prints them.
_RAISE_NOTE = (
    "this plan RAISES a ratio, V target or offset; the numbers come from the reviewed policy; take a stability check between steps"
)
_POINT_NOTE = (
    "per-point offsets change one V/F point (0x11 with P2 = the point); the hardware keeps V(n) >= V(n-1): arltuner models it (CLAMPED warnings) but cannot read the "
    "lower fused voltages"
)


def _words_assumed(last: Mapping[int, Sequence[int]] | None, topology: Topology, targets: Sequence[Any], points: Sequence[Any]) -> bool:
    """G-125: the plan assumed words only when a unit it writes has no CPU in ``last`` (an E cluster is read on one CPU, the others share its words)."""
    cpus_by_unit: dict[str, list[int]] = {}
    for item in (*targets, *points):
        cpus_by_unit.setdefault(unit_of_cpu(topology, item.cpu), []).append(item.cpu)
    return any(not any(c in (last or {}) for c in unit_cpus(topology, unit)) for unit in cpus_by_unit)


def _residual_found(helper: Helper, profile: Profile, topology: Topology, policy: Policy) -> residual.Neutralisation | None:
    """G-147: the residual OC points that this boot's last read shows against ``profile`` (the accepted profile, D-044), or ``None`` without a read / fused words. The apply
    re-reads every word: this is a preview and the size of the budget it reserves, not the decision."""
    last = _last_words(helper)
    fused = helper.fused_words or {}
    vf = {c: w[0] for c, w in (last or {}).items() if w}
    if not vf or not fused:
        return None
    return residual.neutralise(profile, topology, policy, vf, fused)


def _residual_ops(found: residual.Neutralisation | None) -> int:
    """Worst-case extra operations of the rows ``found`` adds (as ``count_operations`` counts a target that sets a ratio and a voltage)."""
    per_cpu = READS_PER_CPU + VMAX_EXTRA_READS + 2 * (WRITES_PER_CPU + SPLIT_EXTRA_WRITES)  # the planner's own worst case for a row with a ratio and a voltage (preflight counts 0x1D / 0x27 too)
    return 0 if found is None else per_cpu * sum(len(u.cpus) for u in found.units)


def _residual_preview(found: residual.Neutralisation | None) -> list[str]:
    notes = [
        "restore also looks for a residual OC point (an OC ratio the accepted state does not have; it cannot be cleared at runtime): the apply reads two words per unit "
        "that has no accepted OC ratio and rewrites such a unit with a V target below its top fused point, so it runs the accepted voltages"
    ]
    if found is None:
        notes.append("no read of this boot to look at: a residual OC point is rewritten only if the session budget has room for it, so read ratios & voltages first (Tune > Read)")
    return [*notes, *(f"{n} (from the last read)" for n in (found.notes if found is not None else ()))]


def op_tune_plan(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    intent = args["intent"]
    topology = _topology(helper)
    policy = _load_policy(helper)
    resolved = resolve_intent(helper, intent, policy, topology)
    profile = resolved.profile
    _check_turbo_order(profile)
    used, budget = helper.budget.snapshot()
    if used is None:  # G-141: an unreadable audit log is not "0 operations used": the apply could not count them either, so the plan is refused here like the apply would be
        raise OpError(REFUSED, "the audit log cannot be read, so the per-boot operation count is unknown and no plan can be checked against it. Nothing was sent.", {"check": "AUDIT_UNAVAILABLE"})
    ops_left = None if budget is None else max(budget - used, 0)
    restore = "restore" in intent or profile.complete  # G-148: a complete profile is applied like the accepted state (residual rule, rounds, only what differs)
    scan_ops = len(residual.scan_plan(profile, topology, policy)) if restore else 0  # G-147: the residual-OC-point scan reads of a restore
    try:
        targets = preflight(profile, topology, policy, boot_ops_used=used, extra_ops=scan_ops)
    except PreflightError as exc:
        raise _preflight_error(exc) from None
    except (PlanError, ProfileError) as exc:
        raise OpError(BAD_REQUEST, str(exc)) from None
    points = resolve_point_targets(profile, topology)
    rings = resolve_ring_targets(profile, topology)
    tables = sum(1 for key in ("e", "p") if profile.turbo is not None and getattr(profile.turbo, key) is not None)
    ops_needed = count_operations(targets, None, tables, len(points), oc_cap_reads_of(policy, targets, profile), ring_points=len(rings)) if (targets or points or tables or rings) else 0  # G-117: a table-only plan is plain-MSR writes only
    found_residual = _residual_found(helper, profile, topology, policy) if restore else None
    rounds, worst = _rounds_from_last(helper, profile, topology, policy) if restore else (1, None)
    try:  # G-148 (decision 5): the rounds the step limits need, reserved up front; refused here (STEP_LIMIT) when they do not fit, never half-way
        ops_needed = check_rounds(rounds, worst, ops_needed + _residual_ops(found_residual), scan_ops, policy, used)
    except PreflightError as exc:
        raise _preflight_error(exc) from None
    last = _last_words(helper)
    before, after = _before_after(profile, topology, last, helper.fused_words, _state(helper).turbo, _last_ring(helper))
    known = _last_baseline(helper, profile, topology)
    order, ring_refused = _apply_order(profile, topology, policy, known)  # G-153: the Review dialog lists the order the apply sends (G-151)
    steps, writes, reads = _predicted_steps(profile, topology, known if profile.complete else None, order, bool(ring_refused))
    warnings = [_POINT_NOTE if w == POINT_CAVEAT else w for w in plan_warnings(targets, policy, points)]
    warnings.extend(resolved.warnings)
    warnings.extend(Warn("RING_NOT_RESTORED", f"the ring (cache) will NOT be written by this plan: {why}") for why in ring_refused)  # G-154 (D-045): the ring unit alone is refused, the rest stands
    if order is None and (targets or points or rings):
        warnings.append(_ORDER_NOTE)
    if restore:
        warnings.extend(_residual_preview(found_residual))
    if rounds > 1:
        warnings.append(f"the change is larger than the policy's per-operation step limits ({worst}): the apply splits it into up to {rounds} guarded rounds in this one run (G-148)")
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
    _state(helper).plans.add(StoredPlan(plan_id, nonce, helper.cfg.monotonic(), profile, resolved.refs, ops_needed, restore=restore, rounds=rounds))
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
        "complete": profile.complete,  # G-148 (additive): a complete profile: only the words that differ are written
        "rounds": rounds,  # G-148 (additive): guarded rounds the step limits need (1 = one write per word)
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
        "complete": profile.complete,
        **({} if profile.ring is None else {"ring": dataclasses.asdict(profile.ring)}),
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
    if step.kind in _RING_STEP_KINDS:  # G-154: the ring is read and written through CPU 0 but is not P-core 0
        return ringmod.UNIT
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
        if s.kind in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT, StepKind.WRITE_RING_POINT):
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


_TABLE_OF_MSR = {0x650: "e", 0x1AD: "p"}


def _fold_applied_tables(helper: Helper, report: Any) -> None:
    """G-141: the read-backs of the turbo-table writes (``WRITE_MSR``) become the tables the helper knows (``TuneState.tables`` / ``turbo``), so ``tune.last``, the next plan's
    raise / lower decision and the predictions see the applied table. A table that was never read stays unknown (nothing is invented)."""
    applied: dict[str, tuple[int, ...]] = {}
    for r in report.steps:
        s = r.step
        if s.kind is StepKind.WRITE_MSR and r.readback_data is not None and s.msr in _TABLE_OF_MSR:
            applied[_TABLE_OF_MSR[s.msr]] = turbo_bytes(r.readback_data)
    if not applied:
        return
    st = _state(helper)
    with st.lock:
        if st.tables is not None:
            st.tables = {**st.tables, **applied}
        if st.turbo is not None:
            turbo = dict(st.turbo)
            for key, ratios in applied.items():
                cur = turbo.get(key.upper())
                if isinstance(cur, dict):
                    turbo[key.upper()] = {**cur, "ratios": list(ratios)}
            st.turbo = turbo


def _merge_applied_words(helper: Helper, report: Any) -> None:
    """The words verified (or seen differing) by the read-backs of this apply update ``helper.last_words`` if there was a complete read; the OC word is
    index 0, point n index n. Nothing is invented for a CPU that was not read."""
    _fold_applied_tables(helper, report)
    ring = _last_ring(helper)
    if ring:  # G-154: the ring channel follows its own verified read-backs (never the CPU-keyed words: the ring is read through CPU 0 but is not P-core 0)
        for r in report.steps:
            s = r.step
            if s.kind is StepKind.WRITE_RING_POINT and r.readback_data is not None and s.p2 < len(ring):
                ring[s.p2] = r.readback_data
        _set_last_ring(helper, ring)
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
#: ``cpuinfo_max_freq`` follows HWP highest a moment after the 0x1D: re-read all unmoved CPUs this many rounds, this far apart (hardware only).
#: ``[UNVERIFIED]``: nobody has measured the kernel's lag; check on the next write sitting by reading ``cpuinfo_max_freq`` right after a 0x1D.
CPUINFO_POLL = cpufreq_rules.POLL


def _cpufreq_before(helper: Helper, cpus: Sequence[int]) -> dict[int, tuple[int | None, int | None]]:
    """``{cpu: (cpuinfo_max_freq, scaling_max_freq)}`` in kHz, read before the first write of an apply. Nothing without the measurement wiring."""
    w = getattr(helper, "measure_wiring", None)
    return {} if w is None else cpufreq_rules.snapshot(w.cpufreq, cpus)


_oc_ratio_cpus = cpufreq_rules.oc_ratio_cpus  # G-147: one implementation for the helper and the CLI


def _lift_cpufreq(
    helper: Helper, ctx: OpContext, before: Mapping[int, tuple[int | None, int | None]], favored: Sequence[int], checked: Sequence[int], warnings: list[str],
) -> list[dict[str, Any]]:
    """G-126: after an apply that changed an OC ratio, ``cpuinfo_max_freq`` follows HWP highest but ``scaling_max_freq`` keeps the old value, so the new ratio is never
    used (``[MEASURED lnx 2026-10-12]``). The rule is ``controls.cpufreq`` (shared with the CLI's apply, G-147): a cap that sat AT the old cpuinfo max follows the new
    max; a cap the user set below the old max is never touched; a cap above a lowered max is lowered to it. Writes go through the measurement wiring's cpufreq IO to
    ``scaling_max_freq`` only, never while a measure/stress journal exists (those own the caps). Never raises. Returns ``[{cpu, before_khz, after_khz}]``."""
    w = getattr(helper, "measure_wiring", None)
    if w is None or not sorted({*favored, *checked}):
        return []
    return cpufreq_rules.lift(
        w.cpufreq, before, favored, checked, warnings,
        owned=Path(w.journal_path).exists(), poll=(1, 0.0) if helper.cfg.simulator else CPUINFO_POLL, wait=ctx.cancel.wait,
    )


def op_tune_apply(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    st = _state(helper)
    plan = st.plans.claim(args["plan_id"], args["typed"])
    topology = _topology(helper)
    policy = _load_policy(helper)
    profile = plan.profile
    scan_ops = len(residual.scan_plan(profile, topology, policy)) if plan.restore else 0  # G-147
    try:
        targets = preflight(profile, topology, policy, extra_ops=scan_ops)  # nothing sent: the numbers against the policy as it is NOW
    except PreflightError as exc:
        raise _preflight_error(exc) from None
    except (PlanError, ProfileError) as exc:
        raise OpError(BAD_REQUEST, str(exc)) from None
    points = resolve_point_targets(profile, topology)
    tables = sum(1 for key in ("e", "p") if profile.turbo is not None and getattr(profile.turbo, key) is not None)
    per_round = count_operations(targets, None, tables, len(points), oc_cap_reads_of(policy, targets, profile), ring_points=len(resolve_ring_targets(profile, topology)))
    needed = per_round + scan_ops
    if plan.restore:  # the guard's session cap is exactly `needed`: reserve the rows the last read says the scan will add (an unforeseen one is dropped by the executor, with a warning)
        # G-148: and the rounds the plan reserved (the executor re-judges them from its own reads before the first write: STEP_LIMIT when they do not fit)
        needed = min(scan_ops + max(plan.rounds, 1) * (per_round + _residual_ops(_residual_found(helper, profile, topology, policy))), policy.max_operations_per_session)
    progress_steps = 0
    freq_cpus = sorted({t.cpu for t in targets} | ({c.cpu for c in topology.cores} if plan.restore else set()))  # a restore may rewrite a unit the profile has no row for
    freq_before = _cpufreq_before(helper, freq_cpus)  # G-126: the old cpuinfo / cap, before any write

    def progress(line: str) -> None:
        nonlocal progress_steps
        if line.startswith("step "):
            progress_steps += 1
            ctx.progress(progress_steps, needed, None if session.used is None else session.used + progress_steps, session.policy.max_operations_per_boot)

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
                neutralize_residual=plan.restore,
                boot_ops_used=session.used or 0,
                auto_ramp=plan.restore,
            )
        except PreflightError as exc:
            raise _preflight_error(exc) from None
        except BaseException:
            helper.tuned_this_boot = True  # type: ignore[attr-defined]  # G-120: an apply that died midway may have written; "stock" is never claimed after it
            raise
    _merge_applied_words(helper, report)
    if report.ok:
        _advance_mark(helper)  # G-141: a complete apply's verified read-backs keep the words in force current; a partial one leaves the older mark (the writes then show as "after the read")
    steps, readback = _report_rows(report, topology)
    cpufreq_warnings: list[str] = []
    favored_cpus, vf_cpus = _oc_ratio_cpus(report)
    cpufreq: list[dict[str, Any]] = []
    if report.ok:
        cpufreq = _lift_cpufreq(helper, ctx, freq_before, favored_cpus, vf_cpus, cpufreq_warnings)
    elif favored_cpus:  # a partial apply: the page says "do not rely on this state"; the OS caps are left exactly as they are
        cpufreq_warnings.append("the apply did not complete, so scaling_max_freq was not adjusted to the OC ratios that did land")
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
        "residual": list(report.residual),  # G-147: the units whose residual OC point this restore rewrote (additive)
        "ring_refused": list(report.ring_refused),  # G-154 (additive): why the ring unit alone was not written (the P/E units were); [] = written or no ring
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
    """``{name, intent}``; G-148: ``intent`` is the Tune editor's rows laid over this boot's read (rows that change nothing, or none, save the read as it is)."""
    _only(args, {"name", "intent"})
    name = args.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise _bad("'name' must be 1..64 characters of a-z 0-9 . _ -")
    try:
        intent: dict[str, Any] | None = canonical_intent(args.get("intent"), rows_only=True)
    except OpError as exc:
        if (exc.detail or {}).get("check") != "NOTHING_TO_DO":
            raise
        intent = None
    return {"name": name, "intent": intent}


def op_profiles_list(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``{profiles: [{name, source, created, complete}], accepted, note?}``. G-148: ``complete`` false = "partial (legacy)"; an accepted state that is still a chain lists
    its members with a note pointing at ``accepted-migrate`` (Restore accepted refuses it)."""
    out: list[dict[str, Any]] = []
    note: str | None = None
    try:
        paths = _accepted_paths(helper)
        if len(paths) > 1:
            note = f"the accepted state is an older chain of {len(paths)} profiles; Restore accepted refuses it until it is one profile: {CHAIN_HINT}"
            out.extend({"name": Path(p).stem, "source": "accepted", "created": None, "complete": False, "chain": True} for p in paths)
        else:
            name, prof = _accepted_profile(helper)
            out.append({"name": name, "source": "accepted", "created": None, "complete": prof.complete})  # an accepted profile has no creation time of its own
    except OpError as exc:
        note = NO_ACCEPTED_NOTE if exc.message.startswith(NO_ACCEPTED_NOTE) else exc.message
    try:
        out.extend(_profile_store(helper).list())
    except ProfileStoreError as exc:
        note = (note + "; " if note else "") + exc.message
    result: dict[str, Any] = {"profiles": out, "accepted": any(p["source"] == "accepted" for p in out)}  # G-129: false = "No accepted state yet"
    if note:
        result["note"] = note
    return result


def op_profiles_save(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """G-148 (D-044): save a **complete** profile: this boot's last read of every unit (OC word fields, every fused point's offset, both turbo tables when read) with the
    editor's rows laid over it (``profiles.complete.overlay``). Refused without a read of this boot (``NO_READ``, "read first"), when a write came after it
    (``STALE_READ``), or when a word cannot be recorded exactly (``NOT_REPRESENTABLE``). The rows pass the same policy range checks as a plan. No hardware."""
    name = args["name"]
    if name in _accepted_names(helper):
        raise OpError(REFUSED, f"{name!r} is the name of an accepted profile; profiles are never overwritten", {"check": "EXISTS"})
    topology = _topology(helper)
    last = _last_words(helper)
    if not last:
        raise OpError(REFUSED, "no tune.read this boot: a saved profile is the complete state of the CPU (this boot's read with your edits over it), so read first (Tune > Read). "
                      "Nothing was saved.", {"check": "NO_READ"})
    why = stale_since_read(helper)
    if why is not None:
        raise OpError(REFUSED, f"the words of the last read may no longer be what the CPU holds: {why}. Read again first (Tune > Read), then save. Nothing was saved.", {"check": "STALE_READ"})
    fused = getattr(helper, "fused_words", None) or {}
    st = _state(helper)
    with st.lock:
        tables = None if st.tables is None else {k: list(v) for k, v in st.tables.items()}
    try:
        base = complete_from_words(topology, name, last, {c: fused_top_ratio(w) for c, w in fused.items()}, tables, _last_ring(helper))
    except BuildError as exc:
        raise OpError(REFUSED, f"{exc.message} Nothing was saved.", {"check": exc.code}) from None
    profile = base
    if args["intent"] is not None:
        policy = _load_policy(helper)
        edits = _rows_to_profile(helper, args["intent"], policy, topology, name).profile
        try:
            profile = overlay(base, edits, topology, name)
        except ProfileError as exc:
            raise OpError(BAD_REQUEST, f"the edits do not fit the read: {exc}. Nothing was saved.") from None
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(helper.cfg.wall_clock()))
    header = f"Saved {stamp} by arltuner (profiles.save, G-148): this boot's read of every unit with the Tune editor's rows laid over it. A complete profile."
    try:
        record = _profile_store(helper).save(name, profile=dump_profile(profile, header))
    except ProfileError as exc:
        raise OpError(BAD_REQUEST, f"the profile cannot be written: {exc}") from None
    except ProfileStoreError as exc:
        raise OpError(REFUSED if exc.code in ("EXISTS", "UNTRUSTED") else BAD_REQUEST if exc.code in ("BAD_NAME", "TOO_BIG") else INTERNAL, exc.message, {"check": exc.code}) from None
    return {"name": record["name"], "source": "saved", "created": record["created"], "complete": True, "units": len(profile.entries), "points": len(profile.points),
            "turbo": profile.turbo is not None, "ring_points": 0 if profile.ring is None else len(profile.ring.points)}


def _validate_profiles_get(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"name"})
    name = args.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise _bad("'name' must be 1..64 characters of a-z 0-9 . _ -")
    return {"name": name}


def op_profiles_get(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """Contract 9 (G-123): ``{name, source, created, complete, contents: {rows, turbo, complete}, summary}``. G-148: no chain any more -- a profile stands for
    itself; ``complete`` false = a partial profile saved before G-148 (its rows are what it changes)."""
    name = args["name"]
    topology = _topology(helper)

    def contents_of(profile: Profile) -> dict[str, Any]:
        return profile_contents(profile, topology, lambda cpu: unit_of_cpu(topology, cpu), _unit_point_ratios(helper, topology))

    try:
        acc_name, acc = _accepted_profile(helper)
    except OpError:
        acc_name, acc = None, None
    if acc is not None and acc_name == name:
        own = contents_of(acc)
        return {"name": name, "source": "accepted", "created": None, "complete": acc.complete, "contents": own, "summary": summarize(own)}
    if acc is None:  # an older chain: its members stand for their own file only (Restore accepted refuses the chain until accepted-migrate)
        try:
            for rel in _accepted_paths(helper):
                if Path(rel).stem == name:
                    member = load_profile(_read_accepted_text(helper, rel))
                    own = contents_of(member)
                    return {"name": name, "source": "accepted", "created": None, "complete": member.complete, "chain": True, "contents": own, "summary": summarize(own)}
        except (OpError, ProfileError):
            pass
    try:
        record = _profile_store(helper).load(name)
    except ProfileStoreError as exc:
        raise OpError(BAD_REQUEST if exc.code == "NOT_FOUND" else REFUSED, exc.message, {"check": "PROFILE_" + exc.code}) from None
    created = record.get("created")
    prof = _saved_profile(record)
    if prof is not None:
        own = contents_of(prof)
    else:
        own = intent_contents(canonical_intent(record["intent"], rows_only=True), topology.unit_ids())
    return {"name": name, "source": "saved", "created": created if isinstance(created, str) else None, "complete": prof is not None, "contents": own, "summary": summarize(own)}


# --------------------------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------------------------
def tune_ops() -> list[OpSpec]:
    from .ops import _validate_vf_dump
    from .ops_accept import op_profiles_accept, op_profiles_fingerprint, validate_profiles_accept  # lazy: ops_accept imports this module

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
        # G-129 (D-036): the user's accepted state, written by the root helper (a file write, no mailbox / MSR traffic; a read this boot is required)
        OpSpec("profiles.accept", op_profiles_accept, validate_profiles_accept, slow=True),
        # G-141: records fingerprint.json for the accepted state that exists (a file write, no mailbox / MSR traffic; needs this boot's read of the fused words)
        OpSpec("profiles.fingerprint", op_profiles_fingerprint, _no_args, slow=True),
        # G-123: the stored last read of this boot (a file read, no hardware); also restores the plan state after a helper restart
        OpSpec("tune.last", op_tune_last, _no_args, slow=True),
    ]
