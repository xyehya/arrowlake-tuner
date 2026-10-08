# G-104 — telemetry readers
- **Status:** ready · **Host:** lnx · **Tier:** T0 build (fake sysfs / fake MSR port); first real read T1 by the orchestrator · **Depends on:** G-101

## Goal
Readers for SPEC §8 values with one documented source each; `docs/knowledge/telemetry-sources.md` (value, source, privilege, package, platform-
specific?, evidence tag); read-kind allowlist rows for every telemetry MSR in `docs/knowledge/registers.md`.

## Read first
`docs/SPEC.md` §8 · `docs/knowledge/linux-path.md` (telemetry section) · `docs/knowledge/registers.md` · `src/arltuner/msr/port.py`.

## Owns
`src/arltuner/telemetry/`, `tests/telemetry/`, `docs/knowledge/telemetry-sources.md`.

## Acceptance
Each reader tested on fake files / fake MSR values incl. missing source → "unavailable" (never a guessed number); sampling thread never touches the
mailbox; perf IMC via one long-lived `perf stat -I` with a parser tested on recorded output. Workers never read `~/AI/turbostat*`.
