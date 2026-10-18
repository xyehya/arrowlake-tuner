class Policy:
    """The whole policy: PROCESS settings only (D-049). ``max_operations_per_session`` counts every executed mailbox request (reads included, because
    every request is a WRMSR -- SAFETY.md "OC mailbox rule"); the post-write read-back does not count. The default of 1
    means *one* transaction until the human raises it (a read-then-write session needs at least 2).

    ``e_cpus`` (T-021): which CPUs are E-cores (any other CPU counts as P).

    ``max_operations_per_boot`` (T-016) is the same count taken over **every process** since the current boot, read from the
    audit log (``attempt`` records with the current ``boot_id``): the CLI is one process per command, so a per-session limit
    alone cannot stop back-to-back invocations -- the sequence behind the 2026-08-26 incident (SAFETY.md
    "Mailbox-session discipline", D-011). ``min_dwell_s`` likewise also applies across processes (from the audit log).

    D-049: there is no operator value limit. ``hw_ratio_max`` / ``hw_v_target_max_mv`` carry the CPU's own limits per core kind (``0x01 [7:0]``, ``0x28 [11:0] - 1 LSB`` in whole mV) when the
    helper bound them from this boot's ``tune.read`` (``with_hw_limits``); ``None`` = not read, and a write that needs the limit is refused (``CPU_LIMIT_UNREAD``). ``ignored_keys``
    names the keys of an older policy file that were ignored (``load_policy``); ``source`` names the policy file (for messages). The attributes of the removed operator limits
    (``ratio_max``, ``offset_ceiling_mv``, ``max_*_step*``, ``power``, ``runtime`` ...) remain as read-only properties with neutral values so a reader outside this package keeps working."""

    cpus: frozenset[int]
    max_operations_per_session: int = 1
    min_dwell_s: float = 60.0
    allow: tuple[AllowEntry, ...] = ()
    max_operations_per_boot: int = 12
    e_cpus: frozenset[int] | None = None  # None = not bound to a topology: core_kind() raises instead of assuming a particular layout
    msr_allow: tuple[MsrAllowEntry, ...] = ()  # T-023: plain MSR allowlist; empty (the default) = every plain MSR access is denied
    hw_ratio_max: Mapping[str, int | None] = field(default_factory=lambda: {"P": None, "E": None})  # the CPU's own ratio limit per kind (0x01 [7:0]) read by the helper; None = unread
    hw_v_target_max_mv: Mapping[str, int | None] = field(default_factory=lambda: {"P": None, "E": None})  # the CPU's own V limit per kind (0x28 - 1 LSB), whole mV; None = unread
    source: str | None = field(default=None, compare=False)  # the policy file this policy was loaded from (named in every refusal that quotes it)
    ignored_keys: tuple[str, ...] = field(default=(), compare=False)  # D-049: keys of an older policy file that load but no longer apply

    @property
    def where(self) -> str:
        """How refusals name the policy file."""
        return self.source or "the policy"

    # --- D-049: neutral read-only stand-ins for the removed operator limits (readers outside guard/controls-limits code: report, residual, executor, ops_power, ops_runtime) ---
    @property
    def ratio_max(self) -> Mapping[str, int | None]:
        return MappingProxyType({"P": None, "E": None})

    @property
    def v_target_max_mv(self) -> Mapping[str, int | None]:
        return MappingProxyType({"P": None, "E": None})

    @property
    def assumed_bios_offset_mv(self) -> Mapping[str, int]:
        return MappingProxyType({"P": 0, "E": 0})

    @property
    def effective_offset_floor_mv(self) -> int:
        return ENCODING_OFFSET_FLOOR_MV

    @property
    def offset_ceiling_mv(self) -> int:
        return ENCODING_OFFSET_CEILING_MV

    @property
    def max_ratio_step(self) -> int:
        return ENCODING_RATIO_MAX

    @property
    def max_v_step_mv(self) -> int:
        return 1000

    @property
    def max_offset_step_mv(self) -> int:
        return 2000

    @property
    def power(self) -> None:
        return None

    @property
    def runtime(self) -> None:
        return None

    def with_hw_limits(self, ratio_max_oc: Mapping[str, int | None] | None = None, v_target_max_mv: Mapping[str, int | None] | None = None) -> Policy:
        """The same policy bound to the CPU's own limits read live: ``0x01 [7:0]`` per kind and ``0x28 - 1 LSB`` in mV per kind. Kinds not given keep their value; keys that
        are not a core kind (``"ring"``) are not a policy matter and are dropped."""
        return replace(
            self,
            hw_ratio_max={**self.hw_ratio_max, **{k: v for k, v in (ratio_max_oc or {}).items() if k in (KIND_P, KIND_E)}},
            hw_v_target_max_mv={**self.hw_v_target_max_mv, **{k: v for k, v in (v_target_max_mv or {}).items() if k in (KIND_P, KIND_E)}},
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "cpus", _int_frozenset("cpus", self.cpus, 0, _CPU_MAX))
        object.__setattr__(self, "hw_ratio_max", _kind_ceilings("hw_ratio_max", self.hw_ratio_max, {k: (1, ENCODING_RATIO_MAX) for k in (KIND_P, KIND_E)}, "ratio field [7:0]"))
        object.__setattr__(
            self,
            "hw_v_target_max_mv",
            _kind_ceilings("hw_v_target_max_mv", self.hw_v_target_max_mv, {k: (0, ENCODING_V_TARGET_MAX_MV) for k in (KIND_P, KIND_E)}, "0x28 field [11:0]"),
        )
        if self.e_cpus is not None:
            if isinstance(self.e_cpus, (str, bytes, Mapping)) or not isinstance(self.e_cpus, Iterable):
                raise PolicyError(f"e_cpus must be a collection of ints, got {type(self.e_cpus).__name__}")
            e_cpus = list(self.e_cpus)
            for v in e_cpus:
                if not _is_int(v) or not 0 <= v <= _CPU_MAX:
                    raise PolicyError(f"e_cpus: {v!r} is not an int in 0..{_CPU_MAX}")
            object.__setattr__(self, "e_cpus", frozenset(int(v) for v in e_cpus))
        if not _is_int(self.max_operations_per_session) or self.max_operations_per_session < 1:
            raise PolicyError(f"max_operations_per_session must be an int >= 1, got {self.max_operations_per_session!r}")
        if not _is_int(self.max_operations_per_boot) or self.max_operations_per_boot < 1:
            raise PolicyError(f"max_operations_per_boot must be an int >= 1, got {self.max_operations_per_boot!r}")
        dwell = self.min_dwell_s
        if isinstance(dwell, bool) or not isinstance(dwell, (int, float)) or not math.isfinite(dwell) or dwell < 0:
            raise PolicyError(f"min_dwell_s must be a finite number >= 0, got {dwell!r}")
        object.__setattr__(self, "min_dwell_s", float(dwell))
        if isinstance(self.allow, (str, bytes)) or not isinstance(self.allow, Iterable):
            raise PolicyError("allow must be a sequence of AllowEntry")
        entries = tuple(self.allow)
        for entry in entries:
            if not isinstance(entry, AllowEntry):
                raise PolicyError(f"allow contains a non-AllowEntry: {entry!r}")
        names = [e.name for e in entries]
        if len(set(names)) != len(names):
            raise PolicyError(f"duplicate allow entry names: {sorted(n for n in set(names) if names.count(n) > 1)}")
        for i, a in enumerate(entries):
            for b in entries[i + 1 :]:
                if a.cmd == b.cmd and a.p1 & b.p1 and a.p2 & b.p2:
                    raise PolicyError(f"allow entries {a.name!r} and {b.name!r} overlap (ambiguous check)")
        object.__setattr__(self, "allow", entries)
        if isinstance(self.msr_allow, (str, bytes)) or not isinstance(self.msr_allow, Iterable):
            raise PolicyError("msr_allow must be a sequence of MsrAllowEntry")
        msr_entries = tuple(self.msr_allow)
        for m in msr_entries:
            if not isinstance(m, MsrAllowEntry):
                raise PolicyError(f"msr_allow contains a non-MsrAllowEntry: {m!r}")
            if m.cpus is not None and not m.cpus <= self.cpus:
                raise PolicyError(f"msr_allow entry {m.name!r} names CPUs outside the policy's cpus: {sorted(m.cpus - self.cpus)}")
        msr_names = [m.name for m in msr_entries]
        if len(set(msr_names)) != len(msr_names):
            raise PolicyError(f"duplicate msr_allow entry names: {sorted(n for n in set(msr_names) if msr_names.count(n) > 1)}")
        for i, a in enumerate(msr_entries):
            for b in msr_entries[i + 1 :]:
                if a.msr == b.msr and a.kind == b.kind:
                    raise PolicyError(f"msr_allow entries {a.name!r} and {b.name!r} overlap (one entry per MSR and kind)")
        object.__setattr__(self, "msr_allow", msr_entries)
        object.__setattr__(self, "ignored_keys", tuple(self.ignored_keys))

    def cpu_allowed(self, cpu: object) -> bool:
        return _is_int(cpu) and cpu in self.cpus

    def with_topology(self, topology: Topology) -> Policy:
        """The same policy over a live topology (D-036): ``cpus`` and ``e_cpus`` are exactly that topology's CPUs and E CPUs. Hardware paths bind the topology
        they derived from sysfs; an unbound policy carries the simulated reference CPU's sets."""
        return replace(self, cpus=frozenset(c.cpu for c in topology.cores), e_cpus=frozenset(topology.cpus_of_kind(KIND_E)))

    def core_kind(self, cpu: int) -> str:
        """``"E"`` for a CPU listed in ``e_cpus``, else ``"P"``. ``PolicyError`` for a policy not bound to a topology."""
        if self.e_cpus is None:
            raise PolicyError("this policy is not bound to a CPU topology (e_cpus unset): bind it with Policy.with_topology / load_policy(..., topology) before asking a CPU's kind")
        return "E" if cpu in self.e_cpus else "P"

    def match_msr(self, msr: object, kind: object) -> MsrAllowEntry | None:
        """The ``msr_allow`` entry for (``msr``, ``kind``), or ``None`` (default deny). Non-int / unknown input never matches. The caller still
        asks ``covers_cpu`` and ``cpu_allowed``."""
        if not _is_int(msr) or kind not in ("read", "write"):
            return None
        for entry in self.msr_allow:
            if entry.matches(int(msr), str(kind)):  # type: ignore[call-overload]
                return entry
        return None

    def match(self, cmd: object, p1: object, p2: object) -> AllowEntry | None:
        """The single allow entry covering ``(cmd, p1, p2)``, or ``None`` (default deny). Non-int input never matches."""
        if not (_is_int(cmd) and _is_int(p1) and _is_int(p2)):
            return None
        for entry in self.allow:
            if entry.matches(int(cmd), int(p1), int(p2)):  # type: ignore[call-overload]
                return entry
        return None


# --------------------------------------------------------------------------------------------------------------
