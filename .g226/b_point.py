def _check_vf_point_offset_bounded(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x11 with ``P1`` 0 (core) or 2 (ring, T-028) and ``P2`` = a V/F point 1..15 (T-025): a **per-point offset** write, distinct from the whole-curve offset
    (``P2 = 0``, ``vf_bounded``). ``baseline`` is the ``0x10`` word of the **same CPU and the same P2** read this session (the guard keys its
    baselines by cpu/p1/p2, so another point's or the OC point's word can never stand in for it). Tests, fail closed (D-049):

    1. a bound context (``CheckContext.bound``); the request addresses P1 0 or 2 (the allow entry must list it) and P2 1..15 (P2 0 is the OC point and belongs to ``vf_bounded``'s
       ratio / V limits: refused here);
    2. the word equals the baseline in ratio ``[7:0]``, V target ``[19:8]`` and the mode bit ``[20]`` **exactly** -- only the offset field
       ``[31:21]`` may change (``[MEASURED lnx 2026-10-02]`` E CPU 8: point 6 reads ``0x0000002E`` = 46x, V target 0; OCCT wrote point words as
       ``ratio | offset << 21`` with V target 0, ``docs/knowledge/registers.md``). A baseline with ratio 0 or the static bit set is refused: it
       is not a known fused point, so there is nothing to keep;
    3. the offset fits the 11-bit field (always true of a decoded word) and the entry's optional ``offset_raw_min`` / ``offset_raw_max`` tighteners.

    No offset window and no step limit: the voltages of the fused points are not readable, so a point offset cannot be predicted against the cmd ``0x28`` limit -- the CPU's own
    limit and V/F clamp apply, and a big or positive offset is a Review warning (``controls.planner``). The hardware's V(n) >= V(n-1) rule across points clamps a deeper offset up silently
    ``[MEASURED lnx 2026-10-02]``; it is not checked here (the read-back compares words, not voltages). A caller without a bound context is refused."""
    if ctx is None or not ctx.bound:
        return False, "no check context (the session facts are unknown); the bounds cannot be evaluated"
    if int(req.p1) not in POINT_P1_ALLOWED or not 1 <= int(req.p2) <= POINT_P2_MAX:
        return False, f"P1 {req.p1} / P2 {req.p2}: a point offset write needs P1 0 or 2 and P2 1..{POINT_P2_MAX} (P2 0 is the OC point: vf_bounded)"
    new = VfWord.from_u32(req.data)
    base = VfWord.from_u32(baseline)
    if base.ratio == 0:
        return False, "the point's baseline word has ratio 0: not a fused point, nothing to keep"
    if base.static:
        return False, "the point's baseline word is in static (override) mode; the bounds cannot be evaluated"
    if new.ratio != base.ratio:
        return False, f"ratio {new.ratio} differs from the point's baseline ratio {base.ratio}: only the offset may change"
    if new.v_target_raw != base.v_target_raw:
        return False, f"V target raw {new.v_target_raw:#x} differs from the point's baseline {base.v_target_raw:#x}: only the offset may change"
    if new.static != base.static:
        return False, "the mode bit differs from the point's baseline: only the offset may change"
    low, high = params.get("offset_raw_min", -1024), params.get("offset_raw_max", 1023)
    if not low <= new.offset_raw <= high:
        return False, f"offset_raw {new.offset_raw} outside {low}..{high} (the 11-bit offset field is -1024..1023; entry params {dict(params)})"
    return True, "ok"
