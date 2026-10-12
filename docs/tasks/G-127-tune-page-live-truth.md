# G-127 — Tune page: live truth, favored ★, plain names (D-036)

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-127-tune-page-live-truth` · **Depends on:** none (parallel with G-128)

## Context (orchestrator, verified on HW 2026-10-12)
A fresh `tune.read` matches what the page shows: the **offsets, OC ratio/target and fused top values are live and correct** — do NOT change how they
are read or "fix" them to match anything. The operator found the page confusing and partly built on stored/prior knowledge. Their snippets:
- E-cluster 0: `now −35.2 mV to 0 mV · target n/a · ratio n/a · fused top 46× 1.1563 V`, points `0 mV · now → 0.775 V`, point 3 `no measured voltage`,
  OC cell `47–48× run on point 6 (turbo table); an E OC ratio adds an OC point`; EC2 OC cell `ratio 46× (BIOS)`.
- P-core 1 (non-favored, tops at 55×): point 7 (56×) `no measured voltage · not reachable on this core`, point 8 (57×) `→ 1.305 V (fused) · not
  reachable on this core` — inconsistent.

## Goal
1. **Names:** button "Read tuning words"/"Re-read tuning words" → **"Read ratios & voltages"** / **"Re-read ratios & voltages"**; replace "tuning
   words" in every user-visible string (status lines, errors, NO_READ, tooltips, Profiles/Curves hints, index.html) with plain words ("ratios and
   voltages", "the read"). Internal names/op names unchanged.
2. **Read provenance:** beside the Tune data show when the shown read was taken and how: "Read from the CPU at 09:49 (3 min ago)" vs "Showing the
   helper's earlier read from 03:40 (no mailbox operations used)"; update it after apply read-back. Use the timestamp the helper returns (add one to the
   fake if missing; G-128 adds `read_at` (ISO UTC) to `tune.read`/`tune.last` results — tolerate its absence: show "time unknown").
3. **Favored ★ live:** delete `const FAVORED = ["P0","P3"]`. Source order: `preflight` result `topology.units[].favored` (G-128 contract below) →
   else `tune.read` `turbo.favored` (P-cores whose ratio equals the max, only if not all equal) → else no favored at all. Show **★** after the unit
   name everywhere a P-core is labelled (Tune rows/cards, Curves toggles, Stress unit buttons, dashboard core map tooltips, Simple "Favored" preset —
   preset hidden when none; its title lists the detected cores, not "P0 and P3"). Title text: "favored core (highest CPPC performance)" / "(0x1C
   favored ratio)" per source.
4. **Points above a core's reach:** for any point whose ratio > that unit's reach (`reachOf`), show only the reach caption ("not reachable on this core,
   tops at 55×") and **no** predicted voltage — the same for 56× and 57×. Fused stand-in only for a reachable top point. Point 3 / any point without a
   measurement: "no measured voltage" with the reason when the baseline gives one.
5. **Predictions are labelled user data:** every "→ V" comes from the baseline curve: say so once per Tune card header ("predicted on baseline
   '<name>' measured <date>"), and when there is no baseline show nothing per point plus one hint "Measure a stock curve (Curves) to see predicted
   voltages". Never present a prediction as a CPU read.
6. **No guessed provenance / lore:** "(BIOS)" → "= fused top (no OC point)". OC cell E note: derive from the live E turbo table ("E turbo table: 48×
   all-core; ratios above point 6 (46×) run on point 6's voltage") instead of the fixed "47–48×"; omit when no turbo table was read. The E 40× text
   ("gave OCCT errors on this machine") and WHEA ~1.2 V text: keep showing the warning only when the helper sends the code, reword UI text to a
   neutral **community hint** ("Reported in community testing: …") — no "this machine". VccIA panel: drop the fixed `VCCIA {ring:1.20, margin:0.105}` and
   "40×" ring; show "highest predicted VID among units" only, and the live board Vcore from telemetry when available. Op cost: replace
   `COST_LOW/COST_HIGH` and index.html "≈124–136" with the count computed from units × points of the last read (or "about one per point" before any read).
7. **Fake/tests:** `tests/web/fake_v3.py` returns `topology` in `preflight`, `read_at` in tune.read/last; make FAVORED in the fake configurable and add a
   test with favored = {P2} and with none. Browser/static tests for: button label, provenance line, ★ on detected cores only, 56/57× on a non-favored
   unit both show reach only, no "(BIOS)", no "this machine" strings in web/.

## Contract with G-128 (`preflight` result gains)
`topology: {units: [{id: "P0", kind: "P", cpus: [0], favored: bool, max_ratio: int|null, perf: int|null}, … {id: "EC0", kind: "E", cpus: [8,9,10,11],
favored: false, max_ratio: int|null, perf: null}], favored_source: "acpi_cppc"|"cpuinfo_max_freq"|null}` and `tune.read`/`tune.last`: `read_at`.
Keep `UNIT_IDS` as is in this task (G-128 owns topology; dynamic unit lists are a later follow-up) but read favored only from the contract.

## Read first
`web/app.js` (Tune section ~2290–3100, FAVORED users: grep `FAVORED`), `web/index.html` (Tune), `tests/web/fake_v3.py`, `tests/web/test_cpu_facts_page.py`.

## Owns
`web/*`, `tests/web/*`. Nothing under `src/`.

## Acceptance
`python3 tools/run_tests.py` green; `grep -n 'tuning words\|this machine\|(BIOS)\|"P0", "P3"' web/` returns nothing user-visible; the new tests above.

## Non-goals
Backend changes; dynamic unit counts; any hardware access.
