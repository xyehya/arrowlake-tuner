"""G-216: ``runtime.read``, ``runtime.plan``, ``runtime.apply`` -- the runtime sysfs controls: per-CPU maximum-ratio caps (``scaling_max_freq``) and the uncore (ring) frequency limits
(``intel_uncore_frequency/.../{min,max}_freq_khz``). Core parking is **held** (a parked CPU renumbers the live topology; D-pending): ``parking`` in a plan request is ``BAD_REQUEST`` and no
``online`` file is ever written -- ``runtime.read`` only reports each CPU's online state, and :func:`~arltuner.helper.cpu_online.refuse_if_cpu_offline` stops plans / applies (here and in
``tune.apply``, ``vf.measure``, ``stress.start``) while a CPU is offline outside the app.

* ``runtime.read``: caps, online state, uncore limits, the ``[runtime]`` policy keys, what holds the caps (a stress / measure run or its journal) and the journal. No write; allowed in every state.
* ``runtime.plan``: ``{caps?: {cpu: ratio}, uncore?: {min_ratio?, max_ratio?}}`` or ``{revert: true}`` (the values of the journal) -> the file writes in a safe order, the nonce, nothing
  written. Same plan-id / ``CONFIRM <nonce>`` / 600 s / single-use semantics as ``tune.plan`` (:class:`~arltuner.helper.ops_tune.PlanStore`, this module's own store).
  The rules live in :mod:`arltuner.controls.caps` and ``uncore``; an absent ``[runtime]`` key refuses the control and names itself (a revert needs no key). A cap means "never above ratio R":
  the bin comes from the measured kHz -> perf -> ratio model (:func:`~arltuner.controls.caps.cap_choice`, E 46 -> 4.7 GHz) and a ratio no cap reaches is flagged in the plan with the one it gives.
  A forward plan / apply refuses while a CPU is offline (``CPU_OFFLINE``); a **revert** does not: it restores per-CPU files by number from the journal, an offline CPU's cap is skipped
  (reported; the revert is then not ``complete``) and the uncore is restored normally.
* ``runtime.apply``: one sysfs file per transaction, planned again from the values now in force (any difference is ``PLAN_STALE``). Before the first write the values being replaced are
  journaled (``runtime-journal.json``, root 0600, boot-id bound, bounded; a key is journaled the first time it is written in a boot and never overwritten) with ``in_flight`` set; each write is audited
  (``runtime_attempt``, fsync'd) before it is sent, under the guard's cross-process lock, read back from the file, and a mismatch stops the apply (no retry, no auto-revert). When the apply ends --
  complete or stopped -- ``in_flight`` is cleared.

**What "restore at helper start" means here.** The journal is kept for ``revert`` (like the power journal) and is **not** applied at every start: the systemd-activated helper idles out after
600 s and comes back at the next connect, and an unconditional restore would silently undo the operator's caps each time. Only a journal with ``in_flight`` set -- the helper
died *during* an apply -- is restored, by :func:`recover_journal` at start (``Helper.start`` calls it next to the measure journal) and, as a net, a plan is refused while one exists
(``JOURNAL_IN_FLIGHT``: revert first). The restore (and a revert) puts back the **whole boot's journal**, including the values of earlier completed applies of this boot -- the safe
direction (back to the values the boot started with). A restore that had to skip something (a CPU offline: its cap is not restored) keeps the in-flight mark.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arltuner.controls import caps as cp
from arltuner.controls import uncore as un
from arltuner.guard import AuditLog, Policy
from arltuner.helper.config import INSTALLED_LOCK_DIR
from arltuner.topology import Topology

from .cpu_online import refuse_if_cpu_offline
from .ops import OpContext, OpSpec, _no_args, _only, _trust, _trust_refusal, helper_topology
from .ops_power import _boot_id, _NotSent, _open_audit, _release_lock, _take_lock
from .ops_tune import _NONCE_ALPHABET, PLAN_EXPIRES_S, PlanStore, _load_policy, _validate_tune_apply
from .protocol import BAD_REQUEST, CANCELLED, INTERNAL, PLAN_STALE, REFUSED, OpError
from .state import State
from .trust import TrustError, open_checked

if TYPE_CHECKING:
    from .core import Helper

__all__ = ["JOURNAL_NAME", "Journal", "RuntimeWiring", "SimRuntimeFiles", "parse_journal", "recover_journal", "runtime_ops"]

JOURNAL_NAME = "runtime-journal.json"
JOURNAL_VERSION = 1
_JOURNAL_MAX = 65536
_CPU_MAX = 1024
_LOCK = threading.Lock()
_BLOCKING_OPS = ("stress.start", "vf.measure")
_CPU_DIR = re.compile(r"cpu(\d+)")


@dataclass
class RuntimeWiring:
    """Where the helper finds the runtime sysfs files and keeps the journal: ``files`` (:class:`~arltuner.controls.caps.RuntimeFiles` or the simulator) and ``journal_path``. Tests and the
    launcher may set ``helper.runtime_wiring``; otherwise ``--sim`` gets the simulated files next to the audit log and hardware the installed paths."""

    files: cp.RuntimeFiles
    journal_path: Path


class SimRuntimeFiles(cp.RuntimeFiles):
    """The kernel's runtime files in memory (``--sim``, tests): ``cpu<N>/cpufreq/*`` is the simulated cpufreq (the measure / stress model's, so a held cap journal and a cap written here are
    the same state), ``cpu<N>/online`` (none for cpu0; an offline CPU has no cpufreq directory; read-only here, this app never writes it), the uncore limits (the driver refuses min > max
    and values outside ``initial_*``, ``EINVAL``). An E-core cap reads back as the kernel's perf step (``SimModel.e_cap_quantized``, like ``SimMeasureIO.write_cap``). ``writes`` lists ``(file, value)``; ``drop_writes`` makes the kernel accept and ignore them. Defaults: the reference machine's uncore ``[MEASURED lnx 2026-10-18]``."""

    UNCORE = "intel_uncore_frequency/package_00_die_00"

    def __init__(self, cpufreq: Any, cpus: Sequence[int], *, uncore: tuple[int, int] = (800000, 4000000), drop_writes: bool = False) -> None:
        super().__init__("/nonexistent/sim-cpu")
        self.cpufreq = cpufreq
        self.cpus = sorted(cpus)
        self.online = {c: 1 for c in self.cpus if c != 0}
        self.uncore = {"min_freq_khz": uncore[0], "max_freq_khz": uncore[1], "initial_min_freq_khz": uncore[0], "initial_max_freq_khz": uncore[1]}
        self.drop_writes = drop_writes
        self.writes: list[tuple[str, int]] = []

    def _route(self, name: str) -> tuple[str, int | None, str]:
        parts = name.split("/")
        m = _CPU_DIR.fullmatch(parts[0])
        if m and len(parts) == 2 and parts[1] == "online":
            return "online", int(m.group(1)), ""
        if m and len(parts) == 3 and parts[1] == "cpufreq":
            return "cpufreq", int(m.group(1)), parts[2]
        if len(parts) == 3 and f"{parts[0]}/{parts[1]}" == self.UNCORE:
            return "uncore", None, parts[2]
        return "none", None, ""

    def read_text(self, name: str) -> str | None:
        kind, cpu, attr = self._route(name)
        if kind == "online":
            return str(self.online[cpu]) if cpu in self.online else None  # type: ignore[index]
        if kind == "cpufreq":
            return self.cpufreq.read(cpu, attr) if cpu in self.cpus and self.online.get(cpu, 1) else None  # type: ignore[arg-type]
        if kind == "uncore":
            return str(self.uncore[attr]) if attr in self.uncore else None
        return None

    def listdir(self, name: str) -> list[str]:
        if name == "":
            return [f"cpu{c}" for c in self.cpus]
        return ["package_00_die_00"] if name == "intel_uncore_frequency" else []

    def write_int(self, name: str, value: int) -> None:
        self.writes.append((name, int(value)))
        kind, cpu, attr = self._route(name)
        if kind in ("none", "online") or self.read_text(name) is None:
            raise cp.SysfsError(f"cannot write {self.path(name)}: No such file or directory")
        if self.drop_writes:
            return
        if kind == "cpufreq":
            if attr == cp.CAP_ATTR and self.cpufreq.model.unit_of(cpu).startswith("E"):
                value = self.cpufreq.model.e_cap_quantized(int(value))
            self.cpufreq.write(cpu, attr, str(int(value)))
        else:
            new = {**self.uncore, attr: int(value)}
            if attr not in ("min_freq_khz", "max_freq_khz") or new["min_freq_khz"] > new["max_freq_khz"] or not new["initial_min_freq_khz"] <= int(value) <= new["initial_max_freq_khz"]:
                raise cp.SysfsError(f"cannot write {self.path(name)}: Invalid argument")
            self.uncore = new


def _wiring(helper: Helper) -> RuntimeWiring:
    with _LOCK:
        w = getattr(helper, "runtime_wiring", None)
        if w is None:
            if helper.cfg.simulator:
                mw = getattr(helper, "measure_wiring", None)
                if mw is None or not hasattr(mw.cpufreq, "model"):
                    raise OpError(INTERNAL, "this helper has no runtime-control support configured")
                w = RuntimeWiring(SimRuntimeFiles(mw.cpufreq, [c.cpu for c in helper_topology(helper).cores]), Path(helper.cfg.audit_path).parent / JOURNAL_NAME)
            else:
                w = RuntimeWiring(cp.RuntimeFiles(), INSTALLED_LOCK_DIR / JOURNAL_NAME)
            helper.runtime_wiring = w  # type: ignore[attr-defined]
        return w


class _RuntimePlanStore(PlanStore):
    def is_revert(self, plan_id: str) -> bool:
        """Whether ``plan_id`` names a stored revert plan (no claim, no state change): the offline-CPU guard lets a revert through."""
        with self._lock:
            plan = self._plans.get(plan_id)
            return plan is not None and bool(getattr(plan, "revert", False))


def _plans(helper: Helper) -> _RuntimePlanStore:
    with _LOCK:
        store = getattr(helper, "_runtime_plans", None)
        if store is None:
            store = _RuntimePlanStore(helper.cfg.monotonic)
            helper._runtime_plans = store  # type: ignore[attr-defined]
        return store


@dataclass
class StoredRuntimePlan:
    plan_id: str
    nonce: str
    created: float
    steps: list[cp.RuntimeStep]
    revert: bool
    intent: dict[str, Any]
    used: bool = False
    mismatches: int = 0


# --------------------------------------------------------------------------------------------------------------
# the journal
# --------------------------------------------------------------------------------------------------------------
@dataclass
class Journal:
    """The values in force before the first write of each key in this boot: ``caps`` {cpu: kHz}, ``uncore`` {min_ratio|max_ratio: kHz}. ``in_flight``: an apply was running when it was
    last written. A revert / recovery restores this whole boot's journal (the safe direction: the values the boot started with)."""

    caps: dict[int, int] = field(default_factory=dict)
    uncore: dict[str, int] = field(default_factory=dict)
    in_flight: bool = False

    def doc(self, boot_id: str, created: float, pid: int | None = None) -> dict[str, Any]:
        return {
            "version": JOURNAL_VERSION, "boot_id": boot_id, "created": created, "pid": os.getpid() if pid is None else pid, "in_flight": self.in_flight,
            "caps": {str(k): v for k, v in sorted(self.caps.items())}, "uncore": dict(sorted(self.uncore.items())),
            "note": "the values at the first runtime write of this boot, not necessarily the BIOS or kernel defaults",
        }

    def empty(self) -> bool:
        return not (self.caps or self.uncore)

    def remember(self, steps: Sequence[cp.RuntimeStep]) -> bool:
        """Journal ``before`` of every step whose key is not journaled yet; ``True`` if anything was added."""
        added = False
        for s in steps:
            if s.domain == "uncore":
                table: dict[Any, int] = self.uncore
                key: Any = s.key
            else:
                table = self.caps
                key = int(s.key[3:])
            if key not in table:
                table[key] = s.before
                added = True
        return added


def _int_map(raw: Any, what: str, lo: int, hi: int, keys: Callable[[str], bool]) -> dict[Any, int]:
    if not isinstance(raw, dict):
        raise ValueError(f"{what} is not an object")
    out: dict[Any, int] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not keys(k) or isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"bad {what} entry {k!r}: {v!r}")
        out[int(k) if k.isdigit() else k] = v
    return out


def parse_journal(data: bytes, boot_id: str | None) -> Journal | None:
    """The journal of ``data``, or ``None`` when it is for another boot (a stale file from before a reboot: ignored, replaced by the next write). ``ValueError`` when it is not a runtime
    journal or a value is outside what the controls can hold (a CPU number >= 1024, a frequency above ratio 255)."""
    doc = json.loads(data.decode("ascii"))
    if not isinstance(doc, dict) or doc.get("version") != JOURNAL_VERSION or not isinstance(doc.get("in_flight"), bool):
        raise ValueError("not a runtime journal")
    is_cpu: Callable[[str], bool] = lambda k: k.isdigit() and len(k) <= 4 and int(k) < _CPU_MAX  # noqa: E731
    journal = Journal(
        caps=_int_map(doc.get("caps"), "caps", 1, cp.JOURNAL_KHZ_MAX, is_cpu),
        uncore=_int_map(doc.get("uncore"), "uncore", 1, cp.JOURNAL_KHZ_MAX, lambda k: k in un.KEYS),
        in_flight=doc["in_flight"],
    )
    if doc.get("boot_id") != boot_id or boot_id is None:
        return None
    return journal


def _read_journal(helper: Helper, w: RuntimeWiring) -> Journal | None:
    """This boot's journal, ``None`` when there is none (or it is from another boot). A journal that cannot be trusted or parsed is a refusal, never a guess."""
    _trust(helper, w.journal_path, want="file", must_exist=False)  # the parent chain is root-owned and not writable by others (SPEC 4.4)
    try:
        fd = open_checked(w.journal_path, write=False, create=False, euid=helper.cfg.euid_fn(), what="runtime journal")
    except FileNotFoundError:
        return None
    except TrustError as exc:
        raise _trust_refusal(exc, "JOURNAL_UNTRUSTED") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(_JOURNAL_MAX + 1)
    except OSError as exc:
        raise OpError(REFUSED, f"cannot read the runtime journal {w.journal_path}: {exc.strerror or exc}. Nothing was written.", {"check": "JOURNAL_UNAVAILABLE"}) from None
    try:
        if len(data) > _JOURNAL_MAX:
            raise ValueError("too large")
        return parse_journal(data, _boot_id(helper))
    except ValueError as exc:
        raise OpError(
            REFUSED, f"the runtime journal {w.journal_path} is unusable ({exc}); remove it by hand once the CPU frequency caps and uncore limits are known to be right. Nothing was written.",
            {"check": "JOURNAL_UNUSABLE"},
        ) from None


def _write_journal(helper: Helper, w: RuntimeWiring, journal: Journal, boot_id: str) -> None:
    data = json.dumps(journal.doc(boot_id, helper.cfg.wall_clock()), separators=(",", ":")).encode("ascii")
    path = w.journal_path
    _trust(helper, path, want="file", must_exist=False)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = open_checked(tmp, write=True, create=True, mode=0o600, euid=helper.cfg.euid_fn(), what="runtime journal")
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
        raise OpError(REFUSED, f"cannot write the runtime journal {path}: {exc.strerror or exc}. Nothing was written.", {"check": "JOURNAL_UNAVAILABLE"}) from None


# --------------------------------------------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------------------------------------------
def _cpus(w: RuntimeWiring, topology: Topology) -> list[int]:
    """Every CPU the topology has and every ``cpu<N>`` directory sysfs lists (a CPU taken offline outside the app may have left the live topology but still has its ``online`` file)."""
    found = {int(m.group(1)) for n in w.files.listdir("") if (m := _CPU_DIR.fullmatch(n))}
    return sorted(found | {c.cpu for c in topology.cores})


def _online_state(files: cp.RuntimeFiles, cpus: Sequence[int]) -> dict[int, bool | None]:
    """``{cpu: online}``; ``None`` where the CPU has no ``online`` file (cpu0). Read only. ``SysfsError`` when a file holds anything but 0 / 1."""
    out: dict[int, bool | None] = {}
    for cpu in cpus:
        value = files.read_int(f"cpu{cpu}/online")
        if value not in (None, 0, 1):
            raise cp.SysfsError(f"{files.path(f'cpu{cpu}/online')} holds {value}, not 0 or 1: nothing is assumed")
        out[cpu] = None if value is None else bool(value)
    return out


def _held(helper: Helper) -> str | None:
    """What owns the CPU frequency caps right now, or ``None``: a running stress / measure op, or the cpufreq journal of one (alive or dead)."""
    running = helper.machine.running
    if running is not None and running.op in _BLOCKING_OPS:
        return f"the running {running.op} ({running.op_id})"
    mw = getattr(helper, "measure_wiring", None)
    if mw is not None and Path(mw.journal_path).exists():
        return f"the cpufreq journal {mw.journal_path} of a vf.measure or stress.start run (running, or died and not yet restored)"
    return None


def _unavailable(exc: cp.SysfsError) -> OpError:
    return OpError(REFUSED, f"{exc}. Nothing was written.", {"check": "RUNTIME_UNAVAILABLE"})


def _refused(exc: cp.Refused) -> OpError:
    detail: dict[str, Any] = {"check": exc.check}
    if exc.key is not None:
        detail["key"] = exc.key
    return OpError(REFUSED, f"{exc}. Nothing was written.", detail)


def _policy_json(policy: Policy) -> dict[str, Any]:
    """The ``[runtime]`` keys as the policy has them; ``null`` = absent = that control may not be used."""
    r = policy.runtime
    return {
        "source": policy.where,
        "cap_ratio_min": None if r is None else r.cap_ratio_min,
        "uncore_ratio": None if r is None or r.uncore_ratio is None else list(r.uncore_ratio),
    }


def _journal_json(j: Journal | None) -> dict[str, Any] | None:
    if j is None:
        return None
    return {"caps": {str(k): v for k, v in sorted(j.caps.items())}, "uncore": dict(sorted(j.uncore.items())), "in_flight": j.in_flight}


def op_runtime_read(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    policy = _load_policy(helper)
    w = _wiring(helper)
    topo = helper_topology(helper)
    cpus = _cpus(w, topo)
    result: dict[str, Any] = {
        "caps": None, "offline": None, "uncore": None, "uncore_note": None, "policy": _policy_json(policy), "held": _held(helper), "journal": None, "journal_path": str(w.journal_path),
        "note": None,
    }
    try:
        freq = cp.read_state(w.files, cpus)
        online = _online_state(w.files, cpus)
    except cp.SysfsError as exc:  # includes a weird online value
        result["note"] = str(exc)
        return result
    rows = []
    for cpu in cpus:
        core = next((c for c in topo.cores if c.cpu == cpu), None)
        f = freq[cpu]
        rows.append({
            "cpu": cpu, "kind": None if core is None else core.kind, "unit": None if core is None else topo.unit_of_cpu(cpu), "online": online[cpu] is not False,
            "cpufreq": None if f is None else f.as_json(),
        })
    result["caps"] = rows
    result["offline"] = [cpu for cpu in cpus if online[cpu] is False]
    try:
        result["uncore"] = un.read_state(w.files).as_json()
    except cp.SysfsError as exc:
        result["uncore_note"] = str(exc)
    try:
        result["journal"] = _journal_json(_read_journal(helper, w))
    except OpError as exc:
        result["note"] = exc.message
    return result


# --------------------------------------------------------------------------------------------------------------
# validation (shape only)
# --------------------------------------------------------------------------------------------------------------
def _bad(text: str) -> OpError:
    return OpError(BAD_REQUEST, text)


def _cpu_key(field_name: str, key: object) -> str:
    if not isinstance(key, str) or not re.fullmatch(r"[0-9]{1,4}", key) or int(key) >= _CPU_MAX:
        raise _bad(f"{field_name} keys must be CPU numbers (\"0\"..\"{_CPU_MAX - 1}\"), got {key!r}")
    return str(int(key))


def _whole(what: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
        raise _bad(f"{what} must be a whole ratio 1..255 (1 ratio = 100 MHz)")
    return value


def _validate_runtime_plan(args: dict[str, Any]) -> dict[str, Any]:
    if "parking" in args:
        raise _bad("core parking is not supported (held, D-pending)")
    _only(args, {"caps", "uncore", "revert"})
    if "revert" in args:
        if set(args) != {"revert"} or args["revert"] is not True:
            raise _bad("'revert' must be true and the only field")
        return {"revert": True}
    out: dict[str, Any] = {}
    if args.get("caps") is not None:
        if not isinstance(args["caps"], dict) or not args["caps"]:
            raise _bad("'caps' must be a non-empty object {cpu: ratio}")
        out["caps"] = {_cpu_key("caps", k): _whole(f"caps.{k}", v) for k, v in args["caps"].items()}
    if args.get("uncore") is not None:
        raw = args["uncore"]
        if not isinstance(raw, dict):
            raise _bad("'uncore' must be an object {min_ratio?, max_ratio?}")
        extra = sorted(set(raw) - set(un.KEYS))
        if extra:
            raise _bad(f"unknown uncore key(s): {', '.join(map(repr, extra))}")
        picked = {k: _whole(f"uncore.{k}", v) for k, v in raw.items() if v is not None}
        if picked:
            out["uncore"] = picked
    if not out:
        raise OpError(BAD_REQUEST, "the runtime intent changes nothing (set caps and/or uncore)", {"check": "NOTHING_TO_DO"})
    return out


# --------------------------------------------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------------------------------------------
_PHASE = {"caps": 0, "uncore": 1}


def _order(steps: list[cp.RuntimeStep]) -> list[cp.RuntimeStep]:
    """Caps, then uncore (each domain keeps its own safe order inside); renumbered."""
    ordered = sorted(steps, key=lambda s: _PHASE[s.domain])
    return [dataclasses.replace(s, number=i) for i, s in enumerate(ordered, 1)]


def _revert_topology(helper: Helper) -> Topology | None:
    """The live topology for a revert, or ``None``: a revert restores per-CPU files by number and must not be stopped by a topology an offline CPU has made unusable."""
    try:
        return helper_topology(helper)
    except OpError:
        return None


def _unreachable(steps: Sequence[cp.RuntimeStep]) -> list[str]:
    """One plan note per distinct cap note, naming its CPUs (an E cluster's four share one)."""
    by_note: dict[str, list[int]] = {}
    for s in steps:
        if s.note:
            by_note.setdefault(s.note, []).append(int(s.key[3:]))
    return [f"cpu {', '.join(map(str, sorted(cpus)))}: {note}" for note, cpus in by_note.items()]


def _steps_for(helper: Helper, policy: Policy | None, w: RuntimeWiring, args: dict[str, Any]) -> tuple[list[cp.RuntimeStep], list[str], Journal | None]:
    """``(steps, notes, journal of this boot or None)`` for a plan-shaped ``args``; raises the refusal. ``policy`` is not needed for a revert. For a revert a note means a journaled value was
    **not** restored (the caller then keeps the in-flight mark)."""
    revert = bool(args.get("revert"))
    topo = _revert_topology(helper) if revert else helper_topology(helper)
    journal = _read_journal(helper, w)
    notes: list[str] = []
    try:
        if revert:
            if journal is None or journal.empty():
                raise OpError(REFUSED, "there is nothing to revert to: no runtime value was written in this boot (no journal for this boot). Nothing was written.", {"check": "NO_JOURNAL"})
            steps, notes = cp.plan_revert(journal.caps, cp.read_state(w.files, sorted(journal.caps)), topo)
            if journal.uncore:
                steps += un.plan_revert(journal.uncore, un.read_state(w.files))
            held = _held(helper)
            if held is not None and any(s.domain == "caps" for s in steps):
                # a revert restores a recorded state, but not over a run that owns the caps (its own restore would undo it); the uncore is fine
                raise cp.Refused("CAPS_CONFLICT", f"{held} holds the CPU frequency caps: the revert of caps waits until it is finished or restored")
            return _order(steps), notes, journal
        assert policy is not None
        if journal is not None and journal.in_flight:
            raise OpError(
                REFUSED, "a previous runtime apply did not finish (the journal says it was in flight): run the revert first, then plan again. Nothing was written.", {"check": "JOURNAL_IN_FLIGHT"},
            )
        held = _held(helper)
        steps = []
        if "caps" in args:
            intent = {int(k): v for k, v in args["caps"].items()}
            cap_steps = cp.plan(intent, policy, cp.read_state(w.files, _cpus(w, topo)), topo, held=held)
            wrote = {int(s.key[3:]) for s in cap_steps}
            if wrote - set(intent):
                notes.append(f"an E-cluster runs one ratio: the cap was also set on cpu {', '.join(map(str, sorted(wrote - set(intent))))} of the same cluster(s)")
            notes += _unreachable(cap_steps)
            steps += cap_steps
        if "uncore" in args:
            steps += un.plan(args["uncore"], policy, un.read_state(w.files))
        return _order(steps), notes, journal
    except cp.Refused as exc:
        raise _refused(exc) from None
    except cp.SysfsError as exc:
        raise _unavailable(exc) from None


def _planned(helper: Helper, w: RuntimeWiring, args: dict[str, Any]) -> tuple[list[cp.RuntimeStep], list[str], Journal | None, Policy]:
    policy = _load_policy(helper)
    steps, notes, journal = _steps_for(helper, None if args.get("revert") else policy, w, args)
    if not steps:
        raise OpError(REFUSED, " ".join(["the values in force already are the ones asked: nothing to write.", *(f"{n}." for n in notes), "Nothing was written."]), {"check": "NOTHING_TO_DO"})
    return steps, notes, journal, policy


def _journal_policy_warnings(policy: Policy, journal: Journal) -> list[str]:
    """A revert restores a recorded state and is not bounded by ``[runtime]``; say so when a recorded value lies outside what the policy would allow now."""
    out: list[str] = []
    r = policy.runtime
    if r is not None and r.cap_ratio_min is not None:
        low = [c for c, khz in journal.caps.items() if khz < r.cap_ratio_min * cp.KHZ_PER_RATIO]
        if low:
            out.append(f"the journaled cap of cpu {', '.join(map(str, sorted(low)))} is below [runtime] cap_ratio_min = {r.cap_ratio_min}: the revert restores it anyway")
    if r is not None and r.uncore_ratio is not None:
        for key, khz in journal.uncore.items():
            if not r.uncore_ratio[0] * cp.KHZ_PER_RATIO <= khz <= r.uncore_ratio[1] * cp.KHZ_PER_RATIO:
                out.append(f"the journaled {key} ({khz / cp.KHZ_PER_RATIO:g}) is outside [runtime] uncore_ratio = {list(r.uncore_ratio)}: the revert restores it anyway")
    return out


def op_runtime_plan(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("revert"):  # a revert restores per-CPU files by number: an offline CPU is skipped and reported, not a refusal
        refuse_if_cpu_offline(helper, "runtime.plan")
    w = _wiring(helper)
    steps, notes, journal, policy = _planned(helper, w, args)
    revert = bool(args.get("revert"))
    warnings = list(notes)
    if revert and journal is not None:
        warnings.extend(_journal_policy_warnings(policy, journal))
    if not revert:
        warnings.append("the values now in force are journaled before the first write of each key in this boot; \"revert\" puts them back")
    warnings.append("these are runtime values: a reboot restores the firmware / kernel defaults")
    nonce = "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(6))
    canon = json.dumps({"args": args, "steps": [s.as_json() for s in steps], "nonce": nonce}, sort_keys=True)
    plan_id = hashlib.sha256(canon.encode()).hexdigest()
    _plans(helper).add(StoredRuntimePlan(plan_id, nonce, helper.cfg.monotonic(), steps, revert, dict(args)))  # type: ignore[arg-type]
    return {
        "plan_id": plan_id, "nonce": nonce, "revert": revert, "steps": [s.as_json() for s in steps], "writes": len(steps), "ops_needed": 0, "expires_s": int(PLAN_EXPIRES_S),
        "journal_new": not revert and (journal is None or _would_add(journal, steps)), "warnings": warnings, "policy": _policy_json(policy),
    }


def _would_add(journal: Journal, steps: Sequence[cp.RuntimeStep]) -> bool:
    return dataclasses.replace(journal, caps=dict(journal.caps), uncore=dict(journal.uncore)).remember(steps)


# --------------------------------------------------------------------------------------------------------------
# audit, the lock, one step
# --------------------------------------------------------------------------------------------------------------
def _audit(helper: Helper, log: AuditLog, event: str, **fields: Any) -> None:
    """One audit record of a runtime sysfs action (``kind = "runtime"``). ``OSError`` propagates."""
    cfg = helper.cfg
    log.append(event, ts=cfg.wall_clock(), operator=cfg.operator, kind="runtime", boot_id=_boot_id(helper), **fields)


def _audit_quiet(helper: Helper, log: AuditLog, event: str, **fields: Any) -> str | None:
    """After the first write: a record that cannot be written is reported, never allowed to hide the outcome."""
    try:
        _audit(helper, log, event, **fields)
    except OSError as exc:
        return f"could not write the audit record ({event}): {exc}"
    return None


@dataclass
class _Sent:
    """What one sent step left: the file read-back (``None`` = unreadable), a failed write, a lock that could not be released."""

    file_value: int | None = None
    write_error: cp.SysfsError | None = None
    lock_problem: str | None = None


def _send_step(helper: Helper, w: RuntimeWiring, log: AuditLog, step: cp.RuntimeStep, record: dict[str, Any]) -> _Sent:
    """Under the lock: the ``runtime_attempt`` record (written and fsync'd first), the one file write, the file read-back. :class:`_NotSent` before the write is sent."""
    _take_lock(helper)
    sent = _Sent()
    try:
        try:
            _audit(helper, log, "runtime_attempt", detail=f"runtime write {step.domain} {step.key}: {step.before} -> {step.value}", **record)
        except OSError as exc:
            raise _NotSent("AUDIT_UNAVAILABLE", f"the audit log cannot be written ({exc}); the write was not sent") from None
        try:
            w.files.write_int(step.file, step.value)
            try:
                sent.file_value = w.files.read_int(step.file)
            except cp.SysfsError:
                sent.file_value = None
        except cp.SysfsError as exc:
            sent.write_error = exc
    finally:
        sent.lock_problem = _release_lock(helper)
    return sent


@dataclass
class _Run:
    table: list[dict[str, Any]]
    readback: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stop: dict[str, Any] | None = None
    sent: int = 0


def _run_steps(helper: Helper, w: RuntimeWiring, log: AuditLog, steps: list[cp.RuntimeStep], plan_id: str | None, revert: bool, cancel: threading.Event | None) -> _Run:
    """The transactions, one file at a time. A step refused before it is sent with nothing sent so far raises (nothing changed); any later stop is the result's ``stop``."""
    run = _Run([{"n": s.number, "domain": s.domain, "key": s.key, "file": s.file, "before": s.before, "value": s.value, "status": "not_sent"} for s in steps])  # every step is listed
    for step, entry in zip(steps, run.table):
        if cancel is not None and cancel.is_set():
            run.stop = {"kind": "cancelled", "reason": f"step {step.number}: cancelled between transactions; nothing was sent for it and the steps after it", "tripped": False}
            break
        record = dict(domain=step.domain, key=step.key, path=str(w.files.path(step.file)), before=step.before, written=step.value, plan_id=plan_id, revert=revert)
        try:
            outcome = _send_step(helper, w, log, step, record)
        except _NotSent as exc:
            entry["status"] = "refused"
            reason = f"step {step.number}: {exc}"
            if run.sent == 0:
                raise OpError(REFUSED, f"{reason}. Nothing was written.", {"check": exc.check}) from None
            run.stop = {"kind": "refused", "reason": reason, "tripped": False}
            break
        run.sent += 1  # a failed write may have reached the file
        if outcome.lock_problem:
            run.warnings.append(outcome.lock_problem)
        if outcome.write_error is not None:
            entry["status"] = "error"
            entry["detail"] = str(outcome.write_error)
            run.warnings.extend(filter(None, [_audit_quiet(helper, log, "error", detail=str(outcome.write_error), **record)]))
            run.stop = {"kind": "transport", "reason": f"step {step.number}: {outcome.write_error}", "tripped": True}
            break
        problem = cp.readback_problem(step, outcome.file_value)
        run.readback.append({"domain": step.domain, "key": step.key, "expected": list(step.expect), "file": outcome.file_value, "match": problem is None, **({} if problem is None else {"problem": problem})})
        entry.update(status="ok" if problem is None else "unverified", file_value=outcome.file_value)
        run.warnings.extend(filter(None, [_audit_quiet(helper, log, "readback", detail=problem or "the file shows the value written", read_value=outcome.file_value, matches=problem is None, **record)]))
        if problem is not None:
            run.warnings.extend(filter(None, [_audit_quiet(helper, log, "trip", reason="TRIPPED", detail=problem, **record)]))
            run.stop = {"kind": "unverified", "reason": f"step {step.number}: read-back mismatch: {problem}; stopping (no retry, no automatic revert)", "tripped": True}
            break
        if outcome.lock_problem:  # the next transaction would refuse on the stale lock: stop here, loudly
            run.stop = {"kind": "refused", "reason": f"step {step.number}: {outcome.lock_problem}", "tripped": True}
            break
    return run


# --------------------------------------------------------------------------------------------------------------
# runtime.apply
# --------------------------------------------------------------------------------------------------------------
def op_runtime_apply(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    if not _plans(helper).is_revert(str(args.get("plan_id"))):
        refuse_if_cpu_offline(helper, "runtime.apply")
    plan = _plans(helper).claim(args["plan_id"], args["typed"])
    w = _wiring(helper)
    boot_id = _boot_id(helper)
    if boot_id is None:
        raise OpError(REFUSED, "the boot id is unknown, so the runtime journal cannot be bound to this boot. Nothing was written.", {"check": "BOOT_ID_UNKNOWN"})
    # the plan was made from the values then in force: plan again from the values now; any difference is stale (nothing written)
    steps, notes, journal, _policy = _planned(helper, w, plan.intent)
    if _identity(steps) != _identity(plan.steps):
        raise OpError(PLAN_STALE, "the runtime values changed since the plan was made (or the plan no longer fits the policy); nothing was written. Plan again.")
    try:
        log = _open_audit(helper)  # the one AuditLog of this apply
    except OSError as exc:
        raise OpError(REFUSED, f"the audit log cannot be opened ({exc}); nothing was written.", {"check": "AUDIT_UNAVAILABLE"}) from None
    if journal is None:
        journal = Journal()
    if not plan.revert:
        journal.remember(steps)
        journal.in_flight = True
        _write_journal(helper, w, journal, boot_id)  # before the first write: what is being replaced, and that an apply is under way
    try:
        run = _run_steps(helper, w, log, steps, plan.plan_id, plan.revert, ctx.cancel)
    except OpError:
        if not plan.revert:
            _end_in_flight(helper, w, journal, boot_id, [])  # nothing was sent: nothing is in flight
        raise
    skipped = plan.revert and bool(notes)  # a revert that left a journaled value unrestored is not complete: the in-flight mark stays
    run.warnings.extend(f"{n}. The journal is kept." for n in notes if plan.revert)
    if not plan.revert or (run.stop is None and journal.in_flight and not skipped):  # a complete revert also settles a journal left in flight by a dead helper
        _end_in_flight(helper, w, journal, boot_id, run.warnings)
    complete = run.stop is None and not skipped  # a revert that skipped a journaled value is not complete (its warning says which)
    out: dict[str, Any] = {
        "plan_id": plan.plan_id, "revert": plan.revert, "steps": run.table, "readback": run.readback, "complete": complete, "writes_sent": run.sent, "after": _after(w, steps), "journal": str(w.journal_path),
        "warnings": run.warnings,
    }
    if complete:
        return out
    if run.stop is None or run.sent:
        out["stop"] = run.stop  # the hardware is partly changed: the table must reach the page, so this is a result, not an error
        return out
    assert run.stop is not None
    if run.stop["kind"] == "cancelled":
        raise OpError(CANCELLED, "runtime.apply was cancelled before the first write; nothing was changed")
    raise OpError(INTERNAL, str(run.stop["reason"]), {"tripped": run.stop["tripped"]})


def _identity(steps: Sequence[cp.RuntimeStep]) -> list[tuple[str, str, int, int]]:
    return [(s.domain, s.key, s.before, s.value) for s in steps]


def _after(w: RuntimeWiring, steps: Sequence[cp.RuntimeStep]) -> dict[str, Any] | None:
    """The values now in force of what the steps touched; ``None`` if they cannot be read."""
    try:
        caps = cp.read_state(w.files, sorted({int(s.key[3:]) for s in steps if s.domain == "caps"}))
        return {
            "caps": {str(c): None if f is None else f.cap_khz for c, f in caps.items()},
            "uncore": un.read_state(w.files).as_json() if any(s.domain == "uncore" for s in steps) else None,
        }
    except cp.SysfsError:
        return None


def _end_in_flight(helper: Helper, w: RuntimeWiring, journal: Journal, boot_id: str, warnings: list[str]) -> None:
    """Rewrite the journal with ``in_flight`` cleared. A failure is a warning (the journal then says in-flight until the next revert / start restores it: loud, and the safe direction)."""
    journal.in_flight = False
    try:
        _write_journal(helper, w, journal, boot_id)
    except OpError as exc:
        warnings.append(f"could not clear the in-flight mark of the runtime journal: {exc.message}")


# --------------------------------------------------------------------------------------------------------------
# helper start: a journal that was in flight is restored
# --------------------------------------------------------------------------------------------------------------
def recover_journal(helper: Helper) -> bool:
    """At helper start (``Helper.start``, next to the measure journal): an ``in_flight`` journal of this boot is an apply that died with the previous helper -- put the journaled values back
    (a restore of a recorded state, audited like every other write), then clear the mark. A journal that is not in flight is kept for ``revert`` and left alone; one of another boot or
    one that cannot be trusted / parsed is logged and not applied. Never raises (it must not keep the helper from starting). Returns ``True`` when a restore was attempted."""
    log = helper.cfg.log
    try:
        w = _wiring(helper)
        journal = _read_journal(helper, w)
    except OpError as exc:
        if exc.code != INTERNAL:  # INTERNAL = no runtime-control support configured (a simulator without wiring): nothing to recover, nothing to say
            log(f"runtime journal not applied: {exc.message}")
        return False
    except Exception as exc:
        log(f"runtime journal recovery FAILED: {type(exc).__name__}: {exc}")
        return False
    if journal is None or not journal.in_flight:
        return False
    try:
        log("runtime journal: an apply did not finish before the previous helper ended; restoring the journaled values")
        steps, notes, _ = _steps_for(helper, None, w, {"revert": True})
        for n in notes:
            log(f"runtime journal: {n}")
        boot_id = _boot_id(helper)
        if boot_id is None:
            raise OpError(REFUSED, "the boot id is unknown")
        if steps:
            audit = _open_audit(helper)
            run = _run_steps(helper, w, audit, steps, None, True, None)
            for p in run.warnings:
                log(f"runtime journal: {p}")
            if run.stop is not None:
                log(f"runtime journal NOT fully restored, in-flight mark kept: {run.stop['reason']}")
                return True
        if notes:
            log(f"runtime journal NOT fully restored, in-flight mark kept: {len(notes)} value(s) skipped")
            return True
        _end_in_flight(helper, w, journal, boot_id, warnings := [])
        for p in warnings:
            log(f"runtime journal: {p}")
        log(f"runtime journal restored: {len(steps)} value(s) written back, in-flight mark cleared")
    except OpError as exc:
        log(f"runtime journal NOT restored, in-flight mark kept: {exc.message}")
    except Exception as exc:
        log(f"runtime journal recovery FAILED: {type(exc).__name__}: {exc}")
    return True


# --------------------------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------------------------
def runtime_ops() -> list[OpSpec]:
    return [
        # sysfs reads only: allowed in every state (a running stress shows in "held")
        OpSpec("runtime.read", op_runtime_read, _no_args, slow=True),
        OpSpec("runtime.plan", op_runtime_plan, _validate_runtime_plan, long=True, state=State.PLANNING),
        # D-029: every write op is long and write-gated (the preflight write gate on hardware), state "applying"
        OpSpec("runtime.apply", op_runtime_apply, _validate_tune_apply, long=True, write=True, state=State.APPLYING),
    ]
