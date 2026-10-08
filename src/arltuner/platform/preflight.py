"""Preflight (SPEC section 3): pure checks over an injectable filesystem root. Read-only, no hardware access, no subprocess.

``preflight(root)`` reads a handful of small files under ``root`` (``/proc/cpuinfo``, ``/sys/kernel/security/lockdown``, ``/dev/cpu/*/msr``,
``/sys/module/msr/parameters/allow_writes``, ``/proc/<pid>/comm``, executables in the usual bin directories) and returns one named ``CheckResult``
per check. A failed check is a named refusal, never a workaround. The fused-word identity half of "recognised platform" is a mailbox read and
belongs to ``vf.dump`` (G-102); this module only covers CPUID (vendor, family, model,
stepping on every online CPU); the CPU count is ``topology.detect``'s job.
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .schema import Platform, default_platform

__all__ = [
    "MIN_PYTHON",
    "OPTIONAL_TOOLS",
    "CheckResult",
    "PreflightReport",
    "Status",
    "preflight",
]

MIN_PYTHON = (3, 12)
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"

#: optional tool -> package hint for "missing: install <pkg>" (SPEC section 3).
OPTIONAL_TOOLS: dict[str, str] = {
    "stress-ng": "stress-ng",
    "stressapptest": "stressapptest",
    "perf": "perf (Fedora) / linux-tools (Debian, Ubuntu)",
    "turbostat": "kernel-tools (Fedora) / linux-tools (Debian, Ubuntu)",
    "sensors": "lm_sensors (Fedora) / lm-sensors (Debian, Ubuntu)",
    "nvidia-smi": "the NVIDIA driver utilities (nvidia-utils / nvidia-smi)",
}

#: Process names (``/proc/<pid>/comm``, max 15 chars) that host a polkit authentication agent. GNOME's agent lives inside the shell.
_AGENT_COMMS = frozenset({"gnome-shell", "plasmashell", "cinnamon", "budgie-wm", "pantheon-agent-", "pkttyagent"})
_AGENT_WORDS = ("polkit", "policykit")


class Status(Enum):
    PASS = "pass"
    READ_ONLY = "read-only"  # the machine can be read but writes are not possible / not allowed
    REFUSE = "refuse"  # the app cannot run at all
    UNKNOWN = "unknown"  # not determinable from here (unprivileged); the root helper's re-run is authoritative; never changes the mode
    MISSING = "missing"  # an optional feature's tool is absent (the feature shows "missing: install <pkg>")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    reason: str  # human explanation, also for PASS (what was seen)

    @property
    def ok(self) -> bool:
        return self.status is Status.PASS


@dataclass(frozen=True)
class PreflightReport:
    platform_id: str
    results: tuple[CheckResult, ...]

    def get(self, name: str) -> CheckResult:
        for r in self.results:
            if r.name == name:
                return r
        raise KeyError(name)

    @property
    def refused(self) -> bool:
        return any(r.status is Status.REFUSE for r in self.results)

    @property
    def mode(self) -> str:
        """``"refuse"`` | ``"read-only"`` | ``"write"``. MISSING optional tools never change the mode."""
        if self.refused:
            return "refuse"
        return "read-only" if any(r.status is Status.READ_ONLY for r in self.results) else "write"

    @property
    def writes_allowed(self) -> bool:
        return self.mode == "write"

    def problems(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status in (Status.READ_ONLY, Status.REFUSE))

    def to_dict(self) -> dict[str, object]:
        """JSON-able form for the helper protocol (op ``preflight``)."""
        return {
            "platform": self.platform_id,
            "mode": self.mode,
            "checks": [{"name": r.name, "status": r.status.value, "reason": r.reason} for r in self.results],
        }


# --- individual checks (each pure: root-relative reads only) -------------------------------------------------------------------------------

def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8", errors="replace")


def _cpuinfo_blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        key, sep, value = line.partition(":")
        if sep:
            cur.setdefault(key.strip(), value.strip())
    if cur:
        blocks.append(cur)
    return [b for b in blocks if "processor" in b]


def check_cpuid(blocks: Sequence[dict[str, str]] | None, plat: Platform) -> CheckResult:
    name = "cpuid"
    if not blocks:
        return CheckResult(name, Status.READ_ONLY, "/proc/cpuinfo is unreadable or has no processor entries: CPU not recognised")
    want = plat.cpuid
    for b in blocks:
        try:
            got = (b.get("vendor_id", ""), int(b.get("cpu family", "")), int(b.get("model", "")), int(b.get("stepping", "")))
        except ValueError:
            return CheckResult(name, Status.READ_ONLY, f"processor {b.get('processor', '?')}: cpu family/model/stepping missing or not numeric")
        if got != (want.vendor, want.family, want.model, want.stepping):
            return CheckResult(
                name,
                Status.READ_ONLY,
                f"processor {b.get('processor', '?')}: {got[0]!r} family {got[1]} model {got[2]} stepping {got[3]} does not match platform "
                f"{plat.id} ({want.vendor!r} family {want.family} model {want.model} stepping {want.stepping}, CPUID {want.signature:#x})",
            )
    return CheckResult(name, Status.PASS, f"CPUID {want.signature:#x} matches platform {plat.id} on all {len(blocks)} CPUs (fused-word identity: checked at vf.dump)")


def check_hypervisor(blocks: Sequence[dict[str, str]] | None) -> CheckResult:
    name = "hypervisor"
    if not blocks:
        return CheckResult(name, Status.READ_ONLY, "cannot read /proc/cpuinfo flags: cannot rule out a hypervisor")
    if any("hypervisor" in b.get("flags", "").split() for b in blocks):
        return CheckResult(name, Status.READ_ONLY, "the 'hypervisor' cpuinfo flag is set: running under a hypervisor, MSR writes do not reach the hardware")
    if not any("flags" in b for b in blocks):
        return CheckResult(name, Status.READ_ONLY, "/proc/cpuinfo has no flags line: cannot rule out a hypervisor")
    return CheckResult(name, Status.PASS, "no 'hypervisor' cpuinfo flag")


_LOCKDOWN_WORD = re.compile(r"\[([a-z-]+)\]")


def check_lockdown(root: Path) -> CheckResult:
    name = "lockdown"
    try:
        text = _read(root, "sys/kernel/security/lockdown")
    except FileNotFoundError:
        return CheckResult(name, Status.PASS, "no /sys/kernel/security/lockdown (kernel without the lockdown LSM): nothing blocks MSR writes")
    except OSError as exc:
        return CheckResult(name, Status.READ_ONLY, f"cannot read /sys/kernel/security/lockdown ({exc.strerror or exc}): lockdown state unknown")
    m = _LOCKDOWN_WORD.findall(text)
    if len(m) != 1:
        return CheckResult(name, Status.READ_ONLY, f"unparsable lockdown file {text.strip()!r}: expected exactly one bracketed word")
    if m[0] != "none":
        return CheckResult(name, Status.READ_ONLY, f"kernel lockdown is [{m[0]}] (Secure Boot lockdown blocks MSR writes); needs [none]")
    return CheckResult(name, Status.PASS, "lockdown [none]")


def _msr_loadable(root: Path) -> bool:
    try:
        release = _read(root, "proc/sys/kernel/osrelease").strip()
    except OSError:
        return False
    if not release or "/" in release:
        return False
    base = root / "lib" / "modules" / release
    for sub in ("kernel/arch/x86/kernel", "kernel/drivers"):
        d = base / sub
        try:
            if d.is_dir() and any(p.name.startswith("msr.ko") for p in d.iterdir()):
                return True
        except OSError:
            continue
    return False


def check_msr_device(root: Path, plat: Platform) -> CheckResult:
    name = "msr_device"
    cpus = range(plat.topology.cpu_count)
    missing = [c for c in cpus if not (root / "dev" / "cpu" / str(c) / "msr").exists()]
    if not missing:
        return CheckResult(name, Status.PASS, f"/dev/cpu/0..{plat.topology.cpu_count - 1}/msr present")
    where = f"/dev/cpu/{missing[0]}/msr" if len(missing) == 1 else f"/dev/cpu/*/msr for {len(missing)} of {plat.topology.cpu_count} CPUs (first: {missing[0]})"
    if (root / "sys" / "module" / "msr").exists():
        hint = "the msr module is loaded or built in but the device nodes are absent (devtmpfs/udev problem)"
    elif _msr_loadable(root):
        hint = "the msr module is loadable: load it (modprobe msr) as an explicit operator step"
    else:
        hint = "the msr module is neither loaded, built in nor found under /lib/modules"
    return CheckResult(name, Status.READ_ONLY, f"{where} missing: {hint}")


def check_msr_allow_writes(root: Path) -> CheckResult:
    name = "msr_allow_writes"
    path = root / "sys" / "module" / "msr" / "parameters" / "allow_writes"
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        if (root / "sys" / "module" / "msr").exists():
            return CheckResult(name, Status.PASS, "msr has no allow_writes parameter (kernel predating it): writes not gated by it")
        return CheckResult(name, Status.READ_ONLY, "msr module not present (see msr_device): allow_writes cannot be read")
    except OSError as exc:
        # /sys/module/msr/parameters/allow_writes is mode 0600 (arch/x86/kernel/msr.c): an unprivileged run cannot read it. That is not evidence
        # of "off", so it must not downgrade the mode; the helper (root) re-runs preflight before every apply.
        return CheckResult(name, Status.UNKNOWN, f"cannot read msr allow_writes ({exc.strerror or exc}; root-only file): the root helper's preflight decides")
    if value == "off":
        return CheckResult(name, Status.READ_ONLY, "msr.allow_writes=off: the kernel refuses MSR writes (boot without msr.allow_writes=off, or set it to on/default)")
    if value not in ("on", "default", ""):
        return CheckResult(name, Status.READ_ONLY, f"msr.allow_writes={value!r} is not one of on/default/off")
    return CheckResult(name, Status.PASS, f"msr.allow_writes={value or 'default'}")


def _find_exe(root: Path, name: str, path_dirs: Iterable[str]) -> Path | None:
    for d in path_dirs:
        cand = root / d.lstrip("/") / name
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return cand
        except OSError:
            continue
    return None


def check_pkexec(root: Path, path_dirs: Sequence[str]) -> CheckResult:
    if _find_exe(root, "pkexec", path_dirs) is None:
        return CheckResult("pkexec", Status.READ_ONLY, "pkexec not found in " + ":".join(path_dirs) + ": the root helper cannot start (install polkit)")
    return CheckResult("pkexec", Status.PASS, "pkexec found")


_POLKITD_PATHS = ("usr/lib/polkit-1/polkitd", "usr/libexec/polkitd", "usr/lib/polkit-1/polkitd-1", "usr/lib/policykit-1/polkitd", "usr/libexec/polkit-1/polkitd")


def check_polkit(root: Path) -> CheckResult:
    name = "polkit"
    found = next((p for p in _POLKITD_PATHS if (root / p).is_file()), None)
    if found is None:
        return CheckResult(name, Status.READ_ONLY, "polkitd not found: the root helper cannot start (install polkit)")
    return CheckResult(name, Status.PASS, f"polkitd installed (/{found})")


def _process_comms(root: Path) -> list[str] | None:
    proc = root / "proc"
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    comms = []
    for e in entries:
        if not e.name.isdigit():
            continue
        try:
            comms.append((e / "comm").read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            continue  # the process ended, or hidepid
    return comms


def check_polkit_agent(root: Path) -> CheckResult:
    name = "polkit_agent"
    comms = _process_comms(root)
    if comms is None:
        return CheckResult(name, Status.READ_ONLY, "cannot list /proc: cannot tell whether a polkit agent runs in the session")
    for c in comms:
        low = c.lower()
        # comm is truncated to 15 chars ("polkit-gnome-au", "lxqt-policykit-"), so match the family name and exclude the daemon itself.
        if c in _AGENT_COMMS or (any(w in low for w in _AGENT_WORDS) and not low.startswith("polkitd")):
            return CheckResult(name, Status.PASS, f"polkit agent present ({c})")
    return CheckResult(name, Status.READ_ONLY, "no polkit authentication agent in the session: pkexec cannot ask for the password, the helper cannot start")


def check_python(version: Sequence[int]) -> CheckResult:
    v = tuple(version[:3])
    text = ".".join(str(x) for x in v)
    if v < MIN_PYTHON:
        return CheckResult("python", Status.REFUSE, f"Python {text} is older than the required {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")
    return CheckResult("python", Status.PASS, f"Python {text}")


def check_tools(root: Path, path_dirs: Sequence[str], tools: dict[str, str]) -> list[CheckResult]:
    out = []
    for tool, pkg in tools.items():
        where = _find_exe(root, tool, path_dirs)
        if where is None:
            out.append(CheckResult(f"tool:{tool}", Status.MISSING, f"missing: install {pkg}"))
        else:
            out.append(CheckResult(f"tool:{tool}", Status.PASS, f"{tool} found"))
    return out


# --- entry point -------------------------------------------------------------------------------------------------------------------------

def preflight(
    root: Path = Path("/"),
    *,
    platform: Platform | None = None,
    python_version: Sequence[int] | None = None,
    path_dirs: Sequence[str] | None = None,
    tools: dict[str, str] | None = None,
) -> PreflightReport:
    """Run every SPEC section 3 check against the filesystem under ``root`` and return the named results (never raises on a missing file)."""
    root = Path(root)
    plat = platform if platform is not None else default_platform()
    dirs = tuple(path_dirs) if path_dirs is not None else tuple(DEFAULT_PATH.split(":"))
    version = tuple(python_version) if python_version is not None else tuple(sys.version_info[:3])

    try:
        blocks: list[dict[str, str]] | None = _cpuinfo_blocks(_read(root, "proc/cpuinfo")) or None
    except OSError:
        blocks = None

    checks: list[Callable[[], CheckResult | list[CheckResult]]] = [
        lambda: check_cpuid(blocks, plat),
        lambda: check_hypervisor(blocks),
        lambda: check_lockdown(root),
        lambda: check_msr_device(root, plat),
        lambda: check_msr_allow_writes(root),
        lambda: check_pkexec(root, dirs),
        lambda: check_polkit(root),
        lambda: check_polkit_agent(root),
        lambda: check_python(version),
        lambda: check_tools(root, dirs, OPTIONAL_TOOLS if tools is None else tools),
    ]
    results: list[CheckResult] = []
    for run in checks:
        r = run()
        results.extend(r if isinstance(r, list) else [r])
    return PreflightReport(plat.id, tuple(results))
