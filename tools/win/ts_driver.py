#!/usr/bin/env python3
"""ts_driver.py - carve and manage ThrottleStop's signed kernel driver (host win, stdlib only).

The driver is NOT redistributed: it is cut out of the operator's own ThrottleStop.exe (9.7.3) and verified by SHA-256.
  py -3 tools\\win\\ts_driver.py carve --exe C:\\Users\\yk\\Downloads\\ThrottleStop_9.7.3\\ThrottleStop.exe   # -> %LOCALAPPDATA%\\linuxmon\\ThrottleStop.sys
  py -3 tools\\win\\ts_driver.py install      # elevated: sc create ThrottleStop (kernel, demand) + sc start
  py -3 tools\\win\\ts_driver.py status       # service state + can \\\\.\\ThrottleStop be opened
  py -3 tools\\win\\ts_driver.py remove       # elevated: sc stop + sc delete
Evidence: docs/research/win-throttlestop-9.7.3.md (hashes, offset), win-throttlestop-fivr.md section 1 (service/device names).
"""
from __future__ import annotations

import argparse
import hashlib
import os
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


def default_sys_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "linuxmon" / "ThrottleStop.sys"


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


def carve(exe: Path, out: Path) -> Path:
    drv = carve_bytes(Path(exe).read_bytes())
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
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


def cmd_install(sys_path: Path) -> int:
    need_admin()
    sys_path = Path(sys_path).resolve()
    if not sys_path.is_file():
        raise DriverError(f"{sys_path} not found; run the 'carve' subcommand first")
    got = sha256_hex(sys_path.read_bytes())
    if got != DRIVER_SHA256:
        raise DriverError(f"{sys_path} SHA-256 {got} is not the expected driver; refusing to install")
    state = service_state()
    if state == "RUNNING":
        print(f"service {SERVICE} already exists and is RUNNING; nothing to do")
        return 0
    if state is None:
        r = sc("create", SERVICE, "type=", "kernel", "start=", "demand", "binPath=", str(sys_path))
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            raise DriverError("sc create failed")
    else:
        print(f"service {SERVICE} exists ({state}); starting it")
    r = sc("start", SERVICE)
    out = (r.stdout + r.stderr).strip()
    print(out)
    if r.returncode not in (0, 1056):  # 1056 = already running
        hint = explain_start_failure(out + " " + str(r.returncode))
        raise DriverError("sc start failed (exit %d)%s" % (r.returncode, "\n" + hint if hint else ""))
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
    print(f"admin: {is_admin()}")
    print(can_open_device())
    return 0 if state == "RUNNING" else 1


def cmd_remove() -> int:
    need_admin()
    if service_state() is None:
        print(f"service {SERVICE} not installed; nothing to do")
        return 0
    print((sc("stop", SERVICE).stdout or "").strip())
    r = sc("delete", SERVICE)
    print((r.stdout or r.stderr).strip())
    return 0 if r.returncode == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("carve", help="cut the driver out of ThrottleStop.exe 9.7.3 (hash-verified)")
    c.add_argument("--exe", required=True, type=Path)
    c.add_argument("--out", type=Path, default=None, help="default %%LOCALAPPDATA%%\\linuxmon\\ThrottleStop.sys")
    i = sub.add_parser("install", help="sc create + sc start (elevated)")
    i.add_argument("--sys", dest="sys_path", type=Path, default=None)
    sub.add_parser("status")
    sub.add_parser("remove", help="sc stop + sc delete (elevated)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "carve":
            out = carve(a.exe, a.out or default_sys_path())
            print(f"carved {DRIVER_SIZE} bytes, SHA-256 verified -> {out}")
            return 0
        if a.cmd == "install":
            return cmd_install(a.sys_path or default_sys_path())
        if a.cmd == "status":
            return cmd_status()
        return cmd_remove()
    except DriverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
