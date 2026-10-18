def edit(path, pairs):
    s = open(path).read()
    for old, new in pairs:
        assert old in s, (path, old[:100])
        s = s.replace(old, new, 1)
    open(path, "w").write(s)


P = "src/arltuner/helper/ops_tune.py"
edit(P, [
    ('''    source = "0x28/0x01 read" + (f" {read_at}" if (max_mv is not None or max_ratio is not None) and read_at else "") if (max_mv is not None or max_ratio is not None) else "not read yet (0x28 / 0x01)"
''', '''    known = max_mv is not None or max_ratio is not None
    source = ("0x28/0x01 read" + (f" {read_at}" if read_at else "")) if known else "not read yet (0x28 / 0x01)"
'''),
    # --- do_limits: the ring (P1=2) too -----------------------------------------------------------------------------------
    ('''    * ``limits`` (G-137, D-039): cmd ``0x01 P1=0`` (fused max OC ratio) and ``0x28 P1=0`` (max voltage) on the first P CPU and the first CPU of the first E cluster (4),''',
     '''    * ``limits`` (G-137, D-039, D-049): cmd ``0x01 P1=0`` (fused max OC ratio) and ``0x28 P1=0`` (max voltage) on the first P CPU and the first CPU of the first E cluster (4), and the same two
      with ``P1=2`` (the ring, addressed through the first P CPU) for ``cpu_limits.ring`` (2): 6,'''),
    ('''ONE session of exactly ``units + P-cores + E-clusters + 4 + 4`` guard-counted requests''', '''ONE session of exactly ``units + P-cores + E-clusters + 4 + 6`` guard-counted requests'''),
    ('''    limit_cpus = {kind: cpus[0] for kind, cpus in (("P", p_cpus), ("E", e_cpus)) if cpus}  # one CPU per kind answers for its kind''',
     '''    limit_cpus = {kind: cpus[0] for kind, cpus in (("P", p_cpus), ("E", e_cpus), ("ring", p_cpus)) if cpus}  # one CPU per kind answers for its kind; the ring (P1=2) is package-level, any CPU answers'''),
    ('''                            fill(entry, mailbox(cpu, cmd, 0, f"read {what}"))''', '''                            fill(entry, mailbox(cpu, cmd, 2 if kind == "ring" else 0, f"read {what}" + (" of the ring (P1=2)" if kind == "ring" else "")))'''),
])
print("ok2")
