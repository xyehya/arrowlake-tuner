@dataclass(frozen=True)
class CheckContext:
    """Facts about ``req.cpu`` that the guard (or the planner, mirroring it) has this session, plus the CPU's own limits for its core kind (D-049). ``None`` = not known.
    ``bound`` is the explicit "there is a context" flag (it replaces the old offset-floor sentinel): only :func:`build_context` sets it, and both ``0x11`` checks refuse a context
    that is not bound."""

    bound: bool = False
    favored_word: int | None = None  # 0x1C P1=0 P2=0 read this session: [7:0] favored ratio, [15:8] fused
    fused_word: int | None = None  # 0x09 P1=0x11 P2=0 read this session: the fused V/F point ([7:0] ratio, [19:8] voltage)
    vf_word_sent: int | None = None  # the last 0x11 word (P1=0 P2=0) executed with rc 0 for this CPU in this session
    max_voltage_word: int | None = None  # 0x28 P1=0 P2=0 read this session: [11:0] max voltage S2.10 (XTU uses raw - 1)
    hw_ratio_max: int | None = None  # the CPU's own ratio limit of this kind (cmd 0x01 [7:0]); None = unread (or ratio overclocking not supported: see ``ratio_text``)
    hw_v_max_raw: int | None = None  # the CPU's own voltage limit of this kind (cmd 0x28 [11:0] - 1 LSB, raw S2.10); None = unread
    top_fused_ratio: int | None = None  # G-120: the highest fused V/F point ratio of this CPU's kind; a P2 = 0 ratio at or below it has no OC point. None = not known
    ratio_text: str = field(default="", compare=False)  # where ``hw_ratio_max`` comes from, or why it is unknown (names the read), for refusal messages
    v_text: str = field(default="", compare=False)  # the same for ``hw_v_max_raw``


def fused_top_ratio(fused_word: int | None) -> int | None:
    """The highest fused V/F point ratio of a core, live: ``[7:0]`` of its ``0x09 P1=0x11 P2=0`` word (registers.md), or ``None`` when that word was not read
    (or reads 0). An OC ratio (the ``P2 = 0`` word) at or below it creates no OC point."""
    if not fused_word:
        return None
    return (fused_word & 0xFF) or None


def hw_ratio_max(oc_cap_words: Iterable[int | None]) -> int | None:
    """The CPU's own ratio limit of a core kind, live: ``[7:0]`` of the ``0x01 P1=0 P2=0`` words of that kind read in this session (registers.md 0x01: fused max
    OC ratio; ``[8]`` = ratio overclocking ok). The lowest if several differ (fail closed). ``None`` when no word was read, a word reads 0, or ``[8]`` is clear: the limit
    is unknown, so nothing may be raised. ``[UNVERIFIED]`` that an E CPU answers ``P1=0`` for the E domain (the decompile has no separate E domain, ``[DECOMPILED
    win-xtu-decompile-controls.md]``); ``[MEASURED win 2026-10-01]`` P reads ``0x778`` = 120x."""
    ratios = [w & 0xFF for w in oc_cap_words if w is not None and w & 0x100 and w & 0xFF]
    return min(ratios) if ratios else None


def hw_v_max_raw(max_voltage_word: int) -> int:
    """The highest V target (raw S2.10) the hardware allows from a ``0x28`` word: XTU uses ``[11:0] - 1`` and a field of 0 means 2.0 V = raw 2048 (registers.md "Mailbox commands" 0x28,
    ``[DECOMPILED]``; the values of this system, P ``0x681`` / E ``0x56D``, are ``[MEASURED win 2026-10-01]``). A word that WAS read as 0 is a 2.0 V limit, not "unread"."""
    raw = max_voltage_word & 0xFFF
    return 2048 if raw == 0 else raw - 1


def hw_v_max_mv(max_voltage_word: int | None) -> int | None:
    """The CPU's own V limit in whole mV, rounded down (``hw_v_max_raw``; field 0 -> 2000 mV, ``0x681`` -> 1625). ``None`` only when the word was not read."""
    return None if max_voltage_word is None else hw_v_max_raw(max_voltage_word) * 1000 // 1024


def _mv_to_raw_floor(mv: int) -> int:
    """A whole-mV V limit as raw S2.10, rounded down (never above the limit it stands for)."""
    return mv * 1024 // 1000


def ratio_limit(policy: Policy, kind: str, oc_cap_words: Iterable[int | None] = ()) -> tuple[int | None, str]:
    """``(the CPU's ratio limit of a core kind or None, source text)`` (D-049): the lowest of the ``0x01`` words of this session and the live limit bound to the policy (``Policy.with_hw_limits``).
    ``None`` = unknown -- unread (the text starts with ``CPU_LIMIT_UNREAD`` and names the read) or read but the CPU says ratio overclocking is not supported (``[8]`` clear / max 0)."""
    words = list(oc_cap_words)
    hw_values = [v for v in (hw_ratio_max(words), policy.hw_ratio_max[kind]) if v is not None]
    if hw_values:
        hw = min(hw_values)
        return hw, f"CPU limit {hw}x (cmd 0x01 [7:0] fused max OC ratio of a {kind} CPU, registers.md)"
    if any(w is not None for w in words):
        return None, f"the CPU reports ratio overclocking as not supported (cmd 0x01 of a {kind} CPU: bit 8 clear or max ratio 0)"
    return None, f"{CPU_LIMIT_UNREAD}: the CPU's own ratio limit (cmd 0x01 [7:0] of a {kind} CPU) was not read this session"


def v_target_limit(policy: Policy, kind: str, max_voltage_word: int | None) -> tuple[int | None, str]:
    """``(the CPU's V limit of a core kind as raw S2.10 or None, source text)`` (D-049): the lowest of the ``0x28`` word of this session (``hw_v_max_raw``, XTU's raw - 1 rule) and
    the live limit bound to the policy. ``None`` = unread (the text starts with ``CPU_LIMIT_UNREAD`` and names the read)."""
    candidates: list[int] = []
    if max_voltage_word is not None:
        candidates.append(hw_v_max_raw(max_voltage_word))
    bound = policy.hw_v_target_max_mv[kind]
    if bound is not None:
        candidates.append(_mv_to_raw_floor(bound))
    if candidates:
        raw = min(candidates)
        return raw, f"CPU limit {raw * 1000 // 1024} mV (cmd 0x28 [11:0] - 1 LSB of a {kind} CPU, XTU's rule, registers.md)"
    return None, f"{CPU_LIMIT_UNREAD}: the CPU's own voltage limit (cmd 0x28 [11:0] of a {kind} CPU) was not read this session"


def v_target_limit_mv(policy: Policy, kind: str, max_voltage_word: int | None) -> tuple[int | None, str]:
    """``v_target_limit`` in whole mV, rounded down (``None`` = unread)."""
    raw, text = v_target_limit(policy, kind, max_voltage_word)
    return (None if raw is None else raw * 1000 // 1024), text


def build_context(
    policy: Policy,
    cpu: int,
    *,
    favored_word: int | None,
    fused_word: int | None,
    vf_word_sent: int | None,
    max_voltage_word: int | None = None,
    oc_cap_words_of_kind: Iterable[int | None] = (),
) -> CheckContext:
    """The bound context for a request on ``cpu``: the session facts plus the CPU's own limits for its core kind (``oc_cap_words_of_kind``: the ``0x01`` words of the kind read in this
    session; the ``0x28`` word and the limits bound to the policy as well)."""
    kind = policy.core_kind(cpu)
    ratio_cap, ratio_text = ratio_limit(policy, kind, oc_cap_words_of_kind)
    v_max_raw, v_text = v_target_limit(policy, kind, max_voltage_word)
    return CheckContext(
        bound=True,
        top_fused_ratio=fused_top_ratio(fused_word),
        favored_word=favored_word,
        fused_word=fused_word,
        vf_word_sent=vf_word_sent,
        max_voltage_word=max_voltage_word,
        hw_ratio_max=ratio_cap,
        hw_v_max_raw=v_max_raw,
        ratio_text=ratio_text,
        v_text=v_text,
    )


Check = Callable[..., tuple[bool, str]]


@dataclass(frozen=True)
class MsrCheckContext:
    """What ``turbo_ratio_bounded`` needs besides the request and the baseline: the table's core kind (from the MSR, not the CPU) and the CPU's own ratio limit for that kind
    (``0x01 [7:0]``; ``None`` = unread, so no bucket may be raised)."""

    kind: str | None = None
    hw_ratio_max: int | None = None
    ratio_text: str = field(default="", compare=False)  # where ``hw_ratio_max`` comes from, or why it is unknown (names the read), for the refusal message


def build_msr_context(policy: Policy, msr: int, *, oc_cap_words: Mapping[int, int | None] | None = None) -> MsrCheckContext:
    """The context for a plain write of ``msr``: kind from the MSR (unknown MSR -> ``kind None``, which the check refuses) and the CPU's ratio limit of that kind from the ``0x01``
    words of this session (cpu -> word) and the limit bound to the policy."""
    kind = MSR_TURBO_KIND.get(msr)
    ratio_cap, ratio_text = None, ""
    if kind is not None:
        ratio_cap, ratio_text = ratio_limit(policy, kind, [w for c, w in (oc_cap_words or {}).items() if policy.core_kind(c) == kind])
    return MsrCheckContext(kind=kind, hw_ratio_max=ratio_cap, ratio_text=ratio_text)


