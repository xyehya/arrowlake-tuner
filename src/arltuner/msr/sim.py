"""``SimMsrPort``: a deterministic software model of the per-CPU OC mailbox (MSR 0x150) -- a TEST DOUBLE.

**Every numeric default in this module is SIM-ONLY.** The defaults are modelled on the XTU UI / values logged on this
machine (``docs/research/registers.md`` "V/F data word", "Measured on this machine", ``docs/research/
lnx-turbostat-plus-facts.md``) so golden sequences look realistic, but they are NOT hardware facts and must never be
copied into production code or a write allowlist. Rule behaviour (which rc is returned for which violation) is the
simulator's own model, chosen so the guard/controls/CLI can be tested against every refusal path; real pcode may differ.

Protocol model (registers.md "OC mailbox -- request model"): ``wrmsr(cpu, 0x150, word)`` with bit 63 set latches a
request; ``rdmsr(cpu, 0x150)`` then returns a word with bit 63 set for ``config.busy_polls`` reads and afterwards the
response word ``(rc << 32) | data`` with bit 63 clear, which persists until the next request. A 0x150 write while busy, or
without bit 63, raises ``MsrError(EIO)``. The command is executed when the request is latched; only the busy window is
simulated. The mailbox latch is per CPU; ``0x14``/``0x15`` (misc global config) is package-wide state.

Modelled commands (everything else -> ``Rc.UnrecognizedCommand``): ``0x01`` capability (P1=3 -> ``OCNotSupported``, as XTU's log
shows), ``0x02``, ``0x28`` (per CPU for domain 0), ``0x09 P1=0x11`` (P2=0: the per-CPU fused V/F point), ``0x14/0x15`` (bit 3 =
PerCore Voltage Enable), ``0x10/0x11`` V/F word for domains 0 (per CPU), 1, 2, 7 (per domain), and -- added by T-021 to match what XTU
sends on every per-core Apply (``docs/research/win-xtu-live-trace.md`` S-04) -- ``0x1C/0x1D`` (per-CPU favored word) and ``0x26/0x27``
(per-CPU granular word). T-023 adds the plain registers ``0x1AD/0x1AE/0x650/0x651/0xCE/0x194`` (package-scoped by default, ``plain_msrs_per_cpu`` makes them per CPU; only
``0x1AD``/``0x650`` accept a write). ``SimConfig.fixed_reads`` answers further package-wide reads (``0x04``, ``0x05``, ``0x16``, ``0x18``,
``0x1A`` ...) with configured words.

Two configurations: ``SimConfig()`` keeps the old SIM-ONLY defaults (for the older tests); ``SimConfig.measured()`` is the machine as
MEASURED on Windows (``msr/measured_baseline.py``: per-core ``0x10`` word ``0x0`` except CPU 0 ``0x39``, fused points, favored words,
...). **New code and docs use ``measured()``.** ``reject_zero_fields`` models the one thing nobody has measured: a ``0x11`` whose ratio
or V target is 0.
"""
from __future__ import annotations

import errno
from collections.abc import Callable
from dataclasses import dataclass, field

from arltuner.mailbox import MSR_OC_MAILBOX, Cmd, Domain, Rc, VfWord, is_busy, is_write
from arltuner.platform import default_platform
from arltuner.topology import Topology, arl_s_285k

from . import measured_baseline
from .port import MsrError, MsrPort, check_cpu, check_msr, check_value

_BUSY = 1 << 63
_PER_CORE_VOLTAGE_ENABLE = 1 << 3  # registers.md 0x14/0x15 data bit 3
_VF_DOMAINS = (int(Domain.CORE), int(Domain.GT), int(Domain.RING), int(Domain.NGU))
_MAX_POINT = 15  # registers.md: P2 1..15 = V/F point (hidden on ARL-S)
#: T-025 ``[MEASURED lnx 2026-10-02]`` (docs/research/lnx-first-read.md, E CPU 8): ``0x10 P2=6`` = ``0x0000002E`` (point 6 = 46x, V target 0, offset 0),
#: ``0x10 P2=7`` -> rc ``0x11`` (the OC point is the ``P2 = 0`` word, not a point index). SkatterBencher ``[COMMUNITY]``: E has 6 fused points, P has 8.
RC_NO_SUCH_POINT = 0x11  # not in ``Rc`` (``rc_name`` says ``UNKNOWN_0x11``)
_PLATFORM = default_platform()  # V/F point ratios and turbo defaults: platforms/arl-s-285k.toml (G-101)
POINT_COUNTS_ARL_S: dict[str, int] = {"E": len(_PLATFORM.e_point_ratios), "P": len(_PLATFORM.p_point_ratios)}
#: T-026 ``[MEASURED lnx 2026-10-02]`` (docs/research/lnx-first-read.md "Full point layout", "E cluster 1 V/F curve"): the ratio field of every V/F point
#: and the BIOS per-point offset (raw, /1024 V). P (CPU 0, same table assumed on every P-core): 8,20,30,48,53,55,56,57x with -49.8 mV (raw -51) at 53x
#: and 57x and -14.6 mV (raw -15) at 55x. E: 8,15,23,30,40,46x, offset 0. Used by ``SimConfig.measured()`` only (``SimConfig()`` keeps its old defaults).
POINT_RATIOS_ARL_S: dict[tuple[str, int], int] = {
    **{("P", n): r for n, r in enumerate(_PLATFORM.p_point_ratios, start=1)},  # platforms/arl-s-285k.toml (G-101)
    **{("E", n): r for n, r in enumerate(_PLATFORM.e_point_ratios, start=1)},
}
#: G-117: opt-in only (``measured()`` starts every point at offset 0): ``SimConfig.measured(point_offsets=dict(POINT_OFFSETS_ARL_S))``.
POINT_OFFSETS_ARL_S: dict[tuple[str, int], int] = {("P", 5): -51, ("P", 6): -15, ("P", 8): -51}
#: T-026 SIM-ONLY: the point ratios of a NON-favored P-core. Only CPU 0 (favored) was measured; SkatterBencher ``[COMMUNITY]``: 57x is the boost ratio of the
#: favored P-cores (CPU 0 and 3), the others top out lower and may have another set of points. ``SimConfig.measured()`` uses this for the other six P-cores
#: so the per-core ratio flagging has something to find; it is NOT a hardware fact. G-114: no longer used by ``measured()`` (F1: the tables are identical on all
#: eight P-cores); a test that wants per-core flags builds ``cpu_point_ratios`` from this tuple itself.
POINT_RATIOS_P_NONFAVORED_SIM: tuple[int, ...] = (8, 20, 30, 45, 50, 52, 54, 55)
#: T-028 ``[MEASURED lnx 2026-10-02]`` (docs/research/skatterbencher-285k.md ring section, ring-community.md): ``0x10 P1=2 P2=1..8`` read from CPU 0 = the ring
#: points 8,14,20,28,34,37,38,39x (V target 0), ``P2 = 9`` -> rc ``0x11``; point 8 = ``0xF6600027`` = 39x with the BIOS offset -75.2 mV (raw -77).
RING_POINT_RATIOS_ARL_S: dict[int, int] = dict(enumerate(_PLATFORM.ring_point_ratios, start=1))
RING_POINT_OFFSETS_ARL_S: dict[int, int] = {8: -77}
_ZERO_VF = VfWord(ratio=0, v_target_raw=0, static=False, offset_raw=0)
_SILENT_CMDS = (
    int(Cmd.VOLTAGE_FREQ_WRITE),
    int(Cmd.MISC_GLOBAL_CONFIG_WRITE),
    int(Cmd.FAVORED_CORE_WRITE),
    int(Cmd.GRANULAR_RATIO_WRITE),
)
#: Reads with their own per-CPU/stateful model; ``SimConfig.fixed_reads`` must not shadow them.
_MODELLED_READS = frozenset(
    int(c)
    for c in (
        Cmd.VOLTAGE_FLOOR_READ,
        Cmd.VOLTAGE_FREQ_READ,
        Cmd.MISC_GLOBAL_CONFIG_READ,
        Cmd.FAVORED_CORE_READ,
        Cmd.GRANULAR_RATIO_READ,
        Cmd.MAXIMUM_VOLTAGE_LIMIT_READ,
    )
)

#: T-023 plain (non-mailbox) registers of the turbo ratio tables. ``[MEASURED lnx 2026-10-01]`` (docs/research/lnx-first-read.md, T-023 spec
#: "Facts"): ``0x1AD`` P buckets 57,57,55 x6 (LSB = 1 active core), ``0x1AE`` its core counts 1..8, ``0x650`` E buckets 48 x8, ``0x651`` its core
#: counts, ``0xCE`` PLATFORM_INFO (``[15:8]`` = 37 base ratio), ``0x194`` FLEX_RATIO. The same value was read on CPU 0 and CPU 20.
PLAIN_MSR_DEFAULTS: dict[int, int] = {
    **_PLATFORM.turbo_defaults,  # 0x1AD / 0x1AE / 0x650 / 0x651 from platforms/arl-s-285k.toml (G-101)
    0xCE: 0x08040838F1812500,
    0x194: 0x000E0000,
}
#: Writable in the model: the two ratio tables (what XTU writes, ``[DECOMPILED win-xtu-native-provider.md S6a]``); a write of any other
#: plain register is a #GP (``MsrError(EIO)``).
PLAIN_MSR_WRITABLE = frozenset({0x1AD, 0x650})

# SIM-ONLY defaults (see module docstring). Ring/NGU words are the OCCT/HWiNFO trace values in registers.md; GT is arbitrary.
_DEFAULT_DOMAIN_VF = {
    int(Domain.GT): VfWord(ratio=40, v_target_raw=0x440, static=False, offset_raw=0),
    int(Domain.RING): VfWord(ratio=40, v_target_raw=0x4C8, static=False, offset_raw=0),
    int(Domain.NGU): VfWord(ratio=34, v_target_raw=0x4BD, static=False, offset_raw=0),
}


def _default_core_vf(cpu: int, kind: str, cluster: int | None) -> VfWord:
    """SIM-ONLY per-CPU start word: P0/P3 57x 0x5B4, other P 55x 0x544, E cluster 0 48x 0x4A0, other E 48x 0x48C."""
    if kind == "P":
        return VfWord(57, 0x5B4, False, 0) if cpu in (0, 3) else VfWord(55, 0x544, False, 0)
    return VfWord(48, 0x4A0 if cluster == 0 else 0x48C, False, 0)


def _default_fused_max_ratio(cpu: int, kind: str) -> int:
    """SIM-ONLY fused max ratio reported by cmd 0x1C ``[15:8]``: P0/P3 57, other P 55, E 46."""
    if kind == "P":
        return 57 if cpu in (0, 3) else 55
    return 46


@dataclass
class SimConfig:
    """Knobs of the simulator. SIM-ONLY defaults. The object is read live (mutating it between calls changes the rules);
    the ``initial_*`` / ``misc_global_config`` fields only matter when the port is constructed."""

    writable: bool = True
    busy_polls: int = 1  # busy reads after each accepted request before the response appears
    oc_locked: bool = False  # 0x11 and 0x15 -> Rc.OCLocked (registers.md: XTU log "SET 0x15 data 8 RC 1")
    per_core_requires_enable: bool = True  # domain-0 0x11 needs 0x14/0x15 bit 3, else Rc.OCLocked
    uvp: bool = False  # undervolt protection: a negative offset -> Rc.UndervoltProtection
    allow_static: bool = True  # 0x11 with the static/override bit set; False -> Rc.InvalidParam
    e_cluster_wide: bool = False  # a domain-0 write to an E CPU updates all 4 CPUs of its cluster ([UNVERIFIED] on hardware)
    per_point: bool = False  # accept P2 1..15 with per-(cpu, p2) state; False -> P2 != 0 is Rc.InvalidParam
    # ---- T-025: V/F points (only with ``per_point``) ----
    point_counts: dict[str, int] | None = None  # core kind -> fused points (domain 0): P2 above it -> rc 0x11; None = legacy: every P2 1..15 answers
    point_ratios: dict[tuple[str, int], int] = field(default_factory=lambda: {("E", 6): 46})  # (kind, point) -> the ratio field the point starts with
    cpu_point_ratios: dict[tuple[int, int], int] = field(default_factory=dict)  # T-026: (cpu, point) -> ratio field; wins over ``point_ratios`` (per-core tables)
    drop_target_without_oc_point: bool = True  # G-120: a domain-0 P2 = 0 word whose ratio is <= the top fused ratio has no OC point: the V target is dropped whatever the offset (G-124; needs point_counts) [MEASURED lnx 2026-10-11/12]
    oc_adds_point: bool = False  # T-026: an OC ratio (P2 = 0 word) above the top fused ratio of the core creates ONE extra point (``[COMMUNITY]``, SkatterBencher)
    point_offsets: dict[tuple[str, int], int] = field(default_factory=dict)  # T-026: (kind, point) -> the offset field (raw) the point starts with (default 0)
    cpu_point_offsets: dict[tuple[int, int], int] = field(default_factory=dict)  # T-026: (cpu, point) -> offset raw; wins over ``point_offsets`` (a BIOS that differs per core)
    ring_point_ratios: dict[int, int] = field(default_factory=dict)  # T-028: ring (P1 = 2) point -> ratio field; empty = legacy (every P2 1..15 answers, zero word)
    ring_point_offsets: dict[int, int] = field(default_factory=dict)  # T-028: ring point -> offset raw it starts with (default 0)
    max_voltage_raw: int = 0x800  # 0x28 answer and the 0x11 v_target limit (S2.10 raw; 0x800 = 2.0 V)
    oc_cap_ratio: dict[str, int] = field(default_factory=lambda: {"P": 62, "E": 52})  # 0x11 domain 0 / 0x01 P1=0 by core kind
    domain_cap_ratio: dict[int, int] = field(default_factory=lambda: {1: 50, 2: 50, 7: 40})  # domain -> cap (0x11, 0x01)
    capability_flags: int = 0b111  # 0x01 data bits [10:8]
    per_core_capability_word: int = 0x39393939  # 0x02 answer ("may return fixed bytes")
    floors: dict[int, int] = field(default_factory=dict)  # 0x09 P1=0x11: domain -> raw word (0 unless configured)
    misc_global_config: int = 0  # initial 0x14 word; bit 3 = PerCore Voltage Enable (initially 0)
    topology: Topology = field(default_factory=arl_s_285k)
    initial_vf: dict[int, VfWord] = field(default_factory=dict)  # cpu -> domain-0 start word override
    initial_domain_vf: dict[int, VfWord] = field(default_factory=dict)  # domain 1/2/7 -> start word override
    fused_max_ratio: dict[int, int] = field(default_factory=dict)  # cpu -> 0x1C fused max override ([15:8] of the default word)
    # ---- T-021: what the measured machine needs (all default to the old SIM-ONLY behaviour) ----
    reject_zero_fields: bool = False  # a domain-0 0x11 (or 0x1D) with ratio 0 / V target 0 -> Rc.InvalidParam ([UNVERIFIED] on hardware)
    fused_point: dict[int, int] = field(default_factory=dict)  # cpu -> 0x09 P1=0x11 P2=0 word (default: the SIM-ONLY default V/F word)
    favored_word: dict[int, int] = field(default_factory=dict)  # cpu -> initial 0x1C word (default: fused_max << 8 | default ratio)
    granular_word: dict[int, int] = field(default_factory=dict)  # cpu -> 0x26 word (default: granular_default)
    granular_default: int = 3  # SIM-ONLY default for a CPU not in granular_word (3 is also what every core measured)
    max_voltage_cpu: dict[int, int] = field(default_factory=dict)  # cpu -> 0x28 answer / 0x11 limit for domain 0 (default max_voltage_raw)
    fixed_reads: dict[tuple[int, int, int], int] = field(default_factory=dict)  # (cmd, P1, P2) -> word for package-wide reads
    # ---- T-023: plain registers (turbo ratio tables, PLATFORM_INFO); package-scoped unless ``plain_msrs_per_cpu`` ----
    plain_msrs: bool = True  # model PLAIN_MSR_DEFAULTS (False: they are unconfigured, rdmsr -> EIO, as before T-023)
    plain_msrs_per_cpu: bool = False  # True: every CPU has its own copy, so a write on one CPU is not seen on another (tests the verify-CPU trip)
    plain_msr_values: dict[int, int] = field(default_factory=dict)  # msr -> start value override (applies to every CPU)

    def __post_init__(self) -> None:
        if isinstance(self.busy_polls, bool) or not isinstance(self.busy_polls, int) or self.busy_polls < 0:
            raise ValueError(f"busy_polls must be an int >= 0, got {self.busy_polls!r}")
        for name in ("fused_point", "favored_word", "granular_word", "max_voltage_cpu"):
            for cpu, word in getattr(self, name).items():
                _u32(f"{name}[{cpu}]", word)
        _u32("granular_default", self.granular_default)
        for (kind, point), ratio in self.point_ratios.items():
            if kind not in ("P", "E") or isinstance(point, bool) or not isinstance(point, int) or not 1 <= point <= _MAX_POINT:
                raise ValueError(f"point_ratios key {(kind, point)!r}: expected ('P'|'E', 1..{_MAX_POINT})")
            _u8(f"point_ratios[{(kind, point)}]", ratio)
        for (kind, point), raw in self.point_offsets.items():
            if kind not in ("P", "E") or isinstance(point, bool) or not isinstance(point, int) or not 1 <= point <= _MAX_POINT:
                raise ValueError(f"point_offsets key {(kind, point)!r}: expected ('P'|'E', 1..{_MAX_POINT})")
            VfWord(0, 0, False, raw)  # range-checks the signed 11-bit offset
        for (cpu, point), ratio in self.cpu_point_ratios.items():
            if isinstance(point, bool) or not isinstance(point, int) or not 1 <= point <= _MAX_POINT:
                raise ValueError(f"cpu_point_ratios key {(cpu, point)!r}: expected (cpu, 1..{_MAX_POINT})")
            _u8(f"cpu_point_ratios[{(cpu, point)}]", ratio)
        for (cpu, point), raw in self.cpu_point_offsets.items():
            if isinstance(point, bool) or not isinstance(point, int) or not 1 <= point <= _MAX_POINT:
                raise ValueError(f"cpu_point_offsets key {(cpu, point)!r}: expected (cpu, 1..{_MAX_POINT})")
            VfWord(0, 0, False, raw)
        if self.point_counts is not None:
            for kind, count in self.point_counts.items():
                if kind not in ("P", "E") or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= _MAX_POINT:
                    raise ValueError(f"point_counts[{kind!r}] = {count!r}: expected 'P'|'E' -> 0..{_MAX_POINT}")
        for msr, word in self.plain_msr_values.items():
            check_msr(msr)
            check_value(word)
        for key, word in self.fixed_reads.items():
            cmd, p1, p2 = (_u8("fixed_reads key", k) for k in key)
            if is_write(cmd) or cmd in _MODELLED_READS:
                raise ValueError(f"fixed_reads cannot answer cmd {cmd:#04x}: it is a write or has its own per-CPU model")
            _u32(f"fixed_reads[{key}]", word)

    @classmethod
    def measured(cls, **overrides: object) -> SimConfig:
        """The machine as MEASURED on Windows (``measured_baseline``, ``[MEASURED win 2026-10-01]``): per-core ``0x10`` word ``0x0``
        except CPU 0 (``0x39``), ``0x14`` = 8, fused points, favored words, granular index 3, per-core ``0x28``, GT/ring/NGU words,
        capability/IccMax/... reads. ``overrides`` replace any field (e.g. ``reject_zero_fields=True``, ``e_cluster_wide=True``).
        Rule behaviour (rc for a violation, ratio caps, ...) is still the simulator's own SIM-ONLY model."""
        m = measured_baseline
        topo = overrides.get("topology")
        topo = topo if isinstance(topo, Topology) else arl_s_285k()
        fields: dict[str, object] = {
            "misc_global_config": m.MISC_GLOBAL_CONFIG,
            "initial_vf": {cpu: VfWord.from_u32(word) for cpu, word in m.VF_DOMAIN0.items()},
            "initial_domain_vf": {domain: VfWord.from_u32(word) for domain, word in m.DOMAIN_VF.items()},
            "fused_point": dict(m.FUSED_POINT),
            "favored_word": dict(m.FAVORED_WORD),
            "granular_word": dict(m.GRANULAR_WORD),
            "max_voltage_cpu": dict(m.MAX_VOLTAGE_RAW),
            "fixed_reads": dict(m.FIXED_READS),
            "point_counts": dict(POINT_COUNTS_ARL_S),
            "point_ratios": dict(POINT_RATIOS_ARL_S),  # T-026: the real P / E tables with the BIOS offsets
            # G-117: cold-boot stock has runtime point offsets 0 (as on the machine today); the old BIOS-like offsets are opt-in:
            # ``SimConfig.measured(point_offsets=dict(POINT_OFFSETS_ARL_S))``.
            "point_offsets": {},
            "oc_adds_point": True,  # G-120 [MEASURED lnx 2026-10-11]: OC ratio 48 on E added P2 = 7; at stock P2 = top+1 answers rc 0x11
            "ring_point_ratios": dict(RING_POINT_RATIOS_ARL_S),  # T-028
            "ring_point_offsets": dict(RING_POINT_OFFSETS_ARL_S),
            # G-114 (SPEC-v3 F1): every P-core carries the SAME eight points (8,20,30,48,53,55,56,57x) [MEASURED lnx 2026-10-02, platform-285k.md section 3];
            # non-favored cores simply cannot reach 56/57x. The T-026 SIM-ONLY per-core table (POINT_RATIOS_P_NONFAVORED_SIM) is opt-in via ``cpu_point_ratios``.
        }
        fields.update(overrides)
        return cls(**fields)  # type: ignore[arg-type]


@dataclass(frozen=True)
class SimRequest:
    """A mailbox request the simulator latched (decoded from the 0x150 word)."""

    cpu: int
    cmd: int
    p1: int
    p2: int
    data: int


@dataclass(frozen=True)
class SimCpuState:
    """Snapshot of one CPU's simulated state (``SimMsrPort.state``)."""

    cpu: int
    vf: VfWord  # domain 0, P2 = 0
    busy: bool  # 0x150 currently reads busy
    busy_polls_left: int
    response: int  # raw response word a non-busy read returns


class _Mailbox:
    __slots__ = ("hung", "latched", "polls_left", "response")

    def __init__(self) -> None:
        self.polls_left = 0
        self.hung = False  # stuck_busy(before_write=False): busy forever after the next accepted request
        self.latched = 0  # last request word, bit 63 cleared
        self.response = 0  # last response word (rc << 32 | data)


class SimMsrPort(MsrPort):
    """Simulated ``MsrPort`` (see module docstring). Test observability: ``writes`` (cpu, msr, value) of every ``wrmsr``
    that reached the device model, ``reads`` (cpu, msr), ``requests`` (``SimRequest``, only latched requests), ``state(cpu)``,
    ``vf(...)``, counters. Fault injection: ``stuck_busy``, ``eio``, ``force_rc``, ``silent_ignore``, ``clear_faults``.

    CPUs not in ``config.topology`` raise ``MsrError(ENXIO)`` (like a missing ``/dev/cpu/N/msr``); out-of-range arguments
    raise ``ValueError``. A non-mailbox MSR is a plain register only after ``set_msr``; otherwise ``MsrError(EIO)``.
    """

    def __init__(self, config: SimConfig | None = None) -> None:
        self.config = config if config is not None else SimConfig()
        topo = self.config.topology
        self.writes: list[tuple[int, int, int]] = []
        self.reads: list[tuple[int, int]] = []
        self.requests: list[SimRequest] = []
        self.busy_reads = 0
        self._mb = {core.cpu: _Mailbox() for core in topo.cores}
        self._msrs: dict[tuple[int, int], int] = {}
        self._pkg_msrs: dict[int, int] = {}  # T-023: package-scoped plain registers (one value for every CPU)
        self._ignored_plain: set[int] = set()  # T-023 fault: a write of this plain register is accepted but not stored
        if self.config.plain_msrs:
            for msr, default in PLAIN_MSR_DEFAULTS.items():
                value = self.config.plain_msr_values.get(msr, default)
                if self.config.plain_msrs_per_cpu:
                    for core in topo.cores:
                        self._msrs[(core.cpu, msr)] = value
                else:
                    self._pkg_msrs[msr] = value
        self._misc = self.config.misc_global_config & 0xFFFFFFFF
        self._vf: dict[tuple[int, int, int], VfWord] = {}
        self._fused: dict[int, int] = {}  # cpu -> 0x09 P1=0x11 P2=0 word (read-only)
        self._favored: dict[int, int] = {}  # cpu -> 0x1C word (0x1D replaces [7:0])
        #: G-126: called as ``(cpu, cpuinfo_max_khz)`` after a 0x1D took effect, the way the kernel's ``cpuinfo_max_freq`` follows HWP highest (a test or the
        #: ``--sim`` wiring points it at its cpufreq store); ``None`` = nobody listens.
        self.on_favored_write: Callable[[int, int], None] | None = None
        self._granular: dict[int, int] = {}  # cpu -> 0x26 word (0x27 replaces it)
        for core in topo.cores:
            default = _default_core_vf(core.cpu, core.kind, core.cluster)
            start = self.config.initial_vf.get(core.cpu)
            self._vf[(int(Domain.CORE), core.cpu, 0)] = default if start is None else start
            fused = self.config.fused_point.get(core.cpu)
            self._fused[core.cpu] = default.to_u32() if fused is None else fused
            favored = self.config.favored_word.get(core.cpu)
            if favored is None:
                fused_max = self.config.fused_max_ratio.get(core.cpu)
                if fused_max is None:
                    fused_max = _default_fused_max_ratio(core.cpu, core.kind)
                favored = ((fused_max & 0xFF) << 8) | default.ratio
            self._favored[core.cpu] = favored
            self._granular[core.cpu] = self.config.granular_word.get(core.cpu, self.config.granular_default)
        for domain, word in _DEFAULT_DOMAIN_VF.items():
            override = self.config.initial_domain_vf.get(domain)
            self._vf[(domain, -1, 0)] = word if override is None else override
        self._stuck_before: set[int] = set()
        self._stuck_after: set[int] = set()
        self._eio: set[tuple[int, int]] = set()
        self._forced_rc: dict[int, int] = {}
        self._silent: set[int] = set()
        self._handlers: dict[int, Callable[[int, int, int, int], tuple[int, int]]] = {
            Cmd.OC_CAPABILITY_READ: self._cmd_capability,
            Cmd.PER_CORE_CAPABILITY_READ: self._cmd_per_core_capability,
            Cmd.VOLTAGE_FLOOR_READ: self._cmd_floor,
            Cmd.VOLTAGE_FREQ_READ: self._cmd_vf_read,
            Cmd.VOLTAGE_FREQ_WRITE: self._cmd_vf_write,
            Cmd.MISC_GLOBAL_CONFIG_READ: self._cmd_misc_read,
            Cmd.MISC_GLOBAL_CONFIG_WRITE: self._cmd_misc_write,
            Cmd.FAVORED_CORE_READ: self._cmd_favored_read,
            Cmd.FAVORED_CORE_WRITE: self._cmd_favored_write,
            Cmd.GRANULAR_RATIO_READ: self._cmd_granular_read,
            Cmd.GRANULAR_RATIO_WRITE: self._cmd_granular_write,
            Cmd.MAXIMUM_VOLTAGE_LIMIT_READ: self._cmd_max_voltage,
        }

    # ---- MsrPort -----------------------------------------------------------------------------------------------

    @property
    def writable(self) -> bool:
        """Read live from ``config.writable``."""
        return bool(self.config.writable)

    def rdmsr(self, cpu: int, msr: int) -> int:
        cpu = check_cpu(cpu)
        msr = check_msr(msr)
        self._require_cpu(cpu)
        self.reads.append((cpu, msr))
        self._maybe_eio(cpu, msr, "rdmsr")
        if msr == MSR_OC_MAILBOX:
            return self._read_mailbox(cpu)
        if (cpu, msr) in self._msrs:
            return self._msrs[(cpu, msr)]
        if msr in self._pkg_msrs:
            return self._pkg_msrs[msr]
        raise MsrError(errno.EIO, f"sim rdmsr cpu={cpu} msr={msr:#x}: MSR not configured")

    def wrmsr(self, cpu: int, msr: int, value: int) -> None:
        cpu = check_cpu(cpu)
        msr = check_msr(msr)
        value = check_value(value)
        if not self.writable:
            raise MsrError(errno.EPERM, f"sim wrmsr cpu={cpu} msr={msr:#x}: port is read-only")
        self._require_cpu(cpu)
        self.writes.append((cpu, msr, value))
        self._maybe_eio(cpu, msr, "wrmsr")
        if msr == MSR_OC_MAILBOX:
            self._write_mailbox(cpu, value)
        elif msr in PLAIN_MSR_DEFAULTS and self.config.plain_msrs and msr not in PLAIN_MSR_WRITABLE:
            raise MsrError(errno.EIO, f"sim wrmsr cpu={cpu} msr={msr:#x}: read-only register (#GP)")
        elif msr in self._ignored_plain and ((cpu, msr) in self._msrs or msr in self._pkg_msrs):
            pass  # fault injected by ``ignore_plain_writes``: accepted, not stored
        elif (cpu, msr) in self._msrs:
            self._msrs[(cpu, msr)] = value
        elif msr in self._pkg_msrs:
            self._pkg_msrs[msr] = value
        else:
            raise MsrError(errno.EIO, f"sim wrmsr cpu={cpu} msr={msr:#x}: MSR not configured")

    # ---- test setup / observability -----------------------------------------------------------------------------

    @property
    def rdmsr_count(self) -> int:
        return len(self.reads)

    @property
    def wrmsr_count(self) -> int:
        return len(self.writes)

    @property
    def misc_global_config(self) -> int:
        """Package-wide 0x14/0x15 word."""
        return self._misc

    @property
    def per_core_enabled(self) -> bool:
        """Bit 3 (PerCore Voltage Enable) of the misc global config."""
        return bool(self._misc & _PER_CORE_VOLTAGE_ENABLE)

    def set_msr(self, cpu: int, msr: int, value: int) -> None:
        """Make a non-mailbox MSR readable (and writable) on ``cpu`` with ``value`` (simulator setup, not a device access)."""
        cpu = check_cpu(cpu)
        msr = check_msr(msr)
        value = check_value(value)
        if msr == MSR_OC_MAILBOX:
            raise ValueError("MSR 0x150 is the simulated mailbox; it cannot be set with set_msr")
        if cpu not in self._mb:
            raise ValueError(f"cpu {cpu} is not in the simulated topology")
        self._msrs[(cpu, msr)] = value

    def plain_msr(self, cpu: int, msr: int) -> int:
        """The plain register ``msr`` as ``cpu`` sees it (a test-double peek that is not logged in ``reads``); ``ValueError`` if it is
        not modelled."""
        cpu, msr = check_cpu(cpu), check_msr(msr)
        if cpu not in self._mb:
            raise ValueError(f"cpu {cpu} is not in the simulated topology")
        if (cpu, msr) in self._msrs:
            return self._msrs[(cpu, msr)]
        if msr in self._pkg_msrs:
            return self._pkg_msrs[msr]
        raise ValueError(f"plain MSR {msr:#x} is not modelled")

    def ignore_plain_writes(self, msr: int) -> None:
        """Fault injection: ``wrmsr`` of the plain register ``msr`` succeeds but does not change it (models "read-back differs").
        Cleared by ``clear_faults``."""
        self._ignored_plain.add(check_msr(msr))

    def vf(self, cpu: int, domain: int = 0, p2: int = 0) -> VfWord:
        """Stored V/F word: domain 0 per ``cpu``; other domains are package-wide (``cpu`` ignored). Unwritten points are zero, except a domain-0
        point with a configured ``point_ratios`` entry (T-025): that ratio, V target 0, and the offset of ``cpu_point_offsets`` / ``point_offsets`` (T-026), else 0
        (what ``0x10 P2=6`` read on the E CPU)."""
        stored = self._vf.get(self._vf_key(domain, cpu, p2))
        if stored is not None:
            return stored
        if domain == Domain.RING and p2 != 0 and p2 in self.config.ring_point_ratios:  # T-028
            return VfWord(ratio=self.config.ring_point_ratios[p2], v_target_raw=0, static=False, offset_raw=self.config.ring_point_offsets.get(p2, 0))
        if domain == Domain.CORE and p2 != 0:
            kind = self._kind(cpu)
            ratio = self._point_ratio(cpu, p2)
            if p2 == self._extra_point(cpu):  # T-026: the extra point an OC ratio above the top fused ratio creates
                ratio = self._stored_oc(cpu).ratio
            offset = self.config.cpu_point_offsets.get((cpu, p2), self.config.point_offsets.get((kind, p2), 0))
            return VfWord(ratio=ratio, v_target_raw=0, static=False, offset_raw=offset)
        return _ZERO_VF

    def _point_ratio(self, cpu: int, p2: int) -> int:
        return self.config.cpu_point_ratios.get((cpu, p2), self.config.point_ratios.get((self._kind(cpu), p2), 0))

    def _stored_oc(self, cpu: int) -> VfWord:
        return self._vf.get(self._vf_key(Domain.CORE, cpu, 0), _ZERO_VF)

    def _fused_count(self, cpu: int) -> int:
        counts = self.config.point_counts
        return _MAX_POINT if counts is None else counts.get(self._kind(cpu), _MAX_POINT)

    def _top_fused_ratio(self, cpu: int) -> int | None:
        """The highest fused point ratio of ``cpu``'s core kind, or ``None`` when the point tables are not modelled (legacy config)."""
        if self.config.point_counts is None:
            return None
        return max((self._point_ratio(cpu, n) for n in range(1, self._fused_count(cpu) + 1)), default=None)

    def _extra_point(self, cpu: int) -> int | None:
        """T-026 (``oc_adds_point``): the P2 of the additional point when the stored OC ratio is above the top fused ratio, else ``None``."""
        if not self.config.oc_adds_point or self.config.point_counts is None:
            return None
        count = self._fused_count(cpu)
        top = max((self._point_ratio(cpu, n) for n in range(1, count + 1)), default=0)
        return count + 1 if count < _MAX_POINT and self._stored_oc(cpu).ratio > top else None

    def favored_word(self, cpu: int) -> int:
        """The 0x1C word of ``cpu`` (``[7:0]`` favored ratio, ``[15:8]`` fused); a test-double peek, not a mailbox request."""
        return self._peek(self._favored, cpu)

    def cpuinfo_max_khz(self, cpu: int) -> int:
        """``cpuinfo_max_freq`` the kernel reports for ``cpu`` after its last 0x1D: HWP highest = the 0x1C ``[7:0]`` ratio; a P-core shows ratio x 100 MHz, an E-core
        one bin more (intel_pstate N -> N-1 mapping: 48x reads 4.9 GHz, 49x 5.0 GHz, ``[MEASURED lnx 2026-10-12]``). A test-double peek, not a mailbox request."""
        ratio = self.favored_word(cpu) & 0xFF
        return (ratio + (1 if self._kind(cpu) == "E" else 0)) * 100_000

    def fused_word(self, cpu: int) -> int:
        """The 0x09 P1=0x11 P2=0 word of ``cpu`` (fused V/F point; ``floors[0]`` overrides it on the wire)."""
        return self._peek(self._fused, cpu)

    def granular_word(self, cpu: int) -> int:
        """The 0x26 word of ``cpu``."""
        return self._peek(self._granular, cpu)

    def _peek(self, table: dict[int, int], cpu: int) -> int:
        cpu = check_cpu(cpu)
        if cpu not in self._mb:
            raise ValueError(f"cpu {cpu} is not in the simulated topology")
        return table[cpu]

    def state(self, cpu: int) -> SimCpuState:
        """Snapshot of ``cpu`` (``ValueError`` if it is not in the simulated topology)."""
        cpu = check_cpu(cpu)
        if cpu not in self._mb:
            raise ValueError(f"cpu {cpu} is not in the simulated topology")
        mb = self._mb[cpu]
        return SimCpuState(cpu, self.vf(cpu), self._mailbox_busy(cpu), mb.polls_left, mb.response)

    def clear_log(self) -> None:
        """Forget ``writes``, ``reads``, ``requests`` and the busy-read counter (state and faults are kept)."""
        self.writes.clear()
        self.reads.clear()
        self.requests.clear()
        self.busy_reads = 0

    # ---- fault injection -----------------------------------------------------------------------------------------

    def stuck_busy(self, cpu: int, before_write: bool = False) -> None:
        """``before_write=True``: 0x150 on ``cpu`` reads busy forever (a mailbox write then fails with EIO, so a transport
        aborts with "busy before write"). ``False``: the next request is accepted and recorded but never completes or
        executes; 0x150 stays busy ("busy after write"). Cleared by ``clear_faults``."""
        cpu = check_cpu(cpu)
        if cpu not in self._mb:
            raise ValueError(f"cpu {cpu} is not in the simulated topology")
        (self._stuck_before if before_write else self._stuck_after).add(cpu)

    def eio(self, cpu: int, msr: int) -> None:
        """Every ``rdmsr``/``wrmsr`` of (``cpu``, ``msr``) raises ``MsrError(EIO)`` (writes are still logged in ``writes``)."""
        self._eio.add((check_cpu(cpu), check_msr(msr)))

    def force_rc(self, cmd: int, rc: int) -> None:
        """Every request with command ``cmd`` completes with ``rc`` and data 0, without touching state."""
        self._forced_rc[_u8("cmd", cmd)] = _u8("rc", rc)

    def silent_ignore(self, cmd: int) -> None:
        """Requests of the modelled write command ``cmd`` (0x11, 0x15, 0x1D, 0x27) that pass every check complete ``Rc.Pass`` with
        the normal response data but leave the state unchanged (models "read-back does not equal written")."""
        cmd = _u8("cmd", cmd)
        if cmd not in _SILENT_CMDS:
            raise ValueError(f"silent_ignore supports the modelled write commands 0x11, 0x15, 0x1D and 0x27, not {cmd:#x}")
        self._silent.add(cmd)

    def clear_faults(self) -> None:
        """Remove every injected fault, including hung mailboxes."""
        self._stuck_before.clear()
        self._stuck_after.clear()
        self._eio.clear()
        self._forced_rc.clear()
        self._silent.clear()
        self._ignored_plain.clear()
        for mb in self._mb.values():
            mb.hung = False

    # ---- internals: device level ---------------------------------------------------------------------------------

    def _require_cpu(self, cpu: int) -> None:
        if cpu not in self._mb:
            raise MsrError(errno.ENXIO, f"sim: no /dev/cpu/{cpu}/msr (cpu not in the simulated topology)")

    def _maybe_eio(self, cpu: int, msr: int, op: str) -> None:
        if (cpu, msr) in self._eio:
            raise MsrError(errno.EIO, f"sim {op} cpu={cpu} msr={msr:#x}: injected EIO")

    def _mailbox_busy(self, cpu: int) -> bool:
        mb = self._mb[cpu]
        return cpu in self._stuck_before or mb.hung or mb.polls_left > 0

    def _read_mailbox(self, cpu: int) -> int:
        mb = self._mb[cpu]
        if cpu in self._stuck_before or mb.hung:
            self.busy_reads += 1
            return mb.latched | _BUSY
        if mb.polls_left > 0:
            mb.polls_left -= 1
            self.busy_reads += 1
            return mb.latched | _BUSY
        return mb.response

    def _write_mailbox(self, cpu: int, value: int) -> None:
        mb = self._mb[cpu]
        if not is_busy(value):
            raise MsrError(errno.EIO, f"sim wrmsr cpu={cpu} msr=0x150: request without run/busy bit 63")
        if self._mailbox_busy(cpu):
            raise MsrError(errno.EIO, f"sim wrmsr cpu={cpu} msr=0x150: mailbox busy")
        cmd = (value >> 32) & 0xFF
        p1 = (value >> 40) & 0xFF
        p2 = (value >> 48) & 0xFF
        data = value & 0xFFFFFFFF
        self.requests.append(SimRequest(cpu, cmd, p1, p2, data))
        mb.latched = value & ~_BUSY
        if cpu in self._stuck_after:
            mb.hung = True
            return
        rc, out = self._execute(cpu, cmd, p1, p2, data)
        mb.response = (rc << 32) | out
        mb.polls_left = int(self.config.busy_polls)

    # ---- internals: pcode model ----------------------------------------------------------------------------------

    def _execute(self, cpu: int, cmd: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        forced = self._forced_rc.get(cmd)
        if forced is not None:
            return forced, 0
        fixed = self.config.fixed_reads.get((cmd, p1, p2))
        if fixed is not None:
            return int(Rc.Pass), fixed & 0xFFFFFFFF
        handler = self._handlers.get(cmd)
        if handler is None:
            return int(Rc.UnrecognizedCommand), 0
        return handler(cpu, p1, p2, data)

    def _kind(self, cpu: int) -> str:
        return self.config.topology.by_cpu(cpu).kind

    def _cmd_capability(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x01: [7:0] max OC ratio (SIM: the same cap 0x11 enforces), [10:8] flags. P1=3 -> OCNotSupported."""
        if p2 != 0:
            return int(Rc.InvalidParam), 0
        if p1 == Domain.GT_UNSLICE:
            return int(Rc.OCNotSupported), 0
        if p1 == Domain.CORE:
            ratio = self.config.oc_cap_ratio[self._kind(cpu)]
        elif p1 in self.config.domain_cap_ratio:
            ratio = self.config.domain_cap_ratio[p1]
        else:
            return int(Rc.UnsupportedDomain), 0
        return int(Rc.Pass), (ratio & 0xFF) | ((self.config.capability_flags & 0x7) << 8)

    def _cmd_per_core_capability(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x02: fixed configured word; P1 = n/4 (0..5 for 24 CPUs), P2 0 or 1."""
        if p2 not in (0, 1) or p1 > 5:
            return int(Rc.InvalidParam), 0
        return int(Rc.Pass), self.config.per_core_capability_word & 0xFFFFFFFF

    def _cmd_max_voltage(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x28: [11:0] max voltage S2.10 for the domain."""
        if p2 != 0:
            return int(Rc.InvalidParam), 0
        if p1 not in _VF_DOMAINS:
            return int(Rc.UnsupportedDomain), 0
        return int(Rc.Pass), self._max_voltage(cpu, p1) & 0xFFF

    def _max_voltage(self, cpu: int, domain: int) -> int:
        """0x28 answer and 0x11 V-target limit (S2.10 raw): per CPU for domain 0 when configured, else ``max_voltage_raw``."""
        if domain == Domain.CORE:
            return self.config.max_voltage_cpu.get(cpu, self.config.max_voltage_raw)
        return self.config.max_voltage_raw

    def _cmd_favored_read(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x1C: ``[7:0]`` favored ratio, ``[15:8]`` fused max ratio (the stored per-CPU word; 0x1D replaces ``[7:0]``)."""
        if p1 != 0 or p2 != 0:
            return int(Rc.InvalidParam), 0
        return int(Rc.Pass), self._favored[cpu]

    def _cmd_favored_write(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x1D: the favored-core word = the ratio (no RMW). SIM-ONLY rules: ``oc_locked``, data > 0xFF, a ratio above the core-kind
        cap, and (``reject_zero_fields``) 0 are refused. Success replaces ``[7:0]`` of the 0x1C word ([UNVERIFIED] on hardware)."""
        cfg = self.config
        if p1 != 0 or p2 != 0:
            return int(Rc.InvalidParam), 0
        if cfg.oc_locked:
            return int(Rc.OCLocked), 0
        if data > 0xFF or (cfg.reject_zero_fields and data == 0):
            return int(Rc.InvalidParam), 0
        if data > cfg.oc_cap_ratio[self._kind(cpu)]:
            return int(Rc.RatioExceedsLimits), 0
        if Cmd.FAVORED_CORE_WRITE not in self._silent:
            self._favored[cpu] = (self._favored[cpu] & ~0xFF) | data
            if self.on_favored_write is not None:
                self.on_favored_write(cpu, self.cpuinfo_max_khz(cpu))
        return int(Rc.Pass), data

    def _cmd_granular_read(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x26: the per-CPU granular word (measured: 3 on every core; 0x27 replaces it)."""
        if p1 != 0 or p2 != 0:
            return int(Rc.InvalidParam), 0
        return int(Rc.Pass), self._granular[cpu]

    def _cmd_granular_write(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x27: store the word that 0x26 then returns ([UNVERIFIED] semantics on hardware). SIM-ONLY: ``oc_locked`` and data above
        0xFFFF (only ``[15:8]``/``[2:0]`` are defined, registers.md) are refused."""
        if p1 != 0 or p2 != 0:
            return int(Rc.InvalidParam), 0
        if self.config.oc_locked:
            return int(Rc.OCLocked), 0
        if data > 0xFFFF:
            return int(Rc.InvalidParam), 0
        if Cmd.GRANULAR_RATIO_WRITE not in self._silent:
            self._granular[cpu] = data
        return int(Rc.Pass), data

    def _cmd_floor(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x09 P1=0x11: P2 = domain. P2=0 (core): ``floors[0]`` if configured, else the per-CPU fused V/F point (measured: ratio
        ``[7:0]`` + fused voltage ``[19:8]``); other domains: ``floors`` or 0. Other P1 -> Rc 0x08."""
        if p1 != 0x11:
            return int(Rc.IllegalCommandSelectedInParam1), 0
        if p2 not in _VF_DOMAINS:
            return int(Rc.UnsupportedDomain), 0
        if p2 == Domain.CORE and p2 not in self.config.floors:
            return int(Rc.Pass), self._fused[cpu] & 0xFFFFFFFF
        return int(Rc.Pass), self.config.floors.get(p2, 0) & 0xFFFFFFFF

    def _cmd_misc_read(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        if p1 != 0 or p2 != 0:
            return int(Rc.InvalidParam), 0
        return int(Rc.Pass), self._misc

    def _cmd_misc_write(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        if p1 != 0 or p2 != 0:
            return int(Rc.InvalidParam), 0
        if self.config.oc_locked:
            return int(Rc.OCLocked), 0
        if Cmd.MISC_GLOBAL_CONFIG_WRITE not in self._silent:
            self._misc = data
        return int(Rc.Pass), data

    @staticmethod
    def _vf_key(domain: int, cpu: int, p2: int) -> tuple[int, int, int]:
        return (domain, cpu if domain == Domain.CORE else -1, p2)

    def _vf_address_rc(self, cpu: int, p1: int, p2: int) -> int | None:
        if p1 not in _VF_DOMAINS:
            return int(Rc.UnsupportedDomain)
        if p2 != 0 and not (self.config.per_point and 1 <= p2 <= _MAX_POINT):
            return int(Rc.InvalidParam)
        counts = self.config.point_counts
        extra = self._extra_point(cpu) if p1 == Domain.CORE else None
        if p2 != 0 and p1 == Domain.CORE and counts is not None and p2 > counts.get(self._kind(cpu), _MAX_POINT) and p2 != extra:
            return RC_NO_SUCH_POINT  # T-025: past the last fused point of this core kind
        if p2 != 0 and p1 == Domain.RING and self.config.ring_point_ratios and p2 not in self.config.ring_point_ratios:
            return RC_NO_SUCH_POINT  # T-028: past the last ring point
        return None

    def _cmd_vf_read(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        bad = self._vf_address_rc(cpu, p1, p2)
        if bad is not None:
            return bad, 0
        return int(Rc.Pass), self.vf(cpu, p1, p2).to_u32()

    def _cmd_vf_write(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
        """0x11 rules, checked in this order: address, oc_locked, per-core enable (domain 0), zero fields (domain 0, only with
        ``reject_zero_fields``), ratio cap, voltage limit, undervolt protection, static mode. Success stores the word and answers
        with it."""
        cfg = self.config
        bad = self._vf_address_rc(cpu, p1, p2)
        if bad is not None:
            return bad, 0
        if cfg.oc_locked:
            return int(Rc.OCLocked), 0
        if p1 == Domain.CORE and cfg.per_core_requires_enable and not self.per_core_enabled:
            return int(Rc.OCLocked), 0
        word = VfWord.from_u32(data)
        top = (self._top_fused_ratio(cpu) or max(_PLATFORM.p_point_ratios if self._kind(cpu) == "P" else _PLATFORM.e_point_ratios)) if p1 == Domain.CORE else None
        zero_target = word.v_target_raw == 0 and not (top is not None and 0 < word.ratio <= top)  # G-120: no OC point (ratio <= top fused) = ratio | offset << 21, target 0 is the shape the planner writes
        if cfg.reject_zero_fields and p1 == Domain.CORE and p2 == 0 and (word.ratio == 0 or zero_target):  # T-025: a point word has V target 0 (OCCT, measured)
            return int(Rc.InvalidParam), 0  # models the UNVERIFIED acceptance: XTU never sent a zero ratio / V target
        cap = cfg.oc_cap_ratio[self._kind(cpu)] if p1 == Domain.CORE else cfg.domain_cap_ratio.get(p1)
        if cap is not None and word.ratio > cap:
            return int(Rc.RatioExceedsLimits), 0
        if word.v_target_raw > self._max_voltage(cpu, p1):
            return int(Rc.VoltageExceedsLimit), 0
        if cfg.uvp and word.offset_raw < 0:
            return int(Rc.UndervoltProtection), 0
        if word.static and not cfg.allow_static:
            return int(Rc.InvalidParam), 0
        if p1 == Domain.CORE and p2 == 0 and cfg.drop_target_without_oc_point:
            top = self._top_fused_ratio(cpu)
            if top is not None and word.ratio <= top:  # no OC point: the hardware keeps ratio + offset only ([MEASURED lnx 2026-10-11]: 0xFEC5B439 read back 0xFEC00039)
                word = VfWord(word.ratio, 0, word.static, word.offset_raw)
        if Cmd.VOLTAGE_FREQ_WRITE not in self._silent:
            for target in self._scope(cpu, p1):
                self._vf[self._vf_key(p1, target, p2)] = word
        return int(Rc.Pass), word.to_u32()

    def _scope(self, cpu: int, domain: int) -> tuple[int, ...]:
        """CPUs whose domain-0 state a write addressed to ``cpu`` updates (whole E cluster if ``e_cluster_wide``)."""
        if domain == Domain.CORE and self.config.e_cluster_wide:
            core = self.config.topology.by_cpu(cpu)
            if core.kind == "E" and core.cluster is not None:
                return self.config.topology.cluster_cpus(core.cluster)
        return (cpu,)


def _u32(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name}={value!r} must be an int in 0..0xffffffff")
    return int(value)


def _u8(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise ValueError(f"{name}={value!r} must be an int in 0..0xff")
    return int(value)
