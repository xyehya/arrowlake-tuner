import re

def edit(path, pairs, append=""):
    s = open(path).read()
    for old, new in pairs:
        assert old in s, (path, old[:90])
        s = s.replace(old, new, 1)
    if append:
        s = s.rstrip("\n") + "\n" + append
    open(path, "w").write(s)


edit("src/arltuner/controls/caps.py", [
    ('''never hard-coded; the G-152 rule). Nothing here has a default floor: ``policy.runtime.cap_ratio_min`` says how low a cap may go and an absent key refuses naming itself; the
ceiling is always the CPU's own ``cpuinfo_max_freq``.
"""''', '''never hard-coded; the G-152 rule). D-049: no policy floor. A cap is refused only when the kernel / CPU would: above the CPU's own ``cpuinfo_max_freq`` (``CAPS_RANGE``), below its
``scaling_min_freq`` (``CAPS_BELOW_MIN``: the cpufreq driver refuses a maximum under the minimum) or below ``cpuinfo_min_freq`` when the CPU reports one (``CAPS_RANGE``). A big change is a
*warning* (:func:`warnings`), never a refusal.
"""'''),
    ('''    "plan_revert",
    "read_state",
    "readback_problem",
    "require_runtime_key",
]''', '''    "limits_json",
    "plan_revert",
    "read_state",
    "readback_problem",
    "warnings",
]'''),
    ("from arltuner.guard.policy import Policy", "from arltuner.guard.policy import LARGE_RATIO_CHANGE, Policy"),
    ('''(``RUNTIME_POLICY_MISSING`` ...), ``key`` the ``[runtime]`` key / intent item it is about."""''', '''(``CAPS_RANGE`` ...), ``key`` the intent item it is about."""'''),
    ('''    """What the CPU's cpufreq directory shows (kHz): its own maximum, the user cap, the user floor (``None`` = file absent)."""

    max_khz: int
    cap_khz: int
    min_khz: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {"cpuinfo_max_khz": self.max_khz, "cap_khz": self.cap_khz, "min_khz": self.min_khz, "cap_ratio": self.cap_khz / KHZ_PER_RATIO}''',
     '''    """What the CPU's cpufreq directory shows (kHz): its own maximum, the user cap, the user floor (``None`` = file absent) and (D-049) the CPU's own minimum ``cpuinfo_min_freq``."""

    max_khz: int
    cap_khz: int
    min_khz: int | None = None
    cpuinfo_min_khz: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {"cpuinfo_max_khz": self.max_khz, "cap_khz": self.cap_khz, "min_khz": self.min_khz, "cap_ratio": self.cap_khz / KHZ_PER_RATIO, "cpuinfo_min_khz": self.cpuinfo_min_khz}'''),
    ('''MAX_ATTR = "cpuinfo_max_freq"''', '''MAX_ATTR = "cpuinfo_max_freq"
CPUINFO_MIN_ATTR = "cpuinfo_min_freq"'''),
    ('''CpuFreq(top, cap, files.read_int(f"cpu{cpu}/cpufreq/{MIN_ATTR}"))''', '''CpuFreq(top, cap, files.read_int(f"cpu{cpu}/cpufreq/{MIN_ATTR}"), files.read_int(f"cpu{cpu}/cpufreq/{CPUINFO_MIN_ATTR}"))'''),
    ('''    Checks, in order (each refusal names its check): the policy key ``cap_ratio_min`` (``RUNTIME_POLICY_MISSING``), a measure / stress run or journal that owns the caps (``CAPS_CONFLICT``:
    ``held`` is its name), unknown CPUs and the E-cluster expansion (``CAPS_UNKNOWN_CPU`` / ``CAPS_CLUSTER_CONFLICT``), then per CPU (the expansion's too): not in the policy's ``cpus`` (``CAPS_CPU_NOT_ALLOWED``), no cpufreq
    directory (``CAPS_NO_FILE``: an offline CPU), the floor ``cap_ratio_min`` and the ceiling, the CPU's own ``cpuinfo_max_freq`` (``CAPS_RANGE``), the CPU's own ``scaling_min_freq``
    (``CAPS_BELOW_MIN``). The kHz is :func:`cap_choice`'s; a step carries the ratio the cap will give and, when that is not the ratio asked, a note. Steps that change nothing are dropped.''',
     '''    D-049, checks in order (each refusal names its check and the file it comes from): a measure / stress run or journal that owns the caps (``CAPS_CONFLICT``:
    ``held`` is its name), unknown CPUs and the E-cluster expansion (``CAPS_UNKNOWN_CPU`` / ``CAPS_CLUSTER_CONFLICT``), then per CPU (the expansion's too): not in the policy's ``cpus`` (``CAPS_CPU_NOT_ALLOWED``, an
    access list), no cpufreq directory (``CAPS_NO_FILE``: an offline CPU), the CPU's own range ``cpuinfo_min_freq`` .. ``cpuinfo_max_freq`` (``CAPS_RANGE``), the CPU's own ``scaling_min_freq``
    (``CAPS_BELOW_MIN``: the driver refuses a maximum below the minimum). The kHz is :func:`cap_choice`'s; a step carries the ratio the cap will give and, when that is not the ratio asked, a note.
    Steps that change nothing are dropped. There is no policy floor.'''),
    ('''    floor = require_runtime_key(policy, "cap_ratio_min")
    if held is not None:''', '''    if held is not None:'''),
    ('''        if ratio < floor:
            raise Refused("CAPS_RANGE", f"cpu {cpu}: ratio {ratio} is below [runtime] cap_ratio_min = {floor} ({policy.where})", key)
        if ratio * KHZ_PER_RATIO > freq.max_khz:
            raise Refused("CAPS_RANGE", f"cpu {cpu}: ratio {ratio} is above the CPU's own maximum (cpuinfo_max_freq {freq.max_khz} kHz = ratio {freq.max_khz / KHZ_PER_RATIO:g})", key)''',
     '''        if ratio * KHZ_PER_RATIO > freq.max_khz:
            raise Refused("CAPS_RANGE", f"cpu {cpu}: ratio {ratio} is above the CPU's own maximum (cpuinfo_max_freq {freq.max_khz} kHz = ratio {freq.max_khz / KHZ_PER_RATIO:g})", key)
        if freq.cpuinfo_min_khz is not None and ratio * KHZ_PER_RATIO < freq.cpuinfo_min_khz:
            raise Refused("CAPS_RANGE", f"cpu {cpu}: ratio {ratio} is below the CPU's own minimum (cpuinfo_min_freq {freq.cpuinfo_min_khz} kHz = ratio {freq.cpuinfo_min_khz / KHZ_PER_RATIO:g})", key)'''),
])
# drop require_runtime_key
s = open("src/arltuner/controls/caps.py").read()
a = s.index("def require_runtime_key(")
b = s.index("def readback_problem(")
s = s[:a] + s[b:]
open("src/arltuner/controls/caps.py", "w").write(s)
with open("src/arltuner/controls/caps.py", "a") as f:
    f.write('''

def warnings(intent: Mapping[int, int], state: Mapping[int, CpuFreq | None], topology: Topology) -> list[tuple[str, str]]:
    """D-049 ``[(code, text)]`` warnings of a caps intent: ``LARGE_CHANGE`` when a CPU's cap moves by more than ``LARGE_RATIO_CHANGE`` bins from the one in force. The Review shows them;
    nothing refuses (a cap only ever lowers the maximum, and the kernel bounds it)."""
    out: list[tuple[str, str]] = []
    try:
        targets = expand(intent, topology)
    except Refused:
        return out
    for cpu in sorted(targets):
        freq = state.get(cpu)
        if freq is not None and abs(targets[cpu] - freq.cap_khz / KHZ_PER_RATIO) > LARGE_RATIO_CHANGE:
            out.append(("LARGE_CHANGE", f"cpu {cpu}: cap {freq.cap_khz / KHZ_PER_RATIO:g} -> {targets[cpu]} is a large single change (more than {LARGE_RATIO_CHANGE} bins); the kernel accepts it"))
    return out


def limits_json(state: Mapping[int, CpuFreq | None]) -> dict[str, list[int | None]]:
    """D-049 ``cpu_limits.caps_khz``: ``{"<cpu>": [cpuinfo_min_freq (else scaling_min_freq, else null), cpuinfo_max_freq]}`` of every CPU that has a cpufreq directory (the kernel's own bounds of a cap)."""
    return {str(cpu): [f.cpuinfo_min_khz if f.cpuinfo_min_khz is not None else f.min_khz, f.max_khz] for cpu, f in sorted(state.items()) if f is not None}
''')

edit("src/arltuner/controls/uncore.py", [
    ('''machine; one ratio = 100000 kHz (ratio 8..40). The limits the driver starts with (``initial_*``) are the hardware's own: nothing outside them is ever written, and the policy's
``[runtime] uncore_ratio = [min, max]`` bounds it further (an absent key refuses naming itself). ``min <= max`` holds after **every single write** (the driver refuses otherwise): raising
the minimum above the current maximum writes the maximum first, anything else writes the minimum first.''', '''machine; one ratio = 100000 kHz (ratio 8..40). The limits the driver starts with (``initial_*``) are the hardware's own: nothing outside them is ever written (D-049: no policy range on
top). ``min <= max`` holds after **every single write** (the driver refuses otherwise): raising the minimum above the current maximum writes the maximum first, anything else writes the
minimum first.'''),
    ('''__all__ = ["DIR", "KEYS", "UncoreState", "plan", "plan_revert", "read_state"]''', '''__all__ = ["DIR", "KEYS", "UncoreState", "limits_json", "plan", "plan_revert", "read_state", "warnings"]'''),
    ("from .caps import KHZ_PER_RATIO, Refused, RuntimeFiles, RuntimeStep, SysfsError, require_runtime_key", "from .caps import KHZ_PER_RATIO, Refused, RuntimeFiles, RuntimeStep, SysfsError"),
    ("from arltuner.guard.policy import Policy", "from arltuner.guard.policy import LARGE_RATIO_CHANGE, Policy"),
    ('''    """The write steps for ``intent`` (``{min_ratio?, max_ratio?}``) over the limits in force, or :class:`Refused`. Checks, in order: the policy key ``uncore_ratio``
    (``RUNTIME_POLICY_MISSING``), the hardware's own range ``initial_min..initial_max`` (``UNCORE_RANGE``), the policy range (``UNCORE_RANGE``), ``min <= max`` of the final pair
    (``UNCORE_ORDER``). Steps that change nothing are dropped."""''', '''    """The write steps for ``intent`` (``{min_ratio?, max_ratio?}``) over the limits in force, or :class:`Refused`. D-049, checks in order: the hardware's own range
    ``initial_min_freq_khz..initial_max_freq_khz`` (``UNCORE_RANGE``), ``min <= max`` of the final pair (``UNCORE_ORDER``: the driver refuses otherwise). Steps that change nothing are
    dropped. ``policy`` is kept for the signature: no policy range applies."""'''),
    ('''    lo, hi = require_runtime_key(policy, "uncore_ratio")
    new: dict''', '''    new: dict'''),
    ('''        if not lo <= ratio <= hi:
            raise Refused("UNCORE_RANGE", f"{key} = {ratio} is outside [runtime] uncore_ratio = [{lo}, {hi}] ({policy.where})", key)
''', ''),
])
with open("src/arltuner/controls/uncore.py", "a") as f:
    f.write('''

def warnings(intent: Mapping[str, int], state: UncoreState) -> list[tuple[str, str]]:
    """D-049 ``[(code, text)]`` warnings of an uncore intent: ``LARGE_CHANGE`` when a limit moves by more than ``LARGE_RATIO_CHANGE`` bins. Nothing refuses (the driver bounds it)."""
    out: list[tuple[str, str]] = []
    for key, ratio in intent.items():
        if ratio is None or key not in KEYS:
            continue
        now = state.value(key) / KHZ_PER_RATIO
        if abs(ratio - now) > LARGE_RATIO_CHANGE:
            out.append(("LARGE_CHANGE", f"uncore {key}: {now:g} -> {ratio} is a large single change (more than {LARGE_RATIO_CHANGE} bins); the driver accepts it"))
    return out


def limits_json(state: UncoreState) -> list[int]:
    """D-049 ``cpu_limits.uncore_khz``: ``[initial_min_freq_khz, initial_max_freq_khz]``, the hardware's own range (sysfs)."""
    return [state.initial_min_khz, state.initial_max_khz]
''')
print("ok")
