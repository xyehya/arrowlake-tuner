"""T-013 policy tests: default M1 policy, strict TOML loading, and the write predicates (the T-021 additions -- session facts,
D-015 floor, 0x1D / 0x27 checks -- are in test_measured_checks.py).

Facts: docs/research/registers.md ("V/F data word", "Mailbox commands"); bounds: docs/tasks/T-013-guard.md "Policy".
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from arltuner.guard import CHECKS, AllowEntry, Policy, PolicyError, default_m1_policy, load_policy
from arltuner.guard.policy import CheckContext
from arltuner.mailbox import Cmd, VfWord

STOCK = VfWord(ratio=55, v_target_raw=0x4A0, static=False, offset_raw=0)
#: session facts of the checks below (T-021): 0x1C word and fused 0x09 point equal to STOCK's ratio / V target; E-core floor -102; the step
#: limits are relaxed (these tests are about the offset window and static mode; the steps are tested in test_bounds.py)
CTX = CheckContext(
    offset_floor_raw=-102,
    favored_word=55 | 55 << 8,
    fused_word=55 | 0x4A0 << 8,
    max_ratio_step=8,
    max_v_step_raw=200,
    max_offset_step_raw=200,
)


def req(data: int) -> SimpleNamespace:
    return SimpleNamespace(cpu=0, cmd=0x11, p1=0, p2=0, data=data, purpose="t")


VALID = """
cpus = [0, "8-11"]
max_operations_per_session = 3
min_dwell_s = 5

[[allow]]
name = "vf_read"
cmd = 0x10
p1 = [0, "2-2"]
p2 = [0]
kind = "read"

[[allow]]
name = "vf_write"
cmd = 0x11
p1 = [0]
p2 = [0]
kind = "write"
check = "vf_bounded"
params = { offset_raw_min = -50 }
"""


class DefaultPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = default_m1_policy()

    def test_limits(self) -> None:
        self.assertEqual(self.policy.cpus, frozenset(range(24)))
        self.assertEqual(self.policy.max_operations_per_session, 1)
        self.assertEqual(self.policy.min_dwell_s, 60.0)

    def test_exact_allowlist(self) -> None:
        by_cmd = {e.cmd: e for e in self.policy.allow}
        self.assertEqual(set(by_cmd), {0x01, 0x02, 0x09, 0x1C, 0x26, 0x28, 0x14, 0x10, 0x11, 0x15, 0x1D, 0x27})
        self.assertEqual({c for c, e in by_cmd.items() if e.kind == "write"}, {0x11, 0x15, 0x1D, 0x27})
        self.assertEqual(by_cmd[0x01].p1, frozenset(range(8)))
        self.assertEqual(by_cmd[0x01].p2, frozenset({0}))
        self.assertEqual(by_cmd[0x10].p1, frozenset({0, 2, 7}))
        self.assertEqual(by_cmd[0x10].p2, frozenset({0}))
        self.assertEqual((by_cmd[0x11].p1, by_cmd[0x11].p2), (frozenset({0}), frozenset({0})))
        self.assertEqual((by_cmd[0x15].p1, by_cmd[0x15].p2), (frozenset({0}), frozenset({0})))
        self.assertEqual(by_cmd[0x11].check, "vf_bounded")
        self.assertEqual(dict(by_cmd[0x11].params), {})  # D-016: the bounds are Policy fields, not entry params
        self.assertEqual(by_cmd[0x15].check, "percore_enable_only")
        for cmd in (0x1D, 0x26, 0x27):  # T-021: the XTU follow-ups of a per-core Apply and the read that feeds 0x27
            self.assertEqual((by_cmd[cmd].p1, by_cmd[cmd].p2), (frozenset({0}), frozenset({0})))
        self.assertEqual(by_cmd[0x1D].check, "favored_ratio_follows_vf")
        self.assertEqual(by_cmd[0x27].check, "granular_equals_read")

    def test_match_is_default_deny_and_strict(self) -> None:
        self.assertIsNotNone(self.policy.match(0x10, 0, 0))
        self.assertIsNone(self.policy.match(0x10, 1, 0))  # domain 1 not allowed
        self.assertIsNone(self.policy.match(0x10, 0, 1))  # V/F point 1 not allowed
        self.assertIsNone(self.policy.match(0x12, 0, 0))
        self.assertIsNone(self.policy.match(True, 0, 0))  # bool is not a cmd
        self.assertIsNone(self.policy.match(0x10, "0", 0))
        self.assertIsNone(self.policy.match(0x10, 0.0, 0))  # float p1

    def test_cpu_allowed_strict(self) -> None:
        self.assertTrue(self.policy.cpu_allowed(23))
        for bad in (24, -1, True, "0", 0.0, None):
            with self.subTest(bad=bad):
                self.assertFalse(self.policy.cpu_allowed(bad))

    def test_params_are_read_only(self) -> None:
        with self.assertRaises(TypeError):
            self.policy.allow[-1].params["offset_raw_min"] = -1000  # type: ignore[index]

    def test_check_registry_is_fixed(self) -> None:
        self.assertEqual(set(CHECKS), {"vf_bounded", "vf_point_offset_bounded", "percore_enable_only", "favored_ratio_follows_vf", "granular_equals_read"})
        with self.assertRaises(TypeError):
            CHECKS["evil"] = lambda *a: (True, "")  # type: ignore[index]


class Loading(unittest.TestCase):
    def test_valid_text(self) -> None:
        policy = load_policy(VALID)
        self.assertEqual(policy.cpus, frozenset({0, 8, 9, 10, 11}))
        self.assertEqual(policy.max_operations_per_session, 3)
        self.assertEqual(policy.min_dwell_s, 5.0)
        self.assertEqual([e.name for e in policy.allow], ["vf_read", "vf_write"])
        self.assertEqual(policy.allow[0].p1, frozenset({0, 2}))
        self.assertEqual(policy.allow[1].params["offset_raw_min"], -50)
        self.assertIs(policy.match(0x11, 0, 0), policy.allow[1])

    def test_from_file_path_and_str_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.toml"
            path.write_text(VALID, encoding="utf-8", newline="\n")
            self.assertEqual(load_policy(path), load_policy(VALID))
            self.assertEqual(load_policy(str(path)), load_policy(VALID))
            with self.assertRaises(PolicyError):
                load_policy(Path(tmp) / "missing.toml")

    def test_defaults_when_keys_absent(self) -> None:
        policy = load_policy("cpus = [0]\n")
        self.assertEqual((policy.max_operations_per_session, policy.min_dwell_s, policy.allow), (1, 60.0, ()))

    def assert_rejected(self, text: str, fragment: str = "") -> None:
        with self.assertRaises(PolicyError) as cm:
            load_policy(text)
        self.assertIn(fragment, str(cm.exception))

    def test_strictness(self) -> None:
        entry = 'name="a"\ncmd=0x10\np1=[0]\np2=[0]\nkind="read"\n'
        entry_b = entry.replace('name="a"', 'name="b"')
        write_entry = entry.replace("read", "write").replace("0x10", "0x11")
        percore_entry = entry.replace("read", "write").replace("0x10", "0x15")
        cases = {
            "unknown top-level key": ("cpus=[0]\nfoo=1\n", "foo"),
            "missing cpus": ("max_operations_per_session=1\n", "cpus"),
            "unknown entry key": (f"cpus=[0]\n[[allow]]\n{entry}extra=1\n", "extra"),
            "missing entry key": ("cpus=[0]\n[[allow]]\nname='a'\ncmd=0x10\np1=[0]\nkind='read'\n", "p2"),
            "unknown check": (f"cpus=[0]\n[[allow]]\n{write_entry}check='eval'\n", "eval"),
            "write without check": (f"cpus=[0]\n[[allow]]\n{write_entry}", "check"),
            "read with check": (f"cpus=[0]\n[[allow]]\n{entry}check='vf_bounded'\n", "read entry"),
            "read kind on a write cmd": (f"cpus=[0]\n[[allow]]\n{entry.replace('0x10', '0x11')}", "contradicts"),
            "write kind on a read cmd": (f"cpus=[0]\n[[allow]]\n{entry.replace('read', 'write')}check='vf_bounded'\n", "contradicts"),
            "unnamed cmd": (f"cpus=[0]\n[[allow]]\n{entry.replace('0x10', '0x30')}", "named"),
            "duplicate names": (f"cpus=[0]\n[[allow]]\n{entry}[[allow]]\n{entry.replace('p1=[0]', 'p1=[1]')}", "duplicate"),
            "overlap": (f"cpus=[0]\n[[allow]]\n{entry}[[allow]]\n{entry_b}", "overlap"),
            "bool cpu": ("cpus=[true]\n", "True"),
            "float cpu": ("cpus=[0.5]\n", "0.5"),
            "negative cpu": ("cpus=[-1]\n", "range"),
            "empty cpus": ("cpus=[]\n", "empty"),
            "cpus not a list": ("cpus=0\n", "list"),
            "bad range": ("cpus=['5-2']\n", "range"),
            "zero ops": ("cpus=[0]\nmax_operations_per_session=0\n", "max_operations"),
            "bool ops": ("cpus=[0]\nmax_operations_per_session=true\n", "max_operations"),
            "negative dwell": ("cpus=[0]\nmin_dwell_s=-1\n", "min_dwell_s"),
            "nan dwell": ("cpus=[0]\nmin_dwell_s=nan\n", "min_dwell_s"),
            "not toml": ("cpus = [0\n", "TOML"),
            "allow not tables": ("cpus=[0]\nallow=[1]\n", "allow"),
            "p1 empty": (f"cpus=[0]\n[[allow]]\n{entry.replace('p1=[0]', 'p1=[]')}", "empty"),
            "p1 out of byte range": (f"cpus=[0]\n[[allow]]\n{entry.replace('p1=[0]', 'p1=[256]')}", "range"),
            "params key unknown": (
                f"cpus=[0]\n[[allow]]\n{write_entry}check='vf_bounded'\nparams={{ offset_raw_maximum = 5 }}\n",
                "offset_raw_maximum",
            ),
            "percore takes no params": (
                f"cpus=[0]\n[[allow]]\n{percore_entry}check='percore_enable_only'\nparams={{ x = 1 }}\n",
                "'x'",
            ),
        }
        for label, (text, fragment) in cases.items():
            with self.subTest(label):
                self.assert_rejected(text, fragment)

    def test_offset_raw_bounds_accept_the_whole_field(self) -> None:
        # D-016: the offset window is a pair of bounds; neither side is restricted to the undervolt half any more
        base = 'cpus=[0]\n[[allow]]\nname="w"\ncmd=0x11\np1=[0]\np2=[0]\nkind="write"\ncheck="vf_bounded"\n'
        for key in ("offset_raw_min", "offset_raw_max"):
            for bad in ("1024", "-1025", "'x'", "true", "-50.5"):
                with self.subTest(key=key, bad=bad):
                    self.assert_rejected(base + f"params={{ {key} = {bad} }}\n", key)
            for good in ("0", "-1024", "-102", "1023", "20"):
                with self.subTest(key=key, good=good):
                    self.assertEqual(load_policy(base + f"params={{ {key} = {good} }}\n").allow[0].params[key], int(good))

    def test_non_text_source_rejected(self) -> None:
        with self.assertRaises(PolicyError):
            load_policy(42)  # type: ignore[arg-type]

    def test_direct_construction_is_validated_too(self) -> None:
        with self.assertRaises(PolicyError):
            AllowEntry("w", Cmd.VOLTAGE_FREQ_WRITE, frozenset({0}), frozenset({0}), "write")  # no check
        with self.assertRaises(PolicyError):
            Policy(cpus=frozenset({0}), allow=("not an entry",))  # type: ignore[arg-type]
        with self.assertRaises(PolicyError):
            Policy(cpus=frozenset({0}), min_dwell_s=float("inf"))


class VfBoundedCheck(unittest.TestCase):
    """The offset window and static mode of ``vf_bounded`` (the ceilings, steps and ordering are in test_bounds.py)."""

    check = staticmethod(CHECKS["vf_bounded"])
    base = STOCK.to_u32()
    params: dict = {}

    def run_check(self, new: VfWord, *, baseline: VfWord = STOCK, params=None, ctx: CheckContext | None = CTX) -> tuple[bool, str]:
        return self.check(req(new.to_u32()), baseline.to_u32(), self.params if params is None else params, ctx)

    def test_offset_bounds(self) -> None:
        # window [floor -102 (E core), ceiling 0]: positive offsets are refused until a policy raises offset_ceiling_mv
        for raw, ok in ((-102, True), (0, True), (-50, True), (-103, False), (1, False), (-1024, False), (1023, False)):
            with self.subTest(raw=raw):
                self.assertIs(self.run_check(STOCK.with_(offset_raw=raw))[0], ok)

    def test_the_offset_ceiling_comes_from_the_context(self) -> None:
        raised = CheckContext(**{**CTX.__dict__, "offset_ceiling_raw": 26})
        for raw, ok in ((26, True), (27, False), (1, True)):
            with self.subTest(raw=raw):
                self.assertIs(self.run_check(STOCK.with_(offset_raw=raw), ctx=raised)[0], ok)

    def test_entry_params_only_tighten_the_window(self) -> None:
        self.assertTrue(self.run_check(STOCK.with_(offset_raw=-50), params={"offset_raw_min": -50})[0])
        self.assertFalse(self.run_check(STOCK.with_(offset_raw=-51), params={"offset_raw_min": -50})[0])
        raised = CheckContext(**{**CTX.__dict__, "offset_ceiling_raw": 40})
        self.assertTrue(self.run_check(STOCK.with_(offset_raw=20), params={"offset_raw_max": 20}, ctx=raised)[0])
        self.assertFalse(self.run_check(STOCK.with_(offset_raw=21), params={"offset_raw_max": 20}, ctx=raised)[0])
        # a looser entry param never widens the policy window: min -1024 / max 1023 do not beat the floor / ceiling
        self.assertFalse(self.run_check(STOCK.with_(offset_raw=-103), params={"offset_raw_min": -1024})[0])
        self.assertFalse(self.run_check(STOCK.with_(offset_raw=1), params={"offset_raw_max": 1023})[0])

    def test_default_ceilings_are_the_no_raise_values(self) -> None:
        ok, detail = self.run_check(STOCK.with_(v_target_raw=STOCK.v_target_raw - 1, ratio=STOCK.ratio - 1))
        self.assertFalse(ok)  # ratio and V target both down in ONE word: the ordering rule (frequency first), not a ceiling
        self.assertIn("lower the ratio first", detail)
        self.assertTrue(self.run_check(STOCK.with_(v_target_raw=STOCK.v_target_raw - 1))[0])
        self.assertTrue(self.run_check(STOCK.with_(ratio=STOCK.ratio - 1))[0])
        self.assertFalse(self.run_check(STOCK.with_(v_target_raw=STOCK.v_target_raw + 1))[0])
        self.assertFalse(self.run_check(STOCK.with_(ratio=STOCK.ratio + 1))[0])

    def test_static_mode_refused_either_side(self) -> None:
        self.assertFalse(self.run_check(STOCK.with_(static=True))[0])
        static_base = STOCK.with_(static=True)
        self.assertFalse(self.run_check(STOCK, baseline=static_base)[0])
        self.assertFalse(self.run_check(static_base, baseline=static_base)[0])

    def test_identical_word_is_fine(self) -> None:
        self.assertTrue(self.run_check(STOCK)[0])


class PerCoreEnableCheck(unittest.TestCase):
    check = staticmethod(CHECKS["percore_enable_only"])

    def test_only_bit3_may_be_set(self) -> None:
        for baseline in (0x0, 0x1, 0x11, 0xFFFFFFF7):
            with self.subTest(baseline=hex(baseline)):
                self.assertTrue(self.check(req(baseline | 0x8), baseline, {})[0])
        self.assertFalse(self.check(req(0x0), 0x0, {})[0])  # bit 3 not set: not the allowed change
        self.assertFalse(self.check(req(0x8 | 0x100), 0x0, {})[0])  # extra bit
        self.assertFalse(self.check(req(0x9 & ~0x1), 0x1, {})[0])  # clears bit 0
        self.assertFalse(self.check(req(0xFFFFFFFF), 0x0, {})[0])


class AutoLimitsTests(unittest.TestCase):
    """D-036 / G-128: ``"auto"`` ceilings and CPU sets are derived live; numeric values keep working."""

    TOML = 'cpus = "auto"\ne_cpus = "auto"\n[ratio_max]\nP = "auto"\nE = "auto"\n[v_target_max_mv]\nP = "auto"\nE = "auto"\n'

    def ctx(self, policy, cpu, vmax=None):
        from arltuner.guard.policy import build_context

        return build_context(policy, cpu, favored_word=None, fused_word=None, vf_word_sent=None, max_voltage_word=vmax)

    def test_auto_needs_the_live_topology_never_a_default(self) -> None:
        for text in ('cpus = "auto"\n', 'cpus = [0]\ne_cpus = "auto"\n', 'cpus = [0]\n[ratio_max]\nP = "auto"\n'):
            with self.subTest(text=text), self.assertRaisesRegex(PolicyError, "live topology"):
                load_policy(text)

    def test_cpus_and_ratio_ceilings_come_from_the_topology(self) -> None:
        from arltuner.topology import arl_s_285k, synthetic

        p = load_policy(self.TOML, arl_s_285k())
        self.assertEqual((p.cpus, p.e_cpus), (frozenset(range(24)), frozenset(range(8, 24))))
        self.assertEqual((p.ratio_max["P"], p.ratio_max["E"]), (57, 49))  # the highest cpuinfo_max_freq ratio of each kind
        six = load_policy(self.TOML, synthetic(6, 8, favored_p=(0,), p_ratio=50, favored_ratio=52, e_ratio=40))
        self.assertEqual((six.cpus, six.e_cpus), (frozenset(range(14)), frozenset(range(6, 14))))
        self.assertEqual((six.ratio_max["P"], six.ratio_max["E"]), (52, 40))
        self.assertEqual((six.core_kind(5), six.core_kind(6)), ("P", "E"))

    def test_a_ratio_ceiling_with_no_sysfs_number_is_the_no_raise_value(self) -> None:
        from arltuner.topology import Core, Topology

        bare = Topology((Core("P", 0, 0, None, False), Core("E", 0, 1, 0, False)))
        p = load_policy(self.TOML, bare)
        self.assertEqual((p.ratio_max["P"], p.ratio_max["E"]), (None, None))

    def test_a_live_ratio_above_the_sanity_bound_is_clamped(self) -> None:
        from arltuner.guard.policy import SANITY_RATIO_MAX
        from arltuner.topology import synthetic

        p = load_policy(self.TOML, synthetic(2, 4, p_ratio=90, favored_ratio=90, e_ratio=90))
        self.assertEqual((p.ratio_max["P"], p.ratio_max["E"]), (SANITY_RATIO_MAX["P"], SANITY_RATIO_MAX["E"]))

    def test_numeric_values_keep_working_and_a_policy_without_e_cpus_takes_the_live_ones(self) -> None:
        from arltuner.topology import synthetic

        p = load_policy('cpus = ["0-13"]\n[ratio_max]\nP = 57\nE = 49\n[v_target_max_mv]\nP = 1625\nE = 1355\n', synthetic(6, 8))
        self.assertEqual((p.ratio_max["P"], p.v_target_max_mv["E"], p.v_target_auto), (57, 1355, frozenset()))
        self.assertEqual(p.e_cpus, frozenset(range(6, 14)))

    def test_v_target_auto_is_the_0x28_maximum_minus_one_lsb(self) -> None:
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        self.assertEqual(p.v_target_auto, frozenset({"P", "E"}))
        self.assertEqual(self.ctx(p, 0, 0x681).v_target_max_raw, 0x680)  # P: 1.625 V
        self.assertAlmostEqual(0x680 / 1024, 1.625)
        self.assertEqual(self.ctx(p, 8, 0x56D).v_target_max_raw, 0x56C)  # E: 1.3555 V

    def test_v_target_auto_is_never_above_the_sanity_bound(self) -> None:
        from arltuner.guard.policy import SANITY_V_TARGET_MAX_MV
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        self.assertEqual(self.ctx(p, 0, 0x7FF).v_target_max_raw, VfWord.v_to_raw(SANITY_V_TARGET_MAX_MV["P"] / 1000))
        self.assertEqual(self.ctx(p, 8, 0xFFF).v_target_max_raw, VfWord.v_to_raw(SANITY_V_TARGET_MAX_MV["E"] / 1000))

    def test_v_target_auto_with_a_zero_or_unread_0x28_allows_no_raise(self) -> None:
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        self.assertIsNone(self.ctx(p, 0, 0x0).v_target_max_raw)  # a field of 0 means 2.0 V to hw_v_max_raw: auto must not inherit that
        self.assertIsNone(self.ctx(p, 0, 0x1000).v_target_max_raw)  # only [11:0] counts
        self.assertIsNone(self.ctx(p, 0, None).v_target_max_raw)

    def test_the_default_m1_policy_floor_is_not_widened(self) -> None:
        p = default_m1_policy()
        self.assertEqual(dict(p.assumed_bios_offset_mv), {"P": -50, "E": 0})
        self.assertEqual((p.offset_floor_raw(0), p.offset_floor_raw(8)), (-51, -102))  # P runtime floor -50 mV, E -100 mV, as before G-128

    def test_default_m1_policy_follows_a_given_topology(self) -> None:
        from arltuner.topology import synthetic

        p = default_m1_policy(synthetic(6, 8))
        self.assertEqual((p.cpus, p.e_cpus), (frozenset(range(14)), frozenset(range(6, 14))))
        self.assertEqual(p.with_topology(synthetic(8, 12)).cpus, frozenset(range(20)))


if __name__ == "__main__":
    unittest.main()
