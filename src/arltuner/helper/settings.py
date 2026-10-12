"""``/etc/arltuner/helper.toml``: the root-owned settings of the helper (D-043). Pure parsing plus the trust-checked read; no hardware.

Keys (anything else is refused; the file may be absent = defaults)::

    allow_taint_bits = [2]        # kernel taint bits the first MSR write of a boot is expected to set (health gate)
    idle_exit_s = 600             # socket-activated service only: exit after this many idle seconds (0 = never); default 600
    [access]
    users = ["alice"]             # who may use the helper socket (names, resolved to uids at every check)
    group = "arltuner"            # and/or members of this group; optional; the socket node is mode 0666 and the peer check is the gate

**Fail closed:** a missing or empty ``[access]`` admits nobody, a malformed one makes the whole file invalid (the socket server then admits nobody and the
taint-bit reader refuses), and root (uid 0) is admitted only when it is listed in ``users`` - never through the group.
"""
from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .trust import check_trusted_path, open_checked

__all__ = ["Access", "DEFAULT_IDLE_EXIT_S", "HelperToml", "IDLE_EXIT_MAX_S", "load_helper_toml", "parse_helper_toml"]

DEFAULT_IDLE_EXIT_S = 600
IDLE_EXIT_MAX_S = 86400
_TOP_KEYS = {"allow_taint_bits", "idle_exit_s", "access"}
_ACCESS_KEYS = {"users", "group"}
_MAX_NAME = 256


@dataclass(frozen=True)
class Access:
    """Who may connect. Empty = nobody."""

    users: tuple[str, ...] = ()
    group: str | None = None

    @property
    def empty(self) -> bool:
        return not self.users and not self.group

    def allows(
        self, uid: int, *, getpwnam: Callable[[str], Any] | None = None, getpwuid: Callable[[int], Any] | None = None,
        getgrnam: Callable[[str], Any] | None = None, getgrouplist: Callable[[str, int], list[int]] | None = None,
    ) -> bool:
        """``uid`` is listed by name in ``users``, or (uid != 0) one of its groups is ``group``. Names and groups are resolved now, not at load time.
        Any lookup error for one rule means that rule does not match."""
        import grp
        import pwd

        getpwnam = pwd.getpwnam if getpwnam is None else getpwnam
        getpwuid = pwd.getpwuid if getpwuid is None else getpwuid
        getgrnam = grp.getgrnam if getgrnam is None else getgrnam
        getgrouplist = os.getgrouplist if getgrouplist is None else getgrouplist
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            return False
        for name in self.users:
            try:
                if getpwnam(name).pw_uid == uid:
                    return True
            except (KeyError, OSError, ValueError):
                continue
        if self.group and uid != 0:  # root is never admitted through the group
            try:
                gid = getgrnam(self.group).gr_gid
                pw = getpwuid(uid)
                return gid in getgrouplist(pw.pw_name, pw.pw_gid)
            except (KeyError, OSError, ValueError):
                return False
        return False


@dataclass(frozen=True)
class HelperToml:
    taint_bits: frozenset[int] = frozenset()
    access: Access = field(default_factory=Access)
    idle_exit_s: int = DEFAULT_IDLE_EXIT_S


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_helper_toml(text: str, where: str = "helper.toml") -> HelperToml:
    """``ValueError`` (naming the key) for anything that is not exactly the documented shape."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{where}: not valid TOML: {exc}") from None
    extra = sorted(set(doc) - _TOP_KEYS)
    if extra:
        raise ValueError(f"{where}: unknown key(s) {extra}")
    bits = doc.get("allow_taint_bits", [])
    if not isinstance(bits, list) or not all(_is_int(b) and 0 <= b <= 63 for b in bits):
        raise ValueError(f"{where}: allow_taint_bits must be a list of integers 0..63")
    idle = doc.get("idle_exit_s", DEFAULT_IDLE_EXIT_S)
    if not _is_int(idle) or not 0 <= idle <= IDLE_EXIT_MAX_S:
        raise ValueError(f"{where}: idle_exit_s must be an integer 0..{IDLE_EXIT_MAX_S} (0 = never)")
    access = Access()
    if "access" in doc:
        table = doc["access"]
        if not isinstance(table, dict):
            raise ValueError(f"{where}: [access] must be a table")
        extra = sorted(set(table) - _ACCESS_KEYS)
        if extra:
            raise ValueError(f"{where}: [access] has unknown key(s) {extra}")
        users = table.get("users", [])
        if not isinstance(users, list) or not all(isinstance(u, str) and 0 < len(u) <= _MAX_NAME and "\x00" not in u for u in users):
            raise ValueError(f"{where}: [access] users must be a list of user names")
        group = table.get("group")
        if group is not None and (not isinstance(group, str) or not 0 < len(group) <= _MAX_NAME or "\x00" in group):
            raise ValueError(f"{where}: [access] group must be a group name")
        access = Access(tuple(users), group)
    return HelperToml(frozenset(bits), access, idle)


def load_helper_toml(
    path: Path | str, *, lstat_fn: Callable[[str], Any] = os.lstat, root_uid: int = 0, must_exist: bool = False, check_parents: bool = True,
) -> HelperToml:
    """The trust-checked read (root-owned file and parents, no symlink, not group/world-writable, at most 64 KiB). An absent file is the defaults (nobody
    admitted) unless ``must_exist``. ``TrustError`` for an untrusted file, ``ValueError`` for bad content. ``check_parents=False`` (a non-root simulator
    or test run, whose parents are not root's) leaves the descriptor checks of :func:`open_checked` (owner, mode, one link, no symlink) as the only ones."""
    if check_parents:
        check_trusted_path(path, want="file", must_exist=must_exist, lstat_fn=lstat_fn, root_uid=root_uid)
    try:
        fd = open_checked(path, write=False, create=False, euid=root_uid, what="helper.toml")
    except FileNotFoundError:
        if must_exist:
            raise
        return HelperToml()
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(65536)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8: {exc}") from None
    return parse_helper_toml(text, str(path))
