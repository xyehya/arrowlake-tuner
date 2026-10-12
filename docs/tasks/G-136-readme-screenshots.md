# G-136 — README screenshots: capture tool + gallery layout

- **Status:** in progress · **Host:** lnx · **Tier:** T0 (worker, simulator) then T1 (orchestrator captures on the live app) · **Branch:** `task/G-136-readme-screenshots`

## Goal (operator 2026-10-12)
"some screenshots showing the webpage like VIDs, Tuning page, Memory timings and other functionalities would add a sugar coating to it — I
usually prefer repos with screenshots and visuals." Nothing on the pages is confidential; the real system's pages may be used.

Split (AGENTS.md: workers never use the real helper):
1. **Worker:** `tools/screenshots.py` — stdlib only. Starts nothing on hardware. Given a running GUI (port + launcher file, or `--sim` to start
   the GUI itself against the simulator helper), it drives a headless Chrome (`google-chrome --headless=new --remote-debugging-port`, own temp
   profile) through CDP (minimal stdlib websocket client), logs in via the launcher token (POST `/bootstrap`), and captures a fixed shot list to
   `docs/img/*.png` at 1440-wide, device scale 2 → downscaled or not (keep each PNG ≤ ~400 KB; crop to the element). It must only navigate tabs,
   wait for data, toggle display controls and scroll — **never click anything that calls a write op, measure, stress, apply, accept or read
   tuning words** (it uses data already on the page; for Tune it requires that a tuning read is already present via `tune.last`, else skips
   the shot with a message). Dark theme by default; `--theme light` optional.
2. Shot list (names fixed; adjust after looking at the page): `dashboard.png` (core map with clocks/VIDs/temps + power), `dashboard-memory.png`
   (memory card: MT/s, gear, timings, DIMM temps), `curves.png` (V/F chart, several units, baseline), `curves-table.png`, `tune-per-core.png`
   (Per-Core rows, one P row with Per V/F point Offsets open), `tune-review.png` only if reachable without a write (else drop), `stress.png`
   (idle form), `profiles.png`, `system.png` (preflight checks).
3. **README:** a short "Screenshots" section near the top (after Status): 2–3 hero images inline, the rest in a compact table or `<details>`
   gallery, each with a one-line caption saying what it shows; alt text on every image. Captions state the values are from one example machine
   (Core Ultra 9 285K). Do not edit other README sections (G-133 owns label names; the documenter owns docs/guide/).
4. Worker tests: the CDP/websocket helper and shot list logic against the simulator GUI (a smoke test gated like the browser tests,
   `ARLTUNER_BROWSER_TESTS=1`). Simulator PNGs are for testing only — **do not commit them**; commit the README with the final file names.
5. **Orchestrator (after G-133 merges and is installed):** runs the tool against the live app, reviews every PNG, commits `docs/img/*.png`.

## Owns
`tools/screenshots.py`, `tests/web/test_screenshots*.py`, `README.md` (new Screenshots section only), `docs/img/` (orchestrator commits PNGs).

## Acceptance
Suite green with browser tests; tool documented in its `--help`; README section renders with the final names.
