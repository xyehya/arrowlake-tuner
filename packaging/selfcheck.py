"""Post-install trust self-check (G-106). Installed as ``/usr/lib/arltuner/arltuner-selfcheck`` and run by ``install.sh`` as
``python3 -I /usr/lib/arltuner/arltuner-selfcheck`` - i.e. from the installed, root-owned tree, never from the checkout.

It asks the question the root helper will ask at start-up (SPEC 4.4): is everything it will load or open root-owned, not group/world-writable,
free of symlinks, with root-owned parents? It reuses ``arltuner.helper.trust`` (the helper's own checks) and adds what only the install can know:
the launcher is executable, the polkit action pins the launcher and asks for ``auth_admin`` everywhere (no ``_keep``, never ``allow_active=yes``),
the audit log is 0600. Pure over injectable ``lstat_fn`` / ``listdir_fn`` / ``read_fn`` / ``root_uid``: tests feed fake stat results. Exit 0: all
checks passed; 1: at least one failed (each is named); the install is then NOT usable and ``uninstall.sh`` removes it.
"""
from __future__ import annotations

import os
import stat
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

CODE_DIR = Path("/usr/lib/arltuner")
HELPER = CODE_DIR / "arltuner-helper"
SELFCHECK = CODE_DIR / "arltuner-selfcheck"
POLKIT_FILE = Path("/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy")
TMPFILES_FILE = Path("/usr/lib/tmpfiles.d/arltuner.conf")
ETC_DIR = Path("/etc/arltuner")
STATE_DIR = Path("/var/lib/arltuner")
RUN_DIR = Path("/run/arltuner")
POLKIT_ACTION = "io.github.arrowlake-tuner.helper"
EXEC_PATH_KEY = "org.freedesktop.policykit.exec.path"

Result = tuple[str, str | None]  # (what was checked, None = ok / the reason it failed)


def _read(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(1 << 20)


def check_polkit_text(data: bytes, helper: Path = HELPER) -> None:
    """Raise ``ValueError`` unless the polkit file declares exactly one action, ``auth_admin`` for all three defaults (so no ``_keep`` and never
    ``allow_active=yes``) and an ``exec.path`` annotation equal to the installed helper."""
    if b"<!ENTITY" in data:  # stdlib expat: no external entities are fetched, but nothing here needs entity declarations at all
        raise ValueError("entity declarations are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"not valid XML: {exc}") from None
    actions = root.findall("action")
    if [a.get("id") for a in actions] != [POLKIT_ACTION]:
        raise ValueError(f"expected exactly one action {POLKIT_ACTION!r}, found {[a.get('id') for a in actions]}")
    action = actions[0]
    defaults = action.find("defaults")
    if defaults is None:
        raise ValueError("no <defaults>")
    got = {child.tag: (child.text or "").strip() for child in defaults}
    for tag in ("allow_any", "allow_inactive", "allow_active"):
        if got.get(tag) != "auth_admin":
            raise ValueError(f"<{tag}> is {got.get(tag)!r}, must be 'auth_admin' (no _keep, never yes)")
    if set(got) - {"allow_any", "allow_inactive", "allow_active"}:
        raise ValueError(f"unexpected <defaults> children {sorted(got)}")
    paths = [a.text.strip() for a in action.findall("annotate") if a.get("key") == EXEC_PATH_KEY and a.text]
    if paths != [str(helper)]:
        raise ValueError(f"{EXEC_PATH_KEY} is {paths}, must be exactly [{str(helper)!r}]")


def selfcheck(
    *,
    lstat_fn: Callable[[str], Any] = os.lstat,
    listdir_fn: Callable[[str], list[str]] = os.listdir,
    read_fn: Callable[[Path], bytes] = _read,
    root_uid: int = 0,
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

    def mode_check(label: str, p: Path, *, executable: bool = False, private: bool = False) -> None:
        def go() -> None:
            st = lstat_fn(str(p))
            if executable and not st.st_mode & stat.S_IXUSR:
                raise ValueError(f"{p} is not executable")
            if private and st.st_mode & 0o077:
                raise ValueError(f"{p} mode {stat.S_IMODE(st.st_mode):04o} must be 0600")

        run(label, go)

    tree("code tree /usr/lib/arltuner/arltuner (every dir and file, __pycache__ included)", CODE_DIR / "arltuner")
    tree("platform files /usr/lib/arltuner/platforms", CODE_DIR / "platforms")
    path("helper launcher", HELPER)
    mode_check("helper launcher is executable", HELPER, executable=True)
    path("self-check script", SELFCHECK)
    path("polkit action file", POLKIT_FILE)
    if results[-1][1] is None:  # content is parsed only after the file itself passed the trust check
        run("polkit action content", lambda: check_polkit_text(read_fn(POLKIT_FILE)))
    path("tmpfiles.d file", TMPFILES_FILE)
    path("policy /etc/arltuner/policy.toml", ETC_DIR / "policy.toml")
    path("helper.toml (may be absent)", ETC_DIR / "helper.toml", must_exist=False)
    tree("accepted state /etc/arltuner/accepted", ETC_DIR / "accepted")
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
