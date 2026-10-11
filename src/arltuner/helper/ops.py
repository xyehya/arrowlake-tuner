"""G1 op handlers (read-only): ``preflight``, ``vf.dump``, ``audit.tail``, ``telemetry.start`` / ``telemetry.stop``, ``cancel``.

Handlers are plain functions ``(helper, ctx, args) -> result dict`` that raise :class:`OpError`; the dispatcher in ``core`` owns framing,
state, threads and the one-response rule. Nothing here sends a mailbox request except through ``arltuner.guard.WriteGuard`` (``vf.dump``).
"""
from __future__ import annotations

import dataclasses
import hashlib
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
from arltuner.guard.audit import parse_audit_ts
from arltuner.msr import MailboxTransport
from arltuner.platform.preflight import PreflightReport, Status
from arltuner.topology import TopologyError, UnknownCore

from .protocol import BAD_REQUEST, CANCELLED, INTERNAL, MAX_LINE, PREFLIGHT_FAILED, REFUSED, OpError
from .state import State
from .trust import TrustError, check_lock_file, check_trusted_path, open_checked

if TYPE_CHECKING:
    from .core import Helper

__all__ = ["BudgetMeter", "OpContext", "OpSpec", "accepted_match", "builtin_ops", "write_gate"]

AUDIT_TAIL_DEFAULT = 20
AUDIT_TAIL_MAX = 200
_TAIL_READ_BYTES = 256 * 1024  # audit.tail never reads more than the last 256 KiB
_RESULT_SLACK = 2048  # envelope of the response line around the result
TELEMETRY_INTERVAL_RANGE = (0.2, 60.0)
ACCEPTED_FILE = "EXPECTED-words.txt"  # inside the helper's accepted dir (G-107): the format of profiles/EXPECTED-words.txt
_ACCEPTED_MAX_BYTES = 64 * 1024
_AUDIT_CHUNK = 256 * 1024
_AUDIT_LINE_MAX = 1024 * 1024  # an unterminated audit line longer than this is not our log: no count is claimed (bounds the scan buffer)
_AUDIT_HEAD_BYTES = 4096
MAX_EXPECTED_WORDS = 16  # words per EXPECTED line (OC word + points): a P core has 9, an E core 7; more is not an accepted-state file
MAX_DIFFS_PER_CPU = 32  # diffs reported per CPU (then ``truncated``)
MAX_DIFFS_TOTAL = 200  # and across all CPUs, so the vf.dump response always fits the 64 KiB line


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
    slow: bool = False  # read-only but touches the filesystem: runs on its own thread (bounded), never on the dispatch thread
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
    truncated = start > 0 or len(lines) > len(wanted)
    while records and len(json.dumps(records, separators=(",", ":"))) > MAX_LINE - _RESULT_SLACK:  # the response line has a 64 KiB cap too
        records.pop(0)
        truncated = True
    return {"path": str(path), "records": records, "truncated": truncated}


# --------------------------------------------------------------------------------------------------------------
# Heartbeat budget (G-107, contract 8 addendum): ops_used / ops_budget
# --------------------------------------------------------------------------------------------------------------
class BudgetMeter:
    """``(ops_used, ops_budget)`` for the heartbeat, cheap enough to call every beat.

    ``ops_used`` is what the guard counts for the per-boot cap (``arltuner.guard.read_state``): the ``attempt`` records of the audit log whose
    ``boot_id`` is the current one (``None`` is its own bucket). It is counted *incrementally*: the audit file is opened and ``fstat``-ed every
    call and only the bytes appended since the last call are read (complete lines only), so a long log is read once, never per beat. A replaced or
    truncated file is rescanned from the start. ``ops_budget`` is ``max_operations_per_boot`` of the loaded root policy, cached on the policy
    file's stat. Either is ``None`` (never a guess) when its source is untrusted, unreadable or invalid, which includes an audit log the guard
    itself would refuse (``AuditUnreadable``). Runs on the heartbeat thread, never on the dispatch thread.
    """

    def __init__(self, helper: Helper) -> None:
        self._helper = helper
        self._lock = threading.Lock()
        # audit scan state
        self._audit_key: tuple[int, int, int, int] | None = None  # (dev, ino, size, mtime_ns) at the last scan
        self._offset = 0
        self._by_boot: dict[str | None, int] = {}
        self._audit_error: str | None = None
        self._tail: tuple[str, Any] = ("skip", None)  # the unterminated last line, judged at the last scan
        self._head = b""  # digest of the first <= 4 KiB of the counted part, to notice a same-inode rewrite
        # policy cache
        self._policy_key: tuple[int, int, int, int] | None = None
        self._budget: int | None = None

    def snapshot(self) -> tuple[int | None, int | None]:
        with self._lock:
            try:
                budget = self._budget_locked()
            except Exception:  # a heartbeat must never die of its own telemetry
                budget = None
            try:
                used = self._used_locked()
            except Exception:
                used = None
            return used, budget

    # ---- budget
    def _budget_locked(self) -> int | None:
        cfg = self._helper.cfg
        if cfg.policy_path is None:
            return default_m1_policy().max_operations_per_boot
        try:
            _trust(self._helper, cfg.policy_path, want="file", must_exist=True)
            st = os.stat(cfg.policy_path)
        except (OpError, OSError):
            self._policy_key, self._budget = None, None
            return None
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if key != self._policy_key:
            try:
                self._budget = load_policy(cfg.policy_path).max_operations_per_boot
            except (PolicyError, OSError):
                self._budget = None
            self._policy_key = key
        return self._budget

    # ---- used
    def _used_locked(self) -> int | None:
        helper = self._helper
        cfg = helper.cfg
        path = cfg.audit_path
        try:
            _trust(helper, path, want="file", must_exist=False)
            fd = open_checked(path, write=False, create=False, euid=cfg.euid_fn(), what="audit log")
        except FileNotFoundError:
            self._reset()
            return self._count()  # no log yet: no history, as read_state says
        except (OpError, TrustError, OSError):
            self._reset()
            return None
        try:
            with os.fdopen(fd, "rb") as handle:
                st = os.fstat(handle.fileno())
                key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
                replaced = self._audit_key is not None and (st.st_dev, st.st_ino) != self._audit_key[:2]
                if replaced or st.st_size < self._offset or (self._offset and self._head_digest(handle) != self._head):
                    self._reset()  # a different, shorter or rewritten file (same inode, first bytes changed) is not an append to what was counted
                if key != self._audit_key:
                    self._scan(handle)
                    self._audit_key = key
                    self._head = self._head_digest(handle)
        except OSError:
            self._reset()
            return None
        if self._audit_error is not None or self._tail[0] == "error":
            return None
        return self._count()

    def _reset(self) -> None:
        self._audit_key, self._offset, self._by_boot, self._audit_error, self._tail = None, 0, {}, None, ("skip", None)
        self._head = b""

    def _head_digest(self, handle: Any) -> bytes:
        """Digest of the first (up to 4 KiB of the) already-counted bytes: an append never changes them, an in-place rewrite does."""
        handle.seek(0)
        return hashlib.sha256(handle.read(min(_AUDIT_HEAD_BYTES, self._offset))).digest()

    def _count(self) -> int:
        try:
            boot = self._helper.cfg.boot_id_fn()
        except Exception:
            boot = None
        boot_id = boot.strip() or None if isinstance(boot, str) else None
        return self._by_boot.get(boot_id, 0) + (1 if self._tail == ("count", boot_id) else 0)

    def _scan(self, handle: Any) -> None:
        """Read what was appended since ``self._offset``: complete lines are folded into the counts; a trailing line with no newline is judged
        afresh each time (``self._tail``) and never consumed, exactly as ``read_state`` treats it (ignored if it does not parse, counted if it does)."""
        if self._audit_error is not None:
            return  # an unreadable record stays unreadable (the log is append-only); only a replaced or truncated file resets it
        handle.seek(self._offset)
        pending = b""
        while True:
            chunk = handle.read(_AUDIT_CHUNK)
            if not chunk:
                break
            pending += chunk
            cut = pending.rfind(b"\n")
            if cut < 0:
                if len(pending) > _AUDIT_LINE_MAX:
                    self._audit_error = f"an audit record is longer than {_AUDIT_LINE_MAX} bytes"  # not a log this helper wrote; no count is claimed
                    return
                continue
            complete, pending = pending[: cut + 1], pending[cut + 1 :]
            for raw in complete.split(b"\n"):  # '\n' only, like read_state's line iteration (bytes.splitlines would also split on '\r')
                kind, value = self._classify(raw, partial=False)
                if kind == "error":
                    self._audit_error = value
                    break
                if kind == "count":
                    self._by_boot[value] = self._by_boot.get(value, 0) + 1
            self._offset += len(complete)
            if self._audit_error is not None:
                return
            if len(pending) > _AUDIT_LINE_MAX:
                self._audit_error = f"an audit record is longer than {_AUDIT_LINE_MAX} bytes"
                return
        self._tail = self._classify(pending, partial=True)

    @staticmethod
    def _classify(raw: bytes, *, partial: bool) -> tuple[str, Any]:
        """One audit line as ``read_state`` sees it: ``("skip", None)``, ``("count", boot_id)`` or ``("error", why)`` (``AuditUnreadable``).
        The timestamp rule is ``arltuner.guard.audit.parse_audit_ts`` itself (reused on purpose so the two can never drift apart)."""
        text = raw.strip()
        if not text:
            return "skip", None
        try:
            record = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return ("skip", None) if partial else ("error", "an audit record is not valid JSON")
        if not isinstance(record, dict) or not isinstance(record.get("event"), str):
            return "error", "an audit record is not an object with an 'event'"
        if record["event"] != "attempt":
            return "skip", None
        try:
            parse_audit_ts(record.get("ts"), "audit")
        except AuditUnreadable as exc:
            return "error", str(exc)
        boot = record.get("boot_id")
        if boot is not None and not isinstance(boot, str):
            return "error", "an attempt record's boot_id is neither a string nor null"
        return "count", boot


# --------------------------------------------------------------------------------------------------------------
# accepted-state compare (G-107): vf.dump result vs the trusted EXPECTED words
# --------------------------------------------------------------------------------------------------------------
def _parse_expected(text: str) -> dict[int, list[int]]:
    """``<cpu> <OC word (P2=0)> <point 1> ... <point n>`` per line (hex, no 0x; cpu decimal); ``#`` lines and blanks ignored. ValueError if malformed."""
    table: dict[int, list[int]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        try:
            cpu = int(parts[0])
            words = [int(w, 16) for w in parts[1:]]
        except ValueError:
            raise ValueError(f"line {number}: not '<cpu> <hex words>'") from None
        if len(words) > MAX_EXPECTED_WORDS:
            raise ValueError(f"line {number}: {len(words)} words (at most {MAX_EXPECTED_WORDS} are plausible)")
        if cpu < 0 or len(words) < 2 or any(not 0 <= w <= 0xFFFFFFFF for w in words):
            raise ValueError(f"line {number}: needs a cpu, the OC word and at least one point, each a 32-bit hex word")
        if cpu in table:
            raise ValueError(f"line {number}: cpu {cpu} appears twice")
        table[cpu] = words
    if not table:
        raise ValueError("no expected words")
    return table


def _load_accepted(helper: Helper) -> tuple[dict[int, list[int]] | None, str | None]:
    """The trusted EXPECTED table, or ``(None, why)``. Opened only through :func:`open_checked` (no symlink, one link, owned by the helper's euid,
    not group/world-writable); with ``enforce_trust`` also the SPEC 4.4 parent-chain check. Any doubt is *untrusted*, never a guess."""
    cfg = helper.cfg
    directory = getattr(cfg, "accepted_dir", None)  # HelperConfig field owned by G-106; absent or None: nothing is read
    if directory is None:
        return None, "no accepted state directory is configured"
    path = Path(directory) / ACCEPTED_FILE
    if cfg.enforce_trust:
        try:
            check_trusted_path(path, want="file", must_exist=True, lstat_fn=cfg.lstat_fn)
        except TrustError as exc:
            if exc.reason == "does not exist" and exc.path == str(path):
                return None, f"no accepted state ({path} is absent)"
            return None, f"accepted state untrusted: {exc}"
    try:
        fd = open_checked(path, write=False, create=False, euid=cfg.euid_fn(), what="accepted state")
    except FileNotFoundError:
        return None, f"no accepted state ({path} is absent)"
    except TrustError as exc:
        return None, f"accepted state untrusted: {exc}"
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(_ACCEPTED_MAX_BYTES + 1)
    except OSError as exc:
        return None, f"accepted state unreadable: {exc.strerror or exc}"
    if len(data) > _ACCEPTED_MAX_BYTES:
        return None, f"accepted state untrusted: {path} is larger than {_ACCEPTED_MAX_BYTES} bytes"
    try:
        return _parse_expected(data.decode("utf-8")), None
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"accepted state invalid: {exc}"


def _hex(word: int | None) -> str | None:
    return None if word is None else f"{word:#010x}"


def accepted_match(helper: Helper, document: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Compare the dump ``document`` (``vfdump.to_json``) with the accepted EXPECTED words: ``(per_core, None)`` or ``(None, note)``.
    ``per_core`` has one ``{cpu, match, diffs: [{point, expected, actual}]}`` for each dumped CPU that has an expected line (``point`` 0 is the OC
    word, P2 = 0; 1..n the points; words as ``0x%08x``, ``null`` when one side has no such point). CPUs without an expected line are not listed."""
    try:
        table, note = _load_accepted(helper)
        if table is None:
            return None, note
        out: list[dict[str, Any]] = []
        shown = 0
        for unit in document.get("units", []):
            for cpu in unit.get("cpus", []):
                want = table.get(cpu["cpu"])
                if want is None:
                    continue
                oc = cpu.get("oc")
                got: list[int | None] = [None if oc is None else int(oc["word"], 16)]
                got += [int(p["word"], 16) for p in cpu.get("points", [])]
                diffs = []
                differing = 0
                for point in range(max(len(want), len(got))):
                    expected = want[point] if point < len(want) else None
                    actual = got[point] if point < len(got) else None
                    if expected != actual:
                        differing += 1
                        if len(diffs) < MAX_DIFFS_PER_CPU and shown < MAX_DIFFS_TOTAL:
                            diffs.append({"point": point, "expected": _hex(expected), "actual": _hex(actual)})
                            shown += 1
                entry: dict[str, Any] = {"cpu": cpu["cpu"], "match": not differing, "diffs": diffs}
                if differing > len(diffs):
                    entry["truncated"] = True  # more differences than listed; the response must stay under the 64 KiB line
                out.append(entry)
        return out, None
    except Exception as exc:  # the compare is informational: it must never fail a dump that read the chip
        return None, f"accepted compare failed: {type(exc).__name__}: {exc}"


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
        transport_sleep = sleep
    else:
        clock, wall = cfg.monotonic, cfg.wall_clock
        transport_sleep = cfg.sleep  # a real sleep: a set cancel flag must not turn the mailbox poll loop into a spin (cancel acts between transactions)

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
    refused: list[GuardRefused] = []
    sent = 0
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
    matched, note = accepted_match(helper, document)
    return {**document, "accepted_match": matched, "accepted_note": note, "ops_done": sent}


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
        OpSpec("preflight", op_preflight, _no_args, slow=True),
        OpSpec("audit.tail", op_audit_tail, _validate_audit_tail, slow=True),
        OpSpec("telemetry.start", op_telemetry_start, _validate_telemetry_start),
        OpSpec("telemetry.stop", op_telemetry_stop, _no_args),
        OpSpec("cancel", op_cancel, _validate_cancel),
    ]
    from .ops_tune import tune_ops  # G-112: vf.dump (now remembering the words in force), tune.*, profiles.*; imported here because ops_tune imports this module

    specs.extend(tune_ops())
    return {s.name: s for s in specs}
