# G-128 — backend: live topology, points, limits; no reference-machine lore (D-036)

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-128-backend-live-topology` · **Depends on:** none (parallel with G-127)

## Goal
The helper/CLI must work on any Arrow Lake-S part (285K 8P+16E, 265K 8P+12E, 245K 6P+8E, …) and derive every CPU-specific value live. The
orchestrator's audit (2026-10-12) lists the offenders; fix them in `src/` (simulator keeps reference data as a **labelled fixture**).

1. **Topology from sysfs** (new `src/arltuner/topology/live.py`, used everywhere `UNITS`/`p_count`/`e_first_cpu`/`range(24)`/`range(8)`/`8+4n` are
   hard-coded: `topology/cpumap.py`, `helper/ops_tune.py` UNITS, `helper/ops_measure.py`, `curves/record.py`, `profiles/contents.py`, `profiles/schema.py`,
   `web/server.py` `_UNITS`, `msr/winport.py` E_CLUSTERS, `guard/policy.py` cpus defaults): P cpus = `/sys/devices/cpu_core/cpus`, E cpus =
   `/sys/devices/cpu_atom/cpus`, E clusters = distinct `topology/cluster_cpus_list` of E cpus (unit ids `P<n>` by order, `EC<n>` by order),
   `cpufreq/cpuinfo_max_freq` → `max_ratio` (÷100000), `acpi_cppc/highest_perf` → `perf`. **Favored** = P units with the highest `perf` (fallback
   `max_ratio`), none if all equal; `favored_source`. Root injectable (fake sysfs dir) for tests. Windows (`winport`) keeps its LP map but derives
   clusters from the same unit list it is given.
2. **`preflight` result gains `topology`** — exact contract (G-127 consumes it):
   `topology: {units: [{id, kind: "P"|"E", cpus: [int], favored: bool, max_ratio: int|null, perf: int|null}], favored_source: "acpi_cppc"|"cpuinfo_max_freq"|null}`.
   `tune.read` and `tune.last` results gain `read_at` (ISO-8601 UTC of the read; for `tune.last` the original read time). Cross-check: after a
   `tune.read`, if `turbo.favored` (0x1C) disagrees with the sysfs favored set, add a `problems`/note entry (no failure).
3. **Platform identity = family, not one die.** `platforms/arl-s-285k.toml` → `platforms/arl-s.toml` (family 6, model 0xC6 = 198; steppings as a
   list, unknown stepping ⇒ preflight WARN not FAIL), no topology, no `favored_p`, no `fused_point` fingerprint, no `vf_points`, no `turbo_defaults`.
   Identity gate = CPUID family/model + live topology consistent (P/E cpu sets non-empty, clusters of equal size). Refusal texts say "Arrow Lake-S",
   never "285K map". `_equivalent_oc_words` (ops.py) uses the live `0x09` fused word of the CPU from this session's read, not the platform file.
   Keep the `[sensors]` Vcore mapping but key it by DMI `board_name` (`/sys/class/dmi/id/board_name`) in a small board table; unknown board ⇒
   Vcore "unavailable", never a guess.
4. **V/F point ratios live:** remove `vf_points`/`QUICK_RATIOS`/`POINT_COUNT`/`_POINT_RATIOS` constants from runtime paths. Points = what `tune.read`
   enumerates (cmd 0x10 points until the CPU answers "no such point"; check the existing code path and registers.md for how the count is found today).
   Quick measure uses the point ratios from the helper's kept read of this boot when present; otherwise it measures every 5th ratio from 8 to the
   unit max plus the max (and says so in the result: `plan: "kept read" | "generic"`). `_PLATFORM_TURBO`, `_P_FAVORED_STOCK=55`, `_COLD_OC_WORD`
   (VF_DOMAIN0) and the `STOCK_VID`/`P_REACH*`/`E_QUIRK` constants: out of runtime paths — the real path uses the live 0x1AD/0x650/0x1C reads it already
   does; reach = live (`turbo` tables, cpuinfo max). `msr/measured_baseline.py` may only be imported by `msr/sim.py` and tests (assert this in a test).
5. **Limits live (D-032 semantics kept, values derived):** `guard/policy.py` — V target max = the CPU's `0x28` max-voltage read minus one LSB when read,
   else the policy file value; ratio max per kind = live hardware max (P: max `max_ratio` of P units / 0x1C[15:8]; E: max `max_ratio` of E units);
   `top_fused_ratio` from the live fused read; drop the assumed BIOS offsets (−50/0, `[USER]`) — the effective offset is what the CPU reports.
   Policy toml values `"auto"` = derived live; keep numeric values working. Update `policies/advanced-2026-10-11.toml` → use `"auto"` for ratio and
   V maxima (offsets ±200 stay), strip verbatim operator quotes into a one-line reference to D-032.
6. **Lore out of user-facing text:** `vfmodel.py`/`ops_tune.py` E 40× (`E_NO_OFFSET_RATIO`, `E40_OFFSET`) and WHEA 1.2 V (`E_TARGET_WHEA`) stay as warning
   **codes** but their text becomes "community report: …" without "this machine"/dates; VccIA note: remove `RING_V`/margin 0.105/ring 40× numbers
   (report only "highest requested VID among units", plus live ring ratio if read); strip `[MEASURED … date]` tags from every user-facing string.
7. Simulator: `SimConfig.measured()` and `--sim` stay the 285K reference fixture, labelled "simulated reference CPU" in results (`simulator: true`
   already); add a `SimConfig` topology option for 6P+8E to prove generality.

## Read first
`docs/DECISIONS.md` D-032, D-036 · `docs/knowledge/registers.md` (0x09, 0x10, 0x1C, 0x28, 0x1AD, 0x650) · `platforms/arl-s-285k.toml` ·
`src/arltuner/platform/` · `src/arltuner/topology/cpumap.py` · `src/arltuner/helper/ops.py` (preflight, accepted equivalence) ·
`src/arltuner/helper/ops_tune.py` · `src/arltuner/helper/ops_measure.py` · `src/arltuner/guard/policy.py` · `src/arltuner/msr/sim.py`.

## Owns
`src/arltuner/**` (everything under src), `platforms/*`, `policies/advanced-2026-10-11.toml`, `tests/**` **except** `tests/web/*` (G-127 owns those;
if a `tests/web` test breaks from your contract change, report it — do not edit). Not `web/*`, not `profiles/*`, not `install.sh` (G-129).

## Acceptance
Suite green. New tests: fake sysfs 8P+16E (favored {0,3} via CPPC), 8P+12E, 6P+8E, all-equal (no favored), CPPC missing (cpuinfo fallback);
preflight `topology` shape; `read_at` present; unknown stepping = WARN; sim 6P+8E tune.read/plan/apply works; `measured_baseline` import confined;
`grep -rn "285K\|this machine" src/` only in sim/test-fixture docstrings. CLI commands keep working on the sim.

## Non-goals
`web/*` (G-127); repo profiles/install/captures (G-129); any hardware access; changing mailbox encodings.

## Report-back (≤ 300 words)
What changed · contract fields as implemented · anything left hard-coded and why.
