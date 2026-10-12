"""Preflight tests (G-101): every check, every failure path, against fake roots in a temp dir. No real /proc, /sys or /dev is read."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from arltuner.platform import OPTIONAL_TOOLS, Status, preflight
from arltuner.topology import arl_s_285k, synthetic
from tests.fakesys import add_dmi, make_sysfs, reference_like

GOOD_FLAGS = "fpu vme de pse tsc msr pae mce cx8 apic sep"


def cpuinfo(n: int = 24, *, flags: str = GOOD_FLAGS, family: int = 6, model: int = 198, stepping: int = 2,
            vendor: str = "GenuineIntel", name: str = "Intel(R) Core(TM) Ultra 9 285K") -> str:
    blocks = []
    for i in range(n):
        blocks.append(f"processor\t: {i}\nvendor_id\t: {vendor}\ncpu family\t: {family}\nmodel\t\t: {model}\nmodel name\t: {name}\n"
                      f"stepping\t: {stepping}\nflags\t\t: {flags}\n")
    return "\n".join(blocks)


class FakeRoot(unittest.TestCase):
    """setUp builds a root on which every check passes; tests then break one thing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.write("proc/cpuinfo", cpuinfo())
        self.write("sys/kernel/security/lockdown", "[none] integrity confidentiality\n")
        reference_like(self.root / "sys")  # 8 P + 16 E, favored P0 and P3 by CPPC: the live topology the checks derive (D-036)
        add_dmi(self.root / "sys", "Z890 Steel Legend WiFi", "ASRock")
        for c in range(24):
            self.write(f"dev/cpu/{c}/msr", "")
        self.write("sys/module/msr/parameters/allow_writes", "default\n")
        self.exe("usr/bin/pkexec")
        self.write("usr/lib/polkit-1/polkitd", "")
        self.write("proc/1/comm", "systemd\n")
        self.write("proc/200/comm", "polkit-gnome-au\n")  # comm is truncated to 15 chars
        for tool in OPTIONAL_TOOLS:
            self.exe(f"usr/bin/{tool}")

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def exe(self, rel: str) -> Path:
        p = self.write(rel, "#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def run_pf(self, **kw):
        return preflight(self.root, **kw)

    def status(self, name: str, **kw) -> Status:
        return self.run_pf(**kw).get(name).status


class AllGoodTests(FakeRoot):
    def test_everything_passes(self):
        rep = self.run_pf(python_version=(3, 12, 1))
        self.assertEqual([r.name for r in rep.results if r.status is not Status.PASS], [])
        self.assertEqual(rep.mode, "write")
        self.assertTrue(rep.writes_allowed)
        self.assertEqual(rep.problems(), ())

    def test_check_names(self):
        names = [r.name for r in self.run_pf().results]
        self.assertEqual(names[:10], ["cpuid", "hypervisor", "lockdown", "topology", "msr_device", "msr_allow_writes", "pkexec", "polkit", "polkit_agent", "python"])
        self.assertEqual(names[10:], [f"tool:{t}" for t in OPTIONAL_TOOLS])

    def test_to_dict_is_json(self):
        d = json.loads(json.dumps(self.run_pf().to_dict()))
        self.assertEqual(d["platform"], "arl-s")
        self.assertEqual(d["mode"], "write")
        self.assertEqual(d["board"], "Z890 Steel Legend WiFi")
        self.assertEqual(d["checks"][0], {"name": "cpuid", "status": "pass", "reason": d["checks"][0]["reason"]})

    def test_the_result_carries_the_live_topology_in_the_g127_shape(self):
        topo = self.run_pf().to_dict()["topology"]
        self.assertEqual(set(topo), {"units", "favored_source"})
        self.assertEqual(topo["favored_source"], "acpi_cppc")
        self.assertEqual([u["id"] for u in topo["units"]], [f"P{i}" for i in range(8)] + [f"EC{i}" for i in range(4)])
        self.assertEqual(topo["units"][0], {"id": "P0", "kind": "P", "cpus": [0], "favored": True, "max_ratio": 57, "perf": 92})
        self.assertEqual(topo["units"][8], {"id": "EC0", "kind": "E", "cpus": [8, 9, 10, 11], "favored": False, "max_ratio": 49, "perf": 68})
        self.assertEqual([u["id"] for u in topo["units"] if u["favored"]], ["P0", "P3"])

    def test_a_simulated_topology_is_reported_instead_of_the_real_sysfs(self):
        rep = self.run_pf(topology=synthetic(6, 8, favored_p=(1,)))
        topo = rep.to_dict()["topology"]
        self.assertEqual([u["id"] for u in topo["units"]], [f"P{i}" for i in range(6)] + ["EC0", "EC1"])
        self.assertEqual([u["id"] for u in topo["units"] if u["favored"]], ["P1"])
        self.assertEqual(rep.get("msr_device").status, Status.PASS)  # the device nodes are checked against the simulated CPU list (0..13)


    def test_default_root_argument_is_slash(self):
        import inspect
        self.assertEqual(inspect.signature(preflight).parameters["root"].default, Path("/"))

    def test_get_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.run_pf().get("nope")


class CpuidTests(FakeRoot):
    def test_wrong_model(self):
        self.write("proc/cpuinfo", cpuinfo(model=183))
        r = self.run_pf().get("cpuid")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("not an Arrow Lake-S CPU", r.reason)

    def test_an_unknown_stepping_is_a_warning_not_a_refusal(self):
        self.write("proc/cpuinfo", cpuinfo(stepping=1))
        rep = self.run_pf()
        r = rep.get("cpuid")
        self.assertEqual(r.status, Status.WARN)
        self.assertIn("stepping 1", r.reason)
        self.assertEqual(rep.mode, "write")  # a warning never changes the mode
        self.assertTrue(rep.writes_allowed)
        self.assertEqual(rep.problems(), ())
        self.assertEqual(json.loads(json.dumps(rep.to_dict()))["checks"][0]["status"], "warn")

    def test_another_sku_of_the_family_passes(self):
        self.write("proc/cpuinfo", cpuinfo(n=14, name="Intel(R) Core(TM) Ultra 5 245K"))  # the name is not part of the identity
        self.assertEqual(self.status("cpuid"), Status.PASS)

    def test_wrong_vendor(self):
        self.write("proc/cpuinfo", cpuinfo(vendor="AuthenticAMD"))
        self.assertEqual(self.status("cpuid"), Status.READ_ONLY)

    def test_offline_cpus_do_not_fail_cpuid(self):
        self.write("proc/cpuinfo", cpuinfo(n=16))  # the topology check owns the CPU count
        self.assertEqual(self.status("cpuid"), Status.PASS)

    def test_one_odd_cpu_is_caught(self):
        self.write("proc/cpuinfo", cpuinfo(23) + "\n" + cpuinfo(1, model=1).replace("processor\t: 0", "processor\t: 23"))
        self.assertEqual(self.status("cpuid"), Status.READ_ONLY)

    def test_non_numeric_fields(self):
        self.write("proc/cpuinfo", cpuinfo().replace("stepping\t: 2", "stepping\t: x"))
        r = self.run_pf().get("cpuid")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("not numeric", r.reason)

    def test_missing_cpuinfo(self):
        (self.root / "proc/cpuinfo").unlink()
        rep = self.run_pf()
        self.assertEqual(rep.get("cpuid").status, Status.READ_ONLY)
        self.assertEqual(rep.get("hypervisor").status, Status.READ_ONLY)
        self.assertEqual(rep.mode, "read-only")

    def test_empty_cpuinfo(self):
        self.write("proc/cpuinfo", "")
        self.assertEqual(self.status("cpuid"), Status.READ_ONLY)


class TopologyTests(FakeRoot):
    """The identity gate is CPUID family/model plus a consistent live topology (D-036)."""

    def rebuild(self, p, e, **kw):
        shutil.rmtree(self.root / "sys/devices")
        make_sysfs(self.root / "sys", p, e, **kw)

    def test_8p_16e_passes_and_names_the_favored_cores(self):
        r = self.run_pf().get("topology")
        self.assertEqual(r.status, Status.PASS)
        self.assertIn("8 P-cores + 16 E-cores in 4 clusters of 4", r.reason)
        self.assertIn("favored by acpi_cppc: P0, P3", r.reason)

    def test_other_shapes_pass(self):
        for p, e in ((8, 12), (6, 8), (4, 8)):
            with self.subTest(p=p, e=e):
                self.rebuild(p, e)
                self.assertEqual(self.status("topology"), Status.PASS)

    def test_no_e_cores_is_read_only(self):
        (self.root / "sys/devices/cpu_atom/cpus").write_text("\n")
        r = self.run_pf().get("topology")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertEqual(self.run_pf().mode, "read-only")

    def test_unequal_clusters_are_read_only(self):
        d = self.root / "sys/devices/system/cpu"
        for cpu in (20, 21, 22, 23):
            (d / f"cpu{cpu}/topology/cluster_cpus_list").write_text("20-21\n" if cpu < 22 else "22-23\n")
        r = self.run_pf().get("topology")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("unequal", r.reason)

    def test_missing_sysfs_files_are_read_only_with_the_reason(self):
        shutil.rmtree(self.root / "sys/devices")
        rep = self.run_pf()
        self.assertEqual(rep.get("topology").status, Status.READ_ONLY)
        self.assertIn("cpu_core", rep.get("topology").reason)
        self.assertEqual(rep.get("msr_device").status, Status.READ_ONLY)  # without a CPU list there is nothing to check the device nodes against
        self.assertEqual(rep.to_dict()["topology"], {"units": [], "favored_source": None})

    def test_all_p_cores_equal_means_no_favored_core(self):
        make_sysfs(self.root / "sys", 8, 16, perf={c: 88 for c in range(8)})
        topo = self.run_pf().to_dict()["topology"]
        self.assertIsNone(topo["favored_source"])
        self.assertFalse(any(u["favored"] for u in topo["units"]))
        self.assertIn("no favored core", self.run_pf().get("topology").reason)

    def test_the_msr_device_check_follows_the_live_cpu_list(self):
        self.rebuild(6, 8)
        for c in range(14, 24):  # a 6P+8E part only has /dev/cpu/0..13
            shutil.rmtree(self.root / f"dev/cpu/{c}")
        self.assertEqual(self.status("msr_device"), Status.PASS)
        (self.root / "dev/cpu/13/msr").unlink()
        r = self.run_pf().get("msr_device")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("/dev/cpu/13/msr", r.reason)


class BoardTests(FakeRoot):
    def test_board_name_is_reported_when_readable(self):
        self.assertEqual(self.run_pf().board, "Z890 Steel Legend WiFi")

    def test_no_dmi_means_no_board(self):
        shutil.rmtree(self.root / "sys/class")
        self.assertIsNone(self.run_pf().board)


class HypervisorTests(FakeRoot):
    def test_flag_set(self):
        self.write("proc/cpuinfo", cpuinfo(flags=GOOD_FLAGS + " hypervisor"))
        r = self.run_pf().get("hypervisor")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertEqual(self.run_pf().mode, "read-only")

    def test_substring_does_not_count(self):
        self.write("proc/cpuinfo", cpuinfo(flags=GOOD_FLAGS + " nothypervisorx"))
        self.assertEqual(self.status("hypervisor"), Status.PASS)

    def test_no_flags_line(self):
        self.write("proc/cpuinfo", "\n".join(b.replace("flags\t\t: " + GOOD_FLAGS, "") for b in cpuinfo().split("\n\n")))
        self.assertEqual(self.status("hypervisor"), Status.READ_ONLY)


class LockdownTests(FakeRoot):
    def test_none(self):
        self.assertEqual(self.status("lockdown"), Status.PASS)

    def test_integrity(self):
        self.write("sys/kernel/security/lockdown", "none [integrity] confidentiality\n")
        r = self.run_pf().get("lockdown")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("[integrity]", r.reason)

    def test_confidentiality(self):
        self.write("sys/kernel/security/lockdown", "none integrity [confidentiality]\n")
        self.assertEqual(self.status("lockdown"), Status.READ_ONLY)

    def test_unbracketed(self):
        self.write("sys/kernel/security/lockdown", "none integrity confidentiality\n")
        self.assertEqual(self.status("lockdown"), Status.READ_ONLY)

    def test_two_brackets(self):
        self.write("sys/kernel/security/lockdown", "[none] [integrity]\n")
        self.assertEqual(self.status("lockdown"), Status.READ_ONLY)

    def test_file_absent_is_no_lockdown(self):
        (self.root / "sys/kernel/security/lockdown").unlink()
        r = self.run_pf().get("lockdown")
        self.assertEqual(r.status, Status.PASS)
        self.assertIn("without the lockdown LSM", r.reason)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_unreadable(self):
        p = self.root / "sys/kernel/security/lockdown"
        p.chmod(0)
        self.addCleanup(p.chmod, 0o644)
        self.assertEqual(self.status("lockdown"), Status.READ_ONLY)


class MsrTests(FakeRoot):
    def test_device_missing_one_cpu(self):
        (self.root / "dev/cpu/5/msr").unlink()
        r = self.run_pf().get("msr_device")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("/dev/cpu/5/msr", r.reason)

    def test_device_missing_all_module_not_found(self):
        shutil.rmtree(self.root / "dev")
        shutil.rmtree(self.root / "sys/module")
        r = self.run_pf().get("msr_device")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("neither loaded, built in nor found", r.reason)

    def test_device_missing_module_loadable(self):
        shutil.rmtree(self.root / "dev")
        shutil.rmtree(self.root / "sys/module")
        self.write("proc/sys/kernel/osrelease", "7.2.9-200.fc44.x86_64\n")
        self.write("lib/modules/7.2.9-200.fc44.x86_64/kernel/arch/x86/kernel/msr.ko.xz", "")
        r = self.run_pf().get("msr_device")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("loadable", r.reason)

    def test_device_missing_module_loaded(self):
        shutil.rmtree(self.root / "dev")  # /sys/module/msr still exists (from setUp)
        self.assertIn("loaded or built in", self.run_pf().get("msr_device").reason)

    def test_osrelease_with_slash_is_not_followed(self):
        shutil.rmtree(self.root / "dev")
        shutil.rmtree(self.root / "sys/module")
        self.write("proc/sys/kernel/osrelease", "../../x\n")
        self.assertIn("neither", self.run_pf().get("msr_device").reason)

    def test_allow_writes_off(self):
        self.write("sys/module/msr/parameters/allow_writes", "off\n")
        r = self.run_pf().get("msr_allow_writes")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("off", r.reason)

    def test_allow_writes_on(self):
        self.write("sys/module/msr/parameters/allow_writes", "on\n")
        self.assertEqual(self.status("msr_allow_writes"), Status.PASS)

    def test_allow_writes_garbage(self):
        self.write("sys/module/msr/parameters/allow_writes", "banana\n")
        self.assertEqual(self.status("msr_allow_writes"), Status.READ_ONLY)

    def test_allow_writes_param_absent_but_module_present(self):
        (self.root / "sys/module/msr/parameters/allow_writes").unlink()
        self.assertEqual(self.status("msr_allow_writes"), Status.PASS)

    def test_allow_writes_module_absent(self):
        shutil.rmtree(self.root / "sys/module")
        r = self.run_pf().get("msr_allow_writes")
        self.assertEqual(r.status, Status.READ_ONLY)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_allow_writes_unreadable(self):
        p = self.root / "sys/module/msr/parameters/allow_writes"
        p.chmod(0)
        self.addCleanup(p.chmod, 0o644)
        r = self.run_pf().get("msr_allow_writes")
        self.assertEqual(r.status, Status.UNKNOWN)  # 0600 file: an unprivileged run must not call the machine read-only
        self.assertIn("root-only", r.reason)
        self.assertEqual(self.run_pf().mode, "write")


class PolkitTests(FakeRoot):
    def test_pkexec_missing(self):
        (self.root / "usr/bin/pkexec").unlink()
        r = self.run_pf().get("pkexec")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("helper cannot start", r.reason)

    def test_pkexec_not_executable(self):
        (self.root / "usr/bin/pkexec").chmod(0o644)
        if os.geteuid() != 0:
            self.assertEqual(self.status("pkexec"), Status.READ_ONLY)

    def test_pkexec_in_custom_dir(self):
        (self.root / "usr/bin/pkexec").unlink()
        self.exe("opt/x/pkexec")
        self.assertEqual(self.status("pkexec"), Status.READ_ONLY)
        self.assertEqual(self.status("pkexec", path_dirs=["/opt/x"]), Status.PASS)

    def test_polkitd_missing(self):
        (self.root / "usr/lib/polkit-1/polkitd").unlink()
        self.assertEqual(self.status("polkit"), Status.READ_ONLY)

    def test_polkitd_libexec_location(self):
        (self.root / "usr/lib/polkit-1/polkitd").unlink()
        self.write("usr/libexec/polkitd", "")
        self.assertEqual(self.status("polkit"), Status.PASS)

    def test_no_agent(self):
        shutil.rmtree(self.root / "proc/200")
        r = self.run_pf().get("polkit_agent")
        self.assertEqual(r.status, Status.READ_ONLY)
        self.assertIn("no polkit authentication agent", r.reason)

    def test_polkitd_daemon_is_not_an_agent(self):
        shutil.rmtree(self.root / "proc/200")
        self.write("proc/300/comm", "polkitd\n")
        self.assertEqual(self.status("polkit_agent"), Status.READ_ONLY)

    def test_gnome_shell_counts_as_agent(self):
        shutil.rmtree(self.root / "proc/200")
        self.write("proc/300/comm", "gnome-shell\n")
        self.assertEqual(self.status("polkit_agent"), Status.PASS)

    def test_pkttyagent(self):
        shutil.rmtree(self.root / "proc/200")
        self.write("proc/300/comm", "pkttyagent\n")
        self.assertEqual(self.status("polkit_agent"), Status.PASS)

    def test_kde_agent(self):
        shutil.rmtree(self.root / "proc/200")
        self.write("proc/300/comm", "polkit-kde-agent-1\n")
        self.assertEqual(self.status("polkit_agent"), Status.PASS)

    def test_non_pid_proc_entries_ignored(self):
        shutil.rmtree(self.root / "proc/200")
        self.write("proc/self/comm", "gnome-shell\n")
        self.assertEqual(self.status("polkit_agent"), Status.READ_ONLY)

    def test_proc_missing(self):
        shutil.rmtree(self.root / "proc")
        self.assertEqual(self.status("polkit_agent"), Status.READ_ONLY)


class PythonTests(FakeRoot):
    def test_old_python_refuses(self):
        rep = self.run_pf(python_version=(3, 11, 9))
        self.assertEqual(rep.get("python").status, Status.REFUSE)
        self.assertEqual(rep.mode, "refuse")
        self.assertTrue(rep.refused)
        self.assertFalse(rep.writes_allowed)

    def test_exact_minimum_passes(self):
        self.assertEqual(self.status("python", python_version=(3, 12, 0)), Status.PASS)

    def test_newer_passes(self):
        self.assertEqual(self.status("python", python_version=(3, 14, 1)), Status.PASS)

    def test_refuse_outranks_read_only(self):
        self.write("sys/kernel/security/lockdown", "none [integrity] confidentiality\n")
        self.assertEqual(self.run_pf(python_version=(3, 10, 0)).mode, "refuse")

    def test_running_interpreter_default(self):
        self.assertEqual(self.status("python"), Status.PASS)  # the suite itself needs >= 3.12


class ToolTests(FakeRoot):
    def test_missing_tool_reports_package(self):
        (self.root / "usr/bin/stress-ng").unlink()
        rep = self.run_pf()
        r = rep.get("tool:stress-ng")
        self.assertEqual(r.status, Status.MISSING)
        self.assertEqual(r.reason, "missing: install stress-ng")
        self.assertEqual(rep.mode, "write")  # optional tools never change the mode

    def test_every_tool_checked(self):
        for tool in OPTIONAL_TOOLS:
            (self.root / f"usr/bin/{tool}").unlink()
        rep = self.run_pf()
        for tool in OPTIONAL_TOOLS:
            self.assertEqual(rep.get(f"tool:{tool}").status, Status.MISSING)
        self.assertEqual(set(OPTIONAL_TOOLS), {"stress-ng", "stressapptest", "perf", "turbostat", "sensors", "nvidia-smi"})

    def test_custom_tool_list(self):
        rep = self.run_pf(tools={"foo": "foo-pkg"})
        self.assertEqual(rep.get("tool:foo").reason, "missing: install foo-pkg")
        with self.assertRaises(KeyError):
            rep.get("tool:perf")

    def test_sbin_tool_found(self):
        (self.root / "usr/bin/perf").unlink()
        self.exe("usr/sbin/perf")
        self.assertEqual(self.status("tool:perf"), Status.PASS)


class ModeTests(FakeRoot):
    def test_any_read_only_gives_read_only(self):
        self.write("proc/cpuinfo", cpuinfo(flags=GOOD_FLAGS + " hypervisor"))
        rep = self.run_pf()
        self.assertEqual(rep.mode, "read-only")
        self.assertEqual([r.name for r in rep.problems()], ["hypervisor"])

    def test_every_failure_reason_is_nonempty(self):
        shutil.rmtree(self.root / "proc")
        shutil.rmtree(self.root / "dev")
        shutil.rmtree(self.root / "sys")
        rep = self.run_pf(python_version=(3, 8, 0))
        for r in rep.results:
            self.assertTrue(r.reason, r.name)
        self.assertEqual(rep.mode, "refuse")


if __name__ == "__main__":
    unittest.main()
