"""G-148 (D-044) ``accepted-migrate``: an accepted state written as a **chain** (``accepted/CURRENT`` with several ``profile =`` lines, applied in order) becomes
one complete profile. Pure functions plus one file operation (:func:`apply_migration`); no hardware, no mailbox.

Source of truth: ``EXPECTED-words.txt`` -- the words the chain was verified to leave on the chip -- **never a read**: after a warm reboot the CPU may hold a
neutralised residual OC point (G-147), which must not become the accepted state. The top fused ratio of each unit (what tells an OC point from a fused point)
comes from ``fingerprint.json`` (the fused ``0x09`` words recorded at accept time); a state accepted before G-138 has none, and then the top fused ratio is the
ratio of the unit's last point in ``EXPECTED-words.txt`` (the point an OC ratio adds sits above it and carries the OC ratio: it is skipped).

Turbo tables: the merge of the chain's ``[turbo]`` tables when it has both; a chain without tables (the usual case) yields a complete profile without
``[turbo]``: the tables are not part of the accepted state and a restore leaves them as they are (the next "Set as accepted" from a read records both).
A chain that sets only one table cannot be complete in that respect: the table is left out and the result says so.

:func:`_merge_profiles` (the old restore's merge of the chain, applied in order) lives here only: it is the reference the migrated profile is compared with.
``apply_migration`` copies the old directory to ``<parent>/accepted.bak-<UTC>/`` and swaps the new one in with :meth:`AcceptedStore.replace` (atomic), keeping
``EXPECTED-words.txt`` and ``fingerprint.json`` byte for byte.
"""
from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from arltuner.mailbox import VfWord
from arltuner.topology import Topology

from .accepted import AcceptedError, AcceptedStore
from .complete import BuildError, complete_from_words
from .expected import parse_expected as _parse_expected_with_ring
from .fingerprint import FINGERPRINT_FILE, FingerprintError, parse_fingerprint
from .schema import CoreSetting, PointSetting, Profile, ProfileError, TurboTables, load_profile
from .serialize import dump_profile

__all__ = ["MigrateError", "Migration", "apply_migration", "current_profile_lines", "parse_expected", "parse_expected_with_ring", "plan_migration", "summary_lines"]

_PROFILE_LINE = re.compile(r"\s*profile\s*=\s*\"?([^\"#\s]+)\"?\s*(#.*)?")
_PATH_RE = re.compile(r"profiles/[A-Za-z0-9._-]{1,80}\.toml\Z")
_MAX_BYTES = 256 * 1024


class MigrateError(Exception):
    """The accepted directory cannot be migrated; the message says why. Nothing was changed."""


def current_profile_lines(text: str) -> list[str]:
    """The ``profile =`` paths of a ``CURRENT`` file, in order (comments and a legacy ``policy =`` line are ignored)."""
    out: list[str] = []
    for line in text.splitlines():
        m = _PROFILE_LINE.fullmatch(line)
        if m:
            out.append(m.group(1))
    return out


def parse_expected_with_ring(text: str) -> tuple[dict[int, list[int]], list[int] | None]:
    """``EXPECTED-words.txt``: ``<cpu> <OC word> <point 1> ... <point n>`` (hex, no 0x) per line, plus (G-154) the optional ``ring`` line; ``#`` lines and blanks ignored."""
    try:
        return _parse_expected_with_ring(text)
    except ValueError as exc:
        raise MigrateError(f"EXPECTED-words.txt {exc}") from None


def parse_expected(text: str) -> dict[int, list[int]]:
    """The core lines of ``EXPECTED-words.txt`` (a ``ring`` line, G-154, is accepted and left out; see :func:`parse_expected_with_ring`)."""
    return parse_expected_with_ring(text)[0]


def _merge_profiles(name: str, chain: Sequence[Profile]) -> Profile:
    """The final state of applying ``chain`` in order (the pre-G-148 restore): a later core entry overrides the fields it sets, a later point entry replaces the
    earlier one, a later turbo table replaces the earlier one."""
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


def _top_from_words(words: Sequence[int]) -> int | None:
    """The top fused ratio of a unit from its EXPECTED words alone (no fingerprint): the ratio of its last point, unless that point is the one an OC point added (its
    ratio equals the OC word's ratio, which carries a V target): then the point before it."""
    ratios = [VfWord.from_u32(w).ratio for w in words[1:] if VfWord.from_u32(w).ratio]
    if not ratios:
        return None
    oc = VfWord.from_u32(words[0])
    if len(ratios) >= 2 and oc.v_target_raw and ratios[-1] == oc.ratio and ratios[-1] > ratios[-2]:
        return ratios[-2]
    return ratios[-1]


def _read(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise MigrateError(f"{path} does not exist") from None
    except OSError as exc:
        raise MigrateError(f"cannot open {path}: {exc.strerror or exc} (a symlink is never followed)") from None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise MigrateError(f"{path} is larger than {_MAX_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise MigrateError(f"{path} is not UTF-8 text") from None


@dataclass(frozen=True)
class Migration:
    """What :func:`apply_migration` writes, and how it compares with the chain it replaces."""

    name: str
    chain: tuple[str, ...]  # the profile names of the old CURRENT, in order
    profile: Profile  # the complete profile
    profile_toml: str
    expected_text: str  # EXPECTED-words.txt, unchanged
    fingerprint_text: str | None  # fingerprint.json, unchanged (None = the old state has none)
    header: str  # the comment lines of the new CURRENT and of the profile
    diff: tuple[str, ...]  # where the migrated profile differs from the chain's merge (a value the chain set)
    added: int  # values the chain did not set that the complete profile states (offsets of 0, untouched units, top ratios)
    notes: tuple[str, ...]


def plan_migration(directory: Path | str, topology: Topology, stamp: str) -> Migration:
    """Read ``directory`` (``CURRENT``, its profiles, ``EXPECTED-words.txt``, ``fingerprint.json`` if any) and build the single complete profile. ``stamp`` is a UTC time
    ``YYYYmmddTHHMMSSZ`` (names the profile and the backup). ``MigrateError`` if there is nothing to migrate (``CURRENT`` names one profile) or the files do not fit."""
    directory = Path(directory)
    chain_paths = current_profile_lines(_read(directory / "CURRENT"))
    if not chain_paths:
        raise MigrateError(f"{directory / 'CURRENT'} names no profile: there is no accepted state to migrate")
    if len(chain_paths) == 1:
        raise MigrateError(f"{directory / 'CURRENT'} already names one profile ({chain_paths[0]}): nothing to migrate")
    chain: list[tuple[str, Profile]] = []
    for rel in chain_paths:
        if not _PATH_RE.fullmatch(rel) or ".." in rel:
            raise MigrateError(f"CURRENT names an unacceptable profile path {rel!r}")
        try:
            chain.append((Path(rel).stem, load_profile(_read(directory / rel))))
        except ProfileError as exc:
            raise MigrateError(f"{rel} is not a valid profile: {exc}") from None
    expected_text = _read(directory / "EXPECTED-words.txt")
    expected, expected_ring = parse_expected_with_ring(expected_text)
    notes: list[str] = []
    fingerprint_text: str | None = None
    tops: dict[int, int | None] = {}
    if (directory / FINGERPRINT_FILE).exists() or (directory / FINGERPRINT_FILE).is_symlink():
        fingerprint_text = _read(directory / FINGERPRINT_FILE)
        try:
            fp = parse_fingerprint(fingerprint_text)
        except FingerprintError as exc:
            raise MigrateError(f"{FINGERPRINT_FILE} is unusable: {exc}") from None
        tops = {c.cpu: (c.word & 0xFF) or None for c in fp.cpus if c.word is not None}
        notes.append(f"top fused ratios from {FINGERPRINT_FILE} (the fused 0x09 words recorded at accept time)")
    for cpu, words in expected.items():
        if tops.get(cpu) is None:
            tops[cpu] = _top_from_words(words)
            if fingerprint_text is None:
                continue
            notes.append(f"cpu {cpu}: {FINGERPRINT_FILE} has no fused word; top fused ratio {tops[cpu]} taken from its last point in EXPECTED-words.txt")
    if fingerprint_text is None:
        notes.append(f"no {FINGERPRINT_FILE}: every unit's top fused ratio is the ratio of its last point in EXPECTED-words.txt")
    merged = _merge_profiles("chain", [p for _, p in chain])
    tables: dict[str, tuple[int, ...]] | None = None
    if merged.turbo is not None and merged.turbo.e is not None and merged.turbo.p is not None:
        tables = {"e": merged.turbo.e, "p": merged.turbo.p}
        notes.append("turbo tables: both from the chain (the last profile that sets each)")
    elif merged.turbo is not None:
        only = "e (0x650)" if merged.turbo.e is not None else "p (0x1AD)"
        notes.append(f"turbo tables: the chain sets only the {only} table; a complete profile carries both or neither, so it is LEFT OUT (record both with Set as accepted from a read)")
    else:
        notes.append("turbo tables: the chain sets none, so they are not part of the accepted state (a restore leaves them as they are; Set as accepted from a read records both)")
    name = "migrated-" + stamp.replace("T", "-").rstrip("Z")[:15].lower()
    names = tuple(n for n, _ in chain)
    header = (
        f"Migrated {stamp} by arltuner accepted-migrate (G-148, D-044) from the chain {' -> '.join(names)}: one complete profile built from EXPECTED-words.txt "
        f"(never from a read). Old directory: accepted.bak-{stamp}. Replace it with Profiles > Set as accepted, never by hand."
    )
    try:
        profile = complete_from_words(topology, name, expected, tops, tables, expected_ring)
    except BuildError as exc:
        raise MigrateError(f"EXPECTED-words.txt does not describe a complete state of this CPU: {exc.message}") from None
    except ProfileError as exc:
        raise MigrateError(f"the migrated profile is invalid: {exc}") from None
    try:
        profile.check_complete(topology)
    except ProfileError as exc:
        raise MigrateError(str(exc)) from None
    diff, added = _compare(merged, profile, topology)
    return Migration(name, names, profile, dump_profile(profile, header), expected_text, fingerprint_text, header, tuple(diff), added, tuple(notes))


def _compare(merged: Profile, complete: Profile, topology: Topology) -> tuple[list[str], int]:
    """Values the chain set that the complete profile states differently (``diff``), and how many values the complete profile adds."""

    def unit_of(kind: str, index: int) -> str:
        return topology.unit_of_cpu(topology.resolve(kind, index).cpu)

    cores = {unit_of(e.kind, e.index): e for e in complete.entries}
    pts = {(unit_of(p.kind, p.index), p.point): p for p in complete.points}
    diff: list[str] = []
    seen_core: set[tuple[str, str]] = set()
    seen_pt: set[tuple[str, int]] = set()
    for e in merged.entries:
        unit = unit_of(e.kind, e.index)
        mine = cores.get(unit)
        for key in ("ratio", "oc_voltage_mv", "offset_mv"):
            want = getattr(e, key)
            if want is None or (unit, key) in seen_core:
                continue
            seen_core.add((unit, key))
            have = None if mine is None else getattr(mine, key)
            if have != want:
                diff.append(f"{unit} {key}: chain {want}, EXPECTED-words {have}")
    for p in merged.points:
        key = (unit_of(p.kind, p.index), p.point)
        if key in seen_pt:
            continue
        seen_pt.add(key)
        mine = pts.get(key)
        if mine is None:
            diff.append(f"{key[0]} point {p.point}: chain {p.offset_mv} mV, not a fused point in EXPECTED-words")
        elif mine.offset_mv != p.offset_mv or (p.expect_ratio is not None and mine.expect_ratio != p.expect_ratio):
            diff.append(f"{key[0]} point {p.point}: chain {p.offset_mv} mV at {p.expect_ratio}x, EXPECTED-words {mine.offset_mv} mV at {mine.expect_ratio}x")
    if merged.turbo is not None and complete.turbo is not None:
        for key in ("e", "p"):
            if getattr(merged.turbo, key) is not None and getattr(merged.turbo, key) != getattr(complete.turbo, key):
                diff.append(f"turbo {key}: chain {list(getattr(merged.turbo, key))}, migrated {list(getattr(complete.turbo, key))}")
    stated = sum(1 for e in complete.entries for k in ("ratio", "oc_voltage_mv", "offset_mv") if getattr(e, k) is not None) + len(complete.points)
    return diff, max(stated - len(seen_core) - len(seen_pt), 0)


def apply_migration(directory: Path | str, migration: Migration, *, euid: int, stamp: str) -> Path:
    """Copy ``directory`` to ``<parent>/accepted.bak-<stamp>/`` (symlinks copied as links, never followed), then make the migrated profile the whole accepted
    state (:meth:`AcceptedStore.replace`: ``CURRENT`` with one line, the profile, ``EXPECTED-words.txt`` and ``fingerprint.json`` unchanged). Returns the backup path.
    ``MigrateError`` (nothing changed) when the backup exists already or the store refuses."""
    directory = Path(directory)
    backup = directory.parent / f"{directory.name}.bak-{stamp}"
    if os.path.lexists(backup):
        raise MigrateError(f"{backup} exists already; nothing was changed")
    store = AcceptedStore(directory, euid=euid)
    try:
        store._check_parent()  # the same trust rule as the swap, before the backup is written
    except AcceptedError as exc:
        raise MigrateError(f"{exc.message}; nothing was changed") from None
    try:
        shutil.copytree(directory, backup, symlinks=True)
    except OSError as exc:
        raise MigrateError(f"cannot back up {directory} to {backup}: {exc}; nothing was changed") from None
    try:
        store.replace(migration.name, migration.profile_toml, migration.expected_text, migration.header, migration.fingerprint_text)
    except AcceptedError as exc:
        raise MigrateError(f"{exc.message}; the accepted state is unchanged (backup left at {backup})") from None
    return backup


def summary_lines(migration: Migration, merged_name: str = "chain") -> list[str]:
    """Human text of a migration: what it replaces, the notes, the differences from the chain's merge."""
    lines = [f"accepted chain: {' -> '.join(migration.chain)}", f"becomes one complete profile: {migration.name} "
             f"({len(migration.profile.entries)} units, {len(migration.profile.points)} fused points, turbo tables: {'both' if migration.profile.turbo else 'not part of it'})"]
    lines += [f"note: {n}" for n in migration.notes]
    if migration.diff:
        lines.append(f"differences from the {merged_name}'s merge ({len(migration.diff)}; EXPECTED-words.txt wins, it is what the chain was verified to leave on the chip):")
        lines += [f"  {d}" for d in migration.diff]
    else:
        lines.append(f"every value the chain set is the same in the migrated profile; it adds {migration.added} values the chain left implicit (0 offsets, untouched units)")
    return lines
