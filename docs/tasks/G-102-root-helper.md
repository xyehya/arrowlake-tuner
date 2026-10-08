# G-102 — root helper (protocol, state machine, read-only ops)
- **Status:** ready · **Host:** lnx · **Tier:** T0 build (simulator); first real run T1 by the orchestrator · **Depends on:** G-101

## Goal
`arltuner-helper`: a process speaking JSON lines on stdin/stdout (SPEC §4.5) with the state machine and lock of §4.6; G1 implements only read-only
ops: `preflight`, `vf.dump` (through the existing guard, simulator in tests), `audit.tail`, `telemetry.start/stop` (stub until G-104), `cancel`.

## Read first
`docs/SPEC.md` §4.1, §4.4–4.6 · `docs/ARCHITECTURE.md` contracts 4–5 · `src/arltuner/cli.py` (`confirm_fn`, lock, audit defaults) · `src/arltuner/vfdump.py` · ARCHITECTURE **contract 8 + addendum (D-027)** · `src/arltuner/platform/preflight.py`
(`preflight()` signature, statuses incl. `UNKNOWN`/`MISSING`).

## Owns
`src/arltuner/helper/`, `tests/helper/`, `tools/arltuner-helper.py`.

## Acceptance
Protocol tests: framing, 64 KiB cap, unknown op, heartbeat, EOF/SIGTERM handling, conflicting requests refused by state, lock contention with a
second instance; trust-path checks (refuse a user-writable policy/audit path) with fake stat results; the helper re-runs `preflight` itself (as root on hardware, fake root in tests) and any future write op is refused unless
the mode is `write` with no `UNKNOWN` check (D-027 c) — test the refusal path with a stub write op. Simulator only.

## Non-goals
apply/restore (G2), freq caps and stress (G3), pkexec/polkit install (G-106).
