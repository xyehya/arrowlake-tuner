def _check_vf_bounded(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x11 (V/F write), D-049: judged against the word read with cmd 0x10 **and** the facts of this session, by the CPU's own limits only. Order of the tests (absolute bounds
    first, so a refusal names the root cause):

    1. a bound context (``CheckContext.bound``); mode adaptive in baseline and word (static / Power-Gate stay out of scope, D-016); ``ratio != 0`` and ``v_target_raw != 0`` (XTU never
       sends a zero field and the hardware's acceptance of one was never observed, ``[UNVERIFIED]``) -- except (G-120, D-035) a zero V target on a word with no OC point
       (``0 < ratio <= ctx.top_fused_ratio``: P 57, E 46), which is ``ratio | offset << 21`` as the hardware keeps it ``[MEASURED lnx 2026-10-11]``; that word skips the V reference /
       V ordering tests, every other bound applies;
    2. the ratio against the CPU's own limit (cmd ``0x01 [7:0]``, ``ctx.hw_ratio_max``): above it is refused; a **raise** (above the current ratio, ``max(baseline, 0x1C[7:0])``) with the
       limit unread is refused naming the read (``CPU_LIMIT_UNREAD``, fail closed);
    3. the predicted voltage against the CPU's own limit (cmd ``0x28 [11:0] - 1 LSB``, XTU's rule, field 0 = 2.0 V): the V target of the word (else the fused ``0x09`` voltage) plus a positive
       offset must not exceed it; a **raise** of that prediction over the baseline's with the limit unread is refused naming the read (``CPU_LIMIT_UNREAD``);
    4. the offset fits the 11-bit field (always true of a decoded word) and the entry's optional ``offset_raw_min`` / ``offset_raw_max`` tighteners (the allow entry, not a policy value);
    5. ordering (registers.md "Ordering", XTU: raise => voltage first, lower => frequency first): one word may not move the ratio and the V target the same way relative to the last
       word sent this session (else the baseline). Raise the V target first, then the ratio; lower the ratio first, then the V target.

    There is no offset window, no ceiling and no step limit any more: a big change is a Review warning (``controls.planner``), not a refusal. ``[MEASURED win 2026-10-01]``: XTU's word has
    ratio = the non-zero baseline ratio else ``0x1C[7:0]`` and V target = the fused voltage. ``[UNVERIFIED]`` on this system: that the hardware accepts a raise at all; that ``0x1C`` follows a ``0x1D``.
    """
    if ctx is None or not ctx.bound:
        return False, "no check context (the session facts and the CPU's limits are unknown); the bounds cannot be evaluated"
    if int(req.p2) != 0:  # T-025: a V/F point (P2 1..15) is judged by vf_point_offset_bounded only; this check's ratio/V logic is the OC point's
        return False, f"P2 {req.p2}: vf_bounded judges the OC point (P2 0) only; a V/F point offset needs a vf_point_offset_bounded entry"
    new = VfWord.from_u32(req.data)
    base = VfWord.from_u32(baseline)
    if base.static:
        return False, "baseline word is in static (override) mode; the bounds cannot be evaluated"
    if new.static:
        return False, "static (override) mode is not allowed (out of scope, D-016)"
    if new.ratio == 0:
        return False, "ratio 0 (XTU never sends a zero ratio; acceptance by the hardware is unverified)"
    # G-120 (D-035): with no OC point (ratio <= the top fused ratio) the hardware keeps no V target next to an offset ([MEASURED lnx 2026-10-11]: wrote
    # 0xFEC5B439, read 0xFEC00039), so the word is ratio | offset << 21 with target 0. Above the top fused ratio an OC point needs a real target.
    no_oc_point = new.v_target_raw == 0 and ctx.top_fused_ratio is not None and 0 < new.ratio <= ctx.top_fused_ratio
    if new.v_target_raw == 0 and not no_oc_point:
        return False, "v_target_raw 0 (XTU never sends a zero V target; only a word with no OC point, ratio <= the top fused ratio, may carry none)"
    favored_ratio = None if ctx.favored_word is None else ctx.favored_word & 0xFF
    fused_v = None if ctx.fused_word is None else (ctx.fused_word >> 8) & 0xFFF
    ratio_now = base.ratio or favored_ratio or 0
    v_now = base.v_target_raw or fused_v or 0
    if ratio_now == 0:
        return False, "no ratio reference: the baseline ratio is 0 and 0x1C[7:0] is 0 or was not read this session"
    if v_now == 0 and not no_oc_point:
        return False, "no voltage reference: the baseline V target is 0 and the fused 0x09 V target is 0 or was not read this session"
    ratio_noraise = max(base.ratio, favored_ratio or 0)
    if ctx.hw_ratio_max is not None:
        if new.ratio > ctx.hw_ratio_max:
            return False, f"ratio {new.ratio} above the CPU limit {ctx.hw_ratio_max}x ({ctx.ratio_text}; baseline {base.ratio}, 0x1C[7:0] {favored_ratio})"
    elif new.ratio > ratio_noraise:
        return False, f"ratio {new.ratio} raises the ratio (now {ratio_noraise}) but {ctx.ratio_text}"
    # predicted voltage: the V target of the word (else the fused top-point voltage) plus a positive offset; the baseline's own prediction is the "no raise" reference
    v_ref = new.v_target_raw or (fused_v or 0)
    if v_ref == 0 and new.offset_raw > 0:
        return False, "no voltage reference: a positive offset on a word with no OC point needs the fused 0x09 voltage, which was not read this session"
    predicted = v_ref + max(new.offset_raw, 0)
    predicted_now = max(base.v_target_raw, fused_v or 0) + max(base.offset_raw, 0)
    what = f"V target {new.v_target_raw:#x}" + (f" + offset {new.offset_raw}" if new.offset_raw > 0 else "") if new.v_target_raw else f"fused voltage {v_ref:#x} + offset {new.offset_raw}"
    if ctx.hw_v_max_raw is not None:
        if predicted > ctx.hw_v_max_raw:
            return False, f"predicted voltage {predicted:#x} raw ({what}) above the CPU limit {ctx.hw_v_max_raw:#x} ({ctx.v_text}; baseline {base.v_target_raw:#x}, fused {fused_v})"
    elif predicted > predicted_now:
        return False, f"predicted voltage {predicted:#x} raw ({what}) raises the voltage (now {predicted_now:#x}) but {ctx.v_text}"
    low, high = params.get("offset_raw_min", -1024), params.get("offset_raw_max", 1023)
    if not low <= new.offset_raw <= high:
        return False, f"offset_raw {new.offset_raw} outside {low}..{high} (the 11-bit offset field is -1024..1023; entry params {dict(params)})"
    if base.ratio == 0 and ctx.top_fused_ratio is not None and 0 < new.ratio <= ctx.top_fused_ratio:
        # G-122: a cold word (ratio field 0) written at or below the top fused ratio has no OC point: its ratio field is not a ratio change. The reference is then
        # capped at the top fused ratio (E: 0x1C[7:0] reads 48, the E turbo, but the word without an OC point is 46), so 0 -> 46 / 57 is no change of ratio.
        ratio_now = min(ratio_now, ctx.top_fused_ratio)
    ratio_before, v_before = ratio_now, v_now
    if ctx.vf_word_sent is not None:
        sent = VfWord.from_u32(ctx.vf_word_sent)
        ratio_before, v_before = sent.ratio, sent.v_target_raw
    d_ratio, d_v = new.ratio - ratio_before, 0 if no_oc_point else new.v_target_raw - v_before
    if d_ratio > 0 and d_v > 0:
        return False, "the word raises the ratio and the V target together: raise the V target first (one 0x11), the ratio in the next"
    if d_ratio < 0 and d_v < 0:
        return False, "the word lowers the ratio and the V target together: lower the ratio first (one 0x11), the V target in the next"
    return True, "ok"
