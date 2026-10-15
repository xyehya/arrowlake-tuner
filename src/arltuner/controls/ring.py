"""G-154 (D-045): the ring (cache, mailbox domain ``P1 = 2``) as one more unit of the tuning state -- reads, words and walking. Pure apart from the injected ``execute``.

Facts [MEASURED lnx 2026-10-15] (docs/knowledge/platform-arl-s.md section 3, ``captures/lnx/ring-pl2-20261015.txt``): the ring is package-level and answers on any CPU (CPU 0 is
used); ``0x10 P1=2 P2=0`` is the ring OC word (BIOS-owned: ratio 40, V target 0), ``P2 = 1..8`` are the fused points 8,14,20,28,34,37,38,39x and ``P2 = 9`` is the 40x slot the
BIOS ring OC ratio 40 creates; ``P2 = 10`` answers rc ``0x11`` (no such point). An offset on ``P2 = 9`` moves 40x, an offset on ``P2 = 8`` does not.

The ring is **one unit** (id :data:`UNIT`): its words are ``[OC word, point 1, ..., point n]`` like a core's. Unlike a core's OC slot (G-151: derived, never written) every ring point
including the OC-created ``P2 = 9`` is an ordinary offset-only point (``0x11 P1=2 P2=k``, the read word with only ``[31:21]`` replaced, read back by the guard). The ring OC word
itself is recorded and compared, **never written**.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from arltuner.guard import GuardRefused, Refusal, Request
from arltuner.mailbox import Cmd, Domain, VfWord, rc_name
from arltuner.topology import Topology
from arltuner.vfdump import RC_NO_SUCH_POINT, Pacer, Stop

__all__ = [
    "RING_P1",
    "RING_WALK_CAP",
    "UNIT",
    "RingCurve",
    "format_lines",
    "from_json",
    "ring_cpu",
    "to_json",
    "walk",
    "walk_cap",
    "word_json",
]

#: The mailbox domain of the ring (registers.md "Domain P1 wire values").
RING_P1 = int(Domain.RING)
#: The unit id of the ring in profiles, ``EXPECTED-words.txt`` (key ``ring``), reads and plans.
UNIT = "ring"
#: Hard cap of the ring walk: ``P2 <= 10`` (the shipped policy reads ring points 1..10; the table ends at the first rc ``0x11``, P2 = 10 on the reference machine).
RING_WALK_CAP = 10


def ring_cpu(topology: Topology) -> int:
    """The CPU the ring is read and written through (it is package-level): the lowest CPU of the topology (CPU 0 on the reference machine)."""
    return min(c.cpu for c in topology.cores)


def walk_cap(policy: Any) -> int | None:
    """Highest ring ``P2`` the policy lets a read reach (at most :data:`RING_WALK_CAP`), or ``None`` when the policy cannot read the ring at all (no read entry for
    ``0x10 P1=2 P2=0`` or none for ``P2 = 1``): the ring is then left out of a dump, with a note, instead of refusing the dump of the cores."""
    for p2 in (0, 1):
        entry = policy.match(int(Cmd.VOLTAGE_FREQ_READ), RING_P1, p2)
        if entry is None or entry.kind != "read":
            return None
    cap = 1
    for p2 in range(2, RING_WALK_CAP + 1):
        entry = policy.match(int(Cmd.VOLTAGE_FREQ_READ), RING_P1, p2)
        if entry is None or entry.kind != "read":
            break
        cap = p2
    return cap


@dataclass
class RingCurve:
    """What was read of the ring: the OC word and the point words in order, and how the table ended (``"rc 0x11 at P2=n"``, ``"empty word at P2=n"``, ``"cap reached ..."``,
    ``"stopped"``)."""

    cpu: int
    oc: int | None = None
    points: list[int] = field(default_factory=list)
    end: str = "stopped"

    @property
    def words(self) -> list[int]:
        """``[OC word, point 1, ...]`` (empty when the OC word was not read)."""
        return [] if self.oc is None else [self.oc, *self.points]


def walk(
    cpu: int,
    execute: Callable[[Request], Any],
    pacer: Pacer,
    refusal_text: Callable[[GuardRefused], str],
    cap: int = RING_WALK_CAP,
) -> tuple[RingCurve, Stop | None]:
    """Read the ring table, one guarded request at a time: ``P2 = 0``, then ``P2 = 1, 2, ...`` until the first rc ``0x11`` (the expected end), a word without a ratio
    (a simulator that answers every ``P2``; no real point has ratio 0) or the cap. Any other rc, a guard refusal / trip, a transport error or Ctrl-C stops the walk with a
    :class:`Stop`; what was read so far is returned."""
    curve = RingCurve(cpu)
    for p2 in range(cap + 1):
        what = "OC word" if p2 == 0 else f"point {p2}"
        req = Request(cpu, int(Cmd.VOLTAGE_FREQ_READ), RING_P1, p2, 0, f"ring read: V/F {what} of the ring (domain P1=2) via cpu {cpu}")
        try:
            pacer.wait()
            result = execute(req)
            pacer.mark()
        except GuardRefused as exc:
            pacer.mark()
            return curve, Stop(3 if exc.reason is Refusal.TRIPPED else 2, refusal_text(exc))
        except KeyboardInterrupt:
            return curve, Stop(3, "interrupted: the ring read was stopped; requests already sent are in the audit log.")
        except Exception as exc:  # MailboxTimeout / MsrError: the guard has audited it and tripped
            sent = getattr(exc, "write_sent", None)
            extra = "" if sent is None else f" (request written: {sent})"
            return curve, Stop(3, f"hardware error on the ring read cpu {cpu} P2={p2}: {type(exc).__name__}: {exc}{extra}")
        rc = result.response.rc
        if rc == RC_NO_SUCH_POINT and p2 >= 1:
            curve.end = f"rc 0x11 at P2={p2}"
            return curve, None
        if rc != 0:
            return curve, Stop(3, f"ring cpu {cpu} P2={p2}: mailbox completion code {rc} ({rc_name(rc)}); the ring read was stopped")
        if p2 == 0:
            curve.oc = result.response.data
        elif VfWord.from_u32(result.response.data).ratio == 0:
            curve.end = f"empty word at P2={p2}"
            return curve, None
        else:
            curve.points.append(result.response.data)
    curve.end = f"cap reached (P2={cap}), no rc 0x11 seen"
    return curve, None


def word_json(w: int) -> dict[str, Any]:
    """The ``vfdump.to_json`` shape of one word."""
    vf = VfWord.from_u32(w)
    return {
        "word": f"{w:#010x}", "ratio": vf.ratio, "v_target_raw": vf.v_target_raw, "v_target_v": vf.v_target_v, "static": vf.static,
        "offset_raw": vf.offset_raw, "offset_mv": round(vf.offset_mv, 3),
    }


def to_json(curve: RingCurve) -> dict[str, Any]:
    """The ring in the shape of one ``cpus[]`` entry of ``vfdump.to_json`` (``{cpu, oc, points, end}``) plus ``unit``."""
    return {
        "unit": UNIT, "cpu": curve.cpu, "oc": None if curve.oc is None else word_json(curve.oc),
        "points": [{"point": n, **word_json(w)} for n, w in enumerate(curve.points, start=1)], "end": curve.end,
    }


def from_json(doc: Mapping[str, Any] | None) -> list[int] | None:
    """``[OC word, point 1, ...]`` of a ring entry of a read / dump document, or ``None`` when there is none (not read, or no OC word)."""
    if not isinstance(doc, Mapping) or not isinstance(doc.get("oc"), Mapping):
        return None
    try:
        return [int(doc["oc"]["word"], 16)] + [int(p["word"], 16) for p in doc.get("points", [])]
    except (KeyError, TypeError, ValueError):
        return None


def format_lines(curve: RingCurve) -> list[str]:
    """The readable table of the ring (as ``vfdump.format_dump`` shows a unit)."""
    lines = [f"Ring (cache, domain P1=2)   [read CPU {curve.cpu}]", f"  {'point':<6}{'ratio':>6}  offset (mV, raw)"]
    for label, word in [("OC", curve.oc), *((str(n), w) for n, w in enumerate(curve.points, start=1))]:
        if word is None:
            continue
        vf = VfWord.from_u32(word)
        lines.append(f"  {label:<6}{str(vf.ratio) + 'x':>6}  {vf.offset_mv:+.1f} mV ({vf.offset_raw:+d})")
    if curve.oc is not None:
        lines.append(f"  OC word {curve.oc:#010x} (BIOS-owned: recorded and compared, never written)")
    lines.append(f"  table end: {curve.end}")
    lines.append("")
    return lines


def requests_worst(cap: int) -> int:
    """Worst-case number of mailbox reads of one ring walk (``P2 = 0 .. cap``)."""
    return cap + 1


def check_words(words: Sequence[int]) -> None:
    """``ValueError`` unless ``words`` is ``[OC word, point 1, ...]`` of 32-bit words with at least one point."""
    if len(words) < 2 or any(not 0 <= w <= 0xFFFFFFFF for w in words):
        raise ValueError("the ring needs the OC word and at least one point, each a 32-bit word")
