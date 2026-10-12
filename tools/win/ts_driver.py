#!/usr/bin/env python3
"""ts_driver.py - carve and manage ThrottleStop's signed kernel driver (host win, stdlib only).

The driver is NOT redistributed: it is cut out of the operator's own ThrottleStop.exe (9.7.3) and verified by SHA-256.
Everything that decides what runs in the kernel lives in ONE admin-only directory, %ProgramData%\\arltuner (owner Administrators, ACL: Administrators + SYSTEM
only; G-142, D-042): a file a non-admin process can replace must never be what ``sc start`` loads. Elevated commands re-verify the directory's ACL and the
driver's SHA-256 immediately before every ``sc start``; the service loads a NEW uniquely named copy written for that run (O_EXCL), held open
while it starts, and ``remove`` deletes the service and the copy. No component of the path may be a junction / symlink (reparse point).
  py -3 tools\\win\\ts_driver.py init-dir     # elevated: create + lock down %ProgramData%\\arltuner (also done by carve)
  py -3 tools\\win\\ts_driver.py carve --exe %USERPROFILE%\\Downloads\\ThrottleStop_9.7.3\\ThrottleStop.exe   # elevated: -> %ProgramData%\\arltuner\\ThrottleStop.sys
  py -3 tools\\win\\ts_driver.py start        # elevated: verify dir/ACL/reparse/SHA-256, write a fresh copy, sc create ThrottleStop (kernel, demand), verify again, sc start
  py -3 tools\\win\\ts_driver.py install      # same as start
  py -3 tools\\win\\ts_driver.py status       # service state + can \\\\.\\ThrottleStop be opened
  py -3 tools\\win\\ts_driver.py remove       # elevated: sc stop + sc delete + delete the per-run copy (the restore scripts do this after every run)
  py -3 tools\\win\\ts_driver.py check-dir DIR   # exit 0 when DIR is admin-only (the restore scripts check the accepted state's directory)
Evidence: docs/knowledge/windows-path.md (hashes, offset), win-throttlestop-fivr.md section 1 (service/device names).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

EXE_SHA256 = "52D86D3D0EC54D3B2F4FB7BD87DD44111EB770719AF03AED3071935A62312BA2"
DRIVER_SHA256 = "16F83F056177C4EC24C7E99D01CA9D9D6713BD0497EEEDB777A3FFEFA99C97F0"
DRIVER_OFFSET = 0x34EA20
DRIVER_SIZE = 50216
SERVICE = "ThrottleStop"
DEVICE = r"\\.\ThrottleStop"


class DriverError(Exception):
    pass


# Owner and every allow entry of the admin-only directory must be one of these (SDDL abbreviations and the well-known SIDs; locale independent).
ADMIN_TRUSTEES = frozenset({"BA", "SY", "S-1-5-32-544", "S-1-5-18", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"})  # + TrustedInstaller


def data_dir() -> Path:
    """``%ProgramData%\\arltuner``: the one admin-only directory (driver, accepted state, policy, audit log, read-back files)."""
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "arltuner"


def default_sys_path() -> Path:
    return data_dir() / "ThrottleStop.sys"


def legacy_sys_path() -> Path:
    """Where earlier versions put the driver (a per-user, user-writable directory): never used any more."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "arltuner" / "ThrottleStop.sys"


# ---- the admin-only directory ---------------------------------------------------------------------------------------

def sddl_admin_only(sddl: str) -> str | None:
    """None when the security descriptor (SDDL text, e.g. ``O:BAG:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)``) is admin-only: the owner is
    Administrators / SYSTEM / TrustedInstaller, a DACL exists, and every *allow* entry names only those trustees (deny entries only restrict). Otherwise
    the reason. Unparseable input is a refusal (fail closed)."""
    m = re.fullmatch(r"\s*(?:O:(?P<owner>.*?))?(?:G:(?P<group>.*?))?D:(?P<dflags>[A-Z]*)(?P<aces>(?:\([^()]*\))*)(?:S:.*)?\s*", sddl or "")
    if not m or not m.group("owner"):
        return f"unparseable or ownerless security descriptor {sddl!r}"
    if m.group("owner") not in ADMIN_TRUSTEES:
        return f"owner {m.group('owner')} is not Administrators/SYSTEM"
    allowed = 0
    for ace in re.findall(r"\(([^()]*)\)", m.group("aces")):
        f = ace.split(";")
        if len(f) < 6:
            return f"unparseable entry ({ace})"
        if f[0] in ("D", "OD", "XD"):
            continue
        if f[0] not in ("A", "OA", "XA"):
            return f"unexpected entry type in ({ace})"
        if f[5] not in ADMIN_TRUSTEES:
            return f"{f[5]} is granted access ({ace})"
        allowed += 1
    return None if allowed else "no allow entry for Administrators"


REPARSE_POINT = 0x400    # FILE_ATTRIBUTE_REPARSE_POINT: symlinks, junctions, mount points
RUN_NAME = re.compile(r"ThrottleStop-[0-9a-f]{16}\.sys\Z")   # the per-run copies of the driver


class Win32:
    """The thin, replaceable layer over Windows. Everything the checks below decide is decided from what these four methods report, so the decision
    logic is unit-tested with fakes on Linux (tests/tools_win); the methods themselves are only exercised on Windows (untested there: G-142)."""

    def is_reparse(self, path: Path) -> bool:
        """True when ``path`` itself is a reparse point (junction, symlink, mount point) or a symlink: it is never followed."""
        st = os.lstat(path)
        return bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT) or stat.S_ISLNK(st.st_mode)

    def sddl(self, path: Path) -> str:
        """The SDDL of a file or directory as Windows reports it (PowerShell Get-Acl; read-only)."""
        q = str(path).replace("'", "''")
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"(Get-Acl -LiteralPath '{q}').Sddl"], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            raise DriverError(f"cannot read the ACL of {path}: {(r.stderr or r.stdout).strip()}")
        return r.stdout.strip()

    def icacls(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["icacls.exe", *args], capture_output=True, text=True)

    def hold_deny_write(self, path: Path):
        """Open ``path`` for reading with share mode READ only and keep it open: nobody (admin included) can write, truncate, rename or delete it until the
        returned object's ``close()``. The service loader may still read it. Off Windows: a plain read handle (no sharing control)."""
        if os.name != "nt":
            return open(path, "rb")
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        h = k.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x00200000, None)  # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT
        if h in (None, wintypes.HANDLE(-1).value):
            raise DriverError(f"cannot hold {path} open (Win32 error {ctypes.get_last_error()})")

        class _Hold:
            def close(self_, _h=h, _k=k):
                _k.CloseHandle(_h)

        return _Hold()


win = Win32()


def dir_sddl(path: Path) -> str:
    return win.sddl(path)


def sddl_owner(sddl: str) -> str | None:
    m = re.match(r"\s*O:(.*?)(?:G:|D:|S:|$)", sddl or "")
    return m.group(1) if m and m.group(1) else None


def check_no_reparse(path: Path) -> None:
    """No component of ``path`` (the drive root down to the path itself) may be a reparse point: a junction planted earlier would redirect the driver path.
    Every existing ancestor must also be owned by Administrators / SYSTEM / TrustedInstaller (the drive root and ProgramData are; a user-owned parent is not)."""
    path = Path(path)
    chain = [path, *path.parents]
    for comp in reversed(chain):
        if str(comp) in ("", ".") or not comp.exists() and not comp.is_symlink():
            continue
        if win.is_reparse(comp):
            raise DriverError(f"{comp} is a reparse point (junction / symlink); refusing to use {path}")
        if comp != path and comp != comp.parent:   # ancestors: the owner must be trusted (their ACL is the system's, not ours to demand)
            owner = sddl_owner(win.sddl(comp))
            if owner not in ADMIN_TRUSTEES:
                raise DriverError(f"{comp} (a parent of {path}) is owned by {owner}, not by Administrators/SYSTEM; refusing")


def check_admin_dir(path: Path, *, get_sddl=None) -> None:
    """Raise DriverError unless ``path`` is a real directory (no component a reparse point), owned by Administrators/SYSTEM, whose ACL grants nothing to
    any other principal."""
    path = Path(path)
    if not path.is_dir():
        raise DriverError(f"{path} is not a directory")
    check_no_reparse(path)
    why = sddl_admin_only((get_sddl or dir_sddl)(path))
    if why:
        raise DriverError(f"{path} is not an admin-only directory ({why}). Refusing to use it: a non-administrator could replace what runs. "
                          f"Inspect it, remove it (rmdir /s /q \"{path}\") and run 'ts_driver.py init-dir' from an Administrator terminal")


def check_admin_file(path: Path, *, get_sddl=None) -> None:
    """Raise DriverError unless ``path`` is a regular file, not a reparse point, owned by Administrators/SYSTEM, with an admin-only ACL."""
    path = Path(path)
    if not path.is_file():
        raise DriverError(f"{path} is not a file")
    if win.is_reparse(path):
        raise DriverError(f"{path} is a reparse point; refusing")
    why = sddl_admin_only((get_sddl or dir_sddl)(path))
    if why:
        raise DriverError(f"{path} is not admin-only ({why}); refusing to load it")


def icacls(*args: str) -> subprocess.CompletedProcess:
    return win.icacls(*args)


def init_dir(path: Path | None = None, *, run=None, get_sddl=None) -> Path:
    """Create ``path`` (default ``data_dir()``) fresh and lock it down: owner Administrators, inheritance off, Administrators + SYSTEM full control, nothing
    else. A directory that already exists is only *checked* (real directory, no reparse point in the path, admin owner, admin-only ACL), never adopted: one a
    user pre-created could hold planted entries or be a junction."""
    path = Path(path) if path else data_dir()
    run = run or icacls
    if path.exists() or path.is_symlink():
        check_admin_dir(path, get_sddl=get_sddl)
        return path
    check_no_reparse(path.parent)
    path.mkdir()   # the parent must exist: no silent creation of a chain
    steps = (("/setowner", "*S-1-5-32-544"), ("/grant:r", "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"), ("/inheritance:r",))
    for step in steps:
        r = run(str(path), *step)
        if r.returncode != 0:
            raise DriverError(f"icacls {' '.join(step)} failed on {path}: {(r.stderr or r.stdout).strip()}")
    check_admin_dir(path, get_sddl=get_sddl)
    return path


def verify_driver_file(path: Path, *, get_sddl=None, data: bytes | None = None) -> bytes:
    """Everything that must hold right before the driver is loaded: no reparse point anywhere in the path, an admin-only directory and file (ACL and owner),
    and the SHA-256 of the bytes actually on disk (read now) equal to DRIVER_SHA256. Returns those bytes."""
    path = Path(path)
    check_admin_dir(path.parent, get_sddl=get_sddl)
    check_admin_file(path, get_sddl=get_sddl)
    got = path.read_bytes()
    h = sha256_hex(got)
    if h != DRIVER_SHA256:
        raise DriverError(f"{path} SHA-256 {h} is not the expected driver; refusing to start it")
    return got


def write_new_copy(directory: Path, data: bytes) -> Path:
    """Write ``data`` to a NEW, uniquely named file in ``directory`` (``O_CREAT | O_EXCL``: an existing name, link or planted file makes it fail). The bytes
    that were written are hashed (not the source): the copy that gets loaded is the copy that was verified."""
    if sha256_hex(data) != DRIVER_SHA256:
        raise DriverError("the driver bytes in memory are not the expected driver; refusing to write a copy")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(8):
        path = Path(directory) / f"ThrottleStop-{secrets.token_hex(8)}.sys"
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return path
    raise DriverError(f"could not create a new uniquely named file in {directory}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def carve_bytes(exe_data: bytes, *, exe_sha=None, drv_sha=None, offset=None, size=None) -> bytes:
    """Verify the exe hash, cut the driver out, verify the driver hash. Constants are resolved at call time (patchable)."""
    exe_sha = exe_sha or EXE_SHA256
    drv_sha = drv_sha or DRIVER_SHA256
    offset = DRIVER_OFFSET if offset is None else offset
    size = DRIVER_SIZE if size is None else size
    got = sha256_hex(exe_data)
    if got != exe_sha.upper():
        raise DriverError(f"ThrottleStop.exe SHA-256 mismatch: got {got}, expected {exe_sha.upper()} (only 9.7.3 is supported)")
    if offset + size > len(exe_data):
        raise DriverError(f"exe too short for offset 0x{offset:X} + {size} bytes")
    drv = exe_data[offset:offset + size]
    got = sha256_hex(drv)
    if got != drv_sha.upper():
        raise DriverError(f"carved driver SHA-256 mismatch: got {got}, expected {drv_sha.upper()}")
    return drv


def carve(exe: Path, out: Path, *, secure=None) -> Path:
    """Cut the driver out of ``exe`` into ``out``. On Windows ``out``'s directory is first created / checked admin-only (``secure=False`` skips that: tests)."""
    drv = carve_bytes(Path(exe).read_bytes())
    out = Path(out)
    if os.name == "nt" if secure is None else secure:
        need_admin()
        init_dir(out.parent)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    if (out.exists() or out.is_symlink()) and win.is_reparse(out):
        raise DriverError(f"{out} is a reparse point; refusing to write through it")
    out.write_bytes(drv)
    return out


# ---- Windows service management -------------------------------------------------------------------------------------

def is_admin() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def sc(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True)


def service_state():
    """None if the service does not exist, else the STATE word (RUNNING, STOPPED, ...)."""
    r = sc("query", SERVICE)
    if r.returncode == 1060 or "1060" in r.stdout:
        return None
    for line in r.stdout.splitlines():
        if "STATE" in line:
            return line.split()[-1]
    return "UNKNOWN" if r.returncode == 0 else None


def explain_start_failure(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ("1275", "577", "blocked", "blocklist")):
        return ("Windows refused to load the driver (error 1275 / 577 = blocked / signature or policy). Likely the Microsoft Vulnerable Driver\n"
                "Blocklist or HVCI. Windows Security > Device security > Core isolation > Memory integrity: turn OFF (reboot), and\n"
                "Core isolation > 'Microsoft Vulnerable Driver Blocklist': turn OFF (reboot). Then re-run install. Re-enable both after the sitting.")
    if "5" in text.split() and "access" in t:
        return "Access denied: run from an elevated (Administrator) prompt."
    return ""


def need_admin():
    if not is_admin():
        raise DriverError("not elevated: run this from an Administrator terminal (Start > right-click Terminal > Run as administrator)")


def _norm_path(p: str) -> str:
    p = p.strip().strip('"')
    if p.startswith("\\??\\"):
        p = p[4:]
    return os.path.normcase(os.path.normpath(p)) if p else ""


def service_binpath() -> str | None:
    """The BINARY_PATH_NAME the service is configured with (normalized), or None."""
    r = sc("qc", SERVICE)
    for line in r.stdout.splitlines():
        if "BINARY_PATH_NAME" in line and ":" in line:
            return _norm_path(line.split(":", 1)[1])
    return None


def _delete_service() -> None:
    """Stop and delete the ThrottleStop service if there is one, and delete the per-run driver copy it pointed at (only a ``ThrottleStop-<hex>.sys``
    directly inside the admin-only directory is ever deleted)."""
    if service_state() is None:
        return
    bp = service_binpath()
    sc("stop", SERVICE)
    r = sc("delete", SERVICE)
    if r.returncode != 0:
        raise DriverError("sc delete failed: " + (r.stdout + r.stderr).strip())
    _remove_run_copy(bp)


def _remove_run_copy(binpath: str | None) -> None:
    if not binpath:
        return
    p = Path(binpath)
    if RUN_NAME.match(p.name) and _norm_path(str(p.parent)) == _norm_path(str(data_dir())):
        try:
            p.unlink()
        except OSError:
            pass


def cmd_install(sys_path: Path) -> int:
    """Start the driver for this run: a NEW uniquely named copy of the verified carved driver is written into the admin-only directory, a fresh service points
    at that copy, and directory / file ACLs, reparse points and the SHA-256 of the bytes on disk are verified again immediately before ``sc start`` while the
    file is held open (no writer, no rename, no delete). ``remove`` deletes the service and the copy. ``sys_path`` is the carved master copy."""
    need_admin()
    sys_path = Path(sys_path)
    master = verify_driver_file(sys_path)          # nothing is touched unless the carved master is the real driver in a trustworthy place
    _delete_service()                              # per-run service: never reuse one that exists (it may point anywhere)
    copy = write_new_copy(sys_path.parent, master)
    created = False
    hold = None
    try:
        verify_driver_file(copy)
        r = sc("create", SERVICE, "type=", "kernel", "start=", "demand", "binPath=", str(copy.resolve()))
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            raise DriverError("sc create failed")
        created = True
        hold = win.hold_deny_write(copy)           # from here the file cannot be changed under us
        verify_driver_file(copy)                   # the check that matters: immediately before sc start, on the held file
        r = sc("start", SERVICE)
        out = (r.stdout + r.stderr).strip()
        print(out)
        if r.returncode not in (0, 1056):          # 1056 = already running
            hint = explain_start_failure(out + " " + str(r.returncode))
            raise DriverError("sc start failed (exit %d)%s" % (r.returncode, "\n" + hint if hint else ""))
    except BaseException:
        if hold is not None:
            hold.close()
            hold = None
        if created:
            sc("stop", SERVICE)
            sc("delete", SERVICE)
        try:
            copy.unlink()
        except OSError:
            pass
        raise
    finally:
        if hold is not None:
            hold.close()   # the loaded image keeps the file in use; the loader's own handle prevents deletion until `remove`
    return 0


def can_open_device() -> str:
    if os.name != "nt":
        return "n/a (not Windows)"
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k.CreateFileW(DEVICE, 0xC0000000, 0, None, 3, 0, None)  # GENERIC_READ|WRITE, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        return f"cannot open {DEVICE}: Win32 error {ctypes.get_last_error()} (5 = not elevated, 2 = driver not running)"
    k.CloseHandle(h)
    return f"{DEVICE} opens OK"


def cmd_status() -> int:
    state = service_state()
    print(f"service {SERVICE}: {state or 'not installed'}")
    if legacy_sys_path().exists():
        print(f"note: an old copy of the driver exists at {legacy_sys_path()} (user-writable; no longer used). Delete it.")
    print(f"admin: {is_admin()}")
    print(can_open_device())
    return 0 if state == "RUNNING" else 1


def cmd_remove() -> int:
    need_admin()
    if service_state() is None:
        print(f"service {SERVICE} not installed; nothing to do")
        return 0
    bp = service_binpath()
    print((sc("stop", SERVICE).stdout or "").strip())
    r = sc("delete", SERVICE)
    print((r.stdout or r.stderr).strip())
    _remove_run_copy(bp)
    return 0 if r.returncode == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("carve", help="cut the driver out of ThrottleStop.exe 9.7.3 (hash-verified)")
    c.add_argument("--exe", required=True, type=Path)
    c.add_argument("--out", type=Path, default=None, help="default %%ProgramData%%\\arltuner\\ThrottleStop.sys (an admin-only directory)")
    for name in ("start", "install"):
        i = sub.add_parser(name, help="verify the admin-only directory + driver SHA-256, sc create if needed, verify again, sc start (elevated)")
        i.add_argument("--sys", dest="sys_path", type=Path, default=None)
    sub.add_parser("init-dir", help="create and lock down %%ProgramData%%\\arltuner (elevated)")
    k = sub.add_parser("check-dir", help="exit 0 when DIR is an admin-only directory")
    k.add_argument("dir", type=Path)
    sub.add_parser("status")
    sub.add_parser("remove", help="sc stop + sc delete (elevated)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "carve":
            out = carve(a.exe, a.out or default_sys_path())
            print(f"carved {DRIVER_SIZE} bytes, SHA-256 verified -> {out}")
            return 0
        if a.cmd in ("start", "install"):
            return cmd_install(a.sys_path or default_sys_path())
        if a.cmd == "init-dir":
            need_admin()
            print(f"admin-only directory ready: {init_dir()}")
            return 0
        if a.cmd == "check-dir":
            check_admin_dir(a.dir)
            print(f"{a.dir}: admin-only")
            return 0
        if a.cmd == "status":
            return cmd_status()
        return cmd_remove()
    except DriverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
