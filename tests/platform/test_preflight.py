"""Preflight tests (G-101): every check, every failure path, against fake roots in a temp dir. No real /proc, /sys or /dev is read."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from arltuner.platform import OPTIONAL_TOOLS, Status, preflight

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
        self.assertEqual(names[:9], ["cpuid", "hypervisor", "lockdown", "msr_device", "msr_allow_writes", "pkexec", "polkit", "polkit_agent", "python"])
        self.assertEqual(names[9:], [f"tool:{t}" for t in OPTIONAL_TOOLS])

    def test_to_dict_is_json(self):
        d = json.loads(json.dumps(self.run_pf().to_dict()))
        self.assertEqual(d["platform"], "arl-s-285k")
        self.assertEqual(d["mode"], "write")
        self.assertEqual(d["checks"][0], {"name": "cpuid", "status": "pass", "reason": d["checks"][0]["reason"]})

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
        self.assertIn("does not match", r.reason)

    def test_wrong_stepping(self):
        self.write("proc/cpuinfo", cpuinfo(stepping=1))
        self.assertEqual(self.status("cpuid"), Status.READ_ONLY)

    def test_wrong_vendor(self):
        self.write("proc/cpuinfo", cpuinfo(vendor="AuthenticAMD"))
        self.assertEqual(self.status("cpuid"), Status.READ_ONLY)

    def test_offline_cpus_do_not_fail_cpuid(self):
        self.write("proc/cpuinfo", cpuinfo(n=16))  # topology.detect owns the CPU count
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
