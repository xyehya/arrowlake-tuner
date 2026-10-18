# arltuner desktop — PLAN (static spec; progress lives in `progress/`, never here)

_Decision: D-046 (2026-10-18). Inputs: `discovery/repo-inventory.md` (backend facts, path:line), `discovery/xtu-reference.md` (XTU 10 UI),
`docs/knowledge/apply-latency.md` (measured). Change this file only when a decision changes; record why in `docs/DECISIONS.md`._

## 1. Vision
One native window that shows the whole tuning state of the CPU at a glance and lets the operator change any of it in a few clicks, with
every value labeled with its operating point, unit, fused value, requested value and read-back. Apply happens in seconds, not minutes.
XTU 10's idiom (core tile grid + selected-core inspector + one Apply/Discard bar + always-on monitoring strip) **extended** to what this
project already does better than XTU: **every V/F point of every P-core, every E-cluster and the ring is individually editable** (XTU only
offers package-wide per-point offsets and one per-core offset), with predicted voltage and read-back per point.

## 2. Non-negotiables (carried from AGENTS.md / SAFETY.md / SPEC; the desktop app changes none of them)
- The AppImage is an **unprivileged client**. No root code, policy, accepted state or audit inside it; the helper is installed by
  `install.sh` from root-owned paths (SPEC §4.4, D-029). The app never runs `sudo`/`pkexec`; it shows the exact command for the user.
- Every hardware or root-sysfs write goes through the root helper -> guard -> one transaction at a time -> read-back -> audit (SAFETY.md, D-032).
- Plan-bound confirmation stays: `tune.plan` -> `plan_id` + nonce -> `tune.apply {plan_id, typed:"CONFIRM <nonce>"}`; one Apply click after a
  visible diff (O-4, D-033 d). No auto-apply, no autostart (OD-3/OD-4 unsigned).
- Limits are the operator's policy (`/etc/arltuner/policy.toml`) intersected with the CPU's own reported limits; the app shows the source of every
  limit, never edits or widens policy, never works around a refusal (D-038/D-039).
- Live data only: topology, fused words, favored cores, limits are read from this CPU (D-036). Reference numbers only in tests/fixtures.
- Stress is a quick screen, never "pass" (D-019). Revert = cold boot (or BIOS defaults for runtime sysfs controls), shown next to Apply.
- Front-end hygiene: all text via `textContent`, strict CSP (`default-src 'self'`; no inline script/style, no remote origins), no CDN, no npm,
  no eval. Tauri capabilities: only the app's own commands + event listen; no shell, fs, http plugins.
- Workers never touch hardware: they use the simulator helper, fake sysfs, fixtures. Hardware sittings are orchestrator + operator only.

## 3. Architecture
```
AppImage (user)                                              root (installed by install.sh, unchanged trust model)
┌──────────────────────────────────────────┐                ┌─────────────────────────────────────────────┐
│ ui/ (HTML/CSS/JS ES modules, no npm)     │                │ arltuner-helper (Python stdlib, socket-     │
│   bridge.js ── invoke()/listen() ──┐     │                │ activated, SO_PEERCRED allowlist, 1 client) │
│ src-tauri/ (Rust, Tauri 2)         ▼     │  JSON lines    │  ops: preflight tune.* profiles.* curves.*  │
│   helper_link: UnixStream, root    ──────┼───────────────►│  stress.* telemetry.* audit.tail mem.read   │
│   check, framing, allowlist, events│     │ /run/arltuner/ │  + new: power.* caps.* parking.* uncore.*   │
│   tray, single-instance, notify,   │     │ helper.sock    │  guard → mailbox / MSR / root sysfs         │
│   settings (~/.config/arltuner-desktop)  │                └─────────────────────────────────────────────┘
└──────────────────────────────────────────┘
```
### 3.1 Rust core (`desktop/app/src-tauri/`)
- `helper_link`: connect `/run/arltuner/helper.sock`; refuse unless `lstat` says socket owned by uid 0 **and** `SO_PEERCRED` of the listener is
  uid 0 (mirrors `helperlink.connect_helper`, `src/arltuner/web/helperlink.py:305-362`); handshake = `preflight`; map refusals
  (`ACCESS_DENIED`, `CLIENT_BUSY`, missing, refused) to the same named fixes. Framing: one compact JSON object per line, ≤ 64 KiB both ways,
  ids unique per connection. Reader thread dispatches responses by id and emits events (`heartbeat state progress telemetry vf.point result`)
  to the webview as Tauri events `helper://<event>`.
- **Op allowlist** = the server's `OPS` (`src/arltuner/web/server.py:145-171`) + the new ops of §6; argument shape checked before sending
  (defence in depth; the helper re-validates). Unknown op -> refused locally.
- Commands exposed to JS: `helper_status()`, `helper_connect()`, `helper_call(op, args) -> {ok,result|error}` (short ops; long ops return
  `{id}` and finish via `helper://result`), `settings_get/set`, `notify(title, body)`, `open_guide()`.
- Dev/test seam: a cargo **feature `dev-socket`** (never in release builds; the build script refuses it for the AppImage) lets the link
  accept a socket owned by the current uid at `ARLTUNER_DESKTOP_SOCKET` so tests and the simulator helper work without root.
- Reconnect: after EOF, one reconnect attempt per user action (as G-153); an op in flight is reported lost, never replayed.
- Native: tray icon (status colour: idle/applying/stressing/disconnected; menu: Show, Restore accepted (opens the app's review), Quit),
  single instance (second launch focuses the window), window-state persistence, desktop notifications on long-op results.

### 3.2 UI (`desktop/app/ui/`)
- No framework. ES modules: `bridge.js` (the only file that touches `window.__TAURI__`; a `mock` bridge backed by fixtures for tests),
  `store.js` (single state object + subscribe), `lib/*.js` pure logic (units model, draft, prediction, intent builder, formatting, limits),
  `views/*.js` (one per screen), `components/*.js` (tile, stepper-field, spark, chart, drawer, toast).
- Pure logic is ported from `web/app.js` (cite the source lines in each module header), not imported from it; `web/` stays untouched.
- Unit tests: `node --test desktop/app/ui/tests/` (Node's built-in runner; no packages). UI smoke tests: Playwright + Chrome against
  `ui/index.html?bridge=mock` (the repo's existing browser-test stack, opt-in `ARLTUNER_BROWSER_TESTS=1`). `tools/run_tests.py` gains hooks
  that run `node --test` and `cargo test` when those tools exist (skip with a note when they do not).

## 4. Screen design
### 4.1 Frame (every screen)
- **Left nav rail:** Dashboard · Tune · Curves · Stress · Profiles · System · Audit. Icons + labels, keyboard 1..7.
- **Top status bar:** helper state (connected / busy by another client / not installed — with the fix command), mode (writes allowed /
  read-only + why), applied vs accepted chip (matches / equivalent / differs: n / stock / unknown), last read time, ops this boot.
- **Bottom monitoring strip (always visible, collapsible):** selectable live series (package power, highest clock, hottest sensor, Vcore,
  core power, DRAM GB/s) as sparklines + numbers; decoded limit-reason badges (PL1, PL2, thermal, current, …) when §6.1 lands.
- **Pending-changes bar** (appears whenever the draft differs from the CPU): "N changes on M units · Discard · Review & Apply". Pending
  fields are marked in place (dot + accent) everywhere they appear.

### 4.2 Tune (the core screen) — sub-tabs: Cores · Turbo ratios · Power · Ring & uncore · Ratios & parking
**Cores** — left: tile grid. Row 1-2: P-core tiles P0..P7 (★ favored, from live `0x1C`), E-cluster tiles EC0..EC3 (each shows its 4 CPUs),
one Ring tile. Tile face: Ratio mode = max ratio / OC ratio badge / deepest offset; Live mode = clock, temp, VID (toggle like XTU's
Ratio/Monitors). Selection: click, Ctrl/Shift-click, quick chips "All P", "All E", "Favored", "All".
Right: **Inspector** for the selection.
- Header: unit name(s), fused top ratio, OC ratio state ("OC ratio 48x (fused top 46x) · no OC point" wording from G-150), favored flag.
- **V/F curve editor:** one row per point of the unit's live curve (P: points 1..8 + OC point; E: 1..6 + OC point; ring: 1..9):
  `Point 4 · 48x` | fused V | **offset field** (typed mV, ± stepper, wheel, arrow keys; 1 mV typed, 5 mV step, Shift = 25 mV) |
  predicted V | read-back V | limit hint (policy window, source). Whole-curve offset field above the rows. A small chart: fused curve,
  predicted curve, measured baseline if one exists; points draggable vertically in 5 mV steps (same draft as the fields).
- **Multi-selection:** fields edit the same point (by ratio) on every selected unit; differing values show "mixed (−35…−50)"; typing sets all.
  P and E selected together: the editor shows two sections (points differ).
- **OC section:** OC ratio stepper (fused top+1 … policy∩CPU max), adaptive target voltage (5 mV grid, ≤ policy∩CPU max), "48x offset" helper
  (G-150), "no OC point" state. Static/override mode is never offered (D-016).
- Hardware rules shown inline, not as refusals after the fact: V(n) ≥ V(n−1) clamp preview, E clusters share one ratio, never offset E 40x,
  OC point can only be cleared by a cold boot.
**Turbo ratios** — XTU-style Active Cores table for P and E (1..8 active cores), stepper per bucket, non-increasing enforced as you type,
step chart; bucket core counts read-only.
**Power** (§6.1) — PL1, PL2 (W) and tau (s): slider + typed field each; shows current, BIOS value at boot, CPU-reported info (`0x614`),
policy window; "runtime only — BIOS restores at boot" note. Read-only: IccMax if readable without new mailbox traffic, decoded live limit reasons.
**Ring & uncore** — ring V/F point offsets 1..9 (same editor as the Cores inspector) + ring ratio min/max (§6.3).
**Ratios & parking** — per-core max-ratio caps (XTU per-core ratio analogue, effective = min(turbo table, cap)) and core parking toggles
(CPU0 cannot be parked: it has no `online` file `[MEASURED lnx 2026-10-18]`); both runtime, reverted by reboot or one click.

### 4.3 Apply flow (target: seconds, two clicks, never modal)
1. Edit -> pending bar counts changes (no traffic).
2. **Review & Apply** -> `tune.plan {intent}` (pure, no hardware) -> a **drawer** slides up: per-unit before/after table, the ordered write
   list (G-153 order), ops needed, warnings, predicted VccIA, "Revert: cold boot". Plan errors appear in place on the offending field.
3. **Apply N writes** (one click; nonce sent by the app, D-033 d) -> live progress per unit (queued / writing / read back OK / mismatch),
   ETA from the measured per-op time, **Cancel** (helper `cancel`, takes effect between transactions). The window stays usable.
4. Result: compact summary (all read back equal / n mismatches) + expandable read-back table; draft cleared; tiles update from the result;
   notification if the window is unfocused. "Set as accepted" and "Screen with stress" shortcuts.
- `tune.read` (once per boot, prerequisite) is a prominent "Read CPU state (~N s)" action on the Tune screen with live progress; after G-211
  it takes seconds. Never automatic (each read is mailbox traffic).

### 4.4 Other screens
Dashboard (core map, tiles, charts, memory card — port), Curves (measure, compare, library), Stress (screens, caps, history; D-019 banner),
Profiles (accepted state, list, load into Tune, compare, save, set accepted, Windows fingerprint), System (preflight, limits table with
sources, helper/ops), Audit (tail, filter). Settings (in System): refresh interval, strip series, theme density, notifications.

### 4.5 Visual language
Dark, high-contrast, dense but calm. One accent for "pending", one for "applied/OK", one for "warning/refused". Tabular numerals for all
values; units always shown; every control has a tooltip naming the register/source (e.g. "mailbox 0x11 P1=0 P2=4 · registers.md").
System font stack only (no bundled web fonts needed); SVG icons shipped in the repo.

## 5. Helper performance (apply speed)
Measured: 1 ms per transaction vs 194 ms between transactions; audit log 15.9 MB re-parsed per op (`docs/knowledge/apply-latency.md`).
Target: ≤ 20 ms overhead per transaction on a 16 MB log, verified in the simulator and then live (one read sitting).
Allowed changes (semantics identical, reviewer-gated): incremental audit state (index the log once, then parse only appended bytes, keyed by
inode + size; re-scan on any mismatch); hold the cross-process lock for the whole apply session instead of create/unlink per op (still
one transaction at a time, still excludes the CLI); cheaper health snapshot implementation (same checks: taint, PCI devices, modules, MCE);
remove fixed pacer slack when `min_dwell_s` is 0. Not allowed: dropping the health bracket, read-back, fsync of the audit record before a
write, the nonce, or batching transactions.

## 6. New helper capabilities (D-046 scope)
All are root-sysfs writes made by the helper (the app cannot write them): new ops behind the guard with policy keys, audit records, read-back,
journal-based revert, simulator + fake-sysfs support, and a **separate hardware gate** each. All runtime-only (the BIOS restores them at boot).
They join the same plan/nonce/apply flow through new intent fields, so one Apply covers everything.
### 6.1 Power limits
Read: powercap `intel-rapl:0` constraint_0 (long_term = PL1, µW; time window = tau, µs), constraint_1 (short_term = PL2); MSR `0x610`
(lock bit 63 -> read-only with reason), `0x614` PKG_POWER_INFO (info only), `0x606` units — added to the plain-MSR read allowlist after
`registers.md` entries exist. Write: powercap files only (never MSR `0x610`). Policy: `[power] pl1_w = [min,max]`, `pl2_w`, `tau_s`; shipped
defaults need an operator yes (no widening without it). Decoded limit reasons: `0x64F` / `0x6B0` bit tables documented in `registers.md`
with evidence before any decode ships `[UNVERIFIED]` today.
`[MEASURED lnx 2026-10-18]` now: PL1 250 W, tau 127.9 s, PL2 295 W, `constraint_0_max_power_uw` 125 W (not a ceiling for PL1).
### 6.2 Ring row
`tune.plan` intent gains `ring: {points: {p2: mv}}` (offset-only, P2 1..9, policy ring entry, D-045 rules: ring OC word never written,
a changed ring OC word refuses the ring unit alone). The GUI ring editor uses it.
### 6.3 Ring ratio (uncore) min/max
`/sys/devices/system/cpu/intel_uncore_frequency/package_00_die_00/{min,max}_freq_khz` (initial 800000..4000000 `[MEASURED lnx 2026-10-18]`);
bounded by `initial_min/max` and policy; revert = write initial values.
### 6.4 Per-core max-ratio caps and core parking
Caps: `cpufreq/scaling_max_freq` per CPU (E clusters capped as 4 CPUs, G-152 rule), journaled like the stress caps, conflict-checked
against stress/measure caps. Parking: `cpu<N>/online` (never CPU0; never the last CPU of a kind; refused while stress runs).

## 7. Packaging
- `cargo tauri build --bundles appimage` via `desktop/tools/build-appimage.sh` (pins tauri-cli 2.x, checks the `dev-socket` feature is off,
  prints sha256). Output `desktop/dist/arltuner-desktop-<version>-x86_64.AppImage` (gitignored).
- First run without the helper: a Setup screen with the exact command (`sudo bash install.sh --add-user <user>` from a checkout of this repo)
  and a copy button; the app never elevates. With the helper but not allowlisted / busy: the named fix, retry button.
- Optional desktop integration: `.desktop` entry + icon into `~/.local/share/applications` on user request (the app asks once).
- Version = `pyproject.toml` version + desktop build number; shown in System and `--version`.

## 8. Phases (exit criteria)
| Phase | Content | Exit |
|---|---|---|
| D0 Foundations | D-046, docs, toolchain, Tauri scaffold, AppImage pipeline, test hooks | an AppImage that opens a window, tray, single instance; `cargo test`, `node --test` wired into `run_tests.py` |
| D1 Link + read-only | Rust helper link, bridge, frame (nav, status bar, strip), Dashboard, System, Audit | live telemetry in the app against the simulator; **H1** live read-only connect on the reference machine |
| D2 Tune core | units model, draft, Cores grid + inspector V/F editor, turbo tables, apply drawer, profiles | full edit→plan→apply→read-back loop vs simulator; parity with the web Tune screen |
| D3 Speed | helper bookkeeping optimisation | ≤ 20 ms overhead/op in sim with a 16 MB log; **H2** live `tune.read` timed |
| D4 New controls | ring row, power limits, uncore, caps, parking (helper + UI) | each control passes its sim tests and its own hardware gate **H3–H5** |
| D5 Remaining screens + native | Curves, Stress, settings, notifications, setup screen | web GUI parity checklist all green |
| D6 Release | build script, docs/guide pages, screenshots, install to `~/AppImages` | operator acceptance; web GUI demoted to fallback in README |

## 9. Orchestration model
| Role | Who | Use for |
|---|---|---|
| Orchestrator + validator | Hermes on Opus 5.5 (this session) | plan, task prompts, dispatch, review diffs/test runs, merge, docs, every hardware sitting |
| Implementer (medium tasks) | Claude Code headless: `claude -p --model claude-sonnet-5-5 --effort high` in a git worktree `.claude/worktrees/G-2NN` on `task/G-2NN-slug` | one task from `TASKS.md`, its Owns only, tests, report ≤ 300 words |
| Fast iterator | Hermes subagents (`delegate_task`, deepseek-flash) | run suites, triage failures, small mechanical fixes, lint, fixture generation, screenshot checks |
| Security / design reviewer | Claude Code `--model claude-opus-5-5`, read-only tools | any diff touching the helper, guard, Rust socket link, capabilities/CSP, root-sysfs ops |
Rules: workers simulator-only, no sudo, no `/dev`, no network except cargo fetches already approved; disjoint Owns; `python3 tools/run_tests.py`
green (plus `cargo test`, `node --test`) before merge; merges and pushes by the orchestrator only. Prompts and raw logs: `progress/logs/`.
