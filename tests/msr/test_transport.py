"""Tests for arltuner.msr.transport (T-011). Fake ports and a fake clock only: no hardware, no real sleeping."""
from __future__ import annotations

import errno
import time
import unittest

from arltuner.mailbox import MSR_OC_MAILBOX, Response, pack
from arltuner.msr import MailboxTimeout, MailboxTransport, MsrError

from ._support import BUSY, FakeClock, ScriptedPort, resp


def make(port, **kwargs):
    fc = FakeClock()
    return MailboxTransport(port, sleep=fc.sleep, clock=fc.clock, **kwargs), fc


class TestSequence(unittest.TestCase):
    def test_read_then_write_then_poll_in_order(self):
        port = ScriptedPort([BUSY, BUSY, 0, BUSY, resp(0, 0x1234)])
        transport, fc = make(port)
        out = transport.transact(4, 0x10, 0, 0, 0)
        word = pack(0x10, 0, 0, 0)
        self.assertEqual(
            port.ops,
            [
                ("r", 4, MSR_OC_MAILBOX),
                ("r", 4, MSR_OC_MAILBOX),
                ("r", 4, MSR_OC_MAILBOX),  # first idle read
                ("w", 4, MSR_OC_MAILBOX, word),
                ("r", 4, MSR_OC_MAILBOX),  # busy after write
                ("r", 4, MSR_OC_MAILBOX),  # done
            ],
        )
        self.assertEqual(out, Response(rc=0, data=0x1234))
        self.assertEqual(fc.sleeps, [0.01, 0.01, 0.01])  # between reads only: 2 before the write, 1 after

    def test_written_word_is_the_codec_word(self):
        # registers.md example: SET 0x11 P1=0 P2=0 data 0xF9A00000 (legacy plane word, -50 mV) -> 0x80000011_F9A00000
        port = ScriptedPort([0, 0])
        transport, _ = make(port)
        transport.transact(2, 0x11, 0, 0, 0xF9A00000)
        self.assertEqual(port.writes, [("w", 2, 0x150, 0x80000011F9A00000)])

    def test_default_data_is_zero(self):
        port = ScriptedPort([0, 0])
        transport, _ = make(port)
        transport.transact(0, 0x01, 3, 0)
        self.assertEqual(port.writes[0][3], pack(0x01, 3, 0, 0))

    def test_returns_rc_and_data_of_last_read(self):
        port = ScriptedPort([0, BUSY | 0xFFFF, resp(0x13, 0xDEADBEEF)])
        transport, _ = make(port)
        self.assertEqual(transport.transact(1, 0x11, 0, 0, 0), Response(rc=0x13, data=0xDEADBEEF))

    def test_no_sleep_when_already_idle(self):
        port = ScriptedPort([0, 0])
        transport, fc = make(port)
        transport.transact(0, 0x14, 0, 0)
        self.assertEqual(fc.sleeps, [])
        self.assertEqual(len(port.reads), 2)


class TestTimeouts(unittest.TestCase):
    def test_busy_before_write_aborts_without_writing(self):
        port = ScriptedPort([], then=BUSY)
        transport, fc = make(port, poll_sleep_s=0.25, timeout_s=1.0)  # binary-exact times
        with self.assertRaises(MailboxTimeout) as ctx:
            transport.transact(0, 0x11, 0, 0, 0)
        self.assertEqual(ctx.exception.args, ("busy before write",))
        self.assertEqual(ctx.exception.phase, "before")
        self.assertFalse(ctx.exception.write_sent)
        self.assertEqual(port.writes, [])  # nothing written
        self.assertEqual(len(port.reads), 5)  # reads at t=0, .25, .5, .75, 1.0; raised once elapsed >= timeout
        self.assertEqual(len(fc.sleeps), 4)

    def test_timeout_boundary_is_elapsed_ge_timeout(self):
        port = ScriptedPort([], then=BUSY)
        transport, fc = make(port, poll_sleep_s=1.0, timeout_s=3.0)
        with self.assertRaises(MailboxTimeout):
            transport.transact(0, 0x10, 0, 0)
        self.assertEqual(len(port.reads), 4)
        self.assertEqual(fc.sleeps, [1.0, 1.0, 1.0])

    def test_busy_after_write_raises_but_write_was_sent(self):
        port = ScriptedPort([0], then=BUSY)
        transport, _ = make(port, poll_sleep_s=0.25, timeout_s=1.0)
        with self.assertRaises(MailboxTimeout) as ctx:
            transport.transact(3, 0x11, 0, 0, 0x5B4)
        self.assertEqual(ctx.exception.args, ("busy after write",))
        self.assertEqual(ctx.exception.phase, "after")
        self.assertTrue(ctx.exception.write_sent)
        self.assertEqual(port.writes, [("w", 3, MSR_OC_MAILBOX, pack(0x11, 0, 0, 0x5B4))])

    def test_no_retry_after_timeout_after_write(self):
        port = ScriptedPort([0], then=BUSY)
        transport, _ = make(port, poll_sleep_s=0.25, timeout_s=1.0)
        with self.assertRaises(MailboxTimeout):
            transport.transact(0, 0x11, 0, 0, 0)
        self.assertEqual(len(port.writes), 1)  # exactly one WRMSR; the transport never re-sends
        self.assertEqual(len(port.reads), 1 + 5)  # one idle read + one polling phase, then it gives up

    def test_each_phase_has_its_own_timeout_budget(self):
        # 0.75 s busy before the write + 0.75 s busy after it would exceed a shared 1.0 s budget.
        port = ScriptedPort([BUSY] * 4 + [0] + [BUSY] * 4 + [resp(0, 7)])
        transport, _ = make(port, poll_sleep_s=0.25, timeout_s=1.0)
        self.assertEqual(transport.transact(0, 0x10, 0, 0), Response(0, 7))

    def test_zero_timeout_still_reads_once_and_passes_when_idle(self):
        port = ScriptedPort([0, 0])
        transport, _ = make(port, timeout_s=0)
        self.assertEqual(transport.transact(0, 0x10, 0, 0), Response(0, 0))

    def test_zero_timeout_with_busy_raises_immediately(self):
        port = ScriptedPort([BUSY])
        transport, fc = make(port, timeout_s=0)
        with self.assertRaises(MailboxTimeout):
            transport.transact(0, 0x10, 0, 0)
        self.assertEqual((len(port.reads), fc.sleeps, port.writes), (1, [], []))


class TestErrorsAndValidation(unittest.TestCase):
    def test_msrerror_on_first_read_propagates_without_write(self):
        err = MsrError(errno.EIO, "read failed")
        port = ScriptedPort([err])
        transport, _ = make(port)
        with self.assertRaises(MsrError) as ctx:
            transport.transact(0, 0x10, 0, 0)
        self.assertIs(ctx.exception, err)
        self.assertEqual(port.writes, [])

    def test_msrerror_on_write_propagates_once_no_retry(self):
        err = MsrError(errno.EPERM, "read-only port")
        port = ScriptedPort([0], write_error=err)
        transport, _ = make(port)
        with self.assertRaises(MsrError) as ctx:
            transport.transact(0, 0x11, 0, 0, 0)
        self.assertIs(ctx.exception, err)
        self.assertEqual(ctx.exception.errno, errno.EPERM)
        self.assertEqual(len(port.writes), 1)
        self.assertEqual(len(port.reads), 1)  # no poll after a failed write

    def test_msrerror_while_polling_after_write_propagates(self):
        err = MsrError(errno.EIO, "poll failed")
        port = ScriptedPort([0, err])
        transport, _ = make(port)
        with self.assertRaises(MsrError):
            transport.transact(0, 0x11, 0, 0, 0)
        self.assertEqual(len(port.writes), 1)

    def test_invalid_arguments_touch_nothing(self):
        for args in (
            (0, 256, 0, 0, 0),
            (0, 0x10, -1, 0, 0),
            (0, 0x10, 0, 256, 0),
            (0, 0x10, 0, 0, 2**32),
            (-1, 0x10, 0, 0, 0),
            (10**6, 0x10, 0, 0, 0),
        ):
            port = ScriptedPort([0, 0])
            transport, _ = make(port)
            with self.subTest(args=args), self.assertRaises(ValueError):
                transport.transact(*args)
            self.assertEqual(port.ops, [])

    def test_constructor_rejects_bad_timings(self):
        port = ScriptedPort([])
        for kwargs in ({"poll_sleep_s": -1}, {"timeout_s": -0.1}, {"timeout_s": float("nan")}, {"poll_sleep_s": "1"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MailboxTransport(port, **kwargs)

    def test_defaults_are_the_documented_ones(self):
        transport = MailboxTransport(ScriptedPort([]))
        self.assertEqual(transport._poll_sleep_s, 0.01)  # registers.md: XTU/IPF polls in 10 ms steps
        self.assertEqual(transport._timeout_s, 10.0)  # ... with a ~10 s cap
        self.assertIs(transport._sleep, time.sleep)
        self.assertIs(transport._clock, time.monotonic)


if __name__ == "__main__":
    unittest.main()
