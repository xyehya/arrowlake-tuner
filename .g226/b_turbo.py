def _check_turbo_ratio_bounded(
    req: MsrRequestLike, baseline: int, params: Mapping[str, Any], ctx: MsrCheckContext | None = None
) -> tuple[bool, str]:
    """Plain write of a turbo ratio table (``0x650`` E / ``0x1AD`` P; T-023, D-017), judged against the word read with ``rdmsr`` earlier in the
    session (``baseline``, required) by the CPU's own limit (D-049):

    1. the MSR is one of the two tables (the kind comes from the MSR) and ``value`` is a u64;
    2. every byte non-zero (a zero bucket is outside the field range ``TURBO_RATIO_RANGE``, never observed; XTU never sends one);
    3. every byte ``<=`` the CPU's ratio limit of that kind (cmd ``0x01 [7:0]``) when it is known; a byte above its baseline byte (a raise) **needs** that limit -- unread =
       ``CPU_LIMIT_UNREAD``, fail closed. A lowering never needs it.

    No step limit, no base-ratio floor, no monotonic rule: those are Review warnings (``controls.planner``: ``TURBO_NOT_MONOTONIC``, ``TURBO_RAISE``, below-base), not the CPU's.
    ``[UNVERIFIED]`` on this system: that the hardware accepts a raised table, and the scope of the write (package vs core, hence the
    guard's read-back on a second CPU). A caller without a context is refused.
    """
    if ctx is None or ctx.kind is None:
        return False, f"msr {getattr(req, 'msr', None)!r} is not a turbo ratio table (0x650 E / 0x1AD P) or no check context was given"
    value = req.value
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        return False, f"value {value!r} is not a u64"
    new, old = turbo_bytes(value), turbo_bytes(baseline)
    for k, byte in enumerate(new):
        if byte == 0:
            return False, f"bucket {k} is 0 (a zero ratio bucket was never observed; XTU never sends one)"
    for k, (byte, was) in enumerate(zip(new, old)):
        if ctx.hw_ratio_max is not None:
            if byte > ctx.hw_ratio_max:
                return False, f"bucket {k}: ratio {byte} above the CPU limit {ctx.hw_ratio_max}x ({ctx.ratio_text})"
        elif byte > was:
            return False, f"bucket {k}: ratio {byte} raises the bucket from {was} but {ctx.ratio_text}"
    return True, "ok"
