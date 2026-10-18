#: D-049 (operator, 2026-10-18): **no operator value limit.** The only bounds are the CPU's own limits, read live (cmd ``0x01 [7:0]`` ratio, cmd ``0x28 [11:0] - 1 LSB`` voltage), and the field
#: encodings (``ENCODING_*``, re-exported from :mod:`arltuner.mailbox.encoding`, the one home of the field-encoding bounds; G-141): a value outside them cannot be written into the word at all.
#: An unread CPU limit is *unknown*: a write that needs it is refused with :data:`CPU_LIMIT_UNREAD` (fail closed), naming the read.
CPU_LIMIT_UNREAD = "CPU_LIMIT_UNREAD"

#: D-049 warning thresholds: constants of the Review (``controls.planner.plan_warnings``), NOT policy and never a refusal. A large single change is allowed and warned.
LARGE_OFFSET_CHANGE_MV = 50  # |offset delta| of one apply
LARGE_RATIO_CHANGE = 4  # |ratio delta| of one apply
LARGE_POWER_CHANGE_W = 50  # |PL1 / PL2 delta| of one apply
DEEP_WHOLE_CURVE_MV = -100  # a whole-curve offset deeper than this: the CPU's V/F monotonic clamp does not protect a whole-curve offset (cpu-rules-audit.md row 4a)

