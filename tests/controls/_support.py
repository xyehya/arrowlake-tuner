"""Shared helpers for tests/controls and tests/test_cli: the simulator behind the REAL ``WriteGuard`` with a fake clock.

No hardware: the only MSR port is ``SimMsrPort`` (or a delegating wrapper around it). Temp files only.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from arltuner.controls import ApplyReport, CoreReadings, apply_profile
from arltuner.guard import ENV_FLAG, AllowEntry, AuditLog, Confirmation, Policy, WriteGuard, default_m1_policy, read_all
from arltuner.mailbox import MSR_OC_MAILBOX, Cmd, VfWord
from arltuner.msr import MailboxTransport, SimConfig, SimMsrPort
from arltuner.profiles import Profile, load_profile
from arltuner.topology import Topology, arl_s_285k

TOKEN = "tok-14a"
OPERATOR = "tester"
WRITE_CMDS = (0x11, 0x15, 0x1D, 0x27)

#: SIM-ONLY defaults of SimMsrPort (tests/msr, src/arltuner/msr/sim.py): P4 is 55x / raw 0x544 / adaptive / offset 0.
P4_BASELINE = VfWord(ratio=55, v_target_raw=0x544, static=False, offset_raw=0)
E_BASELINE = VfWord(ratio=48, v_target_raw=0x48C, static=False, offset_raw=0)  # E clusters 1..3 (cluster 0 is 0x4A0)


def readings_of(vf: VfWord, *, favored: int | None = None, fused: int | None = None, granular: int = 3) -> CoreReadings:
    """``CoreReadings`` for a core whose 0x10 word is ``vf``, with the 0x1C / 0x09 words the OLD SIM-ONLY defaults would give:
    ``favored`` = fused ratio and favored ratio both ``vf.ratio``, ``fused`` = the stock V/F point (``vf.ratio`` | ``vf.v_target_raw`` << 8)."""
    return CoreReadings(
        vf=vf,
        favored=(vf.ratio | vf.ratio << 8) if favored is None else favored,
        fused=(vf.ratio | vf.v_target_raw << 8) if fused is None else fused,
        granular=granular,
    )


P4_READINGS = readings_of(P4_BASELINE)
#: E clusters 1..3 of the old SIM-ONLY defaults: 0x1C = fused 46 << 8 | favored 48, fused point 48x / 0x48C
E_READINGS = readings_of(E_BASELINE, favored=46 << 8 | 48)


def profile_text(*entries: str, name: str = "t") -> str:
    """TOML text of a profile; each entry is e.g. ``'kind = "P"\\nindex = 4\\noffset_mv = -30'``."""
    body = "".join(f"\n[[core]]\n{entry}\n" for entry in entries)
    return f'[meta]\nname = "{name}"\nplatform = "arl-s-285k"\n{body}'


def profile(*entries: str, name: str = "t") -> Profile:
    return load_profile(profile_text(*entries, name=name))


def point_text(*points: str, entries: tuple[str, ...] = (), name: str = "t") -> str:
    """T-025: TOML text of a profile with ``[[point]]`` entries (each e.g. ``point_entry("E", 8, 6, -20)``) and optionally ``[[core]]`` ``entries``."""
    body = "".join(f"\n[[point]]\n{p}\n" for p in points)
    return profile_text(*entries, name=name) + body


def point_profile(*points: str, entries: tuple[str, ...] = (), name: str = "t") -> Profile:
    return load_profile(point_text(*points, entries=entries, name=name))


def point_entry(kind: str, index: int, point: int, offset_mv: int, expect_ratio: int | None = None) -> str:
    lines = [f'kind = "{kind}"', f"index = {index}", f"point = {point}", f"offset_mv = {offset_mv}"]
    if expect_ratio is not None:
        lines.append(f"expect_ratio = {expect_ratio}")
    return "\n".join(lines)


def with_point_entries(policy: Policy, **changes: object) -> Policy:
    """T-025: ``policy`` plus the two point entries (reads of ``0x10`` P2 1..15 and writes of ``0x11`` P2 1..15 under ``vf_point_offset_bounded``);
    the shipped policy has none (inert for point writes)."""
    entries = (
        AllowEntry("vf_point_read", Cmd.VOLTAGE_FREQ_READ, frozenset({0}), frozenset(range(1, 16)), "read"),
        AllowEntry("vf_point_write", Cmd.VOLTAGE_FREQ_WRITE, frozenset({0}), frozenset(range(1, 16)), "write", "vf_point_offset_bounded"),
    )
    return dataclasses.replace(policy, allow=(*policy.allow, *entries), **changes)


def p4(offset_mv: int | None = None, ratio: int | None = None, oc_voltage_mv: int | None = None) -> str:
    return _entry("P", 4, offset_mv, ratio, oc_voltage_mv)


def e_core(index: int, offset_mv: int | None = None, ratio: int | None = None, oc_voltage_mv: int | None = None) -> str:
    return _entry("E", index, offset_mv, ratio, oc_voltage_mv)


def _entry(kind: str, index: int, offset_mv: int | None, ratio: int | None, oc_voltage_mv: int | None) -> str:
    lines = [f'kind = "{kind}"', f"index = {index}"]
    if ratio is not None:
        lines.append(f"ratio = {ratio}")
    if oc_voltage_mv is not None:
        lines.append(f"oc_voltage_mv = {oc_voltage_mv}")
    if offset_mv is not None:
        lines.append(f"offset_mv = {offset_mv}")
    return "\n".join(lines)


#: The sanity maxima of the per-operation step limits (``guard.policy``): tests that are not about the step limits use them so that a plan may
#: move a value by more than the shipped one-step defaults (1 ratio bin / 20 mV / 25 mV); tests about the steps pass ``**DEFAULT_STEPS``.
RELAXED_STEPS = dict(max_ratio_step=8, max_v_step_mv=200, max_offset_step_mv=200)
DEFAULT_STEPS = dict(max_ratio_step=1, max_v_step_mv=20, max_offset_step_mv=25)


def policy_with(*, max_ops: int = 400, dwell: float = 60.0, boot_ops: int = 1000, **changes: object) -> Policy:
    """The default M1 policy with higher session / per-boot limits (the defaults of 1 / 12 cannot run any plan: a plan is
    ``2 + 7 per CPU`` operations since T-021, plus one read-back per write since T-022) and **relaxed step limits** (``RELAXED_STEPS``);
    every other D-016 bound stays at the shipped no-raise default. ``changes`` replace any ``Policy`` field."""
    base = dataclasses.replace(
        default_m1_policy(), max_operations_per_session=max_ops, min_dwell_s=dwell, max_operations_per_boot=boot_ops, **RELAXED_STEPS
    )
    return dataclasses.replace(base, **changes) if changes else base


def sim_config(**kwargs: object) -> SimConfig:
    """``SimConfig`` with the unmeasured rule switched on (``reject_zero_fields``): every planner/executor test runs with it."""
    kwargs.setdefault("reject_zero_fields", True)
    return SimConfig(**kwargs)  # type: ignore[arg-type]


class FakeClock:
    """``sleep`` advances the clock by exactly the requested time; nothing ever really sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if len(self.sleeps) > 200_000:  # a broken timeout must fail the test, not hang the suite
            raise AssertionError("FakeClock: a polling loop never terminated")


class HookPort:
    """``MsrPort`` wrapper around a ``SimMsrPort`` that runs a hook just before a 0x150 write of a given command reaches the
    simulator (fault injection at one precise step of a run)."""

    def __init__(self, sim: SimMsrPort, hooks: Mapping[int, Callable[[int], None]]) -> None:
        self.sim = sim
        self.hooks = dict(hooks)

    @property
    def writable(self) -> bool:
        return self.sim.writable

    def rdmsr(self, cpu: int, msr: int) -> int:
        return self.sim.rdmsr(cpu, msr)

    def wrmsr(self, cpu: int, msr: int, value: int) -> None:
        if msr == MSR_OC_MAILBOX:
            hook = self.hooks.get((value >> 32) & 0xFF)
            if hook is not None:
                hook(cpu)
        self.sim.wrmsr(cpu, msr, value)


class CountingTransport(MailboxTransport):
    """Counts every ``transact`` so a test can prove each one was preceded by a guard audit record."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.transacts = 0

    def transact(self, cpu: int, cmd: int, p1: int, p2: int, data: int = 0):  # type: ignore[no-untyped-def]
        self.transacts += 1
        return super().transact(cpu, cmd, p1, p2, data)


@dataclass
class Session:
    """A simulator wired to the real guard, transport, audit log and lock file (all in a temp directory)."""

    sim: SimMsrPort
    port: object
    guard: WriteGuard
    transport: CountingTransport
    policy: Policy
    fake: FakeClock
    confirm: Confirmation
    audit_path: Path
    lock_path: Path
    topology: Topology

    def apply(self, prof: Profile, **kwargs: object) -> ApplyReport:
        params: dict[str, object] = {
            "guard": self.guard,
            "topology": self.topology,
            "confirm": self.confirm,
            "policy": self.policy,
            "sleep": self.fake.sleep,
            "clock": self.fake.clock,
        }
        params.update(kwargs)
        return apply_profile(prof, **params)  # type: ignore[arg-type]

    def requests(self) -> list[tuple[int, int, int]]:
        """Every request the simulator latched as (cpu, cmd, data) - read-backs included."""
        return [(r.cpu, r.cmd, r.data) for r in self.sim.requests]

    def write_requests(self) -> list[tuple[int, int, int]]:
        return [r for r in self.requests() if r[1] in WRITE_CMDS]

    def audit(self) -> list[dict]:
        return read_all(self.audit_path)


def make_session(
    tmp: Path,
    *,
    config: SimConfig | None = None,
    policy: Policy | None = None,
    env: Mapping[str, str] | None = None,
    health_gate: Callable[[str], tuple[bool, str]] | None = None,
    wrap_port: Callable[[SimMsrPort], object] | None = None,
    guard_token: str = TOKEN,
    confirm_token: str = TOKEN,
    reject_zero_fields: bool = True,
) -> Session:
    cfg = dataclasses.replace(config if config is not None else SimConfig(), reject_zero_fields=reject_zero_fields)
    sim = SimMsrPort(cfg)
    port = sim if wrap_port is None else wrap_port(sim)
    fake = FakeClock()
    pol = policy if policy is not None else policy_with()
    audit_path = tmp / "audit.jsonl"
    lock_path = tmp / "mailbox.lock"
    transport = CountingTransport(port, sleep=fake.sleep, clock=fake.clock)
    guard = WriteGuard(
        transport,
        pol,
        AuditLog(audit_path, fsync=False),
        session_token=guard_token,
        boot_id_fn=lambda: "test-boot",  # T-016: never read the real /proc in a test
        env={ENV_FLAG: "1"} if env is None else env,
        health_gate=health_gate,
        lock_path=lock_path,
        clock=fake.clock,
        msr_port=port,  # T-023: the plain-MSR path (turbo tables); the same port as the mailbox transport
    )
    return Session(sim, port, guard, transport, pol, fake, Confirmation(confirm_token, OPERATOR), audit_path, lock_path, arl_s_285k())
