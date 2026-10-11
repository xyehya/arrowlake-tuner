"""Fake stress binaries and a StressWiring over fakes for the G-116 tests. Nothing here starts a real stress-ng / stressapptest, touches hardware or needs root.

The fake "tools" are tiny Python scripts generated into a temp directory (never the real ``/usr/bin`` paths) that print one of the recorded outputs under
``tests/stress/fakebin/*.txt`` and exit with a chosen code, optionally hanging with a grandchild in the same process group.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arltuner.health import HealthSnapshot
from arltuner.helper.ops_stress import Invoker, StressWiring, Workdir
from arltuner.stress import KernelLogWatch, StressStore

FAKEBIN = Path(__file__).parent / "fakebin"

_TEMPLATE = """#!{python}
import os, signal, subprocess, sys, time
sys.stdout.write({text!r}); sys.stdout.flush()
if {ignore_term}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if {hang}:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    with open(os.path.abspath(sys.argv[0]) + ".pid", "w") as handle:
        handle.write(str(child.pid))
    time.sleep(300)
time.sleep({delay})
sys.exit({rc})
"""


def make_fake(directory: Path, name: str, recorded: str, rc: int = 0, *, hang: bool = False, ignore_term: bool = False, delay: float = 0.0) -> str:
    """A fake tool ``directory/name`` that prints ``fakebin/<recorded>.txt`` and exits ``rc`` (or hangs, with a grandchild whose pid it writes to ``<name>.pid``)."""
    path = directory / name
    text = (FAKEBIN / f"{recorded}.txt").read_text(encoding="utf-8")
    path.write_text(_TEMPLATE.format(python=sys.executable, text=text, hang=hang, ignore_term=ignore_term, delay=delay, rc=rc), encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return str(path)


def alive(pid: int) -> bool:
    """A process that exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        return os.path.exists(f"/proc/{pid}")


class RecordingPopen:
    """Records every spawn's argv and kwargs; as a non-root user it strips the privilege-drop kwargs (setgroups would be EPERM) before the real ``Popen``."""

    DROP = ("user", "group", "extra_groups")

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.Popen:
        self.calls.append((list(cmd), dict(kwargs)))
        real = {k: v for k, v in kwargs.items() if k not in self.DROP}
        return subprocess.Popen(cmd, **real)


class RecordingKillpg:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pgid: int, sig: int) -> None:
        self.calls.append((pgid, int(sig)))
        os.killpg(pgid, sig)


class FakeKlog:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.fail = False

    def mark(self) -> int:
        if self.fail:
            raise OSError(13, "no kernel log here")
        return 0

    def new_lines(self, mark: int) -> list[str]:
        return list(self.lines)


def health_snapshot(**overrides: Any) -> HealthSnapshot:
    values: dict[str, Any] = dict(
        taken_at=0.0, taint=0, taint_bits=(), pci=frozenset({"0000:00:00.0"}), modules=frozenset({"msr"}), thermal_mc=None, machinecheck=None
    )
    values.update(overrides)
    return HealthSnapshot(**values)


class FakeWorkdirs:
    """A ``workdir_fn`` that hands out real scratch directories under ``root`` (no chown, no ``/run/user``) and records them and their removal."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.made: list[str] = []
        self.removed: list[str] = []

    def __call__(self, invoker: Invoker) -> Workdir:
        self.root.mkdir(exist_ok=True)
        path = tempfile.mkdtemp(prefix="work-", dir=self.root)
        self.made.append(path)

        def cleanup() -> str | None:
            shutil.rmtree(path)
            self.removed.append(path)
            return None

        return Workdir(path, cleanup)


def stress_wiring(tmp: Path, **overrides: Any) -> StressWiring:
    defaults = tmp / "default-bin"  # not ``tmp`` itself: a test's own fake of the same name must not be overwritten by the default one
    defaults.mkdir(exist_ok=True)
    values: dict[str, Any] = dict(
        store=StressStore(tmp / "stress"),
        popen=RecordingPopen(),
        stress_ng=make_fake(defaults, "stress-ng", "stress-ng-ok"),
        stressapptest=make_fake(defaults, "stressapptest", "stressapptest-ok"),
        trust_check=None,
        invoker_fn=lambda: Invoker(1000, 1000, "operator"),
        sample_fn=lambda helper: None,
        klog_factory=lambda: KernelLogWatch([FakeKlog()]),
        health_fn=lambda: health_snapshot(),
        meminfo_fn=lambda: 16384,
        killpg=RecordingKillpg(),
        progress_s=0.02,
        settle_s=0.0,
        grace_s=30.0,
        term_wait_s=0.5,
        workdir_fn=FakeWorkdirs(tmp / "workdirs"),
    )
    values.update(overrides)
    return StressWiring(**values)


def sim_sampler(cpufreq: Any, model: Any, ratio_override: Callable[[int], int | None] | None = None) -> Callable[[Any], dict[str, Any]]:
    """A telemetry snapshot (the ``Sampler.collect`` shape) of a fully loaded machine: every CPU runs at what the cap currently set lets it settle at."""
    counter = [0]

    def reading(value: float, unit: str) -> dict[str, Any]:
        return {"value": value, "unit": unit, "source": "test", "status": "ok"}

    def sample(helper: Any) -> dict[str, Any]:
        counter[0] += 1
        cores: dict[int, Any] = {}
        for cpu in range(24):
            ratio = model.settled(model.unit_of(cpu), int(cpufreq.read(cpu, "scaling_max_freq")))
            if ratio_override is not None and ratio_override(cpu) is not None:
                ratio = ratio_override(cpu)  # type: ignore[assignment]
            cores[cpu] = {"mhz": reading(ratio * 100.0 + 1.0, "MHz"), "ratio": reading(float(ratio), "x")}
        temps = {"Package": reading(70.0 + counter[0] % 3, "C"), "Core 0": reading(75.5, "C"), "Core 1": {"value": None, "status": "unavailable"}}
        return {"ts": float(counter[0]), "cores": cores, "temps": temps, "power": {"package": reading(180.0 + counter[0], "W")}}

    return sample
