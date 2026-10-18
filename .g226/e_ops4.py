def edit(path, pairs):
    s = open(path).read()
    for old, new in pairs:
        assert old in s, (path, old[:100])
        s = s.replace(old, new, 1)
    open(path, "w").write(s)


P = "src/arltuner/helper/ops_tune.py"
s = open(P).read()

# _ratio_cap + _check_range
a = s.index("def _ratio_cap(helper: Helper")
b = s.index("def _rows_to_profile(")
new = '''def _ratio_cap(helper: Helper, policy: Policy, kind: str) -> tuple[int | None, str]:
    """The CPU's own ratio limit of a kind at plan time and where it comes from (G-137, D-039, D-049): ``0x01 [7:0]`` of the ``tune.read`` of this boot that ``policy`` is bound to.
    ``None`` = not known (the text names the read): the apply reads it itself and its guard judges the word."""
    return ratio_limit(policy, kind)


def _check_range(policy: Policy, unit: str, cpus: Sequence[int], row: Mapping[str, Any], ratio_cap: tuple[int | None, str] | None = None) -> None:
    """``REFUSED POLICY_RANGE`` naming the field, the limit and where the limit comes from (the CPU's own register, or the field encoding), before anything else is computed. D-049: the
    only bounds are the 11-bit offset field, the CPU's voltage limit (``0x28``) and ratio limit (``0x01``) when this boot's ``tune.read`` got them; an unread limit is the apply's to
    read (and its guard refuses a raise without it: ``CPU_LIMIT_UNREAD``)."""
    kind = _kind_of(unit)

    def refuse(text: str) -> OpError:
        return OpError(REFUSED, f"{text}. Nothing was sent.", {"check": "POLICY_RANGE"})

    offset = row["offset"]
    if offset is not None:
        values = [offset["mv"]] if offset["kind"] == "curve" else list(offset["mv"].values())
        for mv in values:
            try:
                VfWord.mv_to_offset_raw(mv)
            except ValueError:
                raise refuse(
                    f"{unit}: offset {mv:+d} mV does not fit the 11-bit offset field (registers.md V/F data word: {ENCODING_OFFSET_FLOOR_MV}..{ENCODING_OFFSET_CEILING_MV} mV)"
                ) from None
    v = row["v_target_v"]
    if v is not None:
        mv = round(v * 1000)
        cap_raw, text = v_target_limit(policy, kind, None)  # the 0x28 limit comes from the policy binding (tune.read); the guard judges the word of the session when applied
        if cap_raw is not None and VfWord.v_to_raw(mv / 1000) > cap_raw:
            raise refuse(f"{unit}: OC-point voltage {mv} mV is above {text}")
    ratio = row["ratio"]
    if ratio is not None and ratio_cap is not None:
        cap, text = ratio_cap
        if cap is not None and ratio > cap:
            raise refuse(f"{unit}: ratio {ratio} is above {text}")


'''
s = s[:a] + new + s[b:]

# _check_ring_range
a = s.index("def _check_ring_range(")
b = s.index("def _ring_setting(")
new = '''def _check_ring_range(policy: Policy, topology: Topology, points: Mapping[str, int]) -> None:
    """``REFUSED POLICY_RANGE`` before anything else is computed (G-212): every ring point must have a policy ``0x11 P1=2`` write entry (an access list), and its offset must fit the
    11-bit field and the entry's optional ``offset_raw_min`` / ``_max`` (what the guard evaluates in ``_check_vf_point_offset_bounded``; D-049: no policy window on top)."""

    def refuse(text: str) -> OpError:
        return OpError(REFUSED, f"{text}. Nothing was sent.", {"check": "POLICY_RANGE"})

    for key, mv in points.items():
        p2 = int(key)
        entry = policy.match(int(Cmd.VOLTAGE_FREQ_WRITE), ringmod.RING_P1, p2)
        if entry is None or entry.kind != "write":
            raise refuse(f"ring point {p2}: the policy has no write entry for cmd 0x11 P1=2 P2={p2} ({policy.where})")
        try:
            raw = VfWord.mv_to_offset_raw(mv)
        except ValueError:
            raise refuse(f"ring point {p2}: offset {mv:+d} mV does not fit the 11-bit offset field ({ENCODING_OFFSET_FLOOR_MV}..{ENCODING_OFFSET_CEILING_MV} mV)") from None
        low, high = entry.params.get("offset_raw_min", -1024), entry.params.get("offset_raw_max", 1023)
        if not low <= raw <= high:
            raise refuse(f"ring point {p2}: offset {mv:+d} mV (raw {raw}) is outside the allow entry {entry.name!r} window {low * 1000 / 1024:+.1f}..{high * 1000 / 1024:+.1f} mV ({policy.where})")


'''
s = s[:a] + new + s[b:]
open(P, "w").write(s)

edit(P, [
    # turbo order: refusal -> warning
    ('''    _check_turbo_order(profile)
    used, budget''', '''    used, budget'''),
    ('''    if rounds > 1:
        warnings.append(f"the change is larger than the policy's per-operation step limits ({worst}): the apply splits it into up to {rounds} guarded rounds in this one run (G-148)")
''', ''),
    ('''    for unit, items in after.items():
        for point, now in items.items():
            was = (before.get(unit) or {}).get(point)
            if now["offset_mv"] is not None and now["offset_mv"] > 0 and (was is None or was["offset_mv"] <= 0):
                warnings.append(Warn("POSITIVE_OFFSET", f"positive offset {now['offset_mv']:+.1f} mV on {unit} {'OC word' if point == '0' else 'point ' + point}"))
''', '''    for unit, items in after.items():
        for point, now in items.items():
            was = (before.get(unit) or {}).get(point)
            if now["offset_mv"] is not None and now["offset_mv"] > 0 and (was is None or was["offset_mv"] <= 0):
                warnings.append(Warn("POSITIVE_OFFSET", f"positive offset {now['offset_mv']:+.1f} mV on {unit} {'OC word' if point == '0' else 'point ' + point}"))
            if was is not None:  # D-049: a large single change / a deep whole-curve offset are warnings of the Review, never refusals
                where = f"{unit} {'OC word' if point == '0' else 'point ' + point}"
                for code, text in change_notes(where, ratio=(was.get("ratio"), now.get("ratio")), offset_mv=(was.get("offset_mv"), now.get("offset_mv")), whole_curve=point == "0"):
                    if code != POSITIVE_OFFSET:
                        warnings.append(Warn(code, text))
    warnings.extend(_turbo_shape_warnings(helper, profile))
'''),
    ('''    if isinstance(exc, (NeedsRamp, NeedsRounds)):
        return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": "STEP_LIMIT"})
    if isinstance(exc, PlanRefused):
        first = exc.refusals[0]
        return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": _REFUSAL_CHECK.get(first.reason, first.reason)})''', '''    if isinstance(exc, (NeedsRamp, NeedsRounds)):
        return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": "STEP_LIMIT"})
    if isinstance(exc, PlanRefused):
        first = exc.refusals[0]
        if CPU_LIMIT_UNREAD in first.detail:  # D-049: the CPU's own limit was not read; the detail names the read (cmd 0x01 / cmd 0x28)
            return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": CPU_LIMIT_UNREAD})
        return OpError(REFUSED, f"{exc}. Nothing was sent.", {"check": _REFUSAL_CHECK.get(first.reason, first.reason)})'''),
    ('''def _check_turbo_order(profile: Profile) -> None:
    """F5: bucket ratios must not RISE with the active-core count. ``REFUSED TURBO_ORDER`` before the planner (which would refuse the same table later as a
    generic policy failure). Only a profile can carry a table (the contract 9 row intent has no table field): accepted / saved profiles and ``restore``."""
    if profile.turbo is None:
        return
    for key, label in (("p", "P (MSR 0x1AD)"), ("e", "E (MSR 0x650)")):
        table = getattr(profile.turbo, key)
        if table is None:
            continue
        bad = check_turbo_order(table)
        if bad is not None:
            raise OpError(
                REFUSED,
                f"the {label} turbo table is not non-increasing: bucket {bad + 1} ({table[bad]}x) is above bucket {bad} ({table[bad - 1]}x); more active cores must never raise the ratio. "
                f"Nothing was sent.",
                {"check": "TURBO_ORDER", "table": key.upper(), "bucket": bad, "ratios": list(table)},
            )
''', '''def _turbo_shape_warnings(helper: Helper, profile: Profile) -> list[str]:
    """D-049: F5 (bucket ratios should not RISE with the active-core count), a bucket below the base ratio and a bucket moved by more than ``LARGE_RATIO_CHANGE`` are *warnings*
    (``TURBO_NOT_MONOTONIC`` / ``TURBO_BELOW_BASE`` / ``LARGE_CHANGE``), no longer the ``TURBO_ORDER`` refusal: the CPU accepts such a table. Only a profile can carry a table (the contract
    9 row intent has no table field): accepted / saved profiles and ``restore``."""
    if profile.turbo is None:
        return []
    st = _state(helper)
    with st.lock:
        tables, turbo = st.tables, st.turbo
    out: list[str] = []
    for key, label in (("p", "P turbo table (MSR 0x1AD)"), ("e", "E turbo table (MSR 0x650)")):
        table = getattr(profile.turbo, key)
        if table is None:
            continue
        old = None if tables is None else tables.get(key)
        out.extend(Warn(code, text) for code, text in turbo_change_notes(label, table, old))
    return out
'''),
    ("from arltuner.controls.planner import POINT_CAVEAT, build_plan, _same_state, _goal_word, current_word, kept_word, oc_cap_reads_of",
     "from arltuner.controls.planner import POINT_CAVEAT, POSITIVE_OFFSET, build_plan, _same_state, _goal_word, change_notes, current_word, kept_word, needs_vmax, oc_cap_reads_of, turbo_change_notes"),
    ('''+ sum(1 for t in targets if t.oc_voltage_mv is not None) + len(pts)''', '''+ sum(1 for t in targets if needs_vmax(t)) + len(pts)'''),
])
print("ok4")
