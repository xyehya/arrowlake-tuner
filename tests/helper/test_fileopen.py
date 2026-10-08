"""Files the helper opens for writing (audit log, instance lock, the guard's lock): O_NOFOLLOW + fstat on the descriptor, owned by the helper's euid,
not writable by others. A planted symlink or a foreign-owned file is refused by name (TRUST_PATH / LOCK_UNTRUSTED), never followed or taken over.
Owner checks are injected (``euid_fn``): no test chowns anything or needs root."""
from __future__ import annotations

import os
import types
import unittest
from pathlib import Path

from arltuner.helper.core import EXIT_REFUSED_TO_START
from arltuner.helper.trust import TrustError, check_lock_file, open_checked

from ._support import Harness, HelperCase, error_code

POSIX = os.name == "posix"
TARGET_TEXT = "precious\n"


class OpenCheckedTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.euid = os.geteuid() if POSIX else 0

    def test_creates_with_o_excl_and_reopens(self) -> None:
        p = self.tmp / "f"
        os.close(open_checked(p, write=True, create=True, euid=self.euid))
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        os.close(open_checked(p, write=True, create=True, euid=self.euid))  # exists: opened, not recreated

    def test_missing_file_without_create_propagates(self) -> None:
        with self.assertRaises(FileNotFoundError):
            open_checked(self.tmp / "nope", write=False, create=False, euid=self.euid)

    @unittest.skipUnless(POSIX, "symlink semantics")
    def test_symlink_is_refused_and_the_target_untouched(self) -> None:
        target = self.tmp / "target"
        target.write_text(TARGET_TEXT)
        link = self.tmp / "link"
        link.symlink_to(target)
        for create in (True, False):
            with self.subTest(create=create), self.assertRaises(TrustError) as ctx:
                open_checked(link, write=True, create=create, euid=self.euid)
            self.assertIn("symlink", ctx.exception.reason)
        self.assertEqual(target.read_text(), TARGET_TEXT)

    @unittest.skipUnless(POSIX, "symlink semantics")
    def test_dangling_symlink_is_not_created_through(self) -> None:
        link = self.tmp / "dangling"
        link.symlink_to(self.tmp / "created-if-followed")
        with self.assertRaises(TrustError):
            open_checked(link, write=True, create=True, euid=self.euid)
        self.assertFalse((self.tmp / "created-if-followed").exists())

    def test_foreign_owner_is_refused_via_the_injected_fstat(self) -> None:
        p = self.tmp / "f"
        p.write_text("x")
        os.chmod(p, 0o600)
        foreign = lambda fd: types.SimpleNamespace(st_mode=os.fstat(fd).st_mode, st_uid=4242)  # noqa: E731
        with self.assertRaises(TrustError) as ctx:
            open_checked(p, write=True, create=False, euid=0 if self.euid else 1, fstat_fn=foreign)
        self.assertIn("owned by uid 4242", ctx.exception.reason)

    @unittest.skipUnless(POSIX, "modes")
    def test_group_or_world_writable_is_refused(self) -> None:
        p = self.tmp / "f"
        p.write_text("x")
        for mode in (0o660, 0o606, 0o666):
            os.chmod(p, mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(TrustError):
                open_checked(p, write=True, create=False, euid=self.euid)

    def test_a_directory_is_not_a_file(self) -> None:
        with self.assertRaises(TrustError):
            open_checked(self.tmp, write=False, create=False, euid=self.euid)

    @unittest.skipUnless(POSIX, "symlink semantics")
    def test_check_lock_file(self) -> None:
        check_lock_file(self.tmp / "absent", euid=self.euid)
        plain = self.tmp / "lock"
        plain.write_text("1\n")
        check_lock_file(plain, euid=self.euid)
        with self.assertRaises(TrustError):
            check_lock_file(plain, euid=self.euid + 1)  # foreign owner
        link = self.tmp / "ln"
        link.symlink_to(plain)
        with self.assertRaises(TrustError) as ctx:
            check_lock_file(link, euid=self.euid)
        self.assertIn("symlink", ctx.exception.reason)


@unittest.skipUnless(POSIX, "symlink semantics / euid")
class HelperFileTests(HelperCase):
    def target(self) -> Path:
        t = self.tmp / "target"
        t.write_text(TARGET_TEXT)
        return t

    def foreign_euid(self):
        """An ``euid_fn`` that is truthful for the start-up call (the instance lock) and reports another uid afterwards, so every file the
        running helper then meets that really belongs to this user looks foreign-owned."""
        calls = []

        def euid() -> int:
            calls.append(1)
            return os.geteuid() if len(calls) == 1 else os.geteuid() + 1

        return euid

    # ---- the audit log
    def test_symlink_at_the_audit_path_refuses_vf_dump_and_audit_tail_and_the_target_is_untouched(self) -> None:
        target = self.target()
        self.audit.symlink_to(target)
        h = self.start()
        for rid, op, args in (("d", "vf.dump", {"cpus": "0"}), ("t", "audit.tail", {})):
            reply = h.call(rid, op, args)
            self.assertEqual(error_code(reply), "REFUSED", reply)
            self.assertEqual(reply["error"]["detail"]["check"], "TRUST_PATH")
            self.assertIn("symlink", reply["error"]["detail"]["reason"])
        self.assertEqual(target.read_text(), TARGET_TEXT)
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_dangling_symlink_at_the_audit_path_is_not_created_through(self) -> None:
        ghost = self.tmp / "ghost"
        self.audit.symlink_to(ghost)
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["check"], "TRUST_PATH")
        self.assertFalse(ghost.exists())

    def test_foreign_owned_audit_log_is_refused(self) -> None:
        self.audit.write_text("")
        os.chmod(self.audit, 0o644)
        h = self.start(self.config(euid_fn=self.foreign_euid()))
        for rid, op, args in (("d", "vf.dump", {"cpus": "0"}), ("t", "audit.tail", {})):
            reply = h.call(rid, op, args)
            self.assertEqual(reply["error"]["detail"]["check"], "TRUST_PATH", reply)
            self.assertIn("not by the helper", reply["error"]["detail"]["reason"])
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_world_writable_audit_log_is_refused(self) -> None:
        self.audit.write_text("")
        os.chmod(self.audit, 0o666)
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["check"], "TRUST_PATH")

    # ---- the guard's per-transaction lock file
    def test_symlink_at_the_guard_lock_path_is_lock_untrusted_and_the_target_untouched(self) -> None:
        target = self.target()
        self.lock.symlink_to(target)
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "LOCK_UNTRUSTED")
        self.assertEqual(target.read_text(), TARGET_TEXT)
        self.assertTrue(self.lock.is_symlink())  # left as found
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_foreign_owned_guard_lock_is_lock_untrusted_not_taken_over(self) -> None:
        self.lock.write_text("4242\n")
        h = self.start(self.config(euid_fn=self.foreign_euid()))
        reply = h.call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["check"], "LOCK_UNTRUSTED")
        self.assertEqual(self.lock.read_text(), "4242\n")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_own_stale_guard_lock_is_the_guards_locked_refusal(self) -> None:
        self.lock.write_text("4242\n")
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["check"], "LOCKED")

    # ---- the instance lock (<lock>.helper)
    def instance_lock_path(self) -> Path:
        return self.lock.with_name(self.lock.name + ".helper")

    def refused_start(self, cfg) -> dict:
        h = Harness(cfg)
        self.harnesses.append(h)
        h.start()
        msg = h.response(None)
        self.assertEqual(h.join(), EXIT_REFUSED_TO_START)
        self.assertEqual(h.messages, [msg])
        return msg

    def test_symlink_at_the_instance_lock_path_refuses_to_start_and_the_target_is_untouched(self) -> None:
        target = self.target()
        self.instance_lock_path().symlink_to(target)
        msg = self.refused_start(self.config())
        self.assertEqual((error_code(msg), msg["error"]["detail"]["check"]), ("REFUSED", "LOCK_UNTRUSTED"))
        self.assertEqual(target.read_text(), TARGET_TEXT)  # no pid written through the link

    def test_foreign_owned_instance_lock_refuses_to_start_and_is_not_taken_over(self) -> None:
        self.instance_lock_path().write_text("999\n")
        os.chmod(self.instance_lock_path(), 0o600)
        msg = self.refused_start(self.config(euid_fn=lambda: os.geteuid() + 1))
        self.assertEqual(msg["error"]["detail"]["check"], "LOCK_UNTRUSTED")
        self.assertEqual(self.instance_lock_path().read_text(), "999\n")

    def test_world_writable_instance_lock_refuses_to_start(self) -> None:
        self.instance_lock_path().write_text("")
        os.chmod(self.instance_lock_path(), 0o666)
        msg = self.refused_start(self.config())
        self.assertEqual(msg["error"]["detail"]["check"], "LOCK_UNTRUSTED")

    def test_the_lock_path_is_injectable(self) -> None:
        other = self.tmp / "elsewhere.lock"
        h = self.start(self.config(lock_path=other))
        self.assertTrue(h.call("d", "vf.dump", {"cpus": "0"})["ok"])
        self.assertTrue(other.with_name("elsewhere.lock.helper").exists())


if __name__ == "__main__":
    unittest.main()
