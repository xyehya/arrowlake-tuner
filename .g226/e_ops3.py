def edit(path, pairs):
    s = open(path).read()
    for old, new in pairs:
        assert old in s, (path, old[:100])
        s = s.replace(old, new, 1)
    open(path, "w").write(s)


P = "src/arltuner/helper/ops_tune.py"
edit(P, [
    ('''        hw_limits = None if st.hw_limits is None else {k: dict(v) for k, v in st.hw_limits.items()}
''', '''        hw_limits = None if st.hw_limits is None else {k: dict(v) for k, v in st.hw_limits.items() if k != "ring"}  # P and E; the ring's limits are in cpu_limits
'''),
    ('''    result["read_at"] = _read_at(helper.cfg.wall_clock())
    _add_limits(helper, result, topology, extras.get("favored_words"))
    result = _fit(result)''', '''    result["read_at"] = _read_at(helper.cfg.wall_clock())
    _add_limits(helper, result, topology, extras.get("favored_words"))
    _add_cpu_limits(helper, result)  # D-049
    result = _fit(result)'''),
    ('''            limits = result.get("hw_limits")
            if st.hw_limits is None and isinstance(limits, dict):
                st.hw_limits = {str(k): dict(v) for k, v in limits.items() if isinstance(v, dict)}
''', '''            limits = result.get("hw_limits")
            if st.hw_limits is None and isinstance(limits, dict):
                st.hw_limits = {str(k): dict(v) for k, v in limits.items() if isinstance(v, dict)}
                ring = (result.get("cpu_limits") or {}).get("ring")  # D-049: the ring's limits are kept in cpu_limits only
                if isinstance(ring, dict) and (ring.get("max_mv") is not None or ring.get("max_ratio") is not None):
                    st.hw_limits["ring"] = {"ratio_max_oc": ring.get("max_ratio"), "v_target_max_mv": ring.get("max_mv"), "offset_mv": [ENCODING_OFFSET_FLOOR_MV, ENCODING_OFFSET_CEILING_MV]}
'''),
    ('''    if not isinstance(result.get("read_at"), str) and isinstance(record.get("at"), (int, float)):
        result["read_at"] = _read_at(float(record["at"]))  # a record stored before read_at existed: the time it was stored
''', '''    if not isinstance(result.get("read_at"), str) and isinstance(record.get("at"), (int, float)):
        result["read_at"] = _read_at(float(record["at"]))  # a record stored before read_at existed: the time it was stored
    _add_cpu_limits(helper, result)  # D-049: from this boot's kept limits (they survive a helper restart with the stored read)
'''),
])
print("ok3")
