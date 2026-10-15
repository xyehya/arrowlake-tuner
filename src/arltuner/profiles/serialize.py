"""Profile -> TOML text (G-129, ``profiles.accept``): the inverse of :func:`arltuner.profiles.schema.load_profile`.

The standard library reads TOML (``tomllib``) but cannot write it (D-012), so the few shapes a profile has are emitted by hand. Every value is an
integer or a short validated string, so no escaping beyond the profile name is needed; ``dump_profile`` refuses a name that is not plain. The result
always loads back to an equal :class:`Profile` (the tests round-trip it). No files, no hardware.
"""
from __future__ import annotations

import re

from .schema import PLATFORM, Profile, ProfileError

__all__ = ["dump_profile"]

_PLAIN_NAME = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")


def dump_profile(profile: Profile, header: str = "") -> str:
    """The TOML text of ``profile``. ``header`` (optional) becomes ``#`` comment lines at the top (one per line of ``header``)."""
    if not _PLAIN_NAME.match(profile.name):
        raise ProfileError(f"meta.name: {profile.name!r} is not a plain name (A-Z a-z 0-9 . _ -)")
    lines: list[str] = [f"# {line}".rstrip() for line in header.splitlines()]
    lines += ["[meta]", f'name = "{profile.name}"', f'platform = "{PLATFORM}"']
    if profile.complete:
        lines.append("complete = true  # G-148 (D-044): the whole tuning state of the CPU; applying it writes only the words that differ")
    lines.append("")
    for c in profile.entries:
        lines += ["[[core]]", f'kind = "{c.kind}"', f"index = {c.index}"]
        for key in ("fused_top_ratio", "ratio", "oc_voltage_mv", "offset_mv"):
            value = getattr(c, key)
            if value is not None:
                lines.append(f"{key} = {int(value)}")
        lines.append("")
    for p in profile.points:
        lines += ["[[point]]", f'kind = "{p.kind}"', f"index = {p.index}", f"point = {p.point}", f"offset_mv = {int(p.offset_mv)}"]
        if p.expect_ratio is not None:
            lines.append(f"expect_ratio = {int(p.expect_ratio)}")
        lines.append("")
    if profile.ring is not None:
        lines += ["[ring]", f"oc_word = 0x{int(profile.ring.oc_word):08X}  # G-154: the ring OC word as read (BIOS-owned: compared on a restore, never written)", ""]
        for rp in profile.ring.points:
            lines += ["[[ring.point]]", f"point = {rp.point}", f"offset_mv = {int(rp.offset_mv)}", f"expect_ratio = {int(rp.expect_ratio)}", ""]
    if profile.turbo is not None:
        lines.append("[turbo]")
        for key in ("e", "p"):
            table = getattr(profile.turbo, key)
            if table is not None:
                lines.append(f"{key} = [{', '.join(str(int(x)) for x in table)}]")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
