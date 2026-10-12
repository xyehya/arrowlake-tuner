"""ts_driver.py carve/hash logic on a synthetic file (no real driver, no Windows calls)."""
import hashlib
import importlib.util
import os
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ts_driver", ROOT / "tools" / "win" / "ts_driver.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)

H = lambda b: hashlib.sha256(b).hexdigest().upper()


class CarveTests(unittest.TestCase):
    def setUp(self):
        self.drv = bytes(range(256)) * 4
        self.exe = b"\xAA" * 100 + self.drv + b"\xBB" * 50
        self.kw = dict(exe_sha=H(self.exe), drv_sha=H(self.drv), offset=100, size=len(self.drv))

    def test_ok(self):
        self.assertEqual(ts.carve_bytes(self.exe, **self.kw), self.drv)

    def test_exe_hash_mismatch(self):
        with self.assertRaises(ts.DriverError):
            ts.carve_bytes(self.exe + b"x", **self.kw)

    def test_driver_hash_mismatch(self):
        with self.assertRaises(ts.DriverError):
            ts.carve_bytes(self.exe, **{**self.kw, "offset": 101})

    def test_too_short(self):
        with self.assertRaises(ts.DriverError):
            ts.carve_bytes(self.exe, **{**self.kw, "offset": 10_000})

    def test_carve_file_with_patched_constants(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "ts.exe"
            exe.write_bytes(self.exe)
            ts.EXE_SHA256, ts.DRIVER_SHA256 = self.kw["exe_sha"], self.kw["drv_sha"]
            ts.DRIVER_OFFSET, ts.DRIVER_SIZE = 100, len(self.drv)
            out = ts.carve(exe, Path(d) / "sub" / "x.sys")
            self.assertEqual(out.read_bytes(), self.drv)

    def test_constants_shape(self):
        self.assertEqual(len(ts.__dict__["EXE_SHA256"]), 64)



GOOD_SDDL = "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)"


class AdminOnlyDirectory(unittest.TestCase):
    """G-142 item 1/4: the driver and the accepted state live in an admin-only directory; the ACL is judged from its SDDL (locale independent)."""

    def test_sddl_admin_only_accepts_admins_and_system_only(self):
        for ok in (GOOD_SDDL, "O:SYG:SYD:P(A;OICI;FA;;;BA)", "O:S-1-5-32-544G:SYD:PAI(A;;FA;;;S-1-5-18)(A;;FA;;;S-1-5-32-544)",
                   "O:BAD:PAI(A;OICI;FA;;;BA)(D;OICI;FW;;;BU)",   # a deny entry for others only restricts
                   "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464)S:AI"):
            self.assertIsNone(ts.sddl_admin_only(ok), ok)

    def test_sddl_admin_only_refuses_everything_else_fail_closed(self):
        for bad in (
            "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)",          # Users
            "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;AU)",                # Authenticated Users
            "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;WD)",                # Everyone
            "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;CO)",                # Creator Owner
            "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",   # a user SID
            "O:S-1-5-21-1-2-3-1001G:DUD:PAI(A;OICI;FA;;;BA)",               # a user owns it (could re-grant themselves)
            "O:BAG:SYD:PAI",                                                # DACL without any allow entry
            "O:BAG:SY",                                                     # no DACL = full access for everyone
            "O:BAG:SYD:PAI(XA;;FA;;;BA;(Member_of{SID(BU)}))",              # conditional entries: not understood, refused
            "G:SYD:PAI(A;OICI;FA;;;BA)",                                    # no owner
            "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(Q;;;;;BU)",                      # unknown entry type
            "", "garbage", None,
        ):
            self.assertIsNotNone(ts.sddl_admin_only(bad), bad)

    def test_check_admin_dir(self):
        with tempfile.TemporaryDirectory() as d:
            ts.check_admin_dir(Path(d), get_sddl=lambda p: GOOD_SDDL)
            with self.assertRaises(ts.DriverError) as cm:
                ts.check_admin_dir(Path(d), get_sddl=lambda p: "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;BU)")
            self.assertIn("not an admin-only directory", str(cm.exception))
            with self.assertRaises(ts.DriverError):
                ts.check_admin_dir(Path(d) / "missing", get_sddl=lambda p: GOOD_SDDL)

    def test_init_dir_creates_hardens_and_verifies(self):
        calls = []

        def run(*a):
            calls.append(a)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "arltuner"
            self.assertEqual(ts.init_dir(target, run=run, get_sddl=lambda p: GOOD_SDDL), target)
            self.assertTrue(target.is_dir())
        self.assertEqual([c[1:] for c in calls],
                         [("/setowner", "*S-1-5-32-544"), ("/grant:r", "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"), ("/inheritance:r",)])

    def test_init_dir_refuses_a_hardening_that_did_not_take(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ts.DriverError):   # icacls "succeeded" but the ACL still lets Users in
                ts.init_dir(Path(d) / "a", run=lambda *a: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
                            get_sddl=lambda p: "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;BU)")
            with self.assertRaises(ts.DriverError):   # icacls failed
                ts.init_dir(Path(d) / "b", run=lambda *a: types.SimpleNamespace(returncode=5, stdout="", stderr="denied"), get_sddl=lambda p: GOOD_SDDL)

    def test_a_preexisting_directory_is_checked_never_adopted(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ts.DriverError):
                ts.init_dir(Path(d), run=lambda *a: calls.append(a), get_sddl=lambda p: "O:S-1-5-21-1-2-3-1001G:DUD:PAI(A;;FA;;;BA)")
            self.assertEqual(calls, [])   # no icacls was run on a directory somebody else may have prepared
            self.assertEqual(ts.init_dir(Path(d), run=lambda *a: calls.append(a), get_sddl=lambda p: GOOD_SDDL), Path(d))

    def test_the_default_locations_are_programdata_not_the_user_profile(self):
        old = dict(os.environ)
        try:
            os.environ["ProgramData"] = r"C:\PD"
            os.environ["LOCALAPPDATA"] = r"C:\Users\x\AppData\Local"
            self.assertEqual(ts.default_sys_path().name, "ThrottleStop.sys")
            self.assertIn("PD", str(ts.default_sys_path()))
            self.assertNotIn("AppData", str(ts.default_sys_path()))
            self.assertIn("AppData", str(ts.legacy_sys_path()))
        finally:
            os.environ.clear()
            os.environ.update(old)


class StartVerifiesBeforeEverySc(unittest.TestCase):
    """cmd_install: directory ACL + SHA-256 are re-verified immediately before ``sc start`` (a swap between create and start is caught)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.drv = bytes(range(256)) * 4
        self.path = Path(self.tmp.name) / "ThrottleStop.sys"
        self.path.write_bytes(self.drv)
        saved = (ts.DRIVER_SHA256, ts.is_admin, ts.dir_sddl, ts.sc)
        self.addCleanup(lambda: (setattr(ts, "DRIVER_SHA256", saved[0]), setattr(ts, "is_admin", saved[1]), setattr(ts, "dir_sddl", saved[2]),
                                 setattr(ts, "sc", saved[3])))
        ts.DRIVER_SHA256 = H(self.drv)
        ts.is_admin = lambda: True
        self.sddl = GOOD_SDDL
        ts.dir_sddl = lambda p: self.sddl
        self.log = []
        self.state = None            # None = no service
        self.binpath = ""
        self.on_create = lambda: None
        ts.sc = self.fake_sc

    def fake_sc(self, *a):
        self.log.append(a)
        R = types.SimpleNamespace
        if a[0] == "query":
            return R(returncode=1060 if self.state is None else 0, stdout="" if self.state is None else f"        STATE              : 1  {self.state}\n", stderr="")
        if a[0] == "qc":
            return R(returncode=0, stdout=f"        BINARY_PATH_NAME   : {self.binpath}\n", stderr="")
        if a[0] == "create":
            self.state, self.binpath = "STOPPED", a[a.index("binPath=") + 1]
            self.on_create()
        if a[0] == "start":
            self.state = "RUNNING"
        if a[0] == "delete":
            self.state = None
        return R(returncode=0, stdout="[SC] ok", stderr="")

    def ops(self):
        return [a[0] for a in self.log if a[0] in ("create", "start", "delete", "stop")]

    def test_creates_then_verifies_again_then_starts(self):
        self.assertEqual(ts.cmd_install(self.path), 0)
        self.assertEqual(self.ops(), ["create", "start"])
        create = next(a for a in self.log if a[0] == "create")
        self.assertEqual(create[create.index("binPath=") + 1], str(self.path.resolve()))

    def test_a_driver_swapped_between_create_and_start_is_never_started(self):
        self.on_create = lambda: self.path.write_bytes(b"evil" * 100)
        with self.assertRaises(ts.DriverError) as cm:
            ts.cmd_install(self.path)
        self.assertIn("not the expected driver", str(cm.exception))
        self.assertNotIn("start", self.ops())

    def test_a_directory_that_stopped_being_admin_only_is_never_started(self):
        def loosen():
            self.sddl = "O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;BU)"
        self.on_create = loosen
        with self.assertRaises(ts.DriverError):
            ts.cmd_install(self.path)
        self.assertNotIn("start", self.ops())

    def test_a_wrong_file_or_directory_is_refused_before_any_service_change(self):
        self.path.write_bytes(b"x")
        with self.assertRaises(ts.DriverError):
            ts.cmd_install(self.path)
        self.path.write_bytes(self.drv)
        self.sddl = "O:S-1-5-21-1-2-3-1001G:DUD:PAI(A;;FA;;;BA)"
        with self.assertRaises(ts.DriverError):
            ts.cmd_install(self.path)
        self.assertEqual(self.ops(), [])

    def test_a_service_pointing_elsewhere_is_deleted_and_recreated_from_the_verified_copy(self):
        self.state, self.binpath = "STOPPED", r"\??\C:\Users\x\AppData\Local\arltuner\ThrottleStop.sys"
        ts.cmd_install(self.path)
        self.assertEqual(self.ops(), ["stop", "delete", "create", "start"])

    def test_a_service_already_pointing_at_the_copy_is_reused(self):
        self.state, self.binpath = "STOPPED", str(self.path.resolve())
        ts.cmd_install(self.path)
        self.assertEqual(self.ops(), ["start"])

    def test_not_elevated_does_nothing(self):
        ts.is_admin = lambda: False
        with self.assertRaises(ts.DriverError):
            ts.cmd_install(self.path)
        self.assertEqual(self.log, [])

    def test_carve_on_windows_makes_the_directory_admin_only_first(self):
        exe = b"\xAA" * 100 + self.drv + b"\xBB" * 50
        saved = (ts.EXE_SHA256, ts.DRIVER_OFFSET, ts.DRIVER_SIZE)
        self.addCleanup(lambda: (setattr(ts, "EXE_SHA256", saved[0]), setattr(ts, "DRIVER_OFFSET", saved[1]), setattr(ts, "DRIVER_SIZE", saved[2])))
        ts.EXE_SHA256, ts.DRIVER_OFFSET, ts.DRIVER_SIZE = H(exe), 100, len(self.drv)
        src = Path(self.tmp.name) / "ts.exe"
        src.write_bytes(exe)
        seen = []
        saved_init = ts.init_dir
        self.addCleanup(lambda: setattr(ts, "init_dir", saved_init))
        ts.init_dir = lambda p, **k: seen.append(Path(p)) or Path(p)
        out = Path(self.tmp.name) / "sub" / "x.sys"
        out.parent.mkdir()
        ts.carve(src, out, secure=True)
        self.assertEqual(seen, [out.parent])
        self.assertEqual(out.read_bytes(), self.drv)


class WindowsScriptsAreAdminOnly(unittest.TestCase):
    """Static checks of the PowerShell (not run here - no Windows): where they read and write, and how the verdict is made."""

    def text(self, name):
        return (ROOT / "tools" / "win" / name).read_text()

    def test_state_policy_audit_and_driver_live_in_programdata(self):
        for name in ("restore_tuning.ps1", "reapply_current.ps1"):
            t = self.text(name)
            self.assertIn("$env:ProgramData 'arltuner", t, name)
            self.assertNotIn("$env:LOCALAPPDATA", t, name)
            self.assertIn("check-dir", t, name)
            self.assertIn("'-I'", t, name)     # isolated Python
        self.assertNotIn("LOCALAPPDATA", self.text("Restore-Tuning.cmd"))

    def test_the_service_is_created_per_run_from_the_verified_copy_and_deleted_after(self):
        t = self.text("restore_tuning.ps1")
        self.assertNotIn("sc.exe start", t)
        self.assertNotIn("Get-Service ThrottleStop", t)
        self.assertLess(t.index("Ts-Driver start"), t.index("Ts-Driver status"))
        self.assertLess(t.index("Ts-Driver check-dir") if "Ts-Driver check-dir" in t else t.index("check-dir"), t.index("Ts-Driver start"))
        self.assertIn("Ts-Driver remove", t[t.rindex("finally"):])

    def test_the_verdict_is_the_live_readback_not_the_audit_file(self):
        t = self.text("restore_tuning.ps1")
        self.assertNotIn("Get-Content $audit", t)
        self.assertNotIn("rc_name", t)
        self.assertIn("'vf-dump', '--json', $readback", t)
        self.assertIn('$readback = Join-Path $lm', t)      # the read-back file is in the admin-only directory
        self.assertLess(t.index("$readback"), t.index("RESTORED:"))


if __name__ == "__main__":
    unittest.main()
