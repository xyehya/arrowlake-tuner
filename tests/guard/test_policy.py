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

    OC = 0x778  # cmd 0x01 P1=0 as the reference machine answers it: 120x, bits [10:8] set

    def ctx(self, policy, cpu, vmax=None, favored=None, kind_words=None, oc=(OC,)):
        from arltuner.guard.policy import build_context

        return build_context(
            policy, cpu, favored_word=favored, fused_word=None, vf_word_sent=None, max_voltage_word=vmax, favored_words_of_kind=kind_words,
            oc_cap_words_of_kind=oc,
        )

    def test_auto_needs_the_live_topology_never_a_default(self) -> None:
        for text in ('cpus = "auto"\n', 'cpus = [0]\ne_cpus = "auto"\n'):
            with self.subTest(text=text), self.assertRaisesRegex(PolicyError, "live topology"):
                load_policy(text)

    def test_cpus_come_from_the_topology_and_auto_ratios_are_not_numbers_at_load(self) -> None:
        from arltuner.topology import arl_s_285k, synthetic

        p = load_policy(self.TOML, arl_s_285k())
        self.assertEqual((p.cpus, p.e_cpus), (frozenset(range(24)), frozenset(range(8, 24))))
        self.assertEqual((p.ratio_max["P"], p.ratio_max["E"], p.ratio_auto), (None, None, frozenset({"P", "E"})))  # CPU data of the session, not sysfs
        six = load_policy(self.TOML, synthetic(6, 8))
        self.assertEqual((six.cpus, six.e_cpus), (frozenset(range(14)), frozenset(range(6, 14))))
        self.assertEqual((six.core_kind(5), six.core_kind(6)), ("P", "E"))
        load_policy('cpus = [0]\n[ratio_max]\nP = "auto"\n')  # needs no topology

    def test_auto_ratio_is_the_fused_value_the_cpu_reported_in_0x1c(self) -> None:
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        self.assertEqual(self.ctx(p, 0, None, 0x3939).ratio_max, 60)  # P: 0x1C[15:8] = [7:0] = 57, + 3 (D-038)
        self.assertEqual(self.ctx(p, 8, None, 0x2E30).ratio_max, 51)  # E: max(48, 46) + 3
        self.assertEqual(self.ctx(p, 1, None, 0x3737).ratio_max, 58)
        self.assertEqual(self.ctx(p, 0, None, 0x3739).ratio_max, 60)  # the larger byte
        self.assertEqual(self.ctx(p, 0, None, 0x3739, kind_words=[0x3737, 0x3939]).ratio_max, 60)  # per kind: the highest word read

    def test_fused_ratio_cap_adds_the_oc_headroom_and_clamps_to_the_cpu_limit(self) -> None:
        """D-038: fused max of the kind + AUTO_OC_HEADROOM (3); D-039: clamped by the CPU's own 0x01 limit when given, by no constant; None when no 0x1C of the kind was read."""
        from arltuner.guard.policy import AUTO_OC_HEADROOM, fused_ratio_cap

        self.assertEqual(AUTO_OC_HEADROOM, 3)
        self.assertEqual(fused_ratio_cap("P", [0x3939]), 60)  # P fused 57 -> 60
        self.assertEqual(fused_ratio_cap("E", [0x2E2E]), 49)  # E fused 46 -> 49
        self.assertEqual(fused_ratio_cap("E", [0x2E30, 0x2E2E]), 51)  # the highest word read; [7:0] 48 beats [15:8] 46
        self.assertEqual(fused_ratio_cap("P", [0x3D3D]), 64)  # 61 + 3: no code constant clamps it any more
        self.assertEqual(fused_ratio_cap("P", [0x3D3D], hw_max=62), 62)  # the CPU's own limit does
        self.assertEqual(fused_ratio_cap("P", [0x3939], hw_max=120), 60)
        for words in ([], [None], [0], [None, 0]):
            self.assertIsNone(fused_ratio_cap("P", words))  # nothing read: no raise

    def test_auto_ratio_unread_or_zero_allows_no_raise(self) -> None:
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        self.assertIsNone(self.ctx(p, 0, None, None).ratio_max)
        self.assertIsNone(self.ctx(p, 0, None, 0).ratio_max)

    def test_auto_ratio_is_clamped_by_the_cpu_limit_not_by_a_constant(self) -> None:
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        self.assertEqual(self.ctx(p, 0, None, 0xFFFF, oc=[0x13E]).ratio_max, 62)  # 0x01 says 62x: 255 + 3 clamps to it
        self.assertEqual(self.ctx(p, 8, None, 0xFFFF, oc=[0x134]).ratio_max, 52)
        self.assertEqual(self.ctx(p, 0, None, 0x3939, oc=[0x13E]).ratio_max, 60)  # below the CPU limit: fused 57 + 3
        self.assertIn("CPU limit 62x (cmd 0x01", self.ctx(p, 0, None, 0xFFFF, oc=[0x13E]).ratio_text)

    def test_auto_ratio_without_the_cpu_limit_allows_no_raise(self) -> None:
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        c = self.ctx(p, 0, None, 0x3939, oc=())
        self.assertIsNone(c.ratio_max)
        self.assertIn("no-raise value", c.ratio_text)
        self.assertIn("cmd 0x01", c.ratio_text)

    def test_a_cpuinfo_max_freq_change_does_not_move_the_cap(self) -> None:
        from arltuner.topology import synthetic

        before = load_policy(self.TOML, synthetic(8, 16, favored_p=(0,), e_ratio=49))
        after = load_policy(self.TOML, synthetic(8, 16, favored_p=(0,), e_ratio=50))  # an OC write raised cpuinfo_max_freq by one bin
        self.assertEqual(before.ratio_max, after.ratio_max)
        self.assertEqual(self.ctx(before, 8, None, 0x2E30).ratio_max, self.ctx(after, 8, None, 0x2E30).ratio_max)  # the cap follows the fused word only

    def test_numeric_ratio_values_are_unchanged(self) -> None:
        from arltuner.topology import arl_s_285k

        p = load_policy('cpus = ["0-23"]\n[ratio_max]\nP = 57\nE = 49\n', arl_s_285k())
        self.assertEqual((self.ctx(p, 0, None, 0x3737).ratio_max, self.ctx(p, 8, None, 0x2E30).ratio_max), (57, 49))  # the CPU allows 120x
        self.assertEqual((self.ctx(p, 0, None, 0x3737, oc=()).ratio_max, self.ctx(p, 8, None, 0x2E30, oc=()).ratio_max), (None, None))  # 0x01 unread: no raise (D-039)

    def test_auto_ratio_applies_to_the_plain_turbo_write_context(self) -> None:
        from arltuner.guard.policy import build_msr_context
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        oc = {0: self.OC, 8: self.OC}
        e = build_msr_context(p, 0x650, platform_info=None, favored_words={8: 0x2E30, 9: 0x2E30, 0: 0x3939}, oc_cap_words=oc)
        self.assertEqual(e.ratio_max, 51)  # only the E CPUs' words count for the E table (48 + 3)
        self.assertEqual(build_msr_context(p, 0x1AD, platform_info=None, favored_words={0: 0x3939, 8: 0x2E30}, oc_cap_words=oc).ratio_max, 60)
        self.assertIsNone(build_msr_context(p, 0x650, platform_info=None, favored_words={0: 0x3939}, oc_cap_words=oc).ratio_max)
        self.assertIsNone(build_msr_context(p, 0x650, platform_info=None, oc_cap_words=oc).ratio_max)
        self.assertIsNone(build_msr_context(p, 0x650, platform_info=None, favored_words={8: 0x2E30}, oc_cap_words={0: self.OC}).ratio_max)  # the E limit was not read: no raise
        self.assertEqual(build_msr_context(p, 0x650, platform_info=None, favored_words={8: 0x2E30}, oc_cap_words={9: 0x134}).ratio_max, 51)  # any CPU of the kind answers for it

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

    def test_v_target_auto_follows_the_cpu_whatever_its_value(self) -> None:
        """D-039: the code keeps no V target ceiling of its own; the CPU's 0x28 maximum minus one LSB is the whole limit."""
        from arltuner.topology import arl_s_285k

        p = load_policy(self.TOML, arl_s_285k())
        self.assertEqual(self.ctx(p, 0, 0x7FF).v_target_max_raw, 0x7FE)  # 1.998 V: above the old P bound of 1.625 V
        self.assertEqual(self.ctx(p, 8, 0xFFF).v_target_max_raw, 0xFFE)

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
