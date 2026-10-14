# G-150 — Tune: name a boot-time OC ratio as one (E 48× above fused 46×) and give it an offset control

- **Status:** done (merged 2026-10-14)
- **Host:** lnx
- **Safety tier:** T0 (worker: simulator + fake helper only)
- **Branch/worktree:** `task/G-150-e-oc-ratio-offset`
- **Depends on:** none

## Goal (operator, 2026-10-14)
"it says OC point 7 not set.. it is set to x48 = is an oc ratio. ecores run native at x46 ... i cant set an offset on the gui theres no drop
down menu for x48 which is an OC Ratio!"
On the reference 285K the E-clusters' fused curve ends at point 6 = 46× (`0x1C[15:8]` = 46, `0x09`); the BIOS raises them to 48× through the max
ratio (`0x1C[7:0]` = 48) and the E turbo table (`0x650` = 48×8), with **no OC point** (`P2=0` word = 0). 47–48× then run on point 6's voltage
[MEASURED lnx 2026-10-13: EC1 48× 1.1125 V = point 6 at −25 mV; EC0 48× 1.1226 V]. Today the Tune row says "OC ratio: none", the OC box says
"No OC ratio configured" and the dropdown's first entry is "No OC ratio" — wrong from the user's side: 48× **is** an OC ratio. And there is no
voltage control the user recognises for 48×. Fix the wording and add a 48× offset control that is honest about what the CPU can do.

## Fixed decisions (do not re-decide; report if one is impossible)
1. **Two words, two meanings.** *OC ratio* = any ratio a unit runs above its fused top ratio, however it got there (boot-time max ratio / turbo
   table, or an OC point). *OC point* = the `P2=0` word's ratio + adaptive voltage, the only way a ratio above the fused top gets a voltage of its
   own. Every text on the Tune page uses them this way.
2. **`stockMaxOf` / `ocClass` stay as they are for E** (fused top, 46). 48× must stay class `"oc"` so picking it creates the OC point. Do not
   switch E to `0x1C[7:0]` — that would hide 48× from the dropdown. Fix only the stale comment at `web/app.js` `stockMaxOf` ("E has no 0x1C of
   its own" is false: `docs/knowledge/registers.md` limits paragraph, `0x1C` answers on E CPUs, E `0x2E30`).
3. **Wording, all derived from the live read (no constants):**
   - Header (`nowText`) for a unit that runs above its fused top with no OC point: `OC ratio: 48× (fused top 46×) · no OC point · runs up to 48×`
     (adapt; must contain the ratio and "no OC point"). With an OC point: `OC ratio: 48× · OC point 7`. A unit at or below its fused top: as today.
   - OC box (`ocInfo` "none" text / `eVfFact`): for the case above, say what it is and how to change it, e.g. "48× (OC ratio set at boot) runs on
     point 6's voltage, so point 6's offset sets it. For a voltage of its own, set the 48× offset below." Keep the existing fact text
     `47–48× run on point 6’s voltage (no OC point)` (tests assert it) or update those tests deliberately.
   - Point 6's label for such a unit: `Point 6 · 46× (also 47–48×)`.
   - Ratio dropdown: first option "No OC point" (was "No OC ratio"); aria-label "OC point ratio". An option above the fused top but **below** the
     unit's current max (E 47× while the max is 48×) gets the suffix `· lowers the max from 48×` (choosing it sends `0x1D` = 47, measured to lower
     `cpuinfo_max_freq`, G-147).
   - `OC_TIP` rewritten to the two meanings.
4. **48× offset control** (in the OC box of the per-point panel, for any unit — P or E — whose max ratio is above its fused top):
   - A dropdown "48× offset" (label uses the unit's live max ratio). Offset = the 48× voltage minus the unit's **fused top-point voltage**
     (`0x09`, `fusedOf(u).v`) — the stock voltage of 46–48×, since they share point 6 at stock.
   - First option: `same as point 6 (−35 mV) · no OC point` = the point-6 offset as edited. Choosing it with no OC point read = no draft change;
     with an OC point drafted, it removes that draft (`setRatio(null)` path).
   - Other options: point-6 offset < X ≤ the policy's V-target ceiling expressed as offset, on the point-offset grid. Choosing X drafts the OC point
     at the unit's current max ratio (48) with target = `fusedOf(u).v + X` **per unit** (group rows: each unit its own fused voltage), snapped to the
     target grid and inside `[GRID.vtMin, lim.vtMax]`. This goes through the existing ratio/target draft, intent and plan — no new write path.
   - Offsets below point 6's are **not offered**: the CPU raises a target below the top fused point to it [MEASURED lnx 2026-10-11, 2026-10-13].
     Muted hint under the control: "Lower than point 6: lower point 6 (46–48× share its voltage)."
   - When an OC point exists (read or drafted) the control shows its target as an offset (target − fused V, "· now" for the read value); if point
     6 is then edited above that offset, the row warns "48× is raised to point 6's voltage by the CPU".
   - One-line warning while an OC point is drafted on a unit that has none: "An OC point stays until a cold boot; Restore accepted then reports it
     EQUIVALENT (G-147)."
   - The existing absolute target dropdown stays (same value, absolute view); both controls edit the same draft.
5. **Prediction panel:** with an OC point drafted, the highest-VID prediction at that ratio uses the target, not point 6 (check; fix if not).
6. Same file rules as today: no turbo-table text in unit cards (G-133), favored/limits from live data only (D-036/D-039).

## Read first (only these)
- `web/app.js` Tune section: `ocPointNo` … `nowText`, `fillRatioSelect`, `setRatio`/`setVt`, `refreshRows` (OC/target part), `refreshOcCell`,
  `eVfFact`, `buildPointPanel`, the prediction panel code
- `tests/web/test_g133_page.py`, `tests/web/test_cpu_facts_page.py` (OC wording asserts), `tests/web/v3_harness.py` / `fake_helper.py` (fixtures)
- `docs/knowledge/registers.md` (`0x1C/0x1D`, limits paragraph), `docs/knowledge/platform-arl-s.md` (OC point, clamp)

## Owns
`web/` (app.js, index.html, CSS if needed), `tests/web/`, `src/arltuner/web/` only if a fixture needs it, `tools/screenshots.py` (fixture names only).
Not `docs/guide/` (the documenter updates it after merge), not `docs/STATE.md` / `DECISIONS.md`.

## Acceptance
- `ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green (currently 2901 tests).
- Browser tests on the fake helper with the reference E state (OC word 0, fused 46, `0x1C` 0x2E30, `0x650` 48×8, EC0 point 6 −35 / EC1-3 −25):
  header says OC ratio 48× + no OC point; Point 6 label "also 47–48×"; the 48× offset dropdown's first option is the point-6 offset; picking −20
  on EC1 drafts ratio 48 + target = fused V − 20 mV (snapped) and the review/intent shows that; offsets below −25 are absent; 47× option carries
  the "lowers the max" suffix; "No OC point" wording; P-cores at/below fused top unchanged.
- No new write path; plan/guard untouched; no zero ratio ever drafted.

## Non-goals
No hardware. No change to planner/guard/executor or policy. No change to accept/restore.

## Report-back (≤ 300 words)
What changed per decision (1–6), screenshots of the E row before/after (fake helper), test count line, anything that could not be done as fixed.
