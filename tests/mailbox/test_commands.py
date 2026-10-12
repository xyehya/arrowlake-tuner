"""T-010 golden vectors for arltuner.mailbox.commands (source: docs/knowledge/registers.md, "Mailbox commands" + "Return codes")."""
import unittest

from arltuner.mailbox.commands import WRITE_COMMANDS, Cmd, Domain, Rc, is_write, rc_name

# registers.md "Mailbox commands" table (every id named there, read and write)
TABLE_IDS = {
    0x01, 0x02, 0x03, 0x04, 0x05, 0x09,
    0x10, 0x11, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D,
    0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
}
# registers.md "Unused on ARL-S" (ids only; names/polarity are [UNVERIFIED])
UNUSED_IDS = {0x07, 0x12, 0x13, 0x1E, 0x1F, 0x20, 0x21}

EXPECTED_WRITES = {0x11, 0x13, 0x15, 0x17, 0x19, 0x1B, 0x1D, 0x1F, 0x21, 0x23, 0x25, 0x27}

# registers.md "Return codes (MailboxCompletionCode)"
EXPECTED_RC = {
    0x00: "Pass", 0x01: "OCLocked", 0x02: "UnsupportedDomain", 0x03: "RatioExceedsLimits",
    0x04: "VoltageExceedsMax", 0x05: "OCNotSupported", 0x06: "FIVRError", 0x07: "IccMaxInvalid",
    0x08: "IllegalCommandSelectedInParam1", 0x09: "InvalidAVXLevel", 0x0A: "RuntimeMemoryDisabled",
    0x0B: "InvalidMemControllerId", 0x10: "InvalidParam", 0x12: "VoltageExceedsLimit",
    0x13: "UndervoltProtection", 0x1F: "UnrecognizedCommand", 0xFE: "UnsupportedCmd", 0xFF: "MailboxFailure",
}


class CmdIds(unittest.TestCase):
    def test_every_table_id_is_a_cmd(self):
        self.assertLessEqual(TABLE_IDS, {int(c) for c in Cmd})

    def test_every_unused_id_is_a_cmd(self):
        self.assertLessEqual(UNUSED_IDS, {int(c) for c in Cmd})

    def test_no_ids_beyond_registers_md(self):
        self.assertEqual({int(c) for c in Cmd}, TABLE_IDS | UNUSED_IDS)

    def test_named_read_write_pairs_from_spec(self):
        self.assertEqual(Cmd.VOLTAGE_FREQ_READ, 0x10)
        self.assertEqual(Cmd.VOLTAGE_FREQ_WRITE, 0x11)
        self.assertEqual(Cmd.MISC_GLOBAL_CONFIG_READ, 0x14)
        self.assertEqual(Cmd.MISC_GLOBAL_CONFIG_WRITE, 0x15)
        self.assertEqual(Cmd.FAVORED_CORE_READ, 0x1C)
        self.assertEqual(Cmd.FAVORED_CORE_WRITE, 0x1D)
        self.assertEqual(Cmd.TVB_READ, 0x24)
        self.assertEqual(Cmd.TVB_WRITE, 0x25)
        self.assertEqual(Cmd.GRANULAR_RATIO_READ, 0x26)
        self.assertEqual(Cmd.GRANULAR_RATIO_WRITE, 0x27)
        self.assertEqual(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ, 0x28)

    def test_read_only_table_ids(self):
        self.assertEqual(Cmd.OC_CAPABILITY_READ, 0x01)
        self.assertEqual(Cmd.PER_CORE_CAPABILITY_READ, 0x02)
        self.assertEqual(Cmd.DDR_INFO_READ, 0x03)
        self.assertEqual(Cmd.ICC_MAX_VR_READ, 0x04)
        self.assertEqual(Cmd.REFERENCE_CLK_MONITOR_READ, 0x05)
        self.assertEqual(Cmd.VOLTAGE_FLOOR_READ, 0x09)

    def test_remaining_pairs(self):
        self.assertEqual((Cmd.ICC_MAX_READ, Cmd.ICC_MAX_WRITE), (0x16, 0x17))
        self.assertEqual((Cmd.RESIDENCY_STATE_REGULATION_READ, Cmd.RESIDENCY_STATE_REGULATION_WRITE), (0x18, 0x19))
        self.assertEqual((Cmd.AVX_OFFSET_CONTROL_READ, Cmd.AVX_OFFSET_CONTROL_WRITE), (0x1A, 0x1B))
        self.assertEqual((Cmd.BCLK_READ, Cmd.BCLK_WRITE), (0x22, 0x23))

    def test_cmd_is_an_int(self):
        self.assertEqual(int(Cmd.VOLTAGE_FREQ_WRITE), 0x11)
        self.assertEqual(Cmd(0x11), Cmd.VOLTAGE_FREQ_WRITE)


class WriteSet(unittest.TestCase):
    def test_write_commands_exact(self):
        self.assertEqual(WRITE_COMMANDS, EXPECTED_WRITES)
        self.assertIsInstance(WRITE_COMMANDS, frozenset)

    def test_is_write_examples(self):
        self.assertFalse(is_write(0x10))
        self.assertTrue(is_write(0x11))

    def test_is_write_matches_set_for_every_byte(self):
        for cmd in range(256):
            with self.subTest(cmd=cmd):
                self.assertEqual(is_write(cmd), cmd in EXPECTED_WRITES)

    def test_every_write_is_a_named_cmd(self):
        self.assertLessEqual(EXPECTED_WRITES, {int(c) for c in Cmd})

    def test_reads_are_not_writes(self):
        for cmd in Cmd:
            with self.subTest(cmd=cmd.name):
                self.assertEqual(is_write(cmd), int(cmd) in EXPECTED_WRITES)
        for read in (Cmd.OC_CAPABILITY_READ, Cmd.VOLTAGE_FREQ_READ, Cmd.MISC_GLOBAL_CONFIG_READ,
                     Cmd.FAVORED_CORE_READ, Cmd.TVB_READ, Cmd.GRANULAR_RATIO_READ, Cmd.MAXIMUM_VOLTAGE_LIMIT_READ):
            self.assertFalse(is_write(read), read.name)

    def test_write_named_members_are_all_in_set(self):
        for cmd in Cmd:
            if cmd.name.endswith("_WRITE"):
                self.assertIn(cmd, WRITE_COMMANDS, cmd.name)

    def test_is_write_refuses_bad_input(self):
        for bad in (-1, 256, 1.0, "0x11", None, True):
            with self.subTest(cmd=bad), self.assertRaises(ValueError):
                is_write(bad)

    def test_is_write_docstring_warns_every_request_is_a_wrmsr(self):
        self.assertIn("state-changing command; **every** mailbox request is still a WRMSR", is_write.__doc__)


class Domains(unittest.TestCase):
    def test_wire_values(self):
        self.assertEqual(
            {d.name: int(d) for d in Domain},
            {"CORE": 0, "GT": 1, "RING": 2, "GT_UNSLICE": 3, "SA": 4, "MEMSS": 6, "NGU": 7},
        )

    def test_individual_values(self):
        self.assertEqual(Domain.CORE, 0)
        self.assertEqual(Domain.GT, 1)
        self.assertEqual(Domain.RING, 2)
        self.assertEqual(Domain.GT_UNSLICE, 3)
        self.assertEqual(Domain.SA, 4)
        self.assertEqual(Domain.MEMSS, 6)
        self.assertEqual(Domain.NGU, 7)


class ReturnCodes(unittest.TestCase):
    def test_every_code_present_with_xtu_name(self):
        self.assertEqual({int(r): r.name for r in Rc}, EXPECTED_RC)

    def test_rc_name_defined(self):
        for code, name in EXPECTED_RC.items():
            with self.subTest(code=code):
                self.assertEqual(rc_name(code), name)
        self.assertEqual(rc_name(3), "RatioExceedsLimits")
        self.assertEqual(rc_name(0), "Pass")
        self.assertEqual(rc_name(0x13), "UndervoltProtection")

    def test_rc_name_unknown(self):
        self.assertEqual(rc_name(0x11), "UNKNOWN_0x11")
        self.assertEqual(rc_name(0x0C), "UNKNOWN_0x0C")
        self.assertEqual(rc_name(0xAB), "UNKNOWN_0xAB")

    def test_0x11_not_in_enum(self):
        self.assertNotIn(0x11, {int(r) for r in Rc})

    def test_every_byte_has_a_name(self):
        for code in range(256):
            name = rc_name(code)
            self.assertTrue(name == EXPECTED_RC.get(code, f"UNKNOWN_0x{code:02X}"), code)

    def test_rc_name_accepts_enum_members(self):
        self.assertEqual(rc_name(Rc.OCLocked), "OCLocked")

    def test_rc_name_refuses_bad_input(self):
        for bad in (-1, 256, 1.0, "3", None, True):
            with self.subTest(code=bad), self.assertRaises(ValueError):
                rc_name(bad)


if __name__ == "__main__":
    unittest.main()
