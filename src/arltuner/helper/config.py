"""Everything the helper needs from the outside world, injectable: tests pass fakes, ``arltuner.helper.main`` passes the real ones."""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arltuner.guard import read_boot_id
from arltuner.msr import MsrPort
from arltuner.platform.preflight import PreflightReport
from arltuner.topology import Topology

__all__ = [
    "HEARTBEAT_S", "HelperConfig", "INSTALLED_ACCEPTED_DIR", "INSTALLED_AUDIT", "INSTALLED_CODE_DIR", "INSTALLED_HELPER", "INSTALLED_HELPER_TOML",
    "INSTALLED_LOCK", "INSTALLED_LOCK_DIR", "INSTALLED_PLATFORMS_DIR", "INSTALLED_POLICY", "INSTALLED_STATE_DIR", "INSTALLED_WEB_DIR",
]

# The installed layout (SPEC 4.4 / 11, D-030; created by ``install.sh``). Everything the root helper trusts lives under these root-owned paths.
INSTALLED_CODE_DIR = Path("/usr/lib/arltuner")  # the ``arltuner`` package is INSTALLED_CODE_DIR / "arltuner", platform files in ../platforms
INSTALLED_PLATFORMS_DIR = INSTALLED_CODE_DIR / "platforms"
INSTALLED_HELPER = INSTALLED_CODE_DIR / "arltuner-helper"  # the polkit action's exec.path; started as ``pkexec <this> --device``
INSTALLED_WEB_DIR = Path("/usr/share/arltuner/web")
INSTALLED_POLICY = Path("/etc/arltuner/policy.toml")  # its presence + trust is the CLI's "installed" test (D-030)
INSTALLED_HELPER_TOML = Path("/etc/arltuner/helper.toml")
INSTALLED_ACCEPTED_DIR = Path("/etc/arltuner/accepted")
INSTALLED_STATE_DIR = Path("/var/lib/arltuner")
INSTALLED_AUDIT = INSTALLED_STATE_DIR / "audit.jsonl"
INSTALLED_LOCK_DIR = Path("/run/arltuner")  # tmpfiles.d: 0755 root (``/run/lock`` is 1777: anyone could pre-create the lock file there)
INSTALLED_LOCK = INSTALLED_LOCK_DIR / "mailbox.lock"

HEARTBEAT_S = 2.0  # contract 8
HealthGate = Callable[[str], tuple[bool, str]]


@dataclass
class HelperConfig:
    """``lock_path`` has no default on purpose: the launcher chooses it (the CLI's ``default_lock_path()``, which returns ``INSTALLED_LOCK`` on an installed machine), tests use a temp file.

    ``simulator``: the port is the simulator (no hardware), so the preflight write gate is not applied and ``vf.dump`` runs on a virtual clock
    (dwell is honoured in virtual time, as the CLI's ``--sim`` does). With ``simulator=False`` every op marked ``write`` first needs a fresh
    preflight in mode ``write`` with no ``UNKNOWN`` check (D-027 c), a health gate, and the dwell is real.
    """

    lock_path: Path
    audit_path: Path
    policy_path: Path | None  # None: the built-in conservative M1 policy (simulator and tests only; the launcher requires a file for hardware)
    port_factory: Callable[[], MsrPort]
    topology_fn: Callable[[], Topology]
    preflight_fn: Callable[[], PreflightReport]
    sampler_factory: Callable[[float], Any]  # interval_s -> object with start() / stop() / snapshot() (arltuner.telemetry.Sampler)
    simulator: bool = True
    accepted_dir: Path | None = None  # root-owned accepted state (``EXPECTED-words.txt`` inside); None = no accepted-state compare (simulator, tests)
    enforce_trust: bool = False  # SPEC 4.4 path checks on policy and audit paths; the launcher turns it on for hardware
    lstat_fn: Callable[[str], Any] = os.lstat
    euid_fn: Callable[[], int] = getattr(os, "geteuid", lambda: 0)  # who must own every file the helper opens for writing (root on hardware)
    health_gate_factory: Callable[[], HealthGate | None] = lambda: None
    boot_id_fn: Callable[[], str | None] = read_boot_id
    wall_clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], object] = time.sleep  # the mailbox transport's poll sleep on hardware (never the cancel-aware one)
    heartbeat_s: float = HEARTBEAT_S
    operator: str = "helper"  # recorded in the audit log; the polkit password is the human act (SPEC 4.1)
    extra_ops: Mapping[str, Any] = field(default_factory=dict)  # tests: stub ops (OpSpec) registered next to the built-in ones
    log: Callable[[str], None] = lambda text: None  # stderr in production
