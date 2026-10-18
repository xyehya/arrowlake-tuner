"""G-129 helper op ``profiles.accept`` (D-036): the **user** makes a profile the accepted state, in the app. Nothing is shipped as one.

``profiles.accept {name}`` takes a saved profile, ``profiles.accept {from: "read"}`` the state this boot's last ``tune.read`` found, and replaces
``/etc/arltuner/accepted/`` (``CURRENT`` + ``profiles/<name>.toml`` + ``EXPECTED-words.txt`` + ``fingerprint.json``, see :mod:`arltuner.profiles.accepted`) as a whole.

Contract (what "accepted" means, so the restore and the comparison stay honest):

* **Accepted = what the CPU holds now.** ``EXPECTED-words.txt`` is the V/F words of this boot's last read (the verified read-backs after an apply), so a
  saved profile must already be applied: when its resolved words differ from the read the op refuses (``NOT_APPLIED``, "apply it first"). A profile that
  was never applied cannot be recorded as the state the chip was verified to hold.
* G-148 (D-044): the accepted profile and ``EXPECTED-words.txt`` come from **one source**: the words of this boot's last read (the verified read-backs after
  an apply) and the turbo tables of that read. The profile is the **complete** profile of those words (:func:`arltuner.profiles.complete_from_words`: every
  unit's OC word fields, every fused point's offset, both tables when read), whichever way it is accepted -- ``{from: "read"}``, a saved complete profile,
  or a saved partial (legacy) one: the partial file is never recorded, only the state it was verified to have left. ``CURRENT`` names that one profile.
  A word that millivolts cannot represent exactly (the profile format has whole millivolts) is refused (``NOT_REPRESENTABLE``) rather than rounded, because
  the restore would then write a different word than the one recorded.
* Needs a read this boot (``NO_READ``); the policy still judges a saved profile (its range check runs as for a plan); writes nothing to the hardware.
* The op appends one audit record before the swap (and refuses if it cannot): ``event result``, ``op profiles.accept``, the name, what was replaced.

* G-138: ``fingerprint.json`` records, per Linux CPU, its kind / cluster / favored flag and the fused 0x09 word of this boot's last read, plus the CPU's
  family / model / brand (``/proc/cpuinfo``; none under the simulator): what the Windows restore needs to find the same cores again. A read that did not
  cover the 0x09 words writes the file with ``null`` words, which the Windows restore refuses (the reply says ``fingerprint: incomplete``).

* G-141: the op refuses (``STALE_READ``, "read again first") when the audit log shows a mailbox write (``0x11`` ...) or a plain MSR write after the read the words come
  from began (:func:`arltuner.helper.ops_tune.stale_since_read`); an apply that completed in this process keeps them current. ``{from: "read"}`` records the turbo tables of
  the read too. It runs only while nothing else holds the device (``BUSY`` while a ``tune.apply`` / read / measure runs, and it holds the state machine while it works, so
  none can start in between): the words it records can not change under it.
* G-141 ``profiles.fingerprint``: writes ``fingerprint.json`` alone into the existing accepted directory from this boot's fused ``0x09`` words (the same file ``accept``
  records), for an accepted state made before G-138; ``CURRENT``, the profiles and ``EXPECTED-words.txt`` are not touched. See :func:`op_profiles_fingerprint`.

* G-154 (D-045): the ring (cache, mailbox domain P1 = 2) is one more unit of the accepted state: ``{from: "read"}`` / a saved profile records the ring of this boot's last
  ``tune.read`` -- ``EXPECTED-words.txt`` gets one ``ring  <OC word> <point 1> ... <point n>`` line and the profile a ``[ring]`` section (the OC word as read: compared by the
  restore, never written; every ring point's offset with ``expect_ratio``). A read without a ring (the policy cannot read it, an older helper) records none, and an accepted state
  without a ring line compares and restores nothing about the ring.

* G-228: an accept never destroys the state it replaces (operator incident 2026-10-18: an accidental "Set as accepted" overwrote a known-good state with no copy). Before the swap
  the accepted profile being replaced is saved, as it is, into the profile store as ``accepted-before-<UTC yyyymmdd-HHMMSS>`` (``-2``, ``-3`` ... when that name exists; these do not
  count toward the store's profile limit); when that save fails the accept is refused (``PREVIOUS_NOT_SAVED``, nothing changed). The reply carries ``previous_saved_as`` (a name, or
  ``None`` when there was no accepted state) and ``previous_saved_names`` (a list); the audit record ``previous_saved_names`` only. An accepted state that is still an older chain
  is refused (``ACCEPTED_CHAIN``: run accepted-migrate first). An accepted state that cannot be read (an empty or garbled ``CURRENT``, an untrusted file, a directory without
  ``CURRENT``) cannot be kept: the accept is refused (``ACCEPTED_UNREADABLE``) unless the request carries ``"set_aside_unreadable": true``, which moves the WHOLE directory to
  ``<accepted>.unreadable-<UTC stamp>`` (nothing is deleted, nothing is parsed) and then accepts; the reply says where (``set_aside_as``). ``profiles.diff {a, b}`` (read only)
  lists what differs between two states: the confirm step's per-unit diff.

No mailbox traffic, no MSR. A root file write under the helper's trust rules, like ``profiles.save``.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from arltuner.guard import AuditLog
from arltuner.curves.record import unit_order
from arltuner.guard.policy import fused_top_ratio
from arltuner.profiles import BuildError, Profile, ProfileError, complete_from_words
from arltuner.profiles.accepted import SAFE_PROFILE_NAME, AcceptedError, AcceptedStore
from arltuner.profiles.expected import format_ring_line
from arltuner.profiles.fingerprint import CpuId, FingerprintError, build_fingerprint, cpu_id_from_cpuinfo
from arltuner.profiles.serialize import dump_profile
from arltuner.profiles.store import PREVIOUS_PREFIX, ProfileStoreError
from arltuner.topology import Topology

from . import ops_tune as T
from .ops import OpContext, _only
from .protocol import BAD_REQUEST, BUSY, REFUSED, OpError
from .state import Busy, State
from .trust import TrustError, check_trusted_path

if TYPE_CHECKING:
    from .core import Helper

__all__ = ["diff_profiles", "op_profiles_accept", "op_profiles_diff", "op_profiles_fingerprint", "validate_profiles_accept", "validate_profiles_diff"]

_VOLT_EPS = 0.002  # V: two decoded targets this close are the same word (the decode is raw / 1024)
_MV_EPS = 0.6  # mV: one offset raw step is 0.977 mV


def validate_profiles_accept(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"name", "from", "set_aside_unreadable"})
    if ("name" in args) == ("from" in args):
        raise OpError(BAD_REQUEST, "give exactly one of 'name' (a saved profile) or 'from': 'read' (the state of the last read)")
    out: dict[str, Any]
    if "from" in args:
        if args["from"] != "read":
            raise OpError(BAD_REQUEST, "'from' must be 'read'")
        out = {"from": "read"}
    else:
        name = args["name"]
        if not isinstance(name, str) or not T.NAME_RE.match(name) or not SAFE_PROFILE_NAME.match(name) or ".." in name:
            raise OpError(BAD_REQUEST, "'name' must be a saved profile name: 1..64 characters of a-z 0-9 . _ - (no '..', not starting with '.' or '-')")
        out = {"name": name}
    if "set_aside_unreadable" in args:
        if args["set_aside_unreadable"] is not True:
            raise OpError(BAD_REQUEST, "'set_aside_unreadable' must be true when given")
        out["set_aside_unreadable"] = True
    return out


def _refused(text: str, check: str) -> OpError:
    return OpError(REFUSED, f"{text} Nothing was changed.", {"check": check})


def _held_difference(
    profile: Profile, topology: Topology, last: Mapping[int, Sequence[int]], fused: Mapping[int, int] | None, turbo: Mapping[str, Any] | None, ring: Sequence[int] | None = None
) -> str | None:
    """``None`` when every word the profile writes already reads as the profile resolves it (the plan's ``before`` equals its ``after``), else a short text."""
    before, after = T._before_after(profile, topology, last, fused, turbo, ring)
    for unit, items in after.items():
        for point, now in items.items():
            was = (before.get(unit) or {}).get(point)
            label = f"{unit} {'OC word' if point == '0' else 'point ' + point}"
            if was is None:
                return f"{label} was not read"
            if (now["offset_mv"] is None) != (was["offset_mv"] is None) or (now["offset_mv"] is not None and abs(now["offset_mv"] - was["offset_mv"]) > _MV_EPS):
                return f"{label}: offset {was['offset_mv']} mV now, {now['offset_mv']} mV in the profile"
            if now["ratio"] != was["ratio"]:
                return f"{label}: ratio {was['ratio']}x now, {now['ratio']}x in the profile"
            if (now["v_target_v"] is None) != (was["v_target_v"] is None) or (now["v_target_v"] is not None and abs(now["v_target_v"] - was["v_target_v"]) > _VOLT_EPS):
                return f"{label}: V target {was['v_target_v']} V now, {now['v_target_v']} V in the profile"
    return None


def _profile_from_words(
    topology: Topology,
    name: str,
    last: Mapping[int, Sequence[int]],
    fused: Mapping[int, int] | None,
    tables: Mapping[str, Sequence[int]] | None = None,
    ring: Sequence[int] | None = None,
) -> Profile:
    """G-148: the complete profile of the words of ``last`` (cpu -> words) with the top fused ratio of each unit from ``fused`` (cpu -> live ``0x09`` word) and, when the
    read had both, the turbo tables ``tables`` (``{"e": 8 buckets, "p": 8 buckets}``)."""
    tops = {cpu: fused_top_ratio(word) for cpu, word in (fused or {}).items()}
    try:
        return complete_from_words(topology, name, last, tops, tables, ring)
    except BuildError as exc:
        raise _refused(exc.message, exc.code) from None


def _expected_text(last: Mapping[int, Sequence[int]], header: str, ring: Sequence[int] | None = None) -> str:
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    lines.append("# <linux cpu> <OC word P2=0> <point 1> ... <point n>   (hex, no 0x), one line per core / E-cluster that the last read covered.")
    for cpu in sorted(last):
        lines.append(f"{cpu}  " + " ".join(f"{int(w):X}" for w in last[cpu]))
    if ring:  # G-154 (D-045): the ring (cache) line, same shape; absent = no ring in the accepted state
        lines.append("# ring: the ring (cache, mailbox domain P1=2) as the last read found it: <OC word (BIOS-owned, compared, never written)> <point 1> ... <point n>")
        lines.append(format_ring_line(ring))
    return "\n".join(lines) + "\n"


def _cpu_id(cfg: Any) -> CpuId | None:
    """The CPU's identity from ``cfg.cpuinfo_path`` (``/proc/cpuinfo`` on hardware; None under the simulator and when unreadable)."""
    path = getattr(cfg, "cpuinfo_path", None)
    if path is None:
        return None
    try:
        return cpu_id_from_cpuinfo(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _hold_device(helper: Helper, ctx: OpContext, op: str) -> None:
    """Take the helper's state machine for the duration of ``op`` (G-141): ``BUSY`` while a long op (``tune.apply``, a read, a measurement ...) runs, and none can start
    while this one works -- ``profiles.accept`` / ``profiles.fingerprint`` are quick off-thread ops, so without this a ``tune.apply`` could change the words under them."""
    try:
        helper.machine.begin(ctx.req_id, op, State.PLANNING)
    except Busy as exc:
        r = exc.running
        raise OpError(BUSY, f"the helper is {r.state.value} ({r.op} {r.op_id}); {op} needs the words in force to stand still. Try again when it has finished. Nothing was changed.",
                      {"state": r.state.value, "op_id": r.op_id, "op": r.op}) from None


def _reason(exc: OpError) -> str:
    return exc.message.removesuffix(" Nothing was sent.").rstrip(".")


def _leftovers(directory: Path) -> bool:
    """True when ``directory`` holds something although it names no accepted state (a real directory with files in it, or a plain file): a swap would delete it."""
    try:
        if directory.is_symlink():
            return False
        return directory.is_file() or (directory.is_dir() and any(directory.iterdir()))
    except OSError:
        return True


def _replaced_state(helper: Helper, directory: Path) -> tuple[str, str | None, str]:
    """G-228: what the accept is about to replace: ``("none", None, "")``, ``("one", "<profiles/x.toml>", "")``, ``("chain", None, "")`` (an older chain of several profiles) or
    ``("unreadable", None, "<why>")`` (it exists but cannot be read: it cannot be kept as a profile)."""
    try:
        rels = T._accepted_paths(helper)
    except OpError as exc:
        if not exc.message.startswith(T.NO_ACCEPTED_NOTE):
            return "unreadable", None, _reason(exc)
        if _leftovers(directory):
            return "unreadable", None, f"{directory} has no CURRENT file but is not empty"
        return "none", None, ""
    if len(rels) > 1:
        return "chain", None, ""
    return "one", rels[0], ""


def _save_replaced(helper: Helper, rel: str, stamp: str, new_name: str) -> str:
    """G-228: save the accepted profile about to be replaced into the profile store, text as it is (a comment line first), and return its name. Anything that stops the copy
    refuses the accept (``PREVIOUS_NOT_SAVED``): losing the old state is worse than not accepting."""
    base = PREVIOUS_PREFIX + time.strftime("%Y%m%d-%H%M%S", time.gmtime(helper.cfg.wall_clock()))
    try:
        text = T._read_accepted_text(helper, rel)
        note = f"Previous accepted state ({Path(rel).stem}), saved {stamp} because {new_name} replaced it (profiles.accept, G-228). The profile below is the accepted file as it was."
        return T._profile_store(helper).save_unique(base, profile="# " + note + "\n" + text)["name"]
    except OpError as exc:
        raise _refused(f"the accepted state that this would replace could not be read, so it cannot be kept ({_reason(exc)}).", "PREVIOUS_NOT_SAVED") from None
    except ProfileStoreError as exc:
        raise _refused(f"the accepted state that this would replace could not be saved as a profile ({exc.message.rstrip('.')}); it is kept as it is.", "PREVIOUS_NOT_SAVED") from None


def _aside_target(directory: Path, wall_clock: float) -> Path:
    """``<accepted>.unreadable-<UTC yyyymmdd-HHMMSS>`` next to the accepted directory (``-2``, ``-3`` ... when taken)."""
    base = f"{directory.name}.unreadable-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(wall_clock))
    for n in range(1, 100):
        target = directory.parent / (base if n == 1 else f"{base}-{n}")
        if not os.path.lexists(target):
            return target
    raise _refused(f"{directory.parent} already holds {base} and its numbered variants.", "PREVIOUS_NOT_SAVED")


def _outcome(text: str, previous: str | None, check: str) -> OpError:
    """A refusal after the old state was already copied: say so (the copy stays; the accepted state is as it was)."""
    tail = f" The previous accepted state was saved as {previous}; the accepted state is unchanged." if previous else " Nothing was changed."
    return OpError(REFUSED, text.rstrip(".") + "." + tail, {"check": check})


def op_profiles_accept(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    _hold_device(helper, ctx, "profiles.accept")
    try:
        return _accept(helper, args)
    finally:
        helper.machine.end(ctx.req_id)


def _stale_refusal(helper: Helper) -> OpError | None:
    why = T.stale_since_read(helper)
    if why is None:
        return None
    return _refused(f"the words of the last read may no longer be what the CPU holds: {why}. Read again first (Tune > Read), then accept.", "STALE_READ")


def _accept(helper: Helper, args: dict[str, Any]) -> dict[str, Any]:
    cfg = helper.cfg
    directory = getattr(cfg, "accepted_dir", None)
    if directory is None:
        raise _refused("this helper keeps no accepted state (no accepted-state directory is configured).", "NO_ACCEPTED_DIR")
    directory = Path(directory)
    if cfg.enforce_trust:
        try:
            check_trusted_path(directory.parent, want="dir", must_exist=True, lstat_fn=cfg.lstat_fn)
        except TrustError as exc:
            raise _refused(f"the accepted state's location is untrusted: {exc}.", "NO_ACCEPTED_STATE") from None
    topology = T._topology(helper)
    last = T._last_words(helper)
    if not last:
        raise _refused("no tune.read this boot: the accepted state is what the CPU holds, so read it first (Tune > Read).", "NO_READ")
    stale = _stale_refusal(helper)
    if stale is not None:
        raise stale
    fused = getattr(helper, "fused_words", None) or {}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cfg.wall_clock()))
    if "from" in args:
        name = "read-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(cfg.wall_clock()))
        source = "the state of the last read"
    else:
        name = args["name"]
        try:
            record = T._profile_store(helper).load(name)
        except ProfileStoreError as exc:
            raise OpError(BAD_REQUEST if exc.code == "NOT_FOUND" else REFUSED, exc.message, {"check": "PROFILE_" + exc.code}) from None
        saved = T._saved_profile(record)
        if saved is None:  # a partial (legacy) profile: its rows resolved against the read
            policy = T._load_policy(helper)
            saved = T._rows_to_profile(helper, T.canonical_intent(record["intent"], rows_only=True), policy, topology, name).profile
            source = f"saved profile {name} (partial, legacy: the state it left on the CPU is recorded, not the file)"
        else:
            source = f"saved profile {name}"
        difference = _held_difference(saved, topology, last, fused, T._state(helper).turbo, T._last_ring(helper))
        if difference is not None:
            raise _refused(f"{name!r} is not what the CPU holds now ({difference}). Apply it first (Profiles > Review and apply), then set it as accepted.", "NOT_APPLIED")
    st = T._state(helper)
    with st.lock:
        tables = None if st.tables is None else {k: list(v) for k, v in st.tables.items()}
    ring = T._last_ring(helper)  # G-154: the ring of this boot's last read, or None (not read: no ring is recorded)
    profile = _profile_from_words(topology, name, last, fused, tables, ring)  # D-044 decision 2: the profile and EXPECTED-words.txt are the same words
    replaced = T._accepted_names(helper)
    header = f"Accepted {stamp} from {source}. Written by arltuner (profiles.accept); replace it the same way, never by hand."
    try:
        profile_toml = dump_profile(profile, header)
    except ProfileError as exc:
        raise _refused(f"the profile cannot be written: {exc}.", "NOT_REPRESENTABLE") from None
    words_text = _expected_text(last, header, ring if profile.ring is not None else None)
    fingerprint = build_fingerprint(topology, fused, _cpu_id(cfg))
    try:
        fingerprint.require_restorable()
        fingerprint_note = "complete"
    except FingerprintError as exc:
        fingerprint_note = f"incomplete: {exc}"
    T._trust(helper, cfg.audit_path, want="file", must_exist=False)
    state, rel, why = _replaced_state(helper, directory)
    aside: Path | None = None
    if state == "chain":
        raise _refused(f"the accepted state is an older chain of {len(replaced)} profiles ({', '.join(replaced)}); accepting over it would delete the files that accepted-migrate rebuilds it from. First, {T.CHAIN_HINT}.", "ACCEPTED_CHAIN")
    if state == "unreadable":
        aside = _aside_target(directory, cfg.wall_clock())
        if args.get("set_aside_unreadable") is not True:
            raise _refused(f"the accepted state that this would replace cannot be read ({why}), so it cannot be kept as a profile. You can have the whole directory {directory} moved aside to {aside} "
                           "(nothing is deleted) and accept anyway.", "ACCEPTED_UNREADABLE")
    previous = _save_replaced(helper, rel, stamp, name) if rel is not None else None  # G-228: after every refusal that could still come, before the swap; a failure here refuses the accept
    names = [previous] if previous else []
    try:
        AuditLog(cfg.audit_path).append("result", op="profiles.accept", accepted=name, source=source, replaced=replaced, previous_saved_names=names,
                                        **({"set_aside_as": str(aside), "set_aside_reason": why} if aside is not None else {}), cpus=len(last))
    except (OSError, ValueError) as exc:
        raise _outcome(f"cannot record this in the audit log: {exc}", previous, "AUDIT_UNAVAILABLE") from None
    store = AcceptedStore(directory, euid=cfg.euid_fn())
    if aside is not None:
        try:
            store._check_parent()  # the same trust rule as the swap: never rename inside a directory the helper does not own
            os.rename(directory, aside)
        except (AcceptedError, OSError) as exc:
            raise _refused(f"the unreadable accepted state could not be moved aside ({getattr(exc, 'message', None) or getattr(exc, 'strerror', None) or exc}); it is where it was.", "ACCEPTED_UNREADABLE") from None
    try:
        store.replace(name, profile_toml, words_text, header, fingerprint.to_json(stamp))
    except AcceptedError as exc:
        if aside is not None and not os.path.lexists(directory):
            try:
                os.rename(aside, directory)  # nothing was accepted: the unreadable state goes back where it was
                aside = None
            except OSError:
                pass
        raise _outcome(exc.message, previous, "NO_ACCEPTED_STATE" if exc.code == "UNTRUSTED" else exc.code) from None
    return {"name": name, "source": "accepted", "from": "read" if "from" in args else "saved", "accepted_at": stamp, "replaced": replaced,
            "previous_saved_as": previous, "previous_saved_names": names, "set_aside_as": None if aside is None else str(aside), "cpus": len(last),
            "core_entries": len(profile.entries), "points": len(profile.points), "turbo": profile.turbo is not None, "complete": profile.complete,
            "ring_points": 0 if profile.ring is None else len(profile.ring.points), "fingerprint": fingerprint_note}


def op_profiles_fingerprint(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """G-141: ``profiles.fingerprint`` (no args) -- record ``fingerprint.json`` for the accepted state that exists, from this boot's fused ``0x09`` words, without re-accepting.

    For an accepted state made before G-138 (it has no fingerprint, so the Windows restore refuses it). Writes ONLY ``<accepted>/fingerprint.json`` (atomically); ``CURRENT``,
    the profiles and ``EXPECTED-words.txt`` stay byte for byte as they are. The words are the live ``0x09`` words of this boot's last read (``helper.fused_words``); the
    CPU identity comes from ``/proc/cpuinfo`` as in ``profiles.accept``. Refusals (nothing is changed): ``NO_ACCEPTED_DIR`` / ``NO_ACCEPTED_STATE`` (there is no accepted
    state: use ``profiles.accept``), ``NO_READ`` (this boot's read lacks the ``0x09`` word of a unit: read the whole system first), ``BUSY`` (another long op runs), and the
    store's trust refusals. Result: ``{recorded, cpus, accepted: [profile names], fingerprint: "complete" | "incomplete: <why>"}``; ``incomplete`` (for example no CPU
    identity under the simulator) is written all the same, as ``profiles.accept`` does, and says what the Windows restore will refuse. One audit record (``op profiles.fingerprint``)
    is appended before the file is written. No mailbox traffic, no MSR."""
    _hold_device(helper, ctx, "profiles.fingerprint")
    try:
        return _fingerprint(helper)
    finally:
        helper.machine.end(ctx.req_id)


def _fingerprint(helper: Helper) -> dict[str, Any]:
    cfg = helper.cfg
    directory = getattr(cfg, "accepted_dir", None)
    if directory is None:
        raise _refused("this helper keeps no accepted state (no accepted-state directory is configured).", "NO_ACCEPTED_DIR")
    directory = Path(directory)
    if cfg.enforce_trust:
        try:
            check_trusted_path(directory.parent, want="dir", must_exist=True, lstat_fn=cfg.lstat_fn)
        except TrustError as exc:
            raise _refused(f"the accepted state's location is untrusted: {exc}.", "NO_ACCEPTED_STATE") from None
    try:
        chain = [Path(p).stem for p in T._accepted_paths(helper)]  # the same trusted read as everything else: no CURRENT / no profile = no accepted state
    except OpError as exc:
        raise _refused(f"there is no accepted state to record a fingerprint for ({exc.message.removesuffix(' Nothing was sent.')}); accept a profile first (Profiles > Set as accepted).", "NO_ACCEPTED_STATE") from None
    topology = T._topology(helper)
    T._last_words(helper)  # the lazy restore of this boot's stored read brings the fused words back after a helper restart
    fused = dict(getattr(helper, "fused_words", None) or {})
    missing = [u for u in topology.unit_ids() if topology.unit_cpus(u)[0] not in fused]
    if missing:
        raise _refused(
            f"this boot's read has no fused 0x09 word for {', '.join(missing)}; read the whole system first (Tune > Read), then record the fingerprint.", "NO_READ"
        )
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cfg.wall_clock()))
    fingerprint = build_fingerprint(topology, fused, _cpu_id(cfg))
    try:
        fingerprint.require_restorable()
        note = "complete"
    except FingerprintError as exc:
        note = f"incomplete: {exc}"
    T._trust(helper, cfg.audit_path, want="file", must_exist=False)
    try:
        AuditLog(cfg.audit_path).append("result", op="profiles.fingerprint", accepted=chain, recorded=stamp, cpus=len(fused))
    except (OSError, ValueError) as exc:
        raise _refused(f"cannot record this in the audit log: {exc}.", "AUDIT_UNAVAILABLE") from None
    try:
        AcceptedStore(directory, euid=cfg.euid_fn()).write_fingerprint(fingerprint.to_json(stamp))
    except AcceptedError as exc:
        check = "NO_ACCEPTED_STATE" if exc.code in ("UNTRUSTED", "MISSING") else exc.code
        raise OpError(REFUSED, f"{exc.message}. Nothing was changed.", {"check": check}) from None
    return {"recorded": stamp, "cpus": len(fingerprint.cpus), "accepted": chain, "fingerprint": note}


# --------------------------------------------------------------------------------------------------------------
# profiles.diff (G-228): what differs between two states. Compares the Profile objects themselves, so nothing the profile holds is dropped on the way (the OC word's whole-curve
# offset, the ring, the turbo tables), unlike the Profiles list's contents view (profiles/contents.py) which shows a unit's offsets as one row.
# --------------------------------------------------------------------------------------------------------------
DIFF_ROWS_MAX = 400
_RING_UNIT = "ring"
_OTHER = 3  # unit_order puts P units at 0 and E clusters at 1; the ring and the turbo tables come after


def _diff_ref(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value in ("accepted", "read"):
        return value
    if isinstance(value, str) and T.NAME_RE.match(value):
        return value
    raise OpError(BAD_REQUEST, f"'{key}' must be 'accepted', 'read' or a saved profile name (1..64 characters of a-z 0-9 . _ -)")


def validate_profiles_diff(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"a", "b"})
    if "a" not in args or "b" not in args:
        raise OpError(BAD_REQUEST, "give 'a' and 'b': 'accepted', 'read' or a saved profile name")
    return {"a": _diff_ref(args, "a"), "b": _diff_ref(args, "b")}


def _read_profile(helper: Helper, topology: Topology) -> Profile:
    """The complete profile of this boot's last read, as ``profiles.save`` / ``profiles.accept`` ``{from: "read"}`` record it (same gates, same builder)."""
    last = T._last_words(helper)
    if not last:
        raise OpError(REFUSED, "no tune.read this boot: the state of the CPU is what the last read found, so read it first (Tune > Read).", {"check": "NO_READ"})
    why = T.stale_since_read(helper)
    if why is not None:
        raise OpError(REFUSED, f"the words of the last read may no longer be what the CPU holds: {why}. Read again first (Tune > Read).", {"check": "STALE_READ"})
    st = T._state(helper)
    with st.lock:
        tables = None if st.tables is None else {k: list(v) for k, v in st.tables.items()}
    try:
        return _profile_from_words(topology, "read", last, getattr(helper, "fused_words", None) or {}, tables, T._last_ring(helper))
    except OpError as exc:
        raise OpError(REFUSED, exc.message.removesuffix(" Nothing was changed."), exc.detail) from None


def _diff_side(helper: Helper, topology: Topology, ref: str) -> tuple[Profile, dict[str, Any]]:
    if ref == "read":
        return _read_profile(helper, topology), {"ref": ref, "name": None, "source": "read", "partial": False}
    try:
        acc_name, acc = T._accepted_profile(helper)
    except OpError:
        if ref == "accepted":
            raise
        acc_name, acc = None, None
    if acc is not None and ref in ("accepted", acc_name):
        return acc, {"ref": ref, "name": acc_name, "source": "accepted", "partial": not acc.complete}
    if ref == "accepted":
        raise OpError(REFUSED, T.NO_ACCEPTED_NOTE + ".", {"check": "NO_ACCEPTED_STATE"})
    try:
        record = T._profile_store(helper).load(ref)
    except ProfileStoreError as exc:
        raise OpError(BAD_REQUEST if exc.code == "NOT_FOUND" else REFUSED, exc.message, {"check": "PROFILE_" + exc.code}) from None
    saved = T._saved_profile(record)
    if saved is not None:
        return saved, {"ref": ref, "name": ref, "source": "saved", "partial": False}
    policy = T._load_policy(helper)  # a partial (legacy) profile: its rows resolved against the read; only what it sets is compared
    resolved = T._rows_to_profile(helper, T.canonical_intent(record["intent"], rows_only=True), policy, topology, ref).profile
    return resolved, {"ref": ref, "name": ref, "source": "saved", "partial": True}


def _rank(unit: str) -> tuple[int, int]:
    if unit == _RING_UNIT:
        return (_OTHER, 0)
    if unit.endswith(" turbo"):
        return (_OTHER + 1, 0 if unit[0] == "P" else 1)
    return unit_order(unit)


def _facts(profile: Profile, topology: Topology) -> dict[tuple[str, int], tuple[str, str, Any]]:
    """``{(unit, order): (label, kind, value)}`` for everything the profile holds. ``kind``: ``mv`` (offset), ``v`` (volts), ``x`` (a ratio), ``hex`` (a word). ``None`` = not set."""
    out: dict[tuple[str, int], tuple[str, str, Any]] = {}
    for s in profile.expand(topology):
        unit = topology.unit_of_cpu(s.cpu)
        out[(unit, 0)] = ("OC word: whole-curve offset", "mv", s.offset_mv)
        out[(unit, 1)] = ("OC ratio", "x", s.ratio)
        out[(unit, 2)] = ("OC target voltage", "v", None if s.oc_voltage_mv is None else round(s.oc_voltage_mv / 1000, 4))
    for p in profile.expand_points(topology):
        out[(topology.unit_of_cpu(p.cpu), 10 + p.point)] = (f"Offset at {p.expect_ratio}x" if p.expect_ratio else f"Offset, point {p.point}", "mv", p.offset_mv)
    if profile.ring is not None:
        out[(_RING_UNIT, 0)] = ("OC word (BIOS-owned)", "hex", f"0x{profile.ring.oc_word:08X}")
        for rp in profile.ring.points:
            out[(_RING_UNIT, 10 + rp.point)] = (f"Offset at {rp.expect_ratio}x", "mv", rp.offset_mv)
    if profile.turbo is not None:
        for key in ("p", "e"):
            for i, ratio in enumerate(getattr(profile.turbo, key) or ()):
                out[(f"{key.upper()} turbo", i)] = (f"Bucket {i}", "x", ratio)
    return out


def diff_profiles(pa: Profile, pb: Profile, topology: Topology) -> list[dict[str, Any]]:
    """The settings that differ between two profiles, in display order: ``{unit, label, kind, a, b}`` (``None`` = that profile does not hold it)."""
    fa, fb = _facts(pa, topology), _facts(pb, topology)
    rows: list[dict[str, Any]] = []
    for key in sorted(set(fa) | set(fb), key=lambda k: (_rank(k[0]), k[0], k[1])):
        ta, tb = fa.get(key), fb.get(key)
        va = None if ta is None else ta[2]
        vb = None if tb is None else tb[2]
        if va != vb:
            label, kind, _ = ta or tb  # type: ignore[misc]
            rows.append({"unit": key[0], "label": label, "kind": kind, "a": va, "b": vb})
    return rows


def op_profiles_diff(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``{a, b}`` -> ``{a: side, b: side, rows: [{unit, label, kind, a, b}], units: [{unit, n}], compared: [...], not_compared: [{what, why}], truncated}``. Each side is ``"accepted"``
    (the accepted profile), ``"read"`` (this boot's last read as ``profiles.save`` / ``profiles.accept`` ``{from: "read"}`` record it; ``NO_READ`` / ``STALE_READ`` as there) or a saved
    profile's name. A row is one setting that differs; ``a`` / ``b`` ``None`` = that state does not hold it. Read only: files and the stored read, no hardware."""
    topology = T._topology(helper)
    pa, side_a = _diff_side(helper, topology, args["a"])
    pb, side_b = _diff_side(helper, topology, args["b"])
    rows = diff_profiles(pa, pb, topology)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["unit"]] = counts.get(r["unit"], 0) + 1
    compared = ["each unit's OC word (whole-curve offset, OC ratio, OC target voltage)", "every fused point's offset"]
    not_compared: list[dict[str, str]] = []
    for what, held in (("the ring", pa.ring is not None or pb.ring is not None), ("the turbo tables", pa.turbo is not None or pb.turbo is not None)):
        if held:
            compared.append(what)
        else:
            not_compared.append({"what": what, "why": "neither state records it"})
    if side_a["partial"] or side_b["partial"]:
        not_compared.append({"what": "settings a partial (legacy) profile does not set", "why": "listed with '-' on its side, not as equal"})
    return {"a": side_a, "b": side_b, "rows": rows[:DIFF_ROWS_MAX], "units": [{"unit": u, "n": n} for u, n in counts.items()], "compared": compared, "not_compared": not_compared,
            "truncated": max(0, len(rows) - DIFF_ROWS_MAX)}
