AUTO = "auto"
_LOG = logging.getLogger("arltuner.policy")


def load_policy(source: str | os.PathLike[str], topology: Topology | None = None) -> Policy:
    """Load a policy from TOML text or a file path. Strict: unknown keys, unknown checks, bad types -> ``PolicyError``.

    ``topology`` (D-036): the live topology. ``cpus = "auto"`` / ``e_cpus = "auto"`` are derived from it and are a ``PolicyError`` without one (never a default); a
    policy without an ``e_cpus`` key takes the topology's E CPUs when one is given.

    D-049: the operator value limits of older policy files (``LEGACY_KEYS``: the offset window, the step limits, ``ratio_max``, ``v_target_max_mv`` and the ``[power]`` /
    ``[runtime]`` tables) still load -- the installed ``/etc/arltuner/policy.toml`` has them -- but are IGNORED: one warning line (``logging``, logger ``arltuner.policy``) names
    them and ``Policy.ignored_keys`` carries the names. Their values are not even validated."""
    text = _read_source(source)
    named = str(source) if isinstance(source, os.PathLike) or (isinstance(source, str) and "\n" not in source and os.path.isfile(source)) else None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"policy is not valid TOML: {exc}") from exc
    unknown = set(doc) - _TOP_KEYS
    if unknown:
        raise PolicyError(f"unknown top-level key(s): {sorted(unknown)}")
    if "cpus" not in doc:
        raise PolicyError("policy must define cpus (no default: an absent CPU set is not 'all CPUs')")

    def needs_topology(what: str) -> Topology:
        if topology is None:
            raise PolicyError(f'{what} = "auto" is derived from the live topology, which was not supplied')
        return topology

    if doc["cpus"] == AUTO:
        kwargs: dict[str, Any] = {"cpus": frozenset(c.cpu for c in needs_topology("cpus").cores)}
    else:
        kwargs = {"cpus": frozenset(_parse_int_list("cpus", doc["cpus"]))}
    if named is not None:
        kwargs["source"] = named
    kwargs["e_cpus"] = frozenset(topology.cpus_of_kind(KIND_E)) if topology is not None else _E_CPUS_REFERENCE  # no topology = the simulator / tests, on purpose (G-141)
    if "max_operations_per_session" in doc:
        kwargs["max_operations_per_session"] = doc["max_operations_per_session"]
    if "max_operations_per_boot" in doc:
        kwargs["max_operations_per_boot"] = doc["max_operations_per_boot"]
    if "min_dwell_s" in doc:
        kwargs["min_dwell_s"] = doc["min_dwell_s"]
    if "e_cpus" in doc:
        if doc["e_cpus"] == AUTO:
            kwargs["e_cpus"] = frozenset(needs_topology("e_cpus").cpus_of_kind(KIND_E))
        else:
            kwargs["e_cpus"] = frozenset(_parse_int_list("e_cpus", doc["e_cpus"]))
    ignored: list[str] = []
    for key in _LEGACY_KEYS:
        if key not in doc:
            continue
        value = doc[key]
        if key in _LEGACY_TABLES and isinstance(value, dict) and value:
            ignored.extend(f"{key}.{sub}" for sub in sorted(value))
        else:
            ignored.append(key)
    if ignored:
        kwargs["ignored_keys"] = tuple(ignored)
        _LOG.warning(
            "policy %s: ignoring %s -- D-049: the helper enforces only the CPU's own limits (cmd 0x01 ratio, cmd 0x28 voltage, the offset field, MSR 0x610, cpufreq, uncore); "
            "these operator value limits no longer apply, remove them from the file",
            named or "(text)",
            ", ".join(ignored),
        )
    raw_entries = doc.get("allow", [])
    if not isinstance(raw_entries, list) or not all(isinstance(e, dict) for e in raw_entries):
        raise PolicyError("allow must be an array of tables ([[allow]])")
    entries = []
    for i, raw in enumerate(raw_entries):
        label = f"allow[{i}]" + (f" ({raw['name']})" if isinstance(raw.get("name"), str) else "")
        bad = set(raw) - _ENTRY_KEYS
        if bad:
            raise PolicyError(f"{label}: unknown key(s) {sorted(bad)}")
        missing = _ENTRY_REQUIRED - set(raw)
        if missing:
            raise PolicyError(f"{label}: missing key(s) {sorted(missing)}")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise PolicyError(f"{label}: params must be a table")
        entries.append(
            AllowEntry(
                name=raw["name"],
                cmd=raw["cmd"],
                p1=frozenset(_parse_int_list(f"{label} p1", raw["p1"])),
                p2=frozenset(_parse_int_list(f"{label} p2", raw["p2"])),
                kind=raw["kind"],
                check=raw.get("check"),
                params=params,
            )
        )
    kwargs["allow"] = tuple(entries)
    raw_msr = doc.get("msr_allow", [])
    if not isinstance(raw_msr, list) or not all(isinstance(e, dict) for e in raw_msr):
        raise PolicyError("msr_allow must be an array of tables ([[msr_allow]])")
    msr_entries = []
    for i, raw in enumerate(raw_msr):
        label = f"msr_allow[{i}]" + (f" ({raw['name']})" if isinstance(raw.get("name"), str) else "")
        bad = set(raw) - _MSR_ENTRY_KEYS
        if bad:
            raise PolicyError(f"{label}: unknown key(s) {sorted(bad)}")
        missing = _MSR_ENTRY_REQUIRED - set(raw)
        if missing:
            raise PolicyError(f"{label}: missing key(s) {sorted(missing)}")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise PolicyError(f"{label}: params must be a table")
        msr_entries.append(
            MsrAllowEntry(
                name=raw["name"],
                msr=raw["msr"],
                kind=raw["kind"],
                cpus=None if "cpus" not in raw else frozenset(_parse_int_list(f"{label} cpus", raw["cpus"])),
                check=raw.get("check"),
                params=params,
            )
        )
    kwargs["msr_allow"] = tuple(msr_entries)
    return Policy(**kwargs)
