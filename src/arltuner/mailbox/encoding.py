"""The widest value each tunable field of the V/F word / turbo tables can carry: the only fixed numeric bounds of the program (G-141, D-039).

Every other ceiling is the policy value clamped by the CPU's own limits, read live. Anything that range-checks a ratio, a V target or a turbo bucket
(the policy loader, the profile schema, the helper's intent validation) takes its bound from here, so no module keeps a private, narrower copy. Pure
constants: no I/O, no imports. Encodings: ``docs/knowledge/registers.md`` "V/F data word".
"""
from __future__ import annotations

__all__ = [
    "ENCODING_OFFSET_CEILING_MV",
    "ENCODING_OFFSET_FLOOR_MV",
    "ENCODING_RATIO_MAX",
    "ENCODING_V_TARGET_MAX_MV",
    "RATIO_RANGE",
    "RATIO_SOURCE",
    "TURBO_RATIO_RANGE",
    "V_TARGET_MIN_MV",
    "V_TARGET_RANGE_MV",
    "V_TARGET_SOURCE",
]

ENCODING_RATIO_MAX = 255  # ratio field [7:0] of the 0x11 word
ENCODING_V_TARGET_MAX_MV = 3999  # voltage field [19:8] is S2.10 over 12 bits: 0xFFF / 1024 V = 3.999 V (the real limit is 0x28, read live)
ENCODING_OFFSET_CEILING_MV = 999  # offset field [31:21] is 11-bit signed; XTU allows -1000...+999 mV (registers.md V/F data word)
ENCODING_OFFSET_FLOOR_MV = -1000
V_TARGET_MIN_MV = 500  # XTU's voltage override range starts at 0.5 V (registers.md "Limits XTU applies")

RATIO_RANGE = (1, ENCODING_RATIO_MAX)  # a ratio / multiplier the 8-bit field can hold (0 = unset)
TURBO_RATIO_RANGE = (1, ENCODING_RATIO_MAX)  # one byte per bucket of MSR 0x1AD / 0x650
V_TARGET_RANGE_MV = (V_TARGET_MIN_MV, ENCODING_V_TARGET_MAX_MV)

#: Where a refusal names the source of these numbers (the error says which limit it is, so nobody looks for a policy line that is not there).
RATIO_SOURCE = "ratio field [7:0] of the V/F word"
V_TARGET_SOURCE = "voltage field [19:8] of the V/F word (S2.10, 12 bits)"
