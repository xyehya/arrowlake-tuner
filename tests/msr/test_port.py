"""Tests for arltuner.msr.port and the package surface (T-011)."""
from __future__ import annotations

import errno
import importlib
import sys
import unittest
from unittest import mock

import arltuner
from arltuner.msr import DevCpuMsrPort, MailboxTimeout, MsrError, MsrPort, SimMsrPort
from arltuner.msr.port import MAX_CPU, MAX_MSR, MAX_VALUE, check_cpu, check_msr, check_value


class TestErrors(unittest.TestCase):
    def test_msrerror_is_oserror_and_carries_errno(self):
        err = MsrError(errno.EIO, "boom")
        self.assertIsInstance(err, OSError)
        self.assertEqual(err.errno, errno.EIO)
        self.assertIn("boom", str(err))

    def test_msrerror_stays_msrerror_for_any_errno(self):
        for code in (errno.EPERM, errno.ENOENT, errno.EACCES):
            self.assertIs(type(MsrError(code, "x")), MsrError)

    def test_mailbox_timeout_is_a_plain_exception(self):
        self.assertTrue(issubclass(MailboxTimeout, Exception))
        self.assertFalse(issubclass(MailboxTimeout, OSError))


class TestChecks(unittest.TestCase):
    def test_accepts_boundaries_and_returns_plain_int(self):
        self.assertEqual(check_cpu(0), 0)
        self.assertEqual(check_cpu(MAX_CPU), MAX_CPU)
        self.assertEqual(check_msr(MAX_MSR), MAX_MSR)
        self.assertEqual(check_value(MAX_VALUE), MAX_VALUE)

    def test_rejects_out_of_range_and_non_int(self):
        bad = {
            check_cpu: (-1, MAX_CPU + 1, True, 1.0, "0", None),
            check_msr: (-1, MAX_MSR + 1, False, 2.5),
            check_value: (-1, MAX_VALUE + 1, True, "1"),
        }
        for fn, values in bad.items():
            for value in values:
                with self.subTest(fn=fn.__name__, value=value), self.assertRaises(ValueError):
                    fn(value)


class TestProtocol(unittest.TestCase):
    def test_runtime_checkable(self):
        self.assertIsInstance(SimMsrPort(), MsrPort)
        self.assertIsInstance(DevCpuMsrPort(root="."), MsrPort)
        self.assertNotIsInstance(object(), MsrPort)

    def test_import_has_no_side_effects(self):
        """Re-importing the package in isolation opens no file at all (os.open / builtins.open would raise)."""
        names = [k for k in sys.modules if k == "arltuner.msr" or k.startswith("arltuner.msr.")]
        saved = {k: sys.modules.pop(k) for k in names}
        try:
            with mock.patch("os.open", side_effect=AssertionError("os.open at import")), mock.patch(
                "builtins.open", side_effect=AssertionError("open at import")
            ):
                fresh = importlib.import_module("arltuner.msr")
            self.assertIsNot(fresh, saved["arltuner.msr"])
        finally:
            for k in [k for k in sys.modules if k == "arltuner.msr" or k.startswith("arltuner.msr.")]:
                del sys.modules[k]
            sys.modules.update(saved)
            arltuner.msr = saved["arltuner.msr"]


if __name__ == "__main__":
    unittest.main()
