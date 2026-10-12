"""Contract 9 (docs/ARCHITECTURE.md, SPEC-v3) for the fake helper: ``mem.read``, ``vf.measure``, ``curves.*``, ``tune.*``, ``profiles.*``.

No hardware and no real helper code. Numbers are synthetic but shaped like the machine: P ~0.70 -> 1.40 V, E ~0.65 -> 1.15 V; the memory fixture is the
measured one in ``docs/knowledge/memory-mchbar.md``. ``FakeHelper`` inherits ``V3Ops`` and calls ``handle_v3`` for the ops listed in ``V3_OPS``.

Assumptions the web page binds to (not pinned by contract 9; reported in the G-113 handoff):
  * ``tune.read`` keeps the ``vf.dump`` document shape (units -> cpus -> ``oc`` / ``points``); the page derives the tuning unit from ``kind`` and
    ``cluster_cpus`` (P: cpu n -> ``Pn``; E: first cluster cpu -> ``EC<(cpu-8)//4>``).
  * ``tune.plan`` ``before`` / ``after``: ``{unit: [{point, ratio, offset_mv, v_target_v}]}``; ``point`` 0 is the OC word (vfdump convention).
  * ``tune.apply`` ``readback``: ``[{unit, point, expected, actual, match}]`` with the same point numbering.
  * ``tune.plan`` may carry ``gates: [{name, status}]`` (SPEC §5 badges); the page shows them when present and invents nothing when absent.
  * ``curves.list`` rows carry ``tuning_state`` (object with ``kind``) and ``created`` as ISO-8601 UTC; ``vf.point`` / ``progress`` carry ``op_id``.

G-111 deviations from contract 9 (orchestrator, 2026-10-11), mirrored here:
  * ``vf.measure`` / ``curves.get`` are budgeted to the 64 KiB line: a record may carry ``omitted_units: [unit]``; ``curves.get {id, units: [unit]}`` returns those.
    (``FakeHelper.record_budget_units = N`` switches the budget on; default ``None`` = whole records.)
  * ``curves.update`` returns the list row; ``vf.measure`` may add ``problems: [str]`` (``measure_problems``).
  * ``tuning_state.kind`` is ``"words"`` (a ``tune.read`` this boot), ``"unknown"``, or (G-118) ``"stock"`` (``seed_curve(offset_mv=0.0)``); a point of a
    fused V/F ratio carries ``fused_point`` (1-based int, ``"oc"`` above the table), the in-between ratios of a dense run carry none.
  * G-119: ``stress.start`` / ``stress.stop`` / ``stress.list`` with the contract 9 "G-116" shapes (``FakeHelper.stress_delay`` seconds per 2 s of the screen;
    ``stress_errors`` / ``stress_lines`` / ``stress_health`` shape the result; ``stress_gate`` holds a run open).
  * On cancel / refusal the error ``detail`` carries the scalar ``curve_id`` of the stored partial curve (the server strips non-scalars); new
    ``detail.check`` values NO_CPUFREQ, LOAD_GENERATOR, MEASURE_UNAVAILABLE, JOURNAL_UNAVAILABLE, CURVE_LIMIT, EXISTS, NO_SUCH_CURVE.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from arltuner.helper.ops_measure import interpolate_points

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789"
UNITS = [f"P{i}" for i in range(8)] + [f"EC{i}" for i in range(4)]
UNIT_CPUS = {**{f"P{i}": [i] for i in range(8)}, **{f"EC{i}": list(range(8 + 4 * i, 12 + 4 * i)) for i in range(4)}}


def set_layout(p_cores: int = 8, e_clusters: list[list[int]] | None = None) -> None:
    """The CPU layout the fake simulates, in place (UNITS / UNIT_CPUS are module globals): ``p_cores`` P-cores on cpus 0..n-1 and the E-clusters as cpu lists
    (default: four clusters of four from cpu 8, the 8P+16E reference layout). G-130: e.g. ``set_layout(6, [[6, 7, 8, 9], [10, 11, 12, 13]])`` = 6P + 2 E-clusters."""
    clusters = [list(range(8 + 4 * i, 12 + 4 * i)) for i in range(4)] if e_clusters is None else [list(c) for c in e_clusters]
    UNITS[:] = [f"P{i}" for i in range(p_cores)] + [f"EC{i}" for i in range(len(clusters))]
    UNIT_CPUS.clear()
    UNIT_CPUS.update({**{f"P{i}": [i] for i in range(p_cores)}, **{f"EC{i}": c for i, c in enumerate(clusters)}})


DEFAULT_FAVORED = ("P0", "P3")   # a simulator fixture, not the page's knowledge: the P-cores the fake reports as favored; FakeHelper.favored changes it
FAVORED = DEFAULT_FAVORED
TOPOLOGY = {"source": "acpi_cppc", "favored": None}   # what preflight's topology says: favored_source (also with no favored core: the source judged every P-core equal; None = no source
# could judge) and the favored set (None = the same as FAVORED, which also shapes the 0x1C ratios of a read)
P_QUICK = [8, 20, 30, 48, 53, 55, 56, 57]
E_QUICK = [8, 15, 23, 30, 40, 46]
P_POINTS = [8, 20, 30, 48, 53, 55, 56, 57]   # F1: every P-core carries these 8 points; no per-core tables [MEASURED lnx 2026-10-02]
E_POINTS = [8, 15, 23, 30, 40, 46]
E_TURBO_TOP = 48                               # E 47-48x come from the turbo table, on point 6's voltage (F2/F4); not an OC point
P_REACH = {"favored": 57, "other": 55}         # F3: non-favored P-cores top out at 55x although they carry the 56/57x points
CPUFREQ_UNPINNABLE = {"E": (23,), "P": (52,)}  # F9: caps land on a neighbour
RATIO_MAX = {"P": 57, "E": 49}
V_TARGET_MAX = {"P": 1.625, "E": 1.355}
V_TARGET_MIN = 0.900
OFFSET_LIMIT = 200
CURVE_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-z0-9]{4}\Z")
PROFILE_NAME = re.compile(r"[a-z0-9._-]{1,64}\Z")
V3_OPS = frozenset({"mem.read", "vf.measure", "curves.list", "curves.get", "curves.update", "curves.delete", "tune.read", "tune.plan", "tune.apply",
                    "profiles.list", "profiles.save", "profiles.accept", "profiles.get", "tune.last", "stress.start", "stress.stop", "stress.list"})


class W(str):
    """A warning with its code (``helper/ops_tune.py`` ``Warn``): sent as ``{code, text}``; a plain str is a NOTE."""

    def __new__(cls, code: str, text: str):
        obj = super().__new__(cls, text)
        obj.code = code
        return obj


def warning_objects(items) -> list[dict]:
    return [{"code": getattr(w, "code", "NOTE"), "text": str(w)} for w in items]


def kind_of(unit: str) -> str:
    return "P" if unit.startswith("P") else "E"


def unit_ratios(unit: str) -> list[int]:
    """The V/F point ratios of a unit: fixed per domain (F1), never per core."""
    return list(E_POINTS if kind_of(unit) == "E" else P_POINTS)


def reach_ratio(unit: str) -> int:
    """The highest ratio the unit can hold under load (F3): P0/P3 57x, the other P-cores 55x, E-clusters the turbo table top."""
    if kind_of(unit) == "E":
        return E_TURBO_TOP
    return P_REACH["favored"] if unit in FAVORED or not FAVORED else P_REACH["other"]   # no favored core: all P-cores are equal and reach the top


def unreachable_reason(unit: str, ratio: int) -> str | None:
    """Why a measurement at ``ratio`` ends ``unreachable`` (F3/F9), or None when it can be pinned."""
    if ratio > reach_ratio(unit):
        return "max_turbo"
    if ratio in CPUFREQ_UNPINNABLE[kind_of(unit)]:
        return "cap_granularity"
    return None


def measure_ratios(unit: str, mode: str) -> list[int]:
    """Ratios a measurement visits: Quick = the V/F point ratios of the unit kind (SPEC-v3 section 3), Dense = every ratio from 8 to the top."""
    if mode == "quick":   # F9: the point ratios plus the unit's max turbo ratio when it lies above the top point (E 47, 48x)
        return list(P_QUICK) if kind_of(unit) == "P" else E_QUICK + list(range(E_POINTS[-1] + 1, E_TURBO_TOP + 1))
    return list(range(8, unit_max_ratio(unit) + 1))


def unit_max_ratio(unit: str) -> int:
    return P_POINTS[-1] if kind_of(unit) == "P" else E_TURBO_TOP


def memory_fixture(*, available: bool = True) -> dict:
    """``mem.read`` result: the live values of the reference machine [MEASURED lnx 2026-10-11], DDR5-7200 36-44-44-96."""
    if not available:
        return {"available": False, "reason": "MCHBAR disabled (host bridge 8086:7d1a reports bit 0 clear)"}
    dimms = []
    for i, temp in enumerate((38.5, 39.25)):
        dimms.append({"locator": f"DIMM_A{i + 1}" if i == 0 else "DIMM_B1", "size": "24 GB", "part": "F5-7200J3445G24G", "manufacturer": "G.Skill",
                      "speed": "7200 MT/s", "configured_speed": "7200 MT/s", "voltage": "1.40 V", "rank": 1,
                      "temp_c": {"value": temp, "unit": "C", "source": "spd5118", "status": "ok"}})
    return {"available": True, "mts": 7200, "qclk_mhz": 3600.0, "gear": 2, "cmd_rate": "2T", "bclk_mhz": 100.0,
            "timings": {"tCL": 36, "tRCD": 44, "tRP": 44, "tRAS": 96, "tRC": 140, "tRFC": 574, "tRFCpb": 467, "tREFI": 32767, "tFAW": 32,
                        "tRRD_L": 12, "tRRD_S": 8, "tCWL": 34, "tRTP": 23, "tWR": 110, "tWTP": 152, "tRCDW": 44, "tRPab": 44, "tREFSBRD": 20},
            "turnaround": {"RDRD": {"SG": 18, "DG": 8, "SD": 16, "DD": 16}, "RDWR": {"SG": 23, "DG": 23, "SD": 23, "DD": 23},
                           "WRRD": {"SG": 68, "DG": 48, "SD": 13, "DD": 13}, "WRWR": {"SG": 72, "DG": 8, "SD": 18, "DD": 18}},
            "instances_ok": 6, "cross_check": {"agree": True, "mhz_a": 3600.0, "mhz_b": 3600.0}, "max_bandwidth_mbs": 115200, "dimms": dimms}


def stock_vid(unit: str, ratio: int) -> float:
    """Synthetic stock V/F curve, volts at a ratio (before any offset); a small per-unit silicon skew."""
    skew = ((UNITS.index(unit) * 7) % 5 - 2) * 0.013
    if kind_of(unit) == "P":
        return 0.70 + 0.70 * ((ratio - 8) / 49.0) ** 1.7 + skew
    return 0.65 + 0.50 * ((ratio - 8) / 38.0) ** 1.6 + skew * 0.6


def interp_offset(points: list[dict], ratio: int) -> float:
    """Offset (mV) in force at ``ratio``: linear between V/F points, flat beyond the ends."""
    pts = sorted(points, key=lambda p: p["ratio"])
    if ratio <= pts[0]["ratio"]:
        return pts[0]["offset_mv"]
    for a, b in zip(pts, pts[1:]):
        if ratio <= b["ratio"]:
            return a["offset_mv"] + (b["offset_mv"] - a["offset_mv"]) * (ratio - a["ratio"]) / (b["ratio"] - a["ratio"])
    return pts[-1]["offset_mv"]


def initial_tuning() -> dict:
    """The tuning state at boot: P -25 mV, E -50 mV on every point; the OC word carries the adaptive target (what the cores request at the top ratio)."""
    out = {}
    for u in UNITS:
        off = -25.0 if kind_of(u) == "P" else -50.0
        ratios = unit_ratios(u)
        # the OC word of 23 of 24 cores reads 0 in ratio and V target (unset): None; only P0 carries them (G-112, tune.read)
        # F8: the E 40x point is never offset (a -20 mV there gave OCCT errors on the reference machine)
        out[u] = {"points": [{"point": i + 1, "ratio": r, "offset_mv": 0.0 if (kind_of(u) == "E" and r == 40) else off} for i, r in enumerate(ratios)],
                  "oc": {"point": 0, "ratio": ratios[-1] if u == "P0" else None, "offset_mv": off,
                         "v_target_v": round(stock_vid(u, ratios[-1]) + 0.01, 4) if u == "P0" else None}}
    return out


def _word(unit: str, point: dict) -> str:
    h = hashlib.sha256(f"{unit}:{point['point']}:{point['ratio']}:{point['offset_mv']}".encode()).hexdigest()
    return "0x" + h[:8]


def _entry(unit: str, p: dict) -> dict:
    return {"point": p["point"], "ratio": p["ratio"], "offset_mv": p["offset_mv"], "v_target_v": p.get("v_target_v")}


TURBO_COUNTS = {"P": [1, 2, 3, 4, 5, 6, 7, 8], "E": [1, 2, 3, 4, 6, 8, 12, 16]}   # 0x1AE / 0x651 (read-only by policy)


def fused_of(unit: str) -> dict:
    """F7: the 0x09 P1=0x11 P2=0 fused top-point word of a unit. Synthetic: the voltage is the fake's own stock curve at the top point, so the marker
    cross-checks the seeded baseline; word = ratio[7:0] | V/1024 [19:8] like the real one (e.g. P0 0x5B439 = 57x 1.4258 V)."""
    ratio = unit_ratios(unit)[-1]
    raw = round(stock_vid(unit, ratio) * 1024)
    return {"word": "0x%X" % (ratio | raw << 8), "ratio": ratio, "v": round(raw / 1024, 4)}


def topology_view() -> dict:
    """The ``topology`` object of the G-128 ``preflight`` contract: every tuning unit with its CPUs, the favored flag, the highest ratio it can hold and its CPPC performance."""
    units = []
    for u in UNITS:
        p = kind_of(u) == "P"
        fav = p and u in (FAVORED if TOPOLOGY["favored"] is None else TOPOLOGY["favored"])
        units.append({"id": u, "kind": kind_of(u), "cpus": list(UNIT_CPUS[u]), "favored": fav, "max_ratio": reach_ratio(u), "perf": (255 if fav else 236) if p else None})
    return {"units": units, "favored_source": TOPOLOGY["source"]}


def limits_view(ratio_max: dict) -> dict:
    """The ``limits`` object of the G-133 tune.read / tune.last contract: the bound policy per kind (offsets in mV, V target max in V, ratio max)."""
    return {"offset_mv": {k: {"min": -OFFSET_LIMIT, "max": OFFSET_LIMIT} for k in ("P", "E")},
            "v_target_max_v": dict(V_TARGET_MAX), "ratio_max": dict(ratio_max),
            "source": {"ratio_max": {"P": "auto", "E": "policy"}, "v_target_max": {"P": "auto", "E": "auto"}}}


def read_at_iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def turbo_view(tables: dict) -> dict:
    """The ``turbo`` object of the tune.read addendum: P / E bucket ratios and counts, and the per-core favored ratio (0x1C)."""
    return {"P": {"ratios": list(tables["p"]), "counts": list(TURBO_COUNTS["P"])}, "E": {"ratios": list(tables["e"]), "counts": list(TURBO_COUNTS["E"])},
            "favored": {str(UNIT_CPUS[u][0]): reach_ratio(u) for u in UNITS if u.startswith("P")}}


def predict_unit(rec: dict | None, unit: str, points: list[dict]) -> dict[int, dict]:
    """F4: ``{point: {effective_v, clamped_by}}`` for a unit from the baseline curve ``rec`` and the offsets of ``points``: V(n) = max(base(n) + offset(n), V(n-1)).
    ``clamped_by`` is the point index of the V(n-1) that set the floor (null when not clamped). Points with no measured baseline VID get ``effective_v`` null."""
    base = {}
    if rec and unit in rec["units"]:
        base = {p["ratio"]: p["vid_v"] for p in rec["units"][unit]["points"] if p.get("status") == "ok" and p.get("vid_v") is not None}
    out, prev = {}, None
    for p in sorted(points, key=lambda q: q["point"]):
        b = base.get(p["ratio"])
        if b is None:
            out[p["point"]] = {"effective_v": None, "clamped_by": None}
            continue
        v = b + p["offset_mv"] / 1000.0
        clamped = prev is not None and v < prev[1] - 1e-9
        if clamped:
            v = prev[1]
        out[p["point"]] = {"effective_v": round(v, 4), "clamped_by": prev[0] if clamped else None}
        prev = (p["point"], v)
    return out


class V3Ops:
    """Mixed into ``FakeHelper`` (which provides ``_reply``, ``_err``, ``progress`` / ``emit``)."""

    def set_topology(self, *, source="acpi_cppc", favored=None) -> None:
        """What the preflight topology reports, independent of the 0x1C ratios a read returns (``favored=()`` = the source judged every P-core equal)."""
        TOPOLOGY.update(source=source, favored=None if favored is None else tuple(favored))

    @property
    def favored(self) -> tuple:
        return FAVORED

    @favored.setter
    def favored(self, units) -> None:
        """Which P-cores the fake reports as favored (preflight topology and the ``tune.read`` turbo.favored ratios); ``()`` = none, all P-cores equal."""
        global FAVORED
        FAVORED = tuple(units)

    def set_layout(self, p_cores: int = 8, e_clusters: list[list[int]] | None = None) -> None:
        """Switch the simulated CPU layout (see module ``set_layout``) and rebuild the state derived from the unit list."""
        set_layout(p_cores, e_clusters)
        self.tuning = initial_tuning()
        self.accepted = copy.deepcopy(self.tuning)
        self.fused_vt = {u: round(stock_vid(u, unit_ratios(u)[-1]) + 0.01, 4) for u in UNITS}

    def _init_v3(self) -> None:
        set_layout()
        self.favored = DEFAULT_FAVORED
        TOPOLOGY.update(source="acpi_cppc", favored=None)
        self.topology_enabled = True      # G-128 contract: preflight carries ``topology``; False simulates a helper without it
        self.read_at_enabled = True       # G-128 contract: tune.read / tune.last carry ``read_at`` (ISO UTC)
        self.limits_enabled = True        # G-133 contract: tune.read / tune.last carry ``limits``; False simulates a helper without them
        self.ratio_max = dict(RATIO_MAX)  # the policy's ratio ceiling per kind (the live ``limits.ratio_max``); a test sets e.g. {"P": 60, "E": 49}
        self.fixture_result: dict | None = None   # a recorded tune.last ``result`` (tests/web/fixtures) served instead of the fake's own tuning words
        self.memory = memory_fixture()
        self.curves: dict[str, dict] = {}
        self.tuning = initial_tuning()
        self.accepted = copy.deepcopy(self.tuning)
        self.fused_vt = {u: round(stock_vid(u, unit_ratios(u)[-1]) + 0.01, 4) for u in UNITS}
        self.tables = {"p": [57, 57, 55, 55, 55, 55, 55, 55], "e": [48, 48, 47, 46, 46, 46, 46, 46]}  # 0x1AD / 0x650 bucket bytes, bucket 0 first
        self.tables_note: str | None = None
        self.replay: dict[str, dict] = {}
        self.tuning_version = 0           # bumped by every apply; a plan made before it is PLAN_STALE
        self.plan_steps_truncated = False
        self.apply_stop_after: int | None = None   # stop the apply after this many units: complete False + stop (G-112)
        self.profiles: dict[str, dict] = {"accepted": {"name": "accepted", "source": "accepted", "created": "2026-10-06T21:14:02Z", "intent": None}}
        self.profiles_include_intent = False   # the real profiles.list never carries contents (G-123: profiles.get does)
        self.no_accepted = False          # G-129 / D-036: a fresh install has no accepted state (no accepted profiles, no compare, Restore refused)
        self.accept_refusal: tuple[str, str] | None = None   # (check, message): profiles.accept refuses like the helper (e.g. NOT_APPLIED)
        self.accept_calls: list[dict] = []
        self.tuning_read = False          # tune.read happened this boot (decides a record's tuning_state)
        self.last_read_at: float | None = None        # G-123: when the stored read (tune.last) was made, epoch seconds
        self.last_applied_at: float | None = None     # G-123: set when an apply changed the words after that read
        self.last_read_refused = False                # G-123: tune.last answers available:false although a read happened (another boot)
        self.measure_delay = 0.0          # seconds between vf.point events
        self.measure_gate = threading.Event()
        self.measure_gate.set()
        self.record_budget_units: int | None = None   # G-111: max units per curve response (None = whole records)
        self.measure_problems: list[str] = []
        self.no_turbo = False          # G-132: the power-saver profile (curves.list no_turbo; points above base are turbo_off)
        self.interpolate = True        # G-132 item 8: unmeasurable V/F points between measured ones are interpolated
        self.measure_refusal: dict | None = None      # {"code", "message", "check"}: refuse vf.measure up front
        self.apply_delay = 0.0
        self.apply_mismatch: set[tuple[str, int]] = set()
        self.tele_interval: float | None = None
        self.stress_delay = 0.0                       # seconds of real time per progress event (2 s of the screen); 0 = finish at once
        self.stress_errors = 0
        self.stress_lines: list[str] = []
        self.stress_health = "clean"
        self.stress_refusal: dict | None = None       # {"code", "message", "check"}
        self.stress_gate = threading.Event()
        self.stress_gate.set()
        self.stress_results: list[dict] = []          # stress.list rows, newest first
        self._stressing: set[str] = set()
        self._measuring: set[str] = set()
        self._applying: set[str] = set()
        self.plans_v3: dict[str, dict] = {}
        self.ops_used_v3 = 0

    # ---- helpers ----
    def busy_state(self) -> tuple[str, str | None]:
        """What a real helper's heartbeat would say right now: ``(state, op_id)``."""
        if self._measuring:
            return "measuring", sorted(self._measuring)[0]
        if self._applying:
            return "applying", sorted(self._applying)[0]
        if self._stressing:
            return "stressing", sorted(self._stressing)[0]
        return "idle", None

    def seed_curve(self, name: str = "Stock baseline", *, mode: str = "quick", baseline: bool = False, offset_mv: float | None = None,
                   created: str | None = None, units: list[str] | None = None) -> dict:
        """Store a finished synthetic curve; ``offset_mv`` overrides the offsets in force (e.g. 0.0 for a stock curve)."""
        rec = self._new_record(name, mode, units or list(UNITS), stock=offset_mv == 0.0)
        for u in rec["units"]:
            for r in measure_ratios(u, mode):
                rec["units"][u]["points"].append(self._point(u, r, offset_mv))
            if self.interpolate:
                interpolate_points(rec["units"][u]["points"])
        rec["baseline"] = baseline
        if created:
            rec["created"] = created
        self.curves[rec["id"]] = rec
        return rec

    def _new_record(self, name: str, mode: str, units: list[str], stock: bool = False) -> dict:
        now = datetime.now(timezone.utc)
        cid = now.strftime("%Y%m%dT%H%M%SZ") + "-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
        while cid in self.curves:
            cid = now.strftime("%Y%m%dT%H%M%SZ") + "-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
        kind = "unknown"
        words = None
        if stock:
            kind = "stock"
        elif self.tuning_read:
            kind = "words"
            words = {str(UNIT_CPUS[u][0]): [_word(u, p) for p in self.tuning[u]["points"]] for u in UNITS}
        ts = {"kind": kind}
        if words:
            ts["words"] = words
        return {"id": cid, "name": name or cid, "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "boot_id": "fake-boot-0001", "mode": mode, "complete": True,
                "kernel": "7.2.9-200.fc44.x86_64", "tuning_state": ts, "temps": {"min_c": 41.0, "max_c": 72.5},
                "units": {u: {"cpus": UNIT_CPUS[u], "points": []} for u in units}, "baseline": False}

    def _point(self, unit: str, ratio: int, offset_override: float | None = None) -> dict:
        fused = (P_POINTS if kind_of(unit) == "P" else E_POINTS)
        why = unreachable_reason(unit, ratio)
        if self.no_turbo and ratio > (37 if kind_of(unit) == "P" else 32):   # G-132: turbo off (power-saver profile): not attempted
            return {"ratio": ratio, "mhz": ratio * 100.0, "vid_v": None, "vid_min": None, "vid_max": None, "kept": 0, "total": 0, "status": "unreachable", "reason": "turbo_off",
                    **({"fused_point": fused.index(ratio) + 1} if ratio in fused else {})}
        if why:
            pt = {"ratio": ratio, "mhz": None, "vid_v": None, "vid_min": None, "vid_max": None, "kept": 0, "total": 30, "status": "unreachable", "reason": why}
            if why == "cap_granularity":
                pt["neighbours"] = [ratio - 1, ratio + 1]
            if ratio in fused:   # the real helper labels the unreachable V/F points too
                pt["fused_point"] = fused.index(ratio) + 1
            return pt
        off = offset_override if offset_override is not None else interp_offset(self.tuning[unit]["points"], ratio)
        v = stock_vid(unit, ratio) + off / 1000.0
        pt = {"ratio": ratio, "mhz": ratio * 100.0 + 2.0, "vid_v": round(v, 4), "vid_min": round(v - 0.004, 4), "vid_max": round(v + 0.005, 4),
              "kept": 28, "total": 30, "status": "ok"}
        if ratio in fused:   # G-118: the key is absent for the ratios in between
            pt["fused_point"] = fused.index(ratio) + 1
        return pt

    @staticmethod
    def _summary(rec: dict) -> dict:
        ts = rec.get("tuning_state")
        return {"id": rec["id"], "name": rec["name"], "created": rec["created"], "mode": rec["mode"], "complete": rec["complete"],
                "tuning_state": {"kind": ts["kind"]} if isinstance(ts, dict) else {"kind": "unknown"}, "units": list(rec["units"]), "baseline": rec["baseline"]}

    # ---- dispatcher ----
    def handle_v3(self, rid: str, op: str, args: dict) -> None:
        if op in self.replay:   # dev aid: answer with a result captured from the real simulator helper (shape checks without re-implementing it)
            return self._reply(rid, True, result=self.replay[op])
        try:
            getattr(self, "_op_" + op.replace(".", "_"))(rid, args)
        except Exception as e:  # a fake: surface programming errors as INTERNAL instead of killing the serve loop
            self._err(rid, "INTERNAL", f"fake helper bug: {e!r}")

    def _op_mem_read(self, rid, args):
        self._reply(rid, True, result=self.memory)

    # ---- curves ----
    def _op_curves_list(self, rid, args):
        rows = sorted(self.curves.values(), key=lambda r: r["created"], reverse=True)
        self._reply(rid, True, result={"curves": [self._summary(r) for r in rows], "no_turbo": self.no_turbo})

    def _budgeted(self, rec: dict, only: list[str] | None = None) -> dict:
        """The record as one 64 KiB line would carry it: at most ``record_budget_units`` units, the rest named in ``omitted_units``."""
        names = [u for u in rec["units"] if only is None or u in only]
        if self.record_budget_units is None:
            return {**rec, "units": {u: rec["units"][u] for u in names}} if only is not None else rec
        keep, rest = names[:self.record_budget_units], names[self.record_budget_units:]
        out = {**rec, "units": {u: rec["units"][u] for u in keep}}
        if rest:
            out["omitted_units"] = rest
        return out

    def _op_curves_get(self, rid, args):
        rec = self.curves.get(args.get("id"))
        if rec is None:
            return self._err(rid, "BAD_REQUEST", "no such curve", check="NO_SUCH_CURVE")
        self._reply(rid, True, result=self._budgeted(rec, args.get("units")))

    def _op_curves_update(self, rid, args):
        rec = self.curves.get(args.get("id"))
        if rec is None:
            return self._err(rid, "BAD_REQUEST", "no such curve", check="NO_SUCH_CURVE")
        if "name" in args:
            rec["name"] = str(args["name"])[:64]
        if "baseline" in args:
            if args["baseline"]:
                for r in self.curves.values():
                    r["baseline"] = False
            rec["baseline"] = bool(args["baseline"])
        self._reply(rid, True, result=self._summary(rec))

    def _op_curves_delete(self, rid, args):
        if self.curves.pop(args.get("id"), None) is None:
            return self._err(rid, "BAD_REQUEST", "no such curve", check="NO_SUCH_CURVE")
        self._reply(rid, True, result={"deleted": args["id"]})

    # ---- stress (long; G-116 shapes) ----
    def _op_stress_start(self, rid, args):
        if self.stress_refusal:
            r = self.stress_refusal
            return self._err(rid, r["code"], r["message"], check=r["check"])
        if self._measuring or self._applying or self._stressing:
            return self._err(rid, "BUSY", "another operation is running", state=self.busy_state()[0])
        threading.Thread(target=self._stress, args=(rid, args), daemon=True).start()

    def _op_stress_stop(self, rid, args):
        if self._stressing:
            op = sorted(self._stressing)[0]
            self._cancelled.add(op)
            return self._reply(rid, True, result={"cancelled": True, "state": "stressing", "op_id": op})
        self._reply(rid, True, result={"cancelled": False, "state": self.busy_state()[0]})

    def _op_stress_list(self, rid, args):
        self._reply(rid, True, result={"results": copy.deepcopy(self.stress_results[:50])})

    def _stress(self, rid: str, args: dict) -> None:
        kind = args["kind"]
        total = int(args["duration_s"])
        units = [u for u in UNITS if u in (args.get("units") or UNITS)] if kind == "cpu" else list(UNITS)
        started = datetime.now(timezone.utc)
        self._stressing.add(rid)
        elapsed = 0.0
        by = "time"
        try:
            while elapsed < total:
                while not self.stress_gate.wait(0.02):
                    if rid in self._cancelled:
                        break
                if rid in self._cancelled:
                    by = "cancel"
                    break
                if self.stress_delay:
                    time.sleep(self.stress_delay)
                elapsed = min(total, elapsed + 2.0 if self.stress_delay else total)
                self.emit({"event": "progress", "op_id": rid, "elapsed_s": round(elapsed, 1), "of_s": total, "errors": self.stress_errors if elapsed > total / 2 else 0})
            if self.stress_errors and by == "time":
                by = "error"
            cap = args.get("ratio_cap")
            ach = {u: {"mhz_mean": round(min(cap, unit_max_ratio(u)) * 100 - 9.0, 1) if cap else unit_max_ratio(u) * 100 - 15.0,
                       "ratio_mode": min(cap, unit_max_ratio(u)) if cap else unit_max_ratio(u)} for u in units}
            sid = started.strftime("%Y%m%dT%H%M%SZ") + "-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
            res = {"id": sid, "kind": kind, "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "ended": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "duration_s": total, "elapsed_s": round(elapsed, 1), "stopped_by": by, "errors": self.stress_errors, "error_lines": list(self.stress_lines),
                   "achieved": ach if kind == "cpu" else {}, "cap_valid": (True if cap else None), "max_temp_c": 71.0, "max_pkg_w": 188.5,
                   "mce_new": 0, "health": self.stress_health, "record_path": "/var/lib/arltuner/stress/" + sid + ".json"}
            row = {k: res[k] for k in ("id", "kind", "started", "ended", "duration_s", "elapsed_s", "stopped_by", "errors", "cap_valid", "health", "mce_new", "max_temp_c", "max_pkg_w")}
            row.update({"method": args.get("method", "integer") if kind == "cpu" else None, "units": units if kind == "cpu" else None,
                        "ratio_cap": args.get("ratio_cap"), "memory_mb": args.get("memory_mb", 6144) if kind == "memory" else None})
            self.stress_results.insert(0, row)
            self._reply(rid, True, result=res)
        finally:
            self._stressing.discard(rid)
            self._cancelled.discard(rid)

    # ---- measure (long) ----
    def _op_vf_measure(self, rid, args):
        if self.measure_refusal:
            r = self.measure_refusal
            return self._err(rid, r["code"], r["message"], check=r["check"])
        threading.Thread(target=self._measure, args=(rid, args), daemon=True).start()

    def _measure(self, rid: str, args: dict) -> None:
        mode = args.get("mode", "quick")
        units = [u for u in UNITS if u in (args.get("units") or UNITS)]
        rec = self._new_record(str(args.get("name") or ""), mode, units)
        plan = [(u, r) for u in units for r in measure_ratios(u, mode)]
        self._measuring.add(rid)
        t0 = time.monotonic()
        try:
            for i, (u, r) in enumerate(plan):
                while not self.measure_gate.wait(0.02):
                    if rid in self._cancelled:
                        break
                if rid in self._cancelled:
                    rec["complete"] = False
                    self.curves[rec["id"]] = rec
                    return self._err(rid, "CANCELLED", "vf.measure was cancelled", state="measuring", curve_id=rec["id"])
                if self.measure_delay:
                    time.sleep(self.measure_delay)
                pt = self._point(u, r)
                rec["units"][u]["points"].append(pt)
                eta = (time.monotonic() - t0) / (i + 1) * (len(plan) - i - 1) if self.measure_delay else (len(plan) - i - 1) * 0.5
                self.emit({"event": "progress", "op_id": rid, "step": i + 1, "of": len(plan), "unit": u, "ratio": r, "eta_s": round(eta, 1)})
                self.emit({"event": "vf.point", "op_id": rid, "unit": u, "ratio": r, "mhz": pt["mhz"], "vid_v": pt["vid_v"], "status": pt["status"]})
            if self.interpolate:   # G-132 item 8
                for u in units:
                    interpolate_points(rec["units"][u]["points"])
            self.curves[rec["id"]] = rec
            result = {"curve_id": rec["id"], "record": self._budgeted(rec)}
            notes = (["Turbo is off (power-saver profile?): ratios above base cannot be reached \u2014 switch to Balanced or Performance"] if self.no_turbo else []) + list(self.measure_problems or [])
            if notes:
                result["problems"] = notes
            self._reply(rid, True, result=result)
        finally:
            self._measuring.discard(rid)
            self._cancelled.discard(rid)

    # ---- tuning words (G-112 shapes: docs/ARCHITECTURE.md contract 9 as built) ----
    def _oc_view(self, u: str) -> dict:
        """The OC word as ``tune.read`` decodes it: ratio / V target are ``None`` (unset, the word reads 0) on most cores."""
        oc = self.tuning[u]["oc"]
        return {"point": 0, "ratio": oc["ratio"], "offset_mv": oc["offset_mv"], "v_target_v": oc["v_target_v"], "mode": "adaptive"}

    def _op_tune_read(self, rid, args):
        self.tuning_read = True
        self.last_read_at, self.last_applied_at = time.time(), None
        self.ops_used_v3 += 112
        self._reply(rid, True, result=self._read_result())

    def _read_result(self) -> dict:
        """The ``tune.read`` result for the tuning in force now (also what ``tune.last`` serves, with the words of any apply since the read)."""
        units, tune = [], {}
        for u in UNITS:
            t = self.tuning[u]
            oc_t = self._oc_view(u)
            oc = {**oc_t, "word": _word(u, t["oc"]), "ratio": oc_t["ratio"] or 0, "v_target_v": oc_t["v_target_v"] or 0.0, "static": False}
            points = [{**_entry(u, p), "v_target_v": None, "word": _word(u, p), "mode": "adaptive", "static": False} for p in t["points"]]
            cpus = [{"cpu": UNIT_CPUS[u][0], "name": (f"P-Core {UNIT_CPUS[u][0]}" if kind_of(u) == "P" else f"E-Core {UNIT_CPUS[u][0]}"),
                     "oc": oc, "points": points, "end": None}]
            name = f"P-Core {u[1:]}" if kind_of(u) == "P" else f"E-cluster {u[2:]} (CPUs {UNIT_CPUS[u][0]}-{UNIT_CPUS[u][-1]})"
            units.append({"id": u, "name": name, "kind": kind_of(u), "cluster": None if kind_of(u) == "P" else int(u[2:]), "cluster_cpus": UNIT_CPUS[u],
                          "cpus": cpus, "measured": None})
            tune[u] = {"cpus": [{"cpu": UNIT_CPUS[u][0], "points": [oc_t] + [{"point": p["point"], "ratio": p["ratio"], "offset_mv": p["offset_mv"],
                                                                                "v_target_v": None, "mode": "adaptive"} for p in t["points"]]}]}
            if u in getattr(self, "oc_point_units", ()):  # G-125: the real helper reads the OC point back as P2 = fused count + 1, flagged oc_point
                n = len(t["points"]) + 1
                tune[u]["cpus"][0]["points"].append({"point": n, "ratio": oc_t["ratio"] or 48, "offset_mv": 0.0, "v_target_v": None, "mode": "adaptive", "oc_point": True,
                                                     "point_label": f"OC (point {n})"})
        match = [{"cpu": UNIT_CPUS[u][0], "match": self.tuning[u] == self.accepted[u],
                  "diffs": [] if self.tuning[u] == self.accepted[u] else [{"point": 1, "expected": "0x0", "actual": "0x1"}]} for u in UNITS]
        if self.no_accepted:
            match = None
        fused = {str(UNIT_CPUS[u][0]): fused_of(u) for u in UNITS}
        out = {"simulator": True, "complete": True, "stopped": None, "units": units, "differences": [], "problems": [],
               "ops_done": 112, "accepted_match": match, "accepted_note": "No accepted state yet: save a profile as accepted (Profiles)" if self.no_accepted else None, "tune": tune, "tables": copy.deepcopy(self.tables),
               "tables_note": self.tables_note, "fused": fused, "turbo": turbo_view(self.tables)}
        if self.fixture_result is not None:
            out = copy.deepcopy(self.fixture_result)
            out.pop("limits", None)
        if self.read_at_enabled and self.last_read_at is not None:
            out["read_at"] = read_at_iso(self.last_read_at)
        if self.limits_enabled:
            out["limits"] = limits_view(self.ratio_max)
        return out

    def seed_last_read(self, at: float | None = None) -> None:
        """A read made earlier this boot by another page / helper process: ``tune.last`` serves it (G-123)."""
        self.tuning_read = True
        self.last_read_at = at if at is not None else time.time() - 1500
        self.last_applied_at = None

    def _op_tune_last(self, rid, args):
        if not self.tuning_read or self.last_read_refused or self.last_read_at is None:
            return self._reply(rid, True, result={"available": False})
        self._reply(rid, True, result={"available": True, "boot_id": "fake-boot-0001", "at": self.last_read_at, "applied_at": self.last_applied_at,
                                       "result": self._read_result()})

    # ---- plan ----
    def _refuse_range(self, rid, field: str, limit: str):
        self._err(rid, "REFUSED", f"{field} is outside the policy: {limit}", check="POLICY_RANGE")

    def _targets(self, target) -> list[str] | None:
        if target == "P":
            return [u for u in UNITS if u.startswith("P")]
        if target == "E":
            return [u for u in UNITS if u.startswith("E")]
        return [target] if target in UNITS else None

    def _op_tune_plan(self, rid, args):
        intent = args.get("intent")
        if not isinstance(intent, dict):
            return self._err(rid, "BAD_REQUEST", "intent must be an object")
        new = copy.deepcopy(self.tuning)
        warnings: list = []
        written: dict[str, set[int]] = {}   # unit -> word indexes the plan writes (0 = OC word)
        turbo_new: dict[str, list[int]] = {}   # kind -> the 8 bucket ratios the plan writes (page assumption: intent.turbo = {P: [8], E: [8]})
        if "reset" in intent:
            if not self.tuning_read:
                return self._err(rid, "REFUSED", "reset needs the words last read; run 'Read tuning words' (tune.read) first. Nothing was sent.", check="NO_READ")
            for u in UNITS:   # offsets only: ratio, V target and the turbo tables stay
                for p in new[u]["points"] + [new[u]["oc"]]:
                    p["offset_mv"] = 0.0
                written[u] = {0}
        elif "restore" in intent:
            if self.no_accepted:
                return self._err(rid, "REFUSED", "No accepted state yet: save a profile as accepted (Profiles). Nothing was sent.", check="NO_ACCEPTED_STATE")
            new = copy.deepcopy(self.accepted)
            written = {u: {0} | {p["point"] for p in new[u]["points"]} for u in UNITS if new[u] != self.tuning[u]}
        elif "profile" in intent:
            prof = self.profiles.get(intent["profile"])
            if prof is None:
                return self._err(rid, "REFUSED", "no such profile", check="PROFILE_NOT_FOUND")
            if prof["source"] == "accepted":
                new = copy.deepcopy(self.accepted)
                written = {u: {0} | {p["point"] for p in new[u]["points"]} for u in UNITS if new[u] != self.tuning[u]}
            elif self._apply_rows(rid, new, prof["intent"].get("rows", []), warnings, written):
                return
        elif isinstance(intent.get("rows"), list) or (set(intent) == {"turbo"}):   # G-117: {turbo} alone is a plan (plain-MSR table writes only)
            rows = [r for r in intent.get("rows", []) if isinstance(r, dict) and (r.get("offset") or r.get("v_target_v") is not None or r.get("ratio") is not None)]
            turbo = self._turbo_intent(rid, intent.get("turbo"))
            if turbo is False:
                return
            if not rows and not turbo:
                return self._err(rid, "BAD_REQUEST", "the intent changes nothing (every row is empty)", check="NOTHING_TO_DO")
            scope = intent.get("scope")
            for r in rows:
                if r.get("target") not in (("P", "E") if scope == "simple" else tuple(UNITS)):
                    return self._err(rid, "BAD_REQUEST", f"row target {r.get('target')!r} is not valid for scope {scope!r}")
                if isinstance(r.get("offset"), dict) and r["offset"].get("kind") == "points" and not self.tuning_read:
                    return self._err(rid, "REFUSED", f"{r['target']}: per-point offsets are addressed by ratio and need the words last read; run 'Read tuning words' "
                                     "(tune.read) first. Nothing was sent.", check="NO_READ")
            if self._apply_rows(rid, new, rows, warnings, written):
                return
            turbo_new = turbo or {}
        else:
            return self._err(rid, "BAD_REQUEST", "intent must be {scope, rows}, {profile}, {reset: 'stock'} or {restore: 'accepted'}")
        before, after, ops, changed_units, steps = {}, {}, 0, [], []
        for u in UNITS:
            idx = sorted(written.get(u, ()))
            if not idx:
                continue
            changed_units.append(u)
            word = lambda state, i: _entry(u, state[u]["oc"] if i == 0 else next(p for p in state[u]["points"] if p["point"] == i))  # noqa: E731
            show = lambda e: {"ratio": e["ratio"], "offset_mv": e["offset_mv"], "v_target_v": e["v_target_v"]}  # noqa: E731
            before[u] = {str(i): (show(word(self.tuning, i)) if self.tuning_read else None) for i in idx}
            pred = predict_unit(self._baseline(), u, new[u]["points"])
            after[u] = {str(i): {**show(word(new, i)), **(pred.get(i, {}) if i else {})} for i in idx}
            ops += 3 * len(idx) + 1
            for i in idx:
                steps.append({"kind": "write_vf" if i == 0 else "write_vf_point", "unit": u, "cpu": UNIT_CPUS[u][0], "point": i})
        top = {"P": self.tables["p"][0], "E": self.tables["e"][0]}
        raised = {k for u in changed_units for k in [kind_of(u)] if (new[u]["oc"]["ratio"] or 0) > top[k]}
        tables_written = sorted(raised | set(turbo_new))
        steps = ([{"kind": "write_percore_enable", "conditional": True}] if changed_units else []) + steps + [{"kind": "write_msr", "table": k.lower()} for k in tables_written]
        ops += len(turbo_new) if not changed_units else 3 * len(turbo_new)   # a table-only plan: one plain WRMSR per table (T-023), no mailbox word
        warnings.extend(self._cpu_fact_warnings(new, written, before_state=self.tuning))
        vccia = self._vccia(new, turbo_new)
        for k, ratios in sorted(turbo_new.items()):
            up = [i for i, (a, b) in enumerate(zip(ratios, self.tables[k.lower()])) if a > b]
            if up:
                warnings.append(W("TURBO_RAISE", f"the {k} turbo table is RAISED in bucket(s) {', '.join(str(i) for i in up)} (package-wide)"))
        if self._baseline() is None:
            warnings.append(W("NO_BASELINE", "measure a stock baseline for predictions (no curve is flagged baseline)"))
        if any(new[u]["oc"]["ratio"] != self.tuning[u]["oc"]["ratio"] for u in changed_units):
            warnings.append("Raising the OC ratio or target is not validated on this machine; the hardware may ignore a ratio inside the current turbo table.")
        warnings.append("Revert: cold boot, or reapply the accepted profile.")
        plan_id = hashlib.sha256(json.dumps([after, rid], sort_keys=True).encode()).hexdigest()
        nonce = "".join(secrets.choice(ALPHABET) for _ in range(6))
        self.plans_v3[plan_id] = {"nonce": nonce, "used": False, "new": new, "changed": changed_units, "written": written, "created": time.monotonic(),
                                  "version": self.tuning_version, "bad": 0, "raised": sorted(raised), "turbo": turbo_new}
        result = {"plan_id": plan_id, "nonce": nonce, "name": "tune-" + plan_id[:6], "steps": steps, "writes": len(steps) - (1 if changed_units else 0), "reads": len(changed_units),
                  "ops_needed": ops, "ops_left": self.ops_budget - self.ops_used_v3, "expires_s": 600, "units": changed_units, "before": before,
                  "after": after, "warnings": warning_objects(warnings), "words_assumed": not self.tuning_read}
        if vccia:
            result["vccia"] = vccia
        if self.plan_steps_truncated:
            result["steps"] = steps[: len(steps) // 2]
            result["steps_truncated"] = True
        self._reply(rid, True, result=result)

    def _baseline(self) -> dict | None:
        """The newest curve tagged baseline: a plan's prediction is made on it (contract 9 addendum)."""
        rows = sorted((r for r in self.curves.values() if r.get("baseline")), key=lambda r: r["created"], reverse=True)
        return rows[0] if rows else None

    def _turbo_intent(self, rid, raw):
        """Validate ``intent.turbo``: ``{kind: [8 ratios]}``. Returns the dict, ``None`` (absent) or ``False`` after sending a refusal (F5)."""
        if raw is None:
            return None
        if not isinstance(raw, dict) or not raw or not set(raw) <= {"P", "E"}:
            self._err(rid, "BAD_REQUEST", "turbo must be {P: [8 ratios], E: [8 ratios]}")
            return False
        out = {}
        for k, ratios in raw.items():
            if not (isinstance(ratios, list) and len(ratios) == 8 and all(isinstance(x, int) and not isinstance(x, bool) for x in ratios)):
                self._err(rid, "BAD_REQUEST", f"turbo.{k} must be 8 integer ratios")
                return False
            if any(not (8 <= x <= self.ratio_max[k]) for x in ratios):
                self._refuse_range(rid, f"turbo.{k}", f"8..{self.ratio_max[k]}")
                return False
            for i in range(1, 8):
                if ratios[i] > ratios[i - 1]:
                    self._err(rid, "REFUSED", f"{k} turbo bucket {i} ({ratios[i]}x) is above bucket {i - 1} ({ratios[i - 1]}x): ratios must not rise with more active cores. Nothing was sent.",
                              check="TURBO_ORDER")
                    return False
            if ratios != self.tables[k.lower()]:
                out[k] = list(ratios)
        return out

    def _cpu_fact_warnings(self, new: dict, written: dict, before_state: dict) -> list:
        """F4 / F8 warnings: clamped points, E 40x offset, E target above 1.2 V."""
        out = []
        base = self._baseline()
        for u, idx in written.items():
            if kind_of(u) == "E":
                for p in new[u]["points"]:
                    if p["ratio"] == 40 and p["point"] in idx and abs(p["offset_mv"]) > 1e-9:
                        out.append(W("E40_OFFSET", f"{u} 40x: never offset the 40x point ({p['offset_mv']:+.0f} mV there gave OCCT errors on the reference machine)"))
                vt = new[u]["oc"]["v_target_v"]
                if 0 in idx and vt is not None and vt > 1.2 + 1e-9:
                    out.append(W("E_TARGET_WHEA", f"{u}: an E-core target of {vt:.3f} V is above 1.2 V; E cores report WHEA errors there"))
            pred = predict_unit(base, u, new[u]["points"])
            by_pt = {p["point"]: p for p in new[u]["points"]}
            for i in sorted(idx):
                c = pred.get(i, {}).get("clamped_by")
                if c:
                    out.append(W("CLAMPED", f"{u} {by_pt[i]['ratio']}x: {by_pt[i]['offset_mv']:+.1f} mV cannot be met; the voltage is clamped up to the {by_pt[c]['ratio']}x point (V(n) never below V(n-1))"))
        return out

    def _vccia(self, new: dict, turbo_new: dict) -> dict | None:
        """F6 on the fake, in the shape of the G-128 helper: the highest VID any unit requests with every core loaded (no ring floor, no regulator margin: ``margin_v`` and
        ``contributors.ring`` are null), the 1-2 core case as ``light_load`` and the state now as ``before_v``; None without a baseline curve."""
        base = self._baseline()
        if base is None:
            return None

        def scenario(state: dict, tables: dict, idx: int) -> tuple:
            cands = []
            for u in UNITS:
                pred = predict_unit(base, u, state[u]["points"])
                pts = sorted((p["ratio"], pred[p["point"]]["effective_v"]) for p in state[u]["points"] if pred.get(p["point"], {}).get("effective_v") is not None)
                if not pts:
                    continue
                r = tables[kind_of(u)][idx]
                if r <= pts[0][0]:
                    v = pts[0][1]
                elif r >= pts[-1][0]:
                    v = pts[-1][1]
                else:
                    a, b = next((a, b) for a, b in zip(pts, pts[1:]) if a[0] <= r <= b[0])
                    v = a[1] + (b[1] - a[1]) * (r - a[0]) / (b[0] - a[0])
                cands.append((u, r, v))
            return max(cands, key=lambda c: c[2]), cands

        tabs_new = {"P": turbo_new.get("P", self.tables["p"]), "E": turbo_new.get("E", self.tables["e"])}
        tabs_now = {"P": self.tables["p"], "E": self.tables["e"]}
        who, cands = scenario(new, tabs_new, 7)
        light, _ = scenario(new, tabs_new, 0)
        was, _ = scenario(self.tuning, tabs_now, 7)
        best = {k: max((c for c in cands if kind_of(c[0]) == k), key=lambda c: c[2], default=None) for k in ("P", "E")}
        return {"predicted_v": round(who[2], 4), "set_by": {"unit": who[0], "ratio": who[1]}, "label": "highest requested VID among units", "active_cores": "all",
                "margin_v": None,
                "contributors": {**{k: (None if c is None else {"unit": c[0], "ratio": c[1], "v": round(c[2], 4)}) for k, c in best.items()},
                                 "ring": None},
                "before_v": round(was[2], 4),
                "light_load": {"predicted_v": round(light[2], 4), "set_by": {"unit": light[0], "ratio": light[1]}, "active_cores": "1-2"}}

    def _apply_rows(self, rid, new: dict, rows: list, warnings: list, written: dict) -> bool:
        """Apply plan rows to ``new``; True (after sending the refusal) if a row is out of policy."""
        for row in rows:
            units = self._targets(row.get("target"))
            if units is None:
                self._err(rid, "BAD_REQUEST", f"unknown target {row.get('target')!r}")
                return True
            for u in units:
                k = kind_of(u)
                off = row.get("offset")
                if isinstance(off, dict):
                    mvs = [off["mv"]] if off.get("kind") == "curve" else list(off.get("mv", {}).values())
                    for mv in mvs:
                        if not isinstance(mv, (int, float)) or abs(mv) > OFFSET_LIMIT:
                            self._refuse_range(rid, "offset.mv", f"-{OFFSET_LIMIT}..+{OFFSET_LIMIT} mV")
                            return True
                    if off.get("kind") == "curve":
                        for p in new[u]["points"] + [new[u]["oc"]]:
                            p["offset_mv"] = float(off["mv"])
                        written.setdefault(u, set()).add(0)
                    else:
                        for p in new[u]["points"]:
                            if str(p["ratio"]) in off["mv"]:
                                p["offset_mv"] = float(off["mv"][str(p["ratio"])])
                                written.setdefault(u, set()).add(p["point"])
                    if any(p["offset_mv"] > 0 for p in new[u]["points"]):
                        warnings.append(W("POSITIVE_OFFSET", f"positive offset on {u}: raises voltage above the fused curve"))
                vt = row.get("v_target_v")
                if vt is not None:
                    if not (V_TARGET_MIN - 1e-9 <= vt <= V_TARGET_MAX[k] + 1e-9):
                        self._refuse_range(rid, "v_target_v", f"{V_TARGET_MIN:.3f}..{V_TARGET_MAX[k]:.3f} V for {k}")
                        return True
                    new[u]["oc"]["v_target_v"] = float(vt)
                    written.setdefault(u, set()).add(0)
                    if vt > self.fused_vt[u] + 1e-9:
                        warnings.append(f"target above fused on {u}: {vt:.3f} V > {self.fused_vt[u]:.3f} V")
                ratio = row.get("ratio")
                if ratio is not None:
                    if not (isinstance(ratio, int) and ratio <= self.ratio_max[k]):
                        self._refuse_range(rid, "ratio", f"at most {self.ratio_max[k]} for {k}")
                        return True
                    new[u]["oc"]["ratio"] = ratio
                    written.setdefault(u, set()).add(0)
        return False

    # ---- apply (long) ----
    def _op_tune_apply(self, rid, args):
        p = self.plans_v3.get(args.get("plan_id"))
        if p is None:
            return self._err(rid, "PLAN_STALE", "unknown plan (plans do not survive a helper restart)")
        if p["used"] or args.get("typed") != "CONFIRM " + p["nonce"]:
            p["bad"] += 1
            if p["bad"] >= 3:
                del self.plans_v3[args["plan_id"]]
            return self._err(rid, "CONFIRM_MISMATCH", "typed text does not match")
        if p["version"] != self.tuning_version:
            return self._err(rid, "PLAN_STALE", "a word changed since this plan was made; plan again")
        p["used"] = True
        self._applying.add(rid)
        threading.Thread(target=self._apply, args=(rid, p), daemon=True).start()

    def _apply(self, rid: str, p: dict) -> None:
        n = max(len(p["changed"]), 1)
        stop_after = self.apply_stop_after
        readback, sent, cpufreq = [], 0, []
        for i, u in enumerate(p["changed"]):
            if self.apply_delay:
                time.sleep(self.apply_delay)
            self.emit({"event": "progress", "op_id": rid, "step": i + 1, "of": n, "ops_used": self.ops_used_v3 + 3 * (i + 1), "ops_budget": self.ops_budget})
            if stop_after is not None and i >= stop_after:
                break
            for idx in sorted(p["written"][u]):
                q = p["new"][u]["oc"] if idx == 0 else next(x for x in p["new"][u]["points"] if x["point"] == idx)
                exp = _word(u, q)
                bad = (u, idx) in self.apply_mismatch
                readback.append({"unit": u, "point": idx, "cpu": UNIT_CPUS[u][0], "expected": exp,
                                 "actual": "0x" + format(int(exp, 16) ^ 1, "08x") if bad else exp, "match": not bad})
                sent += 1
            new_r, old_r = p["new"][u]["oc"]["ratio"], self.tuning[u]["oc"]["ratio"] or (48 if kind_of(u) == "E" else 55)  # unset OC word: the stock table top
            if new_r and new_r != old_r:  # G-126: an OC ratio change moves cpuinfo_max_freq; the helper moves scaling_max_freq with it (E-cores read one bin more)
                step = 1 if kind_of(u) == "E" else 0
                cpufreq += [{"cpu": c, "before_khz": (old_r + step) * 100_000, "after_khz": (new_r + step) * 100_000} for c in UNIT_CPUS[u]]
            self.tuning[u] = copy.deepcopy(p["new"][u])
        for k, ratios in p["turbo"].items():
            self.tables[k.lower()] = list(ratios)
            readback.append({"unit": k, "point": "turbo", "cpu": 0 if k == "P" else 8, "expected": "0x" + "".join(format(x, "02x") for x in reversed(ratios)),
                             "actual": "0x" + "".join(format(x, "02x") for x in reversed(ratios)), "match": True})
        for k in p["raised"]:
            if k in p["turbo"]:
                continue
            top = max(p["new"][u]["oc"]["ratio"] or 0 for u in p["changed"] if kind_of(u) == k)
            self.tables[k.lower()] = [top] + self.tables[k.lower()][1:]
            readback.append({"unit": k, "point": "turbo", "cpu": 0 if k == "P" else 8, "expected": "0x" + format(top, "016x"), "actual": "0x" + format(top, "016x"), "match": True})
        sent += len(p["turbo"])
        self.tuning_version += 1
        if self.last_read_at is not None and sent:
            self.last_applied_at = time.time()
        self.ops_used_v3 += 3 * n
        self._applying.discard(rid)
        result = {"plan_id": next((k for k, v in self.plans_v3.items() if v is p), ""), "steps": [{"n": i + 1, "kind": "write_vf", "unit": u, "status": "ok"} for i, u in enumerate(p["changed"])]
                  + [{"n": len(p["changed"]) + i + 1, "kind": "write_msr", "table": k.lower(), "status": "ok"} for i, k in enumerate(sorted(p["turbo"]))],
                  "readback": readback, "cpufreq": cpufreq, "ops_used": 3 * n, "complete": stop_after is None, "writes_sent": sent, "warnings": [], "revert_plan": sent}
        if stop_after is not None:
            result["stop"] = {"kind": "refused", "reason": "the guard refused a step: step limit", "tripped": False}
        self._reply(rid, True, result=result)

    # ---- profiles ----
    def _op_profiles_list(self, rid, args):
        rows = []
        for r in self.profiles.values():
            if self.no_accepted and r["source"] == "accepted":
                continue
            row = {"name": r["name"], "source": r["source"], "created": r["created"]}
            if self.profiles_include_intent and r.get("intent") is not None:
                row["intent"] = r["intent"]
            rows.append(row)
        self._reply(rid, True, result={"profiles": rows, "accepted": any(r["source"] == "accepted" for r in rows)})

    def _op_profiles_accept(self, rid, args):
        """G-129: the user makes a saved profile (or the state of the last read) the accepted state; the fake keeps the current tuning as the accepted one."""
        self.accept_calls.append(dict(args))
        if self.accept_refusal is not None:
            return self._err(rid, "REFUSED", self.accept_refusal[1], check=self.accept_refusal[0])
        if not self.tuning_read:
            return self._err(rid, "REFUSED", "no tune.read this boot: the accepted state is what the CPU holds, so read it first (Tune > Read). Nothing was changed.", check="NO_READ")
        if args.get("from") == "read":
            name = "read-20261012-120000"
        else:
            name = args.get("name")
            rec = self.profiles.get(name) if isinstance(name, str) else None
            if rec is None or rec["source"] != "saved":
                return self._err(rid, "BAD_REQUEST", f"no saved profile named {name!r}", check="PROFILE_NOT_FOUND")
        replaced = [r["name"] for r in self.profiles.values() if r["source"] == "accepted"] if not self.no_accepted else []
        for n in [r["name"] for r in self.profiles.values() if r["source"] == "accepted"]:
            del self.profiles[n]
        self.accepted = copy.deepcopy(self.tuning)
        self.no_accepted = False
        self.accepted_chain = [(name, [])]
        self.profiles[name if args.get("from") == "read" else "accepted:" + name] = {"name": name, "source": "accepted", "created": None, "intent": None}
        self._reply(rid, True, result={"name": name, "source": "accepted", "from": "read" if args.get("from") == "read" else "saved", "accepted_at": "2026-10-12T12:00:00Z",
                                       "replaced": replaced, "cpus": len(UNITS), "core_entries": 0, "points": 0, "turbo": False})

    # G-123: the accepted chain of the reference machine (profiles/CURRENT: e-p6-split3 -> p-boot-ramp -> p-step7), as profiles.get returns it
    def seed_accepted_chain(self) -> None:
        def pts(**by_ratio) -> dict:
            return {"kind": "points", "mv": {r[1:]: v for r, v in by_ratio.items()}}

        def row(unit, offset=None, v=None, ratio=None) -> dict:
            return {"target": unit, "offset": offset, "v_target_v": v, "ratio": ratio}

        e_split = [row("EC0", pts(r40=0, r46=-35))] + [row(f"EC{i}", pts(r40=0, r46=-25)) for i in (1, 2, 3)]
        ramp = [row(f"P{i}", pts(r48=0, r53=-50, r55=-50 if i in (0, 3) else -30, r56=-50, r57=-50)) for i in range(8)]
        step7_55 = {0: -28, 3: -28, 1: -36, 4: -33, 5: -73, 6: -61, 2: -81, 7: -91}
        step7 = []
        for i in range(8):
            kw = dict(r48=0, r53=-50, r55=step7_55[i])
            if i == 0:
                kw.update(r56=-26, r57=-43)
            if i == 3:
                kw.update(r56=-28, r57=-31)
            step7.append(row(f"P{i}", pts(**kw)))
        chain = [("e-p6-split3", e_split), ("p-boot-ramp", ramp), ("p-step7", step7)]
        self.profiles.pop("accepted", None)
        self.accepted_chain = chain
        for name, _rows in chain:
            self.profiles[name] = {"name": name, "source": "accepted", "created": None, "intent": None}

    @staticmethod
    def _spread(intent: dict) -> list[dict]:
        out = {}
        for r in intent.get("rows", []):
            for u in ([x for x in UNITS if x.startswith("P")] if r.get("target") == "P" else [x for x in UNITS if x.startswith("EC")] if r.get("target") == "E" else [r.get("target")]):
                if u in UNITS:
                    out[u] = {"target": u, "offset": r.get("offset"), "v_target_v": r.get("v_target_v"), "ratio": r.get("ratio")}
        return [out[u] for u in UNITS if u in out]

    @staticmethod
    def _summary_of(rows: list[dict]) -> dict:
        vals = []
        points = 0
        for r in rows:
            o = r.get("offset")
            if o:
                vals += [o["mv"]] if o["kind"] == "curve" else list(o["mv"].values())
                points += len(o["mv"]) if o["kind"] == "points" else 0
        return {"units": len(rows), "points": points, "deepest_offset_mv": min(vals) if vals else None,
                "oc_ratios": sum(r.get("ratio") is not None for r in rows), "v_targets": sum(r.get("v_target_v") is not None for r in rows), "turbo": False}

    def _op_profiles_get(self, rid, args):
        name = args.get("name")
        if not isinstance(name, str) or not PROFILE_NAME.match(name):
            return self._err(rid, "BAD_REQUEST", "'name' must be 1..64 characters of a-z 0-9 . _ -")
        chain = getattr(self, "accepted_chain", [])
        for i, (stem, rows) in enumerate(chain):
            if stem != name:
                continue
            merged: dict[str, dict] = {}
            for _s, rs in chain[: i + 1]:
                for r in rs:
                    cur = merged.setdefault(r["target"], {"target": r["target"], "offset": None, "v_target_v": None, "ratio": None})
                    if r["offset"] is not None:
                        old = cur["offset"]["mv"] if cur["offset"] else {}
                        cur["offset"] = {"kind": "points", "mv": {**old, **r["offset"]["mv"]}}
                    for k in ("v_target_v", "ratio"):
                        if r[k] is not None:
                            cur[k] = r[k]
            cum = [merged[u] for u in UNITS if u in merged]
            return self._reply(rid, True, result={"name": name, "source": "accepted", "created": None, "position": i + 1, "of": len(chain),
                                                  "chain": [s for s, _ in chain], "contents": {"rows": rows, "turbo": None}, "cumulative": {"rows": cum, "turbo": None},
                                                  "summary": self._summary_of(rows), "cumulative_summary": self._summary_of(cum)})
        rec = self.profiles.get(name)
        if rec is not None and rec["source"] == "accepted":   # the placeholder "accepted" entry (no chain seeded): the state the fake treats as accepted
            rows = [{"target": u, "offset": {"kind": "points", "mv": {str(p["ratio"]): p["offset_mv"] for p in self.accepted[u]["points"]}}, "v_target_v": None, "ratio": None} for u in UNITS]
            return self._reply(rid, True, result={"name": name, "source": "accepted", "created": rec["created"], "position": 1, "of": 1, "chain": [name],
                                                  "contents": {"rows": rows, "turbo": None}, "cumulative": {"rows": rows, "turbo": None},
                                                  "summary": self._summary_of(rows), "cumulative_summary": self._summary_of(rows)})
        if rec is None or rec.get("intent") is None:
            return self._err(rid, "BAD_REQUEST", f"no saved profile named {name!r}", check="PROFILE_NOT_FOUND")
        rows = self._spread(rec["intent"])
        self._reply(rid, True, result={"name": name, "source": "saved", "created": rec["created"], "contents": {"rows": rows, "turbo": None}, "summary": self._summary_of(rows)})

    def _op_profiles_save(self, rid, args):
        name, intent = args.get("name"), args.get("intent")
        if not isinstance(name, str) or not PROFILE_NAME.match(name) or not isinstance(intent, dict):
            return self._err(rid, "BAD_REQUEST", "name must match ^[a-z0-9._-]{1,64}$ and intent must be an object")
        if name in self.profiles:
            return self._err(rid, "REFUSED", f"profile {name!r} exists and is never overwritten", check="EXISTS")
        self.profiles[name] = {"name": name, "source": "saved", "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "intent": intent}
        self._reply(rid, True, result={"name": name, "saved": True})
