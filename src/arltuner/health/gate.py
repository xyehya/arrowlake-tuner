"""Health delta and the guard-compatible health gate.

``diff`` compares two :class:`~arltuner.health.snapshot.HealthSnapshot` values; ``make_health_gate`` wraps ``take_snapshot`` +
``diff`` into the ``health_gate(phase) -> (ok, detail)`` callable that :class:`arltuner.guard.WriteGuard` calls with
``"before"`` and ``"after"`` around every mailbox transaction (``docs/tasks/T-013-guard.md``).

Rule (``docs/SAFETY.md`` "Mailbox-session discipline"; turbostat+ health bracket in
``docs/research/lnx-turbostat-plus-facts.md``): acceptance is a CLEAN delta. Compare against the snapshot taken *before* this
transaction -- never against zero, because the boot's taint word may already carry bits from earlier tools (OCCT). Taint sticky
bits are never cleared here, and taint letters are not decoded: bit indices only.

The gate fails closed: anything it cannot establish (missing file, "after" without "before", unknown phase) is ``(False, reason)``.
Read-only; no subprocess, no sleep, no network.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .snapshot import HealthError, HealthSnapshot, take_snapshot

__all__ = ["HealthDelta", "diff", "make_health_gate"]

_LIST_LIMIT = 8  # names shown per list in a summary (the guard writes the summary into its audit log)


@dataclass(frozen=True)
class HealthDelta:
    """Result of comparing two snapshots. ``clean`` is the gate verdict; thermal/machinecheck data never influences it.

    ``new_taint_bits`` lists *every* bit that appeared (including any the caller allowed); only a new bit outside
    ``allowed_new_taint_bits`` makes the delta unclean. A cleared taint bit is always unclean.
    """

    clean: bool
    taint_changed: bool
    new_taint_bits: tuple[int, ...]
    cleared_taint_bits: tuple[int, ...]
    pci_lost: tuple[str, ...]
    pci_added: tuple[str, ...]
    modules_added: tuple[str, ...]
    modules_removed: tuple[str, ...]
    summary: str


def _normalise_allowed(allowed: Iterable[int]) -> frozenset[int]:
    try:
        bits = frozenset(allowed)
    except TypeError as exc:
        raise HealthError(f"allowed_new_taint_bits must be an iterable of bit indices: {exc}") from exc
    for bit in bits:
        if isinstance(bit, bool) or not isinstance(bit, int) or bit < 0:
            raise HealthError(f"allowed_new_taint_bits holds {bit!r}; bit indices are non-negative integers")
    return bits


def _names(names: Iterable[str]) -> str:
    ordered = sorted(names)
    shown = ", ".join(ordered[:_LIST_LIMIT])
    return shown + (f", +{len(ordered) - _LIST_LIMIT} more" if len(ordered) > _LIST_LIMIT else "")


def _bits(bits: Iterable[int]) -> str:
    return ",".join(str(b) for b in bits)


def diff(
    before: HealthSnapshot,
    after: HealthSnapshot,
    *,
    allowed_new_taint_bits: frozenset[int] = frozenset(),
) -> HealthDelta:
    """Compare ``after`` with ``before``.

    clean iff: no new taint bit outside ``allowed_new_taint_bits``, no cleared taint bit, no PCI device lost or added, no module
    added or removed. ``allowed_new_taint_bits`` exists because the *first* MSR write of a boot taints the kernel
    (``CPU_OUT_OF_SPEC``); the operator passes the bit learned from a before/after read of ``/proc/sys/kernel/tainted``. Any
    other taint change still fails.
    """
    if not isinstance(before, HealthSnapshot) or not isinstance(after, HealthSnapshot):
        raise HealthError("diff needs two HealthSnapshot values")
    allowed = _normalise_allowed(allowed_new_taint_bits)
    bits_before, bits_after = set(before.taint_bits), set(after.taint_bits)
    new_bits = tuple(sorted(bits_after - bits_before))
    cleared_bits = tuple(sorted(bits_before - bits_after))
    unexpected = tuple(bit for bit in new_bits if bit not in allowed)
    pci_lost = tuple(sorted(before.pci - after.pci))
    pci_added = tuple(sorted(after.pci - before.pci))
    modules_added = tuple(sorted(after.modules - before.modules))
    modules_removed = tuple(sorted(before.modules - after.modules))
    clean = not (unexpected or cleared_bits or pci_lost or pci_added or modules_added or modules_removed)

    problems: list[str] = []
    if unexpected:
        problems.append(f"new taint bit(s) {_bits(unexpected)} (taint {before.taint} -> {after.taint})")
    if cleared_bits:
        problems.append(f"taint bit(s) {_bits(cleared_bits)} cleared (taint {before.taint} -> {after.taint})")
    if pci_lost:
        problems.append(f"PCI lost: {_names(pci_lost)}")
    if pci_added:
        problems.append(f"PCI added: {_names(pci_added)}")
    if modules_added:
        problems.append(f"modules added: {_names(modules_added)}")
    if modules_removed:
        problems.append(f"modules removed: {_names(modules_removed)}")
    if problems:
        summary = "UNCLEAN: " + "; ".join(problems)
    else:
        taint_note = f"taint {after.taint} unchanged"
        if new_bits:
            taint_note = f"taint {before.taint} -> {after.taint}, new bit(s) {_bits(new_bits)} allowed"
        summary = f"CLEAN: {taint_note}; pci={len(after.pci)} modules={len(after.modules)}"
    return HealthDelta(
        clean=clean,
        taint_changed=before.taint != after.taint,
        new_taint_bits=new_bits,
        cleared_taint_bits=cleared_bits,
        pci_lost=pci_lost,
        pci_added=pci_added,
        modules_added=modules_added,
        modules_removed=modules_removed,
        summary=summary,
    )


def make_health_gate(
    root: Path = Path("/"),
    *,
    allowed_new_taint_bits: frozenset[int] = frozenset(),
    clock: Callable[[], float] = time.time,
) -> Callable[[str], tuple[bool, str]]:
    """Build the stateful ``health_gate(phase)`` callable for ``WriteGuard(health_gate=...)``.

    * ``"before"``: take a snapshot, remember it (replacing any earlier one), return ``(True, "snapshot taken: taint=<int>
      pci=<n> modules=<n>")``.
    * ``"after"``: take a snapshot and diff it against the remembered one; return ``(delta.clean, delta.summary)``. The remembered
      snapshot is kept, so a later re-check after a dwell (SAFETY.md: "dwell, then check health") compares with the same
      "before".
    * ``"after"`` with no successful prior ``"before"``, an unknown phase, or any failure to read health -> ``(False, reason)``.

    ``allowed_new_taint_bits`` is validated now (a bad value raises :class:`HealthError` here, not mid-session).
    """
    root = Path(root)
    allowed = _normalise_allowed(allowed_new_taint_bits)
    lock = threading.Lock()  # the guard serialises calls; this only protects the remembered snapshot if someone else calls us
    remembered: list[HealthSnapshot] = []  # zero or one element

    def gate(phase: str) -> tuple[bool, str]:
        if phase not in ("before", "after"):
            return False, f"unknown health-gate phase {phase!r} (expected 'before' or 'after')"
        with lock:
            try:
                snap = take_snapshot(root, clock=clock)
                if phase == "before":
                    remembered[:] = [snap]
                    return True, f"snapshot taken: taint={snap.taint} pci={len(snap.pci)} modules={len(snap.modules)}"
                if not remembered:
                    return False, "health gate 'after' called without a successful 'before' snapshot"
                delta = diff(remembered[0], snap, allowed_new_taint_bits=allowed)
                return delta.clean, delta.summary
            except HealthError as exc:
                if phase == "before":
                    remembered.clear()  # never let an 'after' compare against a stale 'before'
                return False, f"health gate ({phase}) could not read health: {exc}"
            except Exception as exc:  # fail closed: a gate that cannot decide says no
                if phase == "before":
                    remembered.clear()
                return False, f"health gate ({phase}) failed: {type(exc).__name__}: {exc}"

    return gate
