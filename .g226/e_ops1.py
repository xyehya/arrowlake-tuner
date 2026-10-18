def edit(path, pairs):
    s = open(path).read()
    for old, new in pairs:
        assert old in s, (path, old[:100])
        s = s.replace(old, new, 1)
    open(path, "w").write(s)


P = "src/arltuner/helper/ops_tune.py"
s = open(P).read()

# --- limits block: _ratio_limit .. _add_limits -------------------------------------------------------------------
a = s.index("def _ratio_limit(policy: Policy")
b = s.index("# --------------------------------------------------------------------------------------------------------------\n# Words: decode, remember")
new_limits = '''def _limits(policy: Policy, topology: Topology, favored_words: Mapping[int, int] | None) -> dict[str, Any]:
    """The ``limits`` of ``tune.read`` / ``tune.last`` (G-133, kept for the page): D-049 removed every operator value limit, so the *policy* part is empty -- ``ratio_max`` and
    ``v_target_max_v`` are ``null`` with source ``"none"`` (the CPU's own limits are in ``hw_limits`` / ``cpu_limits``) and ``offset_mv`` is the 11-bit field's window (the only offset bound)."""
    out: dict[str, Any] = {"offset_mv": {}, "v_target_max_v": {}, "ratio_max": {}, "source": {"ratio_max": {}, "v_target_max": {}}}
    for kind in ("P", "E"):
        out["offset_mv"][kind] = {"min": ENCODING_OFFSET_FLOOR_MV, "max": ENCODING_OFFSET_CEILING_MV}
        out["v_target_max_v"][kind] = None
        out["ratio_max"][kind] = None
        out["source"]["ratio_max"][kind] = "none"
        out["source"]["v_target_max"][kind] = "none"
    return out


def _add_limits(helper: Helper, result: dict[str, Any], topology: Topology, favored_words: Mapping[int, int] | None) -> None:
    """``result["limits"]`` (see :func:`_limits`); ``None`` plus ``limits_note`` when the policy cannot be loaded (never an error of a read)."""
    try:
        result["limits"] = _limits(_load_policy(helper), topology, favored_words)
        result.pop("limits_note", None)
    except OpError as exc:
        result["limits"], result["limits_note"] = None, f"policy limits not available: {exc.message}"


def _cpu_limit_entry(entry: Mapping[str, Any] | None, read_at: str | None) -> dict[str, Any]:
    """One domain of ``cpu_limits``: ``{max_mv, max_ratio, source}``; a limit that was not read is ``null``."""
    entry = entry or {}
    max_mv, max_ratio = entry.get("v_target_max_mv"), entry.get("ratio_max_oc")
    source = "0x28/0x01 read" + (f" {read_at}" if (max_mv is not None or max_ratio is not None) and read_at else "") if (max_mv is not None or max_ratio is not None) else "not read yet (0x28 / 0x01)"
    return {"max_mv": max_mv, "max_ratio": max_ratio, "source": source}


def cpu_limits_doc(
    helper: Helper,
    result: Mapping[str, Any] | None = None,
    *,
    power: Mapping[str, Any] | None = None,
    caps: Mapping[str, Any] | None = None,
    uncore: Sequence[int] | None = None,
) -> dict[str, Any]:
    """D-049 ``cpu_limits`` (tune.read / tune.last; ``power.read`` / ``runtime.read`` pass ``power`` = ``power.limits_json(...)``, ``caps`` = ``caps.limits_json(...)``, ``uncore`` =
    ``uncore.limits_json(...)``): everything the helper enforces, read at runtime::

        {"P": {"max_mv": 1625|null, "max_ratio": 120|null, "source": "0x28/0x01 read <iso>"}, "E": {...}, "ring": {...},
         "offset_mv": [-1000, 999], "power": {"locked": bool|null, "pl_max_w": number|null, "source": text}, "caps_khz": {"<cpu>": [min, max]}|null, "uncore_khz": [min, max]|null}

    ``null`` = not read yet: the write that needs it is refused with ``CPU_LIMIT_UNREAD`` naming the read. ``max_mv`` is ``0x28 [11:0] - 1 LSB`` in whole mV (XTU's rule; field 0 = 2.0 V),
    ``max_ratio`` ``0x01 [7:0]`` (``registers.md``); the P / E / ring values are the ones of this boot's ``tune.read`` (kept across a helper restart with the stored read; the timestamp of
    ``source`` is that read's), ``offset_mv`` is the 11-bit offset field (``-1024..1023`` raw = XTU's ``-1000..+999`` mV)."""
    st = _state(helper)
    with st.lock:
        limits = {kind: dict(v) for kind, v in (st.hw_limits or {}).items()}
    read_at = (result or {}).get("read_at") if isinstance((result or {}).get("read_at"), str) else None
    out: dict[str, Any] = {kind: _cpu_limit_entry(limits.get(kind), read_at) for kind in ("P", "E", "ring")}
    out["offset_mv"] = [ENCODING_OFFSET_FLOOR_MV, ENCODING_OFFSET_CEILING_MV]
    out["power"] = dict(power) if power is not None else {"locked": None, "pl_max_w": None, "source": "not read here (power.read reads MSR 0x610 / 0x606)"}
    out["caps_khz"] = None if caps is None else dict(caps)
    out["uncore_khz"] = None if uncore is None else list(uncore)
    return out


def _add_cpu_limits(helper: Helper, result: dict[str, Any]) -> None:
    result["cpu_limits"] = cpu_limits_doc(helper, result)


'''
s = s[:a] + new_limits + s[b:]
open(P, "w").write(s)

edit(P, [
    ("from arltuner.guard.policy import ENCODING_OFFSET_CEILING_MV, ENCODING_OFFSET_FLOOR_MV, fused_top_ratio, hw_ratio_max, hw_v_max_mv, ratio_limit, turbo_bytes, v_target_limit, v_target_limit_mv",
     "from arltuner.guard.policy import CPU_LIMIT_UNREAD, ENCODING_OFFSET_CEILING_MV, ENCODING_OFFSET_FLOOR_MV, fused_top_ratio, hw_ratio_max, hw_v_max_mv, ratio_limit, turbo_bytes, v_target_limit"),
    ('''    """G-137 (D-039): the policy bound to the CPU's own limits of this boot's ``tune.read`` (ratio ``0x01``, V target ``0x28``). Every ratio / V target ceiling is
    then the policy value clamped by them (planner, guard and plan-time checks alike); none read = no raise."""''',
     '''    """G-137 (D-039, D-049): the policy bound to the CPU's own limits of this boot's ``tune.read`` (ratio ``0x01``, V target ``0x28``; P and E -- the ring's are for ``cpu_limits`` only).
    They are the only value limits (planner, guard and plan-time checks alike); none read = a raise is refused (``CPU_LIMIT_UNREAD``)."""'''),
])
print("ok1")
