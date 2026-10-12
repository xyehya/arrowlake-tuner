"""G-129 helper op ``profiles.accept`` (D-036): the **user** makes a profile the accepted state, in the app. Nothing is shipped as one.

``profiles.accept {name}`` takes a saved profile, ``profiles.accept {from: "read"}`` the state this boot's last ``tune.read`` found, and replaces
``/etc/arltuner/accepted/`` (``CURRENT`` + ``profiles/<name>.toml`` + ``EXPECTED-words.txt``, see :mod:`arltuner.profiles.accepted`) as a whole.

Contract (what "accepted" means, so the restore and the comparison stay honest):

* **Accepted = what the CPU holds now.** ``EXPECTED-words.txt`` is the V/F words of this boot's last read (the verified read-backs after an apply), so a
  saved profile must already be applied: when its resolved words differ from the read the op refuses (``NOT_APPLIED``, "apply it first"). A profile that
  was never applied cannot be recorded as the state the chip was verified to hold.
* The accepted profile file is the resolved profile (ratio / OC voltage / whole-curve offset per core, per-point offsets with ``expect_ratio``, turbo
  tables). ``{from: "read"}`` derives it from the words; a word that millivolts cannot represent exactly (the profile format has whole millivolts) is
  refused (``NOT_REPRESENTABLE``) rather than rounded, because the restore would then write a different word than the one recorded.
* Needs a read this boot (``NO_READ``); the policy still judges a saved profile (its range check runs as for a plan); writes nothing to the hardware.
* The op appends one audit record before the swap (and refuses if it cannot): ``event result``, ``op profiles.accept``, the name, what was replaced.

No mailbox traffic, no MSR. A root file write under the helper's trust rules, like ``profiles.save``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from arltuner.guard import AuditLog
from arltuner.guard.policy import fused_top_ratio
from arltuner.mailbox import VfWord
from arltuner.profiles import CoreSetting, PointSetting, Profile, ProfileError
from arltuner.profiles.accepted import SAFE_PROFILE_NAME, AcceptedError, AcceptedStore
from arltuner.profiles.serialize import dump_profile
from arltuner.profiles.store import ProfileStoreError
from arltuner.topology import Topology

from . import ops_tune as T
from .ops import OpContext, _only
from .protocol import BAD_REQUEST, REFUSED, OpError
from .trust import TrustError, check_trusted_path

if TYPE_CHECKING:
    from .core import Helper

__all__ = ["op_profiles_accept", "validate_profiles_accept"]

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


def _held_difference(profile: Profile, topology: Topology, last: Mapping[int, Sequence[int]], fused: Mapping[int, int] | None, turbo: Mapping[str, Any] | None) -> str | None:
    """``None`` when every word the profile writes already reads as the profile resolves it (the plan's ``before`` equals its ``after``), else a short text."""
    before, after = T._before_after(profile, topology, last, fused, turbo)
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


def _whole_mv(raw_mv: float, back: int, what: str) -> int:
    mv = round(raw_mv)
    if VfWord.mv_to_offset_raw(mv) != back:
        raise _refused(f"{what} ({back} raw) is not a whole number of millivolts, so a profile file cannot record it exactly.", "NOT_REPRESENTABLE")
    return mv


def _profile_from_words(topology: Topology, name: str, last: Mapping[int, Sequence[int]], fused: Mapping[int, int] | None) -> Profile:
    """The profile that puts back the words of ``last`` (see the module docstring)."""
    entries: list[CoreSetting] = []
    points: list[PointSetting] = []
    for unit in topology.unit_ids():
        ref = T._unit_reference(last, T.unit_cpus(topology, unit))
        if ref is None:
            raise _refused(f"the last read does not cover {unit}; read the whole system again (Tune > Read).", "NO_READ")
        cpu, words = ref
        core = topology.by_cpu(cpu)
        top = fused_top_ratio((fused or {}).get(cpu))
        oc = VfWord.from_u32(words[0])
        if oc.static:
            raise _refused(f"{unit} is in static (override) voltage mode; only adaptive words are recorded (D-016).", "NOT_REPRESENTABLE")
        values: dict[str, int] = {}
        if oc.v_target_raw:
            if top is None:
                raise _refused(f"{unit} has a V target on its OC word but its top fused ratio was not read, so an OC point cannot be told from the neutral word.", "NO_READ")
            if oc.ratio > top:  # an OC point: ratio and adaptive target are the setting; at or below the top fused ratio the word is neutral
                mv = round(oc.v_target_v * 1000)
                if VfWord.v_to_raw(mv / 1000) != oc.v_target_raw:
                    raise _refused(f"{unit}'s OC V target ({oc.v_target_raw} raw) is not a whole number of millivolts.", "NOT_REPRESENTABLE")
                values["ratio"], values["oc_voltage_mv"] = oc.ratio, mv
        if oc.offset_raw:
            values["offset_mv"] = _whole_mv(oc.offset_mv, oc.offset_raw, f"{unit} OC word offset")
        if values:
            entries.append(CoreSetting(core.kind, core.index, **values))
        for n, word in enumerate(words[1:], start=1):
            w = VfWord.from_u32(word)
            if w.ratio == 0 or (top is not None and w.ratio > top):
                continue  # an empty word, or the OC point (ratio above the top fused one): not a fused point
            points.append(PointSetting(core.kind, core.index, n, _whole_mv(w.offset_mv, w.offset_raw, f"{unit} point {n} offset"), w.ratio))
    try:
        return Profile(name, tuple(entries), None, tuple(points))
    except ProfileError as exc:
        raise _refused(f"the last read holds nothing to record ({exc}).", "NOTHING_TO_ACCEPT") from None


def _expected_text(last: Mapping[int, Sequence[int]], header: str) -> str:
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    lines.append("# <linux cpu> <OC word P2=0> <point 1> ... <point n>   (hex, no 0x), one line per core / E-cluster that the last read covered.")
    for cpu in sorted(last):
        lines.append(f"{cpu}  " + " ".join(f"{int(w):X}" for w in last[cpu]))
    return "\n".join(lines) + "\n"


def op_profiles_accept(helper: Helper, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
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
    fused = getattr(helper, "fused_words", None) or {}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cfg.wall_clock()))
    if "from" in args:
        name = "read-" + stamp.replace("-", "").replace(":", "")
        profile = _profile_from_words(topology, name, last, fused)
        source = "the state of the last read"
    else:
        name = args["name"]
        try:
            record = T._profile_store(helper).load(name)
        except ProfileStoreError as exc:
            raise OpError(BAD_REQUEST if exc.code == "NOT_FOUND" else REFUSED, exc.message, {"check": "PROFILE_" + exc.code}) from None
        policy = T._load_policy(helper)
        profile = T._rows_to_profile(helper, T.canonical_intent(record["intent"], rows_only=True), policy, topology, name).profile
        difference = _held_difference(profile, topology, last, fused, T._state(helper).turbo)
        if difference is not None:
            raise _refused(f"{name!r} is not what the CPU holds now ({difference}). Apply it first (Profiles > Review and apply), then set it as accepted.", "NOT_APPLIED")
        source = f"saved profile {name}"
    try:
        replaced = [n for n, _ in T._accepted_chain(helper)]
    except OpError:
        replaced = []
    header = f"Accepted {stamp} from {source}. Written by arltuner (profiles.accept); replace it the same way, never by hand."
    try:
        profile_toml = dump_profile(profile, header)
    except ProfileError as exc:
        raise _refused(f"the profile cannot be written: {exc}.", "NOT_REPRESENTABLE") from None
    words_text = _expected_text(last, header)
    T._trust(helper, cfg.audit_path, want="file", must_exist=False)
    try:
        AuditLog(cfg.audit_path).append("result", op="profiles.accept", accepted=name, source=source, replaced=replaced, cpus=len(last))
    except (OSError, ValueError) as exc:
        raise _refused(f"cannot record this in the audit log: {exc}.", "AUDIT_UNAVAILABLE") from None
    try:
        AcceptedStore(directory, euid=cfg.euid_fn()).replace(name, profile_toml, words_text, header)
    except AcceptedError as exc:
        raise OpError(REFUSED, f"{exc.message}. Nothing was changed.", {"check": "NO_ACCEPTED_STATE" if exc.code == "UNTRUSTED" else exc.code}) from None
    return {"name": name, "source": "accepted", "from": "read" if "from" in args else "saved", "accepted_at": stamp, "replaced": replaced, "cpus": len(last),
            "core_entries": len(profile.entries), "points": len(profile.points), "turbo": profile.turbo is not None}
