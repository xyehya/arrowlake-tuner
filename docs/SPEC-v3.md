# SPEC v3 — Measure → Store → Tune (2026-10-11; D-032 limits, D-033 flow)

Extends `docs/SPEC.md` (v2). Where they conflict, v3 wins. Persona: an **experienced overclocker installing this on Linux for the first time on a
285K**. The story: see the machine (Dashboard) → measure and keep its V/F curves (Curves) → tune from what was measured (Tune) → keep what works
(Profiles). Visual language: the G-110 dark design (core map, cards, single-hue scales, system fonts, CSP rules of G-110 unchanged).
Wire contract: `docs/ARCHITECTURE.md` **Contract 9**. Units everywhere: **12 tuning units** = P-cores P0–P7 (CPU 0–7) + E-clusters EC0–EC3
(CPUs 8–11, 12–15, 16–19, 20–23; one regulator per cluster of four [MEASURED]). The UI always says "12 curves (8 P-cores + 4 E-clusters)".

## 1. Tabs
`Dashboard · Curves · Tune · Profiles · System · Audit` (Overview/Cores/Sensors of G-110 merge into Dashboard as sections + a "Cores" sub-view).

## 2. Dashboard (what an overclocker wants first)
- Everything G-110 shows (core map, key tiles + sparklines, power/temp history, per-core table, sensors, budget ring) stays.
- **Refresh slider** in the header: 0.25 · 0.5 · 1 · 2 · 5 s (default 1 s; remembered per browser). Changing it calls `telemetry.start
  {interval_s}`, which **restarts the sampler at the new rate** (contract 9). History buffers keep wall-clock time, not sample counts.
- **Memory card:** `DDR5-7200 · Gear 2 · 2T` headline, primary timings `36-44-44-96`, tRFC, DIMM count/size/part, DIMM temps (spd5118);
  "All timings" opens a full table (primaries, tRC/tRFC/tRFCpb/tREFI/tFAW/tRRD_L/S/tCWL/tRTP/tWR/tWTP, the RDRD/RDWR/WRRD/WRWR SG/DG/SD/DD
  matrix, BCLK). Source `mem.read` (one call on load + a refresh button; it is static between boots). Unavailable ⇒ reason, never numbers.
- **Experienced-user controls:** pause/resume live updates; chart window (1/5/15 min, history kept in memory); core-map metric (Clock/Busy/
  Temp/VID — exists) + **colour scale lock** (auto vs fixed); per-core table column toggles and sort; "applied state" chip: last known tuning
  words this boot (from the last `tune.read`), "stock", or "unknown".

## 3. Curves — measure and keep V/F curves (no mailbox)
**What it measures:** for each unit and each ratio, the VID the core requests (MSR `0x198[47:32]/8192`) while capped at that ratio under load —
the real volts-vs-frequency curve *as currently tuned* (offsets included). Method = the existing T-026/T-027 `vf-dump --measure` engine
(`vfdump.measure_group`: cap search ratio→ratio+1, only samples at the target ratio kept, `MISMATCH` otherwise), run by the root helper.
- **Modes:** *Quick* — the unit's V/F point ratios (P 8, 20, 30, 48, 53, 55, 56, 57; E 8, 15, 23, 30, 40, 46) ≈ 2 min for all 12 units.
  *Dense* — every ratio from 8 to the unit's max turbo ratio ≈ 10 min. *Selection* — any subset of units (checkboxes on a mini core map).
- **How a run works:** journal + save each CPU's cpufreq `scaling_min_freq`/`scaling_max_freq`/governor/EPP → set EPP `balance_performance`
  (governor unchanged) → for each unit, for each ratio: cap the unit's CPUs, load **all CPUs of the unit** (pinned spinners), settle 0.3 s,
  sample 10 × 0.1 s, keep at-target samples → next. Ratios above what the unit can reach (e.g. 56/57× on non-favored P-cores) end as
  `unreachable` (not an error). **Restore from the journal** on completion, cancel, error, and on the next helper start if a stale journal exists.
  Progress over SSE (unit, ratio, step/of, ETA); Cancel at any time; partial results are kept.
- **Stored record** (root-owned `/var/lib/arltuner/curves/<id>.json`): id, name (editable), created, boot_id, mode, kernel, **tuning state in
  force** (`stock` / `words` from a `tune.read` this boot / `unknown`), temps min/max during the run, per unit: points `[{ratio, mhz, vid_v
  (mean), vid_min, vid_max, kept, total, status}]`. A "Stock baseline" tag can be set on one record.
- **Views:**
  1. *Chart* (default): x = frequency (MHz, ratio ticks), y = V. One line per unit; **legend toggles** which units are drawn; presets
     "All P-cores", "All E-clusters", "Favored (P0, P3)", "All". Hover = crosshair + tooltip with every drawn unit's V at that ratio.
     **Compare**: pick a second stored curve ⇒ dashed overlay + a Δ mV mode (y = mV difference). Spread band (min–max of the drawn units).
  2. *Table*: rows = units, columns = ratios, cells = V (3 dp) coloured on a single-hue scale; toggle "Δ vs baseline (mV)".
  3. *Library*: list of stored curves (name, date, mode, tuning state, units), rename, set baseline, delete (confirm), export JSON (download).

## 4. Tune — XTU-style controls, from what was measured
Top strip: **"Read ratios & voltages"** (the old `vf.dump`, renamed twice: "Read tuning words" until G-127; mailbox, ~112 ops, shows ratios + offsets + V target per point in mV, never hex
by default; hex toggle) and the current state chip. Two **scopes** (segmented control):
- **Simple:** "All P-cores" + "All E-clusters" — two rows.
- **Per unit:** 12 rows (P0–P7, EC0–EC3).
Per row, three controls (dropdowns, XTU-like, values snap to the hardware's 1/1024 V raw with the exact mV shown):
1. **Voltage offset** — mode switch *Whole curve* (one offset applied to every point) or *Per point* (expands to one dropdown per V/F point).
   Range **−200 … +200 mV**, step 5 mV (D-032).
2. **Adaptive target voltage (OC point only)** — the top/OC-ratio point only; the hardware applies the adaptive target there and offsets only on
   lower points [knowledge registers.md "Voltage semantics"]. Range 0.900 V … policy max (P 1.625 V, E 1.355 V), step 5 mV; "Fused/current"
   marked. Never offered on lower points; static/override mode is not offered (D-016).
3. **OC ratio** — current … policy max (P 57, E 49). The plan adds the turbo-table / favored-core writes the ratio needs (existing T-023 path).
- **Preview chart:** the selected stored curve (default: newest) with the *predicted* curve overlaid (measured VID + offset delta per point; the
  OC-point target drawn as a marker). Labelled "prediction"; after Apply the page offers **Measure again** (Quick, changed units only).
- **Apply flow:** `tune.plan` ⇒ modal: before/after table per unit/point (mV, V, ratio), ops needed, warnings (e.g. "positive offset",
  "target above fused"); **Apply** button sends `tune.apply {plan_id, typed: "CONFIRM " + nonce}` ⇒ progress ⇒ read-back table (match /
  mismatch per word). "Revert = cold boot or reapply a profile" shown in the modal. Gates of SPEC §5 appear as badges ("not yet validated on
  this machine"), never locks (D-033).
- **Reset to stock** (all offsets 0, ratios to the turbo defaults) and **Restore accepted** (the one profile `accepted/CURRENT` names; D-044) are buttons that
  go through the same plan → modal → apply flow. Restore writes only the words that differ and splits a large change into guarded rounds (or refuses `STEP_LIMIT` up front).

## 5. Profiles
List (the one installed accepted profile + saved), save current Tune settings as a profile (name `^[a-z0-9._-]{1,64}$`, never overwrite; D-044: a saved profile is complete = this boot's
read with the editor's rows, so a read comes first), diff profile↔profile / profile↔last read, apply (plan flow), mark as accepted ⇒ proposal per SPEC §6.4 (unchanged). Profiles saved
before D-044 are listed as "partial (legacy)".

## 6. Limits (D-032)
Policy, not code: offsets ±200 mV, V target ≤ 0x28 hardware max (P 1625 / E 1355 mV), ratio ≤ P 57 / E 49, per-write steps at the code maxima,
op caps as indicators (100000). Kept: one transaction at a time, guard allowlist, health gate, audit, plan nonce, read-back.

## 7. Safety in the app (unchanged unless stated)
The measurement writes only cpufreq sysfs (journaled, always restored). Every mailbox op goes through the guard, one at a time. Root helper code
only from the installed tree. The orchestrator asks for a go before the first mailbox write from the new UI on the reference machine.

## 8. Phase mapping
G-111 helper ops (measure, curves store, mem, telemetry restart) · G-112 policy D-032 + tune ops (read/plan/apply/reset/restore) · G-113 web
(all pages) · then one reinstall with `--reseed-config`, a Quick stock-baseline measurement on the machine, and the first-write go.

## 9. CPU facts the app must respect (operator 2026-10-11; evidence `docs/knowledge/platform-arl-s.md` §3–5, `tuning-results.md`)
| # | Fact | Where it binds |
|---|---|---|
| F1 | **Point tables are fixed and per domain:** P-cores (all 8) `P2 1..8` = 8, 20, 30, 48, 53, 55, 56, 57×; E-clusters `P2 1..6` = 8, 15, 23, 30, 40, 46×. Beyond ⇒ rc 0x11. Tables come from `tune.read`; the simulator models exactly these (never invented per-core tables) [MEASURED lnx 2026-10-02] | sim, measure, Tune grid |
| F2 | **OC point = `P2 = 0` word** (ratio + adaptive V target). A ratio above the top fused ratio adds an OC point; P0 reads `0x39` (BIOS favored 57×), others 0 at stock. E 48× today comes from `0x1C`/`0x650` (turbo), **not** an OC point; at 47–48× E voltage follows point 6 (46×) | Tune OC column, curve labels |
| F3 | **Reachability:** non-favored P-cores carry 56/57× points but top out at 55× (max turbo); favored P0/P3 reach 57× only with ≤ 2 active cores. Measure labels these "point exists, not reachable on this core", not errors | measure, Curves |
| F4 | **Monotonic clamp:** effective V(n) ≥ V(n−1); a deeper offset is silently clamped up (P0 57× −49.8 mV → −42.5 mV) [MEASURED]. Between points VID interpolates (54× = midpoint 53/55×); an offset at the top point moves the bins above it (E 46× moves 47–48×) | Tune prediction (clamped line + "clamped by Vn−1" badge), plan warnings |
| F5 | **Turbo tables** `0x1AD` (P, with `0x1AE` counts) / `0x650` (E, `0x651`): bucket ratios must be **non-increasing** with active-core count; the favored ratio `0x1C` per core sits on top. A plan that breaks the order ⇒ `REFUSED TURBO_ORDER` | tune.plan, Tune turbo editor |
| F6 | **VccIA** ≈ max(highest *allowed* VID over P-cores at the current active-core count, E-clusters, ring ~1.20 V at 40×) + ~100–110 mV [MEASURED lnx 2026-10-03, hypothesis on 3+ points]. Lowering a unit that does not set the max does not lower Vcore | Tune "VccIA set by …" panel |
| F7 | **Fused top point** per core = cmd `0x09 P1=0x11 P2=0` (P0 1.4258 V @57×, …, EC0 1.1563 / EC1–3 1.1367 V @46×; P2 selects the domain, not a point). Lower fused points are not exposed by any known command: the **stock-baseline measured curve (offsets 0) is the fused table as applied**; the 0x09 value cross-checks its top point | tune.read (`fused`), Curves baseline badge |
| F8 | **Never offset E 40×** (−20 mV ⇒ OCCT errors) [MEASURED lnx 2026-10-02]; E > ~1.2 V ⇒ WHEA reports [COMMUNITY] | Tune warnings |
| F9 | Measurement ratio sets include each unit's **max turbo ratio** when it lies above the top point (E 47, 48×); E 23× and 52× are not pinnable through cpufreq caps (land on neighbours) ⇒ `unreachable (cpufreq granularity)` | measure |
