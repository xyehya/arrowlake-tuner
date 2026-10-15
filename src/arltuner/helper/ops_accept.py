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

No mailbox traffic, no MSR. A root file write under the helper's trust rules, like ``profiles.save``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from arltuner.guard import AuditLog
from arltuner.guard.policy import fused_top_ratio
from arltuner.profiles import BuildError, Profile, ProfileError, complete_from_words
from arltuner.profiles.accepted import SAFE_PROFILE_NAME, AcceptedError, AcceptedStore
from arltuner.profiles.expected import format_ring_line
from arltuner.profiles.fingerprint import CpuId, FingerprintError, build_fingerprint, cpu_id_from_cpuinfo
from arltuner.profiles.serialize import dump_profile
from arltuner.profiles.store import ProfileStoreError
from arltuner.topology import Topology

from . import ops_tune as T
from .ops import OpContext, _only
from .protocol import BAD_REQUEST, BUSY, REFUSED, OpError
from .state import Busy, State
from .trust import TrustError, check_trusted_path

if TYPE_CHECKING:
    from .core import Helper

__all__ = ["op_profiles_accept", "op_profiles_fingerprint", "validate_profiles_accept"]

_VOLT_EPS = 0.002  # V: two decoded targets this close are the same word (the decode is raw / 1024)
_MV_EPS = 0.6  # mV: one offset raw step is 0.977 mV


def validate_profiles_accept(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"name", "from"})
    if ("name" in args) == ("from" in args):
        raise OpError(BAD_REQUEST, "give exactly one of 'name' (a saved profile) or 'from': 'read' (the state of the last read)")
    if "from" in args:
        if args["from"] != "read":
            raise OpError(BAD_REQUEST, "'from' must be 'read'")
        return {"from": "read"}
    name = args["name"]
    if not isinstance(name, str) or not T.NAME_RE.match(name) or not SAFE_PROFILE_NAME.match(name) or ".." in name:
        raise OpError(BAD_REQUEST, "'name' must be a saved profile name: 1..64 characters of a-z 0-9 . _ - (no '..', not starting with '.' or '-')")
    return {"name": name}


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
    try:
        AuditLog(cfg.audit_path).append("result", op="profiles.accept", accepted=name, source=source, replaced=replaced, cpus=len(last))
    except (OSError, ValueError) as exc:
        raise _refused(f"cannot record this in the audit log: {exc}.", "AUDIT_UNAVAILABLE") from None
    try:
        AcceptedStore(directory, euid=cfg.euid_fn()).replace(name, profile_toml, words_text, header, fingerprint.to_json(stamp))
    except AcceptedError as exc:
        raise OpError(REFUSED, f"{exc.message}. Nothing was changed.", {"check": "NO_ACCEPTED_STATE" if exc.code == "UNTRUSTED" else exc.code}) from None
    return {"name": name, "source": "accepted", "from": "read" if "from" in args else "saved", "accepted_at": stamp, "replaced": replaced, "cpus": len(last),
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
