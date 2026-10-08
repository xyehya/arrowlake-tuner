"""The helper state machine (SPEC section 4.6): ``idle | planning | applying | capped | stressing``. One mailbox operation at a time.

G1 only enters ``idle`` and ``planning`` (``vf.dump`` is a read session that gathers hardware state, so it runs as ``planning``: SPEC names no
other state for it). ``applying`` / ``capped`` / ``stressing`` belong to G2/G3. Read-only helper ops (``preflight``, ``audit.tail``,
``telemetry.*``, ``cancel``) are allowed in every state; they never change it.
"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass

__all__ = ["Busy", "Running", "State", "StateMachine"]


class State(enum.Enum):
    IDLE = "idle"
    PLANNING = "planning"
    APPLYING = "applying"
    CAPPED = "capped"
    STRESSING = "stressing"


@dataclass(frozen=True)
class Running:
    op_id: str
    op: str
    state: State


class Busy(Exception):
    def __init__(self, running: Running) -> None:
        super().__init__(f"helper is {running.state.value} ({running.op} {running.op_id})")
        self.running = running


class StateMachine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: Running | None = None

    @property
    def state(self) -> State:
        with self._lock:
            return self._running.state if self._running else State.IDLE

    @property
    def running(self) -> Running | None:
        with self._lock:
            return self._running

    def begin(self, op_id: str, op: str, state: State) -> None:
        """Enter ``state`` for a long op; ``Busy`` if anything already runs."""
        if state is State.IDLE:
            raise ValueError("a long op cannot run in 'idle'")
        with self._lock:
            if self._running is not None:
                raise Busy(self._running)
            self._running = Running(op_id, op, state)

    def end(self, op_id: str) -> None:
        with self._lock:
            if self._running is not None and self._running.op_id == op_id:
                self._running = None
