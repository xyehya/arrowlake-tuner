# arrowlake-tuner

**Per-core undervolt, V/F curve, ratio and turbo-table tuning for Intel Core Ultra 200S (Arrow Lake-S) on Linux** — an open, safety-first
alternative to Intel XTU for Linux, with a local web GUI, live telemetry and built-in stability screens. Windows gets a matching PowerShell restore.

Keywords: Arrow Lake undervolt Linux · Core Ultra 9 285K · V/F curve · per-core voltage offset · OC mailbox (MSR 0x150) · turbo ratio limits ·
XTU alternative · stress test · stressapptest · stress-ng.

## Status (2026-10-12)
- **GUI (local web app + root helper) working on the reference machine:** live dashboard with memory timings, V/F curve **measurement** and
  storage, **tuning** (per-point / whole-curve offsets, OC point + adaptive voltage, OC ratios, turbo tables) with plan → review → apply →
  read-back, stress/validation screens, profiles. Verified on hardware 2026-10-11/12 (`docs/knowledge/tuning-results.md`). Start with
  **`docs/USER-GUIDE.md`**.
- **CLI + restore scripts** (Linux and Windows) unchanged. Offline suite: `python3 tools/run_tests.py` (≈ 2250 tests, simulator only).
- **Accepted tuning on the reference machine:** `profiles/CURRENT` (p-step7 + E split3) — passed OCCT Linpack 2021 60 min + SSE 60 min on both OSes.

Platform today: Core Ultra 9 285K + ASRock Z890 Steel Legend WiFi (`docs/HARDWARE.md`). Other CPUs: read-only until described in a platform file.

## Safety
Writing CPU voltages can corrupt data or crash the machine. Every write goes through a guard (allowlist, operator policy limits — D-032), one mailbox
transaction at a time, a plan-bound review/apply, read-back and an audit log; a cold boot reverts everything. Rules: `docs/SAFETY.md`.

## Quick start (no hardware)
```
python3 tools/run_tests.py                       # offline suite (simulator only)
python3 tools/arltuner.py topology               # 24-core map
python3 tools/arltuner.py plan profiles/p-step7.toml --sim --policy policies/session-2026-10-02-points.toml
```

## Layout
| Path | Purpose |
|---|---|
| `AGENTS.md`, `CLAUDE.md`, `docs/ORCHESTRATION.md` | rules for agent sessions; how the orchestrator runs the project; `docs/START-PROMPT.md` = how to resume |
| `docs/STATE.md` · `docs/SPEC.md` · `docs/ROADMAP.md` · `docs/tasks/` | live state · product spec · phases · tasks |
| `docs/SAFETY.md` · `docs/DECISIONS.md` · `docs/ARCHITECTURE.md` | write protocol · decision log · module contracts |
| `docs/knowledge/` | distilled facts: registers (single source of truth), platform, tuning results, Linux/Windows paths |
| `docs/PROVENANCE.md` | where the evidence lives (predecessor repo `linuxmon`) |
| `src/arltuner/`, `tests/`, `tools/` | library + CLI, tests, Linux/Windows scripts |
| `profiles/`, `policies/` | accepted tuning state and its policy |
