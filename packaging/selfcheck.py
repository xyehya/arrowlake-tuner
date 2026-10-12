"""Post-install trust self-check (G-106, D-043). Installed as ``/usr/lib/arltuner/arltuner-selfcheck`` and run by ``install.sh`` as
``python3 -I /usr/lib/arltuner/arltuner-selfcheck`` - i.e. from the installed, root-owned tree, never from the checkout.

It asks the question the root helper will ask at start-up (SPEC 4.4): is everything it will load or open root-owned, not group/world-writable,
free of symlinks, with root-owned parents? It reuses ``arltuner.helper.trust`` (the helper's own checks) and adds what only the install can know:
the launcher is executable, the systemd socket + service units are present and pin the launcher and the socket's owner / group / mode, the group they name
exists, no polkit action file is left over from the old install, ``helper.toml`` has an ``[access]`` that admits somebody, the audit log is 0600.
Pure over injectable ``lstat_fn`` / ``listdir_fn`` / ``read_fn`` / ``getgrnam`` / ``root_uid``: tests feed fake stat results. Exit 0: all checks passed;
1: at least one failed (each is named); the install is then NOT usable and ``uninstall.sh`` removes it.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

CODE_DIR = Path("/usr/lib/arltuner")
HELPER = CODE_DIR / "arltuner-helper"
SELFCHECK = CODE_DIR / "arltuner-selfcheck"
OLD_POLKIT_FILE = Path("/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy")  # the G-106 install's action: must be gone (D-043)
UNIT_DIRS = (Path("/etc/systemd/system"), Path("/usr/lib/systemd/system"))  # install.sh uses the first; a distribution package may use the second
SOCKET_UNIT, SERVICE_UNIT = "arltuner-helper.socket", "arltuner-helper.service"
SOCKET_PATH = Path("/run/arltuner/helper.sock")
SOCKET_GROUP = "arltuner"
EXEC_START = f"/usr/bin/python3 -I {HELPER} --device --socket"
TMPFILES_FILE = Path("/usr/lib/tmpfiles.d/arltuner.conf")
ETC_DIR = Path("/etc/arltuner")
STATE_DIR = Path("/var/lib/arltuner")
RUN_DIR = Path("/run/arltuner")
LAUNCHER_SHA256 = "01e88e7ca735e8d658e02edcb5b73d5716375525016b9a37513fa731aee701f4"  # sha256 of packaging/arltuner-helper (a test keeps the two in step)
LAUNCHER_SHEBANG = b"#!/usr/bin/python3 -I"
# Staging leftovers of an interrupted install (swap_dir / seeding): "<name>.old.<pid>", "<name>.new.<pid>", ".accepted.new.<pid>", ".tmp.<pid>", ".audit.new.<pid>"
LEFTOVER = re.compile(r"(^|.*\.)(old|new)\.\d+$|^\.(tmp|audit\.new|accepted\.new)\.\d+$")
# [Service] settings that would break the helper's MSR / cpufreq / state access (or change who it runs as / what it runs): none may appear.
FORBIDDEN_SERVICE_KEYS = (
    "User", "Group", "DynamicUser", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost", "ExecReload", "Environment", "EnvironmentFile",
    "PrivateDevices", "ProtectKernelTunables", "ProtectSystem", "ReadOnlyPaths", "InaccessiblePaths", "BindPaths", "BindReadOnlyPaths", "DeviceAllow",
)
SOCKET_KEYS_ALLOWED = {"ListenStream", "SocketUser", "SocketGroup", "SocketMode", "DirectoryMode", "RemoveOnStop", "Accept", "Service", "Backlog"}

Result = tuple[str, str | None]  # (what was checked, None = ok / the reason it failed)


def _read(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(1 << 20)


def unit_sections(data: bytes) -> dict[str, dict[str, list[str]]]:
    """A systemd unit file as ``{section: {key: [values...]}}`` (comments and blank lines dropped; a repeated key keeps every value, in order).
    ``ValueError`` for a line that is neither a section header, a comment nor ``key=value``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"not valid UTF-8: {exc}") from None
    out: dict[str, dict[str, list[str]]] = {}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
        elif section is not None and "=" in line:
            key, _, value = line.partition("=")
            out[section].setdefault(key.strip(), []).append(value.strip())
        else:
            raise ValueError(f"unparsable line {raw!r}")
    return out


def check_socket_unit_text(data: bytes) -> None:
    """``ValueError`` unless the socket unit listens on exactly ``/run/arltuner/helper.sock``, owned by root, group ``arltuner``, mode 0660 (no access for
    "other"), and does not hand each connection to its own service instance (``Accept=no``)."""
    sock = unit_sections(data).get("Socket")
    if sock is None:
        raise ValueError("no [Socket] section")
    for key, want in (("ListenStream", [str(SOCKET_PATH)]), ("SocketUser", ["root"]), ("SocketGroup", [SOCKET_GROUP]), ("SocketMode", ["0660"])):
        if sock.get(key) != want:
            raise ValueError(f"{key} is {sock.get(key)}, must be exactly {want}")
    if sock.get("Accept", ["no"]) != ["no"]:
        raise ValueError(f"Accept is {sock.get('Accept')}, must be no (the helper serves its one client itself)")
    extra = sorted(set(sock) - SOCKET_KEYS_ALLOWED)
    if extra:
        raise ValueError(f"unexpected [Socket] setting(s) {extra}")


def check_service_unit_text(data: bytes) -> None:
    """``ValueError`` unless the service runs exactly the pinned command as root, and none of the settings that would break MSR / sysfs access (or change the
    command or the user) is present."""
    svc = unit_sections(data).get("Service")
    if svc is None:
        raise ValueError("no [Service] section")
    if svc.get("ExecStart") != [EXEC_START]:
        raise ValueError(f"ExecStart is {svc.get('ExecStart')}, must be exactly [{EXEC_START!r}]")
    if svc.get("Type", ["simple"]) != ["simple"]:
        raise ValueError(f"Type is {svc.get('Type')}, must be simple")
    present = [k for k in FORBIDDEN_SERVICE_KEYS if k in svc]
    if present:
        raise ValueError(f"{present} must not be set in [Service]: the helper runs exactly the pinned command, as root, with access to MSRs / sysfs / its state")


def check_old_polkit_gone(lstat_fn: Callable[[str], Any]) -> None:
    try:
        lstat_fn(str(OLD_POLKIT_FILE))
    except FileNotFoundError:
        return
    raise ValueError(f"{OLD_POLKIT_FILE} is still installed: the old polkit action must be removed (the helper is a socket service now)")


def selfcheck(
    *,
    lstat_fn: Callable[[str], Any] = os.lstat,
    listdir_fn: Callable[[str], list[str]] = os.listdir,
    read_fn: Callable[[Path], bytes] = _read,
    root_uid: int = 0,
    getgrnam: Callable[[str], Any] | None = None,
) -> list[Result]:
    from arltuner.helper.trust import TrustError, check_code_tree, check_trusted_path

    results: list[Result] = []

    def run(label: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except (TrustError, ValueError, OSError) as exc:
            results.append((label, str(exc)))
        else:
            results.append((label, None))

    def path(label: str, p: Path, *, want: str = "file", must_exist: bool = True) -> None:
        run(label, lambda: check_trusted_path(p, want=want, must_exist=must_exist, lstat_fn=lstat_fn, root_uid=root_uid))

    def tree(label: str, p: Path) -> None:
        run(label, lambda: check_code_tree(p, lstat_fn=lstat_fn, listdir_fn=listdir_fn, root_uid=root_uid))

    def tree_if_present(label: str, p: Path) -> None:
        """D-036: the accepted state is the user's and a fresh install has none; when it is there it is checked like any trusted tree."""
        try:
            lstat_fn(str(p))
        except FileNotFoundError:
            results.append((label, None))
            return
        except OSError:
            pass  # unreadable parent or the like: let the tree check report it
        tree(label, p)

    def mode_check(label: str, p: Path, *, executable: bool = False, private: bool = False) -> None:
        def go() -> None:
            st = lstat_fn(str(p))
            if executable and not st.st_mode & stat.S_IXUSR:
                raise ValueError(f"{p} is not executable")
            if private and st.st_mode & 0o077:
                raise ValueError(f"{p} mode {stat.S_IMODE(st.st_mode):04o} must be 0600")

        run(label, go)

    tree("code tree /usr/lib/arltuner (top level, arltuner/, platforms/, every dir and file, __pycache__ included)", CODE_DIR)
    path("helper launcher", HELPER)
    mode_check("helper launcher is executable", HELPER, executable=True)

    def launcher_content() -> None:
        data = read_fn(HELPER)
        if data.split(b"\n", 1)[0] != LAUNCHER_SHEBANG:
            raise ValueError(f"shebang must be exactly {LAUNCHER_SHEBANG.decode()!r}")
        if hashlib.sha256(data).hexdigest() != LAUNCHER_SHA256:
            raise ValueError("content differs from the packaged launcher (sha256 mismatch)")

    if results[-2][1] is None:  # read only after the launcher passed the trust check
        run("helper launcher content = packaged launcher, isolated-python shebang", launcher_content)
    path("self-check script", SELFCHECK)

    def unit(label: str, name: str, content_check: Callable[[bytes], None]) -> None:
        """The first unit directory that has the file: trusted path, then (only then) pinned content."""
        found = None
        for directory in UNIT_DIRS:
            try:
                lstat_fn(str(directory / name))
            except OSError:
                continue
            found = directory / name
            break
        if found is None:
            results.append((label, f"{name} is in none of {[str(d) for d in UNIT_DIRS]}"))
            return
        path(label, found)
        if results[-1][1] is None:
            run(f"{label}: content", lambda: content_check(read_fn(found)))

    unit("systemd socket unit arltuner-helper.socket", SOCKET_UNIT, check_socket_unit_text)
    unit("systemd service unit arltuner-helper.service", SERVICE_UNIT, check_service_unit_text)

    def group_exists() -> None:
        lookup = getgrnam
        if lookup is None:
            import grp

            lookup = grp.getgrnam
        try:
            lookup(SOCKET_GROUP)
        except KeyError:
            raise ValueError(f"the group {SOCKET_GROUP!r} named by the socket unit does not exist (groupadd -r {SOCKET_GROUP})") from None

    run(f"group {SOCKET_GROUP} exists (the socket's group)", group_exists)
    run("no old polkit action file", lambda: check_old_polkit_gone(lstat_fn))
    path("tmpfiles.d file", TMPFILES_FILE)
    path("policy /etc/arltuner/policy.toml", ETC_DIR / "policy.toml")
    path("helper.toml", ETC_DIR / "helper.toml")
    if results[-1][1] is None:  # content is parsed only after the file itself passed the trust check

        def access_nonempty() -> None:
            from arltuner.helper.settings import parse_helper_toml

            where = str(ETC_DIR / "helper.toml")
            if parse_helper_toml(read_fn(ETC_DIR / "helper.toml").decode("utf-8", "replace"), where).access.empty:
                raise ValueError("no [access] users or group: nobody may use the helper (sudo bash install.sh --add-user NAME)")

        run("helper.toml is valid and its [access] admits somebody", access_nonempty)
    tree_if_present("accepted state /etc/arltuner/accepted (optional: absent until the user saves one)", ETC_DIR / "accepted")
    path("state dir /var/lib/arltuner", STATE_DIR, want="dir")
    path("audit log (may be absent)", STATE_DIR / "audit.jsonl", must_exist=False)

    def audit_mode() -> None:
        try:
            st = lstat_fn(str(STATE_DIR / "audit.jsonl"))
        except FileNotFoundError:
            return
        if st.st_mode & 0o077:
            raise ValueError(f"{STATE_DIR / 'audit.jsonl'} mode {stat.S_IMODE(st.st_mode):04o} must be 0600")

    run("audit log mode 0600", audit_mode)
    path("run dir /run/arltuner (tmpfiles.d)", RUN_DIR, want="dir")

    def leftovers() -> None:
        found = []
        for directory, only_prefixed in ((CODE_DIR.parent, True), (CODE_DIR.parent.parent / "share" / "arltuner", False), (ETC_DIR, False), (STATE_DIR, False)):
            try:
                names = listdir_fn(str(directory))
            except OSError:
                continue
            for name in names:
                if LEFTOVER.match(name) and (not only_prefixed or name.lstrip(".").startswith("arltuner")):
                    found.append(str(directory / name))
        if found:
            raise ValueError(f"leftover staging paths of an interrupted install: {sorted(found)}")

    run("no leftover .old.*/.new.* staging paths", leftovers)
    return results


def main() -> int:
    results = selfcheck()
    failed = [(label, why) for label, why in results if why is not None]
    for label, why in results:
        print(f"{'FAIL' if why else 'ok  '}  {label}" + (f": {why}" if why else ""))
    if failed:
        print(f"arltuner self-check: {len(failed)} of {len(results)} checks FAILED; the helper will refuse to start. Run uninstall.sh or repair.", file=sys.stderr)
        return 1
    print(f"arltuner self-check: all {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent))  # -I removes the script directory: the installed tree is the only source
    sys.exit(main())
