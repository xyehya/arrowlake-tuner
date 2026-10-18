"""G-216: core parking -- taking a CPU offline / online through ``cpu<N>/online``. Pure apart from the files it is given.

Facts: ``cpu0`` has **no** ``online`` file (it cannot be parked) ``[MEASURED lnx 2026-10-18]``. Arrow Lake-S has no SMT: one Linux CPU per core. Rules, each a named refusal:
CPU0 is never parked, the last online CPU of a kind (P or E) is never parked, parking needs ``[runtime] parking = true`` (an absent key refuses naming itself; **unparking is always
allowed** -- it is how a parked CPU comes back and what a revert does), nothing is parked while a stress run or a cpufreq journal is held.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from arltuner.guard.policy import Policy
from arltuner.topology import Topology

from .caps import Refused, RuntimeFiles, RuntimeStep, require_runtime_key

__all__ = ["online_file", "plan", "plan_revert", "read_state"]


def online_file(cpu: int) -> str:
    return f"cpu{cpu}/online"


def read_state(files: RuntimeFiles, cpus: Sequence[int]) -> dict[int, bool | None]:
    """``{cpu: online}``; ``None`` where the CPU has no ``online`` file (cpu0: always online, never parked). ``SysfsError`` when a file holds anything but 0 / 1."""
    out: dict[int, bool | None] = {}
    for cpu in cpus:
        value = files.read_int(online_file(cpu))
        if value not in (None, 0, 1):
            raise Refused("PARK_STATE", f"cpu{cpu}/online holds {value}, not 0 or 1: nothing is assumed", f"cpu{cpu}")
        out[cpu] = None if value is None else bool(value)
    return out


def _bool(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise Refused("PARK_VALUE", f"parking.{key} must be true (park) or false (unpark)", key)
    return value


def plan(intent: Mapping[int, bool], policy: Policy, online: Mapping[int, bool | None], topology: Topology, *, held: str | None = None) -> list[RuntimeStep]:
    """The write steps for ``intent`` (``{cpu: parked}``; ``True`` takes the CPU offline) over the ``online`` state, or :class:`Refused`. Steps that change nothing are dropped; unparks come
    before parks (a safe order for the last-of-kind rule at every single write)."""
    if not intent:
        raise Refused("NOTHING_TO_DO", "the parking intent changes nothing (set parking {cpu: true|false})")
    # a parked CPU may have left the live topology (``cpu_core/cpus`` / ``cpu_atom/cpus`` list online CPUs): it stays known by its ``online`` file, so it can always be unparked
    in_topology = {c.cpu for c in topology.cores}
    known = in_topology | {c for c, state in online.items() if state is not None}
    parks: list[int] = []
    unparks: list[int] = []
    for cpu in sorted(intent):
        want_parked = _bool(f"cpu{cpu}", intent[cpu])
        if cpu not in known or (want_parked and cpu not in in_topology):
            raise Refused("PARK_UNKNOWN_CPU", f"parking names cpu {cpu}, which this system does not have online (CPUs: {min(known)}..{max(known)})", f"cpu{cpu}")
        state = online.get(cpu)
        if want_parked and (cpu == 0 or state is None):
            raise Refused("PARK_CPU0", f"cpu {cpu} cannot be parked: " + ("cpu 0 is the boot CPU and has no online file" if cpu == 0 else "it has no online file"), f"cpu{cpu}")
        if state is None:
            continue  # asked online, has no switch: it is online
        if want_parked and state:
            if not policy.cpu_allowed(cpu):
                raise Refused("PARK_CPU_NOT_ALLOWED", f"cpu {cpu} is not in the policy's cpus ({policy.where}): it may not be parked", f"cpu{cpu}")
            parks.append(cpu)
        elif not want_parked and not state:
            unparks.append(cpu)
    if parks:
        if held is not None:
            raise Refused("PARK_CONFLICT", f"{held}: no CPU may be parked meanwhile")
        allowed = require_runtime_key(policy, "parking")
        if allowed is not True:
            raise Refused("RUNTIME_POLICY_DENIED", f"the policy has [runtime] parking = false ({policy.where}): no CPU may be parked. Set it to true to allow it", "parking")
    final = {c: online.get(c) is not False for c in in_topology}  # a CPU without an online file is online
    final.update({c: True for c in unparks if c in in_topology})
    final.update({c: False for c in parks})
    for kind in ("P", "E"):
        cpus = topology.cpus_of_kind(kind)
        parked_here = [c for c in parks if c in cpus]
        if parked_here and not any(final[c] for c in cpus):
            raise Refused("PARK_LAST_OF_KIND", f"parking {', '.join(f'cpu {c}' for c in parked_here)} would leave no {kind} CPU online: the last online CPU of a kind is never parked", f"cpu{parked_here[0]}")
    steps: list[RuntimeStep] = []
    for cpu in unparks:
        steps.append(RuntimeStep(len(steps) + 1, "parking", f"cpu{cpu}", online_file(cpu), 0, 1, (1, 1)))
    for cpu in parks:
        steps.append(RuntimeStep(len(steps) + 1, "parking", f"cpu{cpu}", online_file(cpu), 1, 0, (0, 0)))
    return steps


def plan_revert(values: Mapping[int, int], online: Mapping[int, bool | None], topology: Topology) -> tuple[list[RuntimeStep], list[str]]:
    """The writes that put the journaled online states back (``values`` = ``{cpu: 0|1}``), unparks first: ``(steps, notes)``. The policy does not apply (a recorded state is restored), but
    a park that would leave no CPU of its kind online (something else emptied the kind meanwhile) is dropped with a note: a revert restores what it safely can."""
    steps: list[RuntimeStep] = []
    notes: list[str] = []
    final = {c.cpu: online.get(c.cpu) is not False for c in topology.cores}
    for cpu in sorted(values):
        if values[cpu] == 1 and online.get(cpu) is False and cpu in final:
            final[cpu] = True
    for want in (1, 0):
        for cpu in sorted(values):
            state = online.get(cpu)
            if values[cpu] != want or state is None or int(state) == want:
                continue
            if want == 0 and cpu in final:
                kind = topology.by_cpu(cpu).kind
                if not any(final[c] for c in topology.cpus_of_kind(kind) if c != cpu):
                    notes.append(f"cpu {cpu} was not parked again: it is the last online {kind} CPU")
                    continue
                final[cpu] = False
            steps.append(RuntimeStep(len(steps) + 1, "parking", f"cpu{cpu}", online_file(cpu), int(state), want, (want, want)))
    return steps, notes
