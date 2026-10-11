# G-110 — dashboard redesign (futuristic, tabbed, diagrams, interactive)

- **Status:** ready
- **Host:** lnx
- **Safety tier:** T0 (simulator helper only)
- **Branch/worktree:** `task/G-110-dashboard-redesign`
- **Depends on:** G-105 (merged). Runs in parallel with G-109 (telemetry data side; it does not touch `web/`).

## Goal
The operator's verdict on the G-105 page (first real run 2026-10-11): "crammed, looks like the 90s". Full visual + interaction revamp of the
read-only dashboard: futuristic look (dark-first, considered accent colours, depth, motion that means something), **tabs**, **diagrams**
(live charts, per-core heatmap / gauges, history sparklines), interactivity (hover details, selection, filtering), smooth scrolling, and above all
**simplicity**: the important numbers at a glance, details one click away. Use the `frontend-design:frontend-design` skill for direction.

Suggested information architecture (adjust with reason): **Overview** (package/core power, hottest temp, Vcore, top clock, DRAM r/w, budget
ring, helper/events state, small live history charts) · **Cores** (8 P + 16 E: live heatmap/grid of MHz, busy, ratio, VID, temp; click a core
for its history) · **Sensors** (temperatures by named core, power, Vcore, DRAM over time) · **V/F** (the guarded "Read V/F curve" session +
Applied-vs-accepted, with a V/F diagram once data exists) · **System** (preflight checks, mode, platform, helper/budget) · **Audit** (log tail).

## Hard constraints (the server enforces them; tests check them)
- Exactly three files: `web/index.html`, `web/app.js`, `web/style.css` (server `MANIFEST`). No other files, no build step, no npm/CDN, no
  web fonts (CSP has no font-src ⇒ system font stacks only), no images except inline SVG built by JS / in the HTML.
- CSP: `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'` ⇒ **no inline `<script>`, no
  `style="..."` attributes, no `<style>` blocks, no `on*=` attributes, no `eval`/`new Function`**. Set dynamic styles via CSSOM
  (`el.style.setProperty`) or classes; build DOM with `createElement`/`textContent` (never `innerHTML` with data).
- Same API and ops as today: only `preflight`, `audit.tail`, `telemetry.start`, `vf.dump`, `cancel` may appear (`tests/web/test_dashboard_static.py`
  `ALLOWED_OPS` / `FORBIDDEN_OPS`). Keep the SSE handling, 202 + SSE `result` flow, cookie session, and the V/F safety UX: it never runs by
  itself, states "every mailbox request is a WRMSR", cost 112-124 ops, ops left, explicit click + cancel. Never add write controls.
- "n/a" values show the `unavailable` reason (hover/tooltip is fine), never a fake number. Preflight mode wording must not read as "this page
  writes" (e.g. "Preflight mode: write · no write controls in this build").
- Accessible: keyboard-navigable tabs (ARIA tablist), visible focus, contrast ≥ 4.5:1 for text, `prefers-reduced-motion` respected; works
  1280-2560 px wide and degrades sanely to ~800 px.
- Telemetry may gain fields from G-109 (temps entries with `name` / `cpu`, a real `vcore`, DRAM readings): bind defensively — use them when
  present, fall back to today's shape. Keep history client-side in memory (ring buffers, e.g. last 5 min at 1 Hz).

## Read first (only these)
`web/` (all three files) · `tests/web/` (what the tests assert; `fake_helper.py` shows the message shapes) · `src/arltuner/web/server.py`
(header + `MANIFEST`/`CSP`/`OPS`) · `docs/ARCHITECTURE.md` contract 8 (telemetry, heartbeat, vf.dump result) · `src/arltuner/telemetry/sampler.py`
(sample JSON shape).

## Owns
`web/index.html`, `web/app.js`, `web/style.css`, `tests/web/test_dashboard_static.py`, `tests/web/test_dashboard_fake_helper.py`
(update assertions that pin old markup; keep every safety assertion: CSP, allowed/forbidden ops, no inline handlers).

## Seeing it (simulator only — never the device helper)
`python3 tools/arltuner-gui.py --launcher <scratch>/launcher.html --helper python3 tools/arltuner-helper.py --sim --audit <scratch>/audit.jsonl --lock <scratch>/mb.lock`
(**always** pass `--helper ... --sim` with scratch `--audit`/`--lock`: without `--helper` the launcher pkexecs the installed *device* helper).
Chrome automation cannot open `file://`: open `http://127.0.0.1:<port>/`, then submit the token from your launcher file with a JS-built form
POST to `/bootstrap` (own origin is accepted). Screenshot each tab; stop the server when done.

## Acceptance
`python3 tools/run_tests.py` green · no CSP violations in the console · every tab screenshotted with sim data · the report lists the IA,
palette, and what each diagram shows.

## Non-goals
New ops, write controls, server/helper changes, new served files, external assets.

## Report-back (≤ 300 words)
Design direction · tabs and diagrams · files · test count · screenshots taken · anything the simulator could not show.
