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
  * ``tuning_state.kind`` is only ``"words"`` (a ``tune.read`` this boot) or ``"unknown"``; never ``"stock"``.
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

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789"
UNITS = [f"P{i}" for i in range(8)] + [f"EC{i}" for i in range(4)]
UNIT_CPUS = {**{f"P{i}": [i] for i in range(8)}, **{f"EC{i}": list(range(8 + 4 * i, 12 + 4 * i)) for i in range(4)}}
FAVORED = ("P0", "P3")
P_QUICK = [8, 20, 30, 48, 53, 55, 56, 57]
E_QUICK = [8, 15, 23, 30, 40, 46]
P_POINTS_FAVORED = [8, 20, 30, 48, 53, 55, 56, 57]
P_POINTS_OTHER = [8, 20, 30, 45, 50, 52, 54, 55]
E_POINTS = [8, 15, 23, 30, 40, 46]
RATIO_MAX = {"P": 57, "E": 49}
V_TARGET_MAX = {"P": 1.625, "E": 1.355}
V_TARGET_MIN = 0.900
OFFSET_LIMIT = 200
CURVE_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-z0-9]{4}\Z")
PROFILE_NAME = re.compile(r"[a-z0-9._-]{1,64}\Z")
V3_OPS = frozenset({"mem.read", "vf.measure", "curves.list", "curves.get", "curves.update", "curves.delete", "tune.read", "tune.plan", "tune.apply",
                    "profiles.list", "profiles.save"})


def kind_of(unit: str) -> str:
    return "P" if unit.startswith("P") else "E"


def unit_ratios(unit: str) -> list[int]:
    """The V/F point ratios of a unit (tables are per core: the favored P-cores boost higher)."""
    if kind_of(unit) == "E":
        return list(E_POINTS)
    return list(P_POINTS_FAVORED if unit in FAVORED else P_POINTS_OTHER)


def measure_ratios(unit: str, mode: str) -> list[int]:
    """Ratios a measurement visits: Quick = the V/F point ratios of the unit kind (SPEC-v3 section 3), Dense = every ratio from 8 to the top."""
    if mode == "quick":
        return list(P_QUICK if kind_of(unit) == "P" else E_QUICK)
    return list(range(8, unit_max_ratio(unit) + 1))


def unit_max_ratio(unit: str) -> int:
    return unit_ratios(unit)[-1]


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
        out[u] = {"points": [{"point": i + 1, "ratio": r, "offset_mv": off} for i, r in enumerate(ratios)],
                  "oc": {"point": 0, "ratio": ratios[-1], "offset_mv": off, "v_target_v": round(stock_vid(u, ratios[-1]) + 0.01, 4)}}
    return out


def _word(unit: str, point: dict) -> str:
    h = hashlib.sha256(f"{unit}:{point['point']}:{point['ratio']}:{point['offset_mv']}".encode()).hexdigest()
    return "0x" + h[:8]


def _entry(unit: str, p: dict) -> dict:
    return {"point": p["point"], "ratio": p["ratio"], "offset_mv": p["offset_mv"], "v_target_v": p.get("v_target_v")}


class V3Ops:
    """Mixed into ``FakeHelper`` (which provides ``_reply``, ``_err``, ``progress`` / ``emit``)."""

    def _init_v3(self) -> None:
        self.memory = memory_fixture()
        self.curves: dict[str, dict] = {}
        self.tuning = initial_tuning()
        self.accepted = copy.deepcopy(self.tuning)
        self.fused_vt = {u: self.tuning[u]["oc"]["v_target_v"] for u in UNITS}
        self.profiles: dict[str, dict] = {"accepted": {"name": "accepted", "source": "accepted", "created": "2026-10-06T21:14:02Z", "intent": None}}
        self.profiles_include_intent = True
        self.tuning_read = False          # tune.read happened this boot (decides a record's tuning_state)
        self.measure_delay = 0.0          # seconds between vf.point events
        self.measure_gate = threading.Event()
        self.measure_gate.set()
        self.record_budget_units: int | None = None   # G-111: max units per curve response (None = whole records)
        self.measure_problems: list[str] = []
        self.measure_refusal: dict | None = None      # {"code", "message", "check"}: refuse vf.measure up front
        self.apply_delay = 0.0
        self.apply_mismatch: set[tuple[str, int]] = set()
        self.tele_interval: float | None = None
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
        return "idle", None

    def seed_curve(self, name: str = "Stock baseline", *, mode: str = "quick", baseline: bool = False, offset_mv: float | None = None,
                   created: str | None = None, units: list[str] | None = None) -> dict:
        """Store a finished synthetic curve; ``offset_mv`` overrides the offsets in force (e.g. 0.0 for a stock curve)."""
        rec = self._new_record(name, mode, units or list(UNITS))
        for u in rec["units"]:
            for r in measure_ratios(u, mode):
                rec["units"][u]["points"].append(self._point(u, r, offset_mv))
        rec["baseline"] = baseline
        if created:
            rec["created"] = created
        self.curves[rec["id"]] = rec
        return rec

    def _new_record(self, name: str, mode: str, units: list[str]) -> dict:
        now = datetime.now(timezone.utc)
        cid = now.strftime("%Y%m%dT%H%M%SZ") + "-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
        while cid in self.curves:
            cid = now.strftime("%Y%m%dT%H%M%SZ") + "-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
        kind = "unknown"
        words = None
        if self.tuning_read:
            kind = "words"
            words = {str(UNIT_CPUS[u][0]): [_word(u, p) for p in self.tuning[u]["points"]] for u in UNITS}
        ts = {"kind": kind}
        if words:
            ts["words"] = words
        return {"id": cid, "name": name or cid, "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "boot_id": "fake-boot-0001", "mode": mode, "complete": True,
                "kernel": "7.2.9-200.fc44.x86_64", "tuning_state": ts, "temps": {"min_c": 41.0, "max_c": 72.5},
                "units": {u: {"cpus": UNIT_CPUS[u], "points": []} for u in units}, "baseline": False}

    def _point(self, unit: str, ratio: int, offset_override: float | None = None) -> dict:
        if kind_of(unit) == "P" and unit not in FAVORED and ratio > 55:
            return {"ratio": ratio, "mhz": None, "vid_v": None, "vid_min": None, "vid_max": None, "kept": 0, "total": 30, "status": "unreachable"}
        off = offset_override if offset_override is not None else interp_offset(self.tuning[unit]["points"], ratio)
        v = stock_vid(unit, ratio) + off / 1000.0
        return {"ratio": ratio, "mhz": ratio * 100.0 + 2.0, "vid_v": round(v, 4), "vid_min": round(v - 0.004, 4), "vid_max": round(v + 0.005, 4),
                "kept": 28, "total": 30, "status": "ok"}

    @staticmethod
    def _summary(rec: dict) -> dict:
        ts = rec.get("tuning_state")
        return {"id": rec["id"], "name": rec["name"], "created": rec["created"], "mode": rec["mode"], "complete": rec["complete"],
                "tuning_state": {"kind": ts["kind"]} if isinstance(ts, dict) else {"kind": "unknown"}, "units": list(rec["units"]), "baseline": rec["baseline"]}

    # ---- dispatcher ----
    def handle_v3(self, rid: str, op: str, args: dict) -> None:
        try:
            getattr(self, "_op_" + op.replace(".", "_"))(rid, args)
        except Exception as e:  # a fake: surface programming errors as INTERNAL instead of killing the serve loop
            self._err(rid, "INTERNAL", f"fake helper bug: {e!r}")

    def _op_mem_read(self, rid, args):
        self._reply(rid, True, result=self.memory)

    # ---- curves ----
    def _op_curves_list(self, rid, args):
        rows = sorted(self.curves.values(), key=lambda r: r["created"], reverse=True)
        self._reply(rid, True, result={"curves": [self._summary(r) for r in rows]})

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
            self.curves[rec["id"]] = rec
            result = {"curve_id": rec["id"], "record": self._budgeted(rec)}
            if self.measure_problems:
                result["problems"] = list(self.measure_problems)
            self._reply(rid, True, result=result)
        finally:
            self._measuring.discard(rid)
            self._cancelled.discard(rid)

    # ---- tuning words ----
    def _op_tune_read(self, rid, args):
        self.tuning_read = True
        units = []
        for u in UNITS:
            t = self.tuning[u]
            oc = {**_entry(u, t["oc"]), "word": _word(u, t["oc"]), "mode": "adaptive", "static": False}
            points = [{**_entry(u, p), "v_target_v": None, "word": _word(u, p), "mode": "adaptive", "static": False} for p in t["points"]]
            cpus = [{"cpu": UNIT_CPUS[u][0], "name": (f"P-Core {UNIT_CPUS[u][0]}" if kind_of(u) == "P" else f"E-Core {UNIT_CPUS[u][0]}"),
                     "oc": oc, "points": points, "end": None}]
            name = f"P-Core {u[1:]}" if kind_of(u) == "P" else f"E-cluster {u[2:]} (CPUs {UNIT_CPUS[u][0]}-{UNIT_CPUS[u][-1]})"
            units.append({"name": name, "kind": kind_of(u), "cluster": None if kind_of(u) == "P" else int(u[2:]), "cluster_cpus": UNIT_CPUS[u],
                          "cpus": cpus, "measured": None})
        match = [{"cpu": UNIT_CPUS[u][0], "match": self.tuning[u] == self.accepted[u],
                  "diffs": [] if self.tuning[u] == self.accepted[u] else [{"point": 1, "expected": "0x0", "actual": "0x1"}]} for u in UNITS]
        self.ops_used_v3 += 112
        self._reply(rid, True, result={"simulator": True, "complete": True, "stopped": None, "units": units, "differences": [], "problems": [],
                                       "ops_done": 112, "accepted_match": match})

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
        cur = copy.deepcopy(self.tuning)
        new = copy.deepcopy(self.tuning)
        warnings: list[str] = []
        if "reset" in intent:
            for u in UNITS:
                for p in new[u]["points"] + [new[u]["oc"]]:
                    p["offset_mv"] = 0.0
                new[u]["oc"]["v_target_v"] = self.fused_vt[u]
                new[u]["oc"]["ratio"] = unit_max_ratio(u)
        elif "restore" in intent:
            new = copy.deepcopy(self.accepted)
        elif "profile" in intent:
            prof = self.profiles.get(intent["profile"])
            if prof is None:
                return self._err(rid, "BAD_REQUEST", "no such profile", check="NO_SUCH_CURVE")
            if prof["source"] == "accepted":
                new = copy.deepcopy(self.accepted)
            else:
                err = self._apply_rows(rid, new, prof["intent"].get("rows", []), warnings)
                if err:
                    return
        elif isinstance(intent.get("rows"), list):
            if self._apply_rows(rid, new, intent["rows"], warnings):
                return
        else:
            return self._err(rid, "BAD_REQUEST", "intent needs rows, profile, reset or restore")
        before, after, ops = {}, {}, 0
        changed_units = []
        for u in UNITS:
            b = [_entry(u, cur[u]["oc"])] + [_entry(u, p) for p in cur[u]["points"]]
            a = [_entry(u, new[u]["oc"])] + [_entry(u, p) for p in new[u]["points"]]
            before[u], after[u] = b, a
            diff = [x for x, y in zip(b, a) if x != y]
            if diff:
                changed_units.append(u)
                ops += 3 * len(diff) + 1
        if not changed_units:
            warnings.append("nothing changes: the plan equals the state last read")
        plan_id = hashlib.sha256(json.dumps([after, rid], sort_keys=True).encode()).hexdigest()
        nonce = "".join(secrets.choice(ALPHABET) for _ in range(6))
        steps = [{"step": i + 1, "what": f"write {u} ({len(UNIT_CPUS[u])} cpu)"} for i, u in enumerate(changed_units)]
        self.plans_v3[plan_id] = {"nonce": nonce, "used": False, "new": new, "changed": changed_units, "created": time.monotonic()}
        self._reply(rid, True, result={"plan_id": plan_id, "nonce": nonce, "steps": steps, "ops_needed": ops, "ops_left": self.ops_budget - self.ops_used_v3,
                                       "expires_s": 600, "before": before, "after": after, "warnings": warnings,
                                       "gates": [{"name": "first write from the new UI", "status": "not yet validated on this machine"}]})

    def _apply_rows(self, rid, new: dict, rows: list, warnings: list[str]) -> bool:
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
                    else:
                        for p in new[u]["points"]:
                            if str(p["ratio"]) in off["mv"]:
                                p["offset_mv"] = float(off["mv"][str(p["ratio"])])
                    if any(p["offset_mv"] > 0 for p in new[u]["points"]):
                        warnings.append(f"positive offset on {u}: raises voltage above the fused curve")
                vt = row.get("v_target_v")
                if vt is not None:
                    if not (V_TARGET_MIN - 1e-9 <= vt <= V_TARGET_MAX[k] + 1e-9):
                        self._refuse_range(rid, "v_target_v", f"{V_TARGET_MIN:.3f}..{V_TARGET_MAX[k]:.3f} V for {k}")
                        return True
                    new[u]["oc"]["v_target_v"] = float(vt)
                    if vt > self.fused_vt[u] + 1e-9:
                        warnings.append(f"target above fused on {u}: {vt:.3f} V > {self.fused_vt[u]:.3f} V")
                ratio = row.get("ratio")
                if ratio is not None:
                    if not (isinstance(ratio, int) and ratio <= RATIO_MAX[k]):
                        self._refuse_range(rid, "ratio", f"at most {RATIO_MAX[k]} for {k}")
                        return True
                    new[u]["oc"]["ratio"] = ratio
        return False

    # ---- apply (long) ----
    def _op_tune_apply(self, rid, args):
        p = self.plans_v3.get(args.get("plan_id"))
        if p is None:
            return self._err(rid, "PLAN_STALE", "unknown plan")
        if p["used"] or args.get("typed") != "CONFIRM " + p["nonce"]:
            return self._err(rid, "CONFIRM_MISMATCH", "typed text does not match")
        p["used"] = True
        self._applying.add(rid)
        threading.Thread(target=self._apply, args=(rid, p), daemon=True).start()

    def _apply(self, rid: str, p: dict) -> None:
        n = max(len(p["changed"]), 1)
        for i in range(n):
            if self.apply_delay:
                time.sleep(self.apply_delay)
            self.emit({"event": "progress", "op_id": rid, "step": i + 1, "of": n, "ops_used": self.ops_used_v3 + 3 * (i + 1), "ops_budget": self.ops_budget})
        readback = []
        for u in p["changed"]:
            for q in [p["new"][u]["oc"]] + p["new"][u]["points"]:
                exp = _word(u, q)
                bad = (u, q["point"]) in self.apply_mismatch
                readback.append({"unit": u, "point": q["point"], "expected": exp, "actual": "0x" + format(int(exp, 16) ^ 1, "08x") if bad else exp, "match": not bad})
        for u in p["changed"]:
            self.tuning[u] = copy.deepcopy(p["new"][u])
        self.ops_used_v3 += 3 * n
        self._applying.discard(rid)
        self._reply(rid, True, result={"steps": [{"step": i + 1, "what": f"write {u}"} for i, u in enumerate(p["changed"])], "readback": readback,
                                       "ops_used": 3 * n})

    # ---- profiles ----
    def _op_profiles_list(self, rid, args):
        rows = []
        for r in self.profiles.values():
            row = {"name": r["name"], "source": r["source"], "created": r["created"]}
            if self.profiles_include_intent and r.get("intent") is not None:
                row["intent"] = r["intent"]
            rows.append(row)
        self._reply(rid, True, result={"profiles": rows})

    def _op_profiles_save(self, rid, args):
        name, intent = args.get("name"), args.get("intent")
        if not isinstance(name, str) or not PROFILE_NAME.match(name) or not isinstance(intent, dict):
            return self._err(rid, "BAD_REQUEST", "name must match ^[a-z0-9._-]{1,64}$ and intent must be an object")
        if name in self.profiles:
            return self._err(rid, "REFUSED", f"profile {name!r} exists and is never overwritten", check="EXISTS")
        self.profiles[name] = {"name": name, "source": "saved", "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "intent": intent}
        self._reply(rid, True, result={"name": name, "saved": True})
