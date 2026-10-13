"""G-148 (D-044) ``arltuner accepted-migrate``: an accepted chain (the reference machine's ``e-p6-split3`` -> ``p-boot-ramp`` -> ``p-step7`` with its
``EXPECTED-words.txt``) becomes one complete profile, from the words (never a read), with a backup; and that one profile, applied on a cold simulated CPU in one
run, reproduces ``EXPECTED-words.txt`` exactly. Temp directories and the simulator only: never ``/etc/arltuner`` (worker rule).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from arltuner.helper.ops import _parse_expected
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.profiles import load_profile
from arltuner.topology import arl_s_285k

from .test_cli import CliCase

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples/reference-285k/profiles"
CHAIN = ("e-p6-split3", "p-boot-ramp", "p-step7")


class Migrate(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = self.tmp / "etc"
        self.parent.mkdir()
        os.chmod(self.parent, 0o755)
        self.acc = self.parent / "accepted"
        (self.acc / "profiles").mkdir(parents=True)
        (self.acc / "CURRENT").write_text("# an accepted state written before G-148\npolicy = policies/x.toml\n" + "".join(f"profile = profiles/{n}.toml\n" for n in CHAIN))
        for n in CHAIN:
            shutil.copy(EXAMPLE / f"{n}.toml", self.acc / "profiles" / f"{n}.toml")
        shutil.copy(EXAMPLE / "EXPECTED-words.txt", self.acc / "EXPECTED-words.txt")

    def migrate(self, *extra: str):
        return self.run_cli("accepted-migrate", "--dir", self.acc, "--sim", *extra, wall_clock=lambda: 1_792_000_000.0)

    def snapshot(self) -> dict[str, str]:
        return {str(p.relative_to(self.parent)): p.read_text() for p in sorted(self.parent.rglob("*")) if p.is_file()}

    def test_dry_run_prints_the_profile_and_changes_nothing(self) -> None:
        before = self.snapshot()
        code, out, err = self.migrate("--dry-run")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("accepted chain: e-p6-split3 -> p-boot-ramp -> p-step7", out)
        self.assertIn("becomes one complete profile", out)
        self.assertIn("every value the chain set is the same in the migrated profile", out)  # EXPECTED-words.txt is what the chain left on the chip
        self.assertIn("complete = true", out)
        self.assertIn("not part of the accepted state", out)  # the chain has no turbo tables
        self.assertEqual(self.snapshot(), before)

    def test_migrate_makes_one_complete_profile_and_keeps_a_backup(self) -> None:
        old_current = (self.acc / "CURRENT").read_text()
        expected_text = (self.acc / "EXPECTED-words.txt").read_text()
        code, out, err = self.migrate()
        self.assertEqual((code, err), (0, ""), out)
        backups = [p for p in self.parent.iterdir() if p.name.startswith("accepted.bak-")]
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "CURRENT").read_text(), old_current)
        lines = [line for line in (self.acc / "CURRENT").read_text().splitlines() if line.startswith("profile")]
        self.assertEqual(len(lines), 1)
        rel = lines[0].split("=", 1)[1].strip()
        prof = load_profile(self.acc / rel)
        self.assertTrue(prof.complete)
        prof.check_complete(arl_s_285k())
        self.assertIsNone(prof.turbo)
        self.assertEqual((self.acc / "EXPECTED-words.txt").read_text(), expected_text)  # unchanged, byte for byte
        self.assertEqual(sorted(p.name for p in (self.acc / "profiles").iterdir()), [Path(rel).name])
        code, out, _ = self.migrate()
        self.assertEqual(code, 0)
        self.assertIn("nothing to migrate", out)

    def test_the_migrated_profile_reproduces_expected_words_on_a_cold_cpu(self) -> None:
        self.assertEqual(self.migrate()[0], 0)
        rel = next(line for line in (self.acc / "CURRENT").read_text().splitlines() if line.startswith("profile")).split("=", 1)[1].strip()
        policy = REPO / "policies" / "default.toml"  # the shipped policy (D-032 window): the reference offsets reach -91 mV
        sim = SimMsrPort(SimConfig.measured(per_point=True, reject_zero_fields=True, sticky_oc_ratio=True))
        code, out, err = self.run_cli("apply", self.acc / rel, "--sim", "--policy", policy, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), sim=sim)
        self.maxDiff = None
        self.assertEqual((code, err), (0, ""), out)
        topo = arl_s_285k()
        for cpu, want in _parse_expected((self.acc / "EXPECTED-words.txt").read_text()).items():
            for c in topo.unit_cpus(topo.unit_of_cpu(cpu)):
                self.assertEqual([sim.vf(c, 0, p).to_u32() for p in range(len(want))], want, f"cpu {c}")
        self.assertFalse([r for r in sim.requests if r.cmd == 0x11 and r.p2 == 0])  # the OC words were already the accepted ones

    def test_a_fingerprint_is_kept_and_supplies_the_top_ratios(self) -> None:
        sim = SimMsrPort(SimConfig.measured(per_point=True))
        from arltuner.profiles.fingerprint import build_fingerprint

        fp = build_fingerprint(arl_s_285k(), {c.cpu: sim.fused_word(c.cpu) for c in arl_s_285k().cores}, None).to_json("2026-10-12T10:00:00Z")
        (self.acc / "fingerprint.json").write_text(fp)
        code, out, err = self.migrate()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("top fused ratios from fingerprint.json", out)
        self.assertEqual((self.acc / "fingerprint.json").read_text(), fp)

    def test_an_untrusted_parent_is_refused_and_nothing_changes(self) -> None:
        os.chmod(self.parent, 0o777)
        self.addCleanup(os.chmod, self.parent, 0o755)
        before = self.snapshot()
        code, out, err = self.migrate()
        self.assertEqual(code, 2)
        self.assertIn("world-writable", err)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse([p for p in self.parent.iterdir() if p.name.startswith("accepted.bak-")])

    def test_expected_words_that_miss_a_unit_are_refused(self) -> None:
        text = (self.acc / "EXPECTED-words.txt").read_text()
        (self.acc / "EXPECTED-words.txt").write_text("\n".join(line for line in text.splitlines() if not line.startswith("20 ")) + "\n")
        code, _, err = self.migrate("--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("EC3", err)
