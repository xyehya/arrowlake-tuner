# G-104 — telemetry readers
- **Status:** ready · **Host:** lnx · **Tier:** T0 build (fake sysfs / fake MSR port); first real read T1 by the orchestrator · **Depends on:** none (runs parallel to G-101: import topology from `src/arltuner/topology/`, never from `src/arltuner/platform/`)

## Goal
Readers for SPEC §8 values with one documented source each; `docs/knowledge/telemetry-sources.md` (value, source, privilege, package, platform-
specific?, evidence tag); **proposed** read-kind allowlist rows (register, bits, kind, evidence tag) for every telemetry MSR, written in `telemetry-sources.md` and the report —
the orchestrator applies them to `docs/knowledge/registers.md` and the code allowlist (`guard/policy.py` `PLAIN_MSR_READABLE`) at merge; the worker edits neither.

## Read first
`docs/SPEC.md` §8 · `docs/knowledge/linux-path.md` (telemetry section) · `docs/knowledge/registers.md` · `src/arltuner/msr/port.py`.

## Owns
`src/arltuner/telemetry/`, `tests/telemetry/`, `docs/knowledge/telemetry-sources.md`.

## Acceptance
Each reader tested on fake files / fake MSR values incl. missing source → "unavailable" (never a guessed number); sampling thread never touches the
mailbox; perf IMC via one long-lived `perf stat -I` with a parser tested on recorded output.
