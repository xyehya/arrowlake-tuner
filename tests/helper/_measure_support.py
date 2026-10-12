"""Fake sysfs + simulator wiring for the G-111 tests: 24 CPUs of cpufreq files in a temp directory, the SimModel for ``0x198``, a store and a journal in temp paths.
Nothing here touches the real /sys, a real MSR, a process or a sleep (the run's sleep is injectable)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from arltuner.curves import CurveStore
from arltuner.helper.ops_measure import (
    CAP_ATTR, EPP_ATTR, GOV_ATTR, MIN_ATTR, CpufreqFiles, MeasureWiring, RunEnv, SimMeasureIO, SimModel, sim_memory,
)
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.topology import Topology

from ._support import Harness, HelperCase

ORIGINAL_EPP = "balance_power"
ORIGINAL_GOV = "powersave"


def reference_read(topology: Topology | None = None) -> tuple[dict[int, list[int]], dict[int, int]]:
    """``(last_words, fused_words)`` as a ``tune.read`` of the simulated reference CPU (or of ``topology``) would leave them on the helper: per CPU ``[OC word, point
    1, ...]`` and the live ``0x09`` word."""
    sim = SimMsrPort(SimConfig.measured(per_point=True, topology=topology) if topology is not None else SimConfig.measured(per_point=True))
    words: dict[int, list[int]] = {}
    fused: dict[int, int] = {}
    for core in sim.config.topology.cores:
        ws = [sim.vf(core.cpu, 0, 0).to_u32()]
        for p2 in range(1, 16):
            w = sim.vf(core.cpu, 0, p2)
            if not w.ratio:
                break
            ws.append(w.to_u32())
        words[core.cpu] = ws
        fused[core.cpu] = sim.fused_word(core.cpu)
    return words, fused


def make_cpufreq(root: Path, model: SimModel | None = None) -> CpufreqFiles:
    model = model or SimModel()
    for cpu in range(24):
        d = root / f"cpu{cpu}" / "cpufreq"
        d.mkdir(parents=True)
        (d / CAP_ATTR).write_text(f"{model.max_khz(cpu)}\n")
        (d / "cpuinfo_max_freq").write_text(f"{model.max_khz(cpu)}\n")
        (d / MIN_ATTR).write_text("800000\n")
        (d / GOV_ATTR).write_text(f"{ORIGINAL_GOV}\n")
        (d / EPP_ATTR).write_text(f"{ORIGINAL_EPP}\n")
    return CpufreqFiles(root)


class RecordingCpufreq(CpufreqFiles):
    """Records every write, and notes whether the journal already existed at the first one."""

    def __init__(self, root: Path, journal: Path) -> None:
        super().__init__(root)
        self.journal = journal
        self.writes: list[tuple[int, str, str]] = []
        self.journal_existed_at_first_write: bool | None = None
        self.fail: Callable[[int, str, str], bool] | None = None

    def write(self, cpu: int, attr: str, value: str) -> None:
        if self.journal_existed_at_first_write is None:
            self.journal_existed_at_first_write = self.journal.exists()
        if self.fail is not None and self.fail(cpu, attr, value):
            raise OSError(5, "injected write failure")
        self.writes.append((cpu, attr, str(value)))
        super().write(cpu, attr, value)


class MeasureCase(HelperCase):
    def setUp(self) -> None:
        super().setUp()
        self.sysfs = self.tmp / "sys-cpu"
        self.journal = self.tmp / "measure-journal.json"
        self.model = SimModel()
        self.cpufreq = RecordingCpufreq(self.sysfs, self.journal)
        make_cpufreq(self.sysfs, self.model)
        self.io = SimMeasureIO(self.cpufreq, self.model)
        self.store = CurveStore(self.tmp / "curves")
        self.sleep_hook: Callable[[float], object] = lambda s: None
        self.closed = 0
        self.perf_hook: Callable[[int], None] | None = None

    def read_perf(self, cpu: int) -> int:
        if self.perf_hook is not None:
            self.perf_hook(cpu)
        return self.io.read_perf(cpu)

    def close_run(self) -> None:
        self.closed += 1

    def wiring(self, **overrides: Any) -> MeasureWiring:
        values: dict[str, Any] = dict(
            cpufreq=self.cpufreq,
            make_run=lambda: RunEnv(self.io, self.read_perf, lambda s: self.sleep_hook(s), self.close_run),
            store=self.store,
            journal_path=self.journal,
            mem_fn=sim_memory,
            kernel_fn=lambda: "6.99-test",
            settle_s=0.0,
            samples=10,
            interval_s=0.0,
        )
        values.update(overrides)
        return MeasureWiring(**values)

    def start_measure(self, wiring: MeasureWiring | None = None, *, last_words: dict[int, list[int]] | None = None, fused_words: dict[int, int] | None = None, kept_read: bool = False
    ) -> Harness:
        h = Harness(self.config())
        h.helper.measure_wiring = wiring or self.wiring()
        if kept_read:  # this boot's tune.read of the simulated reference CPU: the words in force and the live 0x09 words
            words, fused = reference_read()
            h.helper.last_words = words if last_words is None else last_words
            h.helper.fused_words = fused
        else:
            if last_words is not None:
                h.helper.last_words = last_words  # what G-112's tune.read sets
            if fused_words is not None:
                h.helper.fused_words = fused_words
        self.harnesses.append(h)
        h.start()
        return h

    def read(self, cpu: int, attr: str) -> str | None:
        return self.cpufreq.read(cpu, attr)

    def assert_cpufreq_pristine(self) -> None:
        for cpu in range(24):
            self.assertEqual(self.read(cpu, CAP_ATTR), str(self.model.max_khz(cpu)), f"cap of cpu {cpu}")
            self.assertEqual(self.read(cpu, EPP_ATTR), ORIGINAL_EPP, f"EPP of cpu {cpu}")
            self.assertEqual(self.read(cpu, GOV_ATTR), ORIGINAL_GOV)
            self.assertEqual(self.read(cpu, MIN_ATTR), "800000")
        self.assertFalse(self.journal.exists(), "the journal must be gone after a clean restore")
        self.assertEqual(self.io.loaded, set(), "no load left running")


__all__ = ["MeasureCase", "RecordingCpufreq", "make_cpufreq", "reference_read"]
