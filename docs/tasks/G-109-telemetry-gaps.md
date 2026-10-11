# G-109 — telemetry gaps found in the first real GUI run (Vcore, DRAM bandwidth, temp names, mode header)

- **Status:** ready
- **Host:** lnx
- **Safety tier:** T0 (simulator / fake sysfs only; the orchestrator verifies on the machine)
- **Branch/worktree:** `task/G-109-telemetry-gaps`
- **Depends on:** G-104, G-105, G-107 (all merged)

## Goal
The first installed GUI run (2026-10-11, STATE) showed four gaps. Close them so the read-only dashboard shows every value it has a source for.
1. **Vcore n/a.** This board binds hwmon name `nct6686` (driver `nct6683`); its labels are generic: Vcore is `in0`, label **`VIN16`**. There is no
   `Vcore` label, and the code's candidate list lacks `nct6686`. Make the chip name + label come from the platform file
   (`[sensors] vcore_hwmon_name = "nct6686"`, new key `vcore_hwmon_label = "VIN16"`) and pass them from the platform to `read_vcore`. Absent chip
   or label ⇒ `unavailable` + reason (unchanged rule). **Never** fall back to another input; `VIN0`/`in1` is explicitly not Vcore (comment it).
2. **DRAM read/write n/a ("IMC sampling not enabled").** The device helper builds `Sampler(...)` with no `ImcStat`. Wire one in for the device
   (not `--sim`) sampler: started with the sampler, stopped with it (and on helper exit; no orphan `perf`). As root: absolute `/usr/bin/perf`,
   minimal fixed env (no inherited `PATH`/`LD_*`/`PERF_*`/`HOME`), `stdin` DEVNULL, `cwd="/"`; perf missing / not root-owned / group-or-world-
   writable ⇒ `unavailable` with a reason, telemetry continues. Event spelling `uncore_imc_free_running/data_read/` + `/data_write/` is now
   [MEASURED lnx 2026-10-11] (root perf, merged over `_0`/`_1`; sample CSV in `docs/knowledge/telemetry-sources.md`): drop the [UNVERIFIED] tag.
3. **Temperature names.** coretemp labels are `Core N` with N = sysfs `topology/core_id` [MEASURED lnx 2026-10-11]: CPU→core_id
   0:0 1:4 2:16 3:20 4:24 5:28 6:40 7:44 8..15:8..15 16..23:32..39. Show them as the dashboard's core names (`P-Core k` / `E-Core k`, as in the
   per-CPU table) by mapping through the topology the helper already has; unmapped labels stay raw. Do it where the readings are labelled
   (helper/sampler side or the page, your call; keep the JSON contract additive).
4. **Mode header.** The page says "Mode: write · Writes allowed: yes" on a read-only dashboard. Keep the preflight result but word it so it can't
   be read as "this page writes", e.g. "Preflight mode: write (this build has no write controls)".

## Read first (only these)
- `docs/knowledge/telemetry-sources.md` — Vcore / DRAM / temps rows (2026-10-11 facts)
- `src/arltuner/telemetry/` (hwmon.py, imc.py, sampler.py) · `src/arltuner/platform/schema.py` + `platforms/arl-s-285k.toml` [sensors]
- `src/arltuner/helper/main.py` (`sampler_factory`, `PortOwningSampler`) · `web/app.js`, `web/index.html`
- `docs/ARCHITECTURE.md` contract 8 + addenda (only the telemetry / heartbeat parts)

## Owns
`src/arltuner/telemetry/`, `src/arltuner/platform/schema.py`, `platforms/arl-s-285k.toml` ([sensors] only), `src/arltuner/helper/main.py`
(sampler factory only), `tests/telemetry/`, `tests/platform/`, `tests/helper/`. **Not `web/`** (2026-10-11: G-110 owns it; gap 3 = additive
`name`/`cpu` fields per temps entry on the helper side; gap 4 moved to G-110).

## Acceptance
- Fake-sysfs tests: `nct6686` + `in0_label=VIN16` ⇒ Vcore V; no such chip / label ⇒ unavailable; `VIN0` present but not `VIN16` ⇒ unavailable.
- Platform loader: new key parsed, unknown keys still refused; existing platform tests green.
- Helper: device sampler owns an `ImcStat` with the hardened spawn (fake popen asserts argv[0] `/usr/bin/perf`, env, cwd); stop terminates it;
  trust failure ⇒ unavailable reading, no spawn. `--sim` unchanged.
- Temp-name mapping tested with the measured core_id table; page renders mapped names.
- `python3 tools/run_tests.py` green. No hardware access, no `sudo`, never read `~/AI/turbostat*`.

## Non-goals
Decoding 0x6B0 limit reasons; other nct6686 rails (VccSA etc.) as metrics; GPU; any mailbox op.

## Report-back (≤ 300 words)
What changed · files · test count · anything you could not do from the simulator.
