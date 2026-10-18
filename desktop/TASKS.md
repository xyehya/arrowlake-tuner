# arltuner desktop — TASKS (static catalog; status lives in `progress/STATUS.md`)

Numbering G-201+ (desktop track; core track continues at G-155+). Host lnx for all. Branch `task/G-2NN-slug`, worktree `.claude/worktrees/G-2NN`.
Every implementer task: read `AGENTS.md`, `desktop/PLAN.md` (sections named), only the files listed; simulator / fake sysfs / fixtures only;
no sudo, no `/dev`, no hardware; touch only **Owns**; acceptance = commands that pass; report ≤ 300 words (changed files, tests + counts, open questions).
Model: **S** = Claude Code Sonnet 5.5 · **F** = deepseek-flash subagent · **O** = Opus 5.5 (orchestrator or reviewer) · **H** = hardware sitting
(orchestrator + operator go). **R** = Opus security review required before merge.

## D0 Foundations
**G-201 · Decision, docs, scaffolding** · O · deps none
Owns `docs/DECISIONS.md` (D-046), `desktop/{README,PLAN,TASKS}.md`, `desktop/progress/`, `desktop/.gitignore`, `docs/knowledge/apply-latency.md`.
Accept: files exist; STATE.md points at `desktop/`.

**G-202 · Tauri 2 scaffold + AppImage pipeline** · S · deps G-201
Owns `desktop/app/src-tauri/**` (Cargo.toml, build.rs, tauri.conf.json, capabilities/, icons/, src/main.rs, src/lib.rs), `desktop/app/ui/index.html`
(placeholder), `desktop/tools/build-appimage.sh`, `desktop/app/.gitignore`.
Goal: Tauri 2 stable app `arltuner-desktop` (identifier `io.github.xyehya.arltuner`), `frontendDist: ../ui`, `withGlobalTauri: true`, no npm
(`beforeDevCommand`/`beforeBuildCommand` empty), CSP `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src ipc: http://ipc.localhost`,
capabilities = core window/event defaults + app commands only (no shell/fs/http plugins). Plugins: single-instance, window-state, notification;
tray icon with Show/Quit. `--version` prints the version. Cargo feature `dev-socket` declared (empty for now); build script refuses it.
Accept: `cargo test` passes in `src-tauri`; `desktop/tools/build-appimage.sh` produces `desktop/dist/*.AppImage`; running it with `--version`
prints the version; `cargo tree -e normal --depth 1` lists only tauri + the three plugins + serde/serde_json/libc.

**G-203 · Test hooks** · F · deps G-202
Owns `tools/run_tests.py` (hook section only), `desktop/app/ui/tests/smoke.test.mjs`.
Goal: `run_tests.py` runs `node --test desktop/app/ui/tests/` and `cargo test --manifest-path desktop/app/src-tauri/Cargo.toml` when `node`/`cargo`
exist, else prints "skipped: <tool> missing"; failures fail the suite.
Accept: `python3 tools/run_tests.py` green with both hooks active; removing `node` from PATH prints the skip line.

## D1 Link + read-only
**G-204 · Rust helper link** · S · R · deps G-202
Owns `desktop/app/src-tauri/src/helper/**`, `desktop/app/src-tauri/tests/helper_*.rs`, `desktop/app/src-tauri/tests/fixtures/`.
Read: PLAN §3.1; `src/arltuner/web/helperlink.py`; `src/arltuner/helper/protocol.py`; `src/arltuner/web/server.py:40-200` (OPS, validators); `docs/ARCHITECTURE.md` Contract 8/9.
Goal: connect + root checks (lstat uid 0 socket; SO_PEERCRED listener uid 0 via libc getsockopt), preflight handshake, refusal mapping with the same fix
texts, line framing ≤ 64 KiB, id allocation, request/response matching, event emission `helper://<event>`, op allowlist with arg-shape checks,
reconnect-on-demand, commands `helper_status/helper_connect/helper_call`. Feature `dev-socket`: accept a socket owned by the current uid at
`ARLTUNER_DESKTOP_SOCKET`.
Accept: `cargo test` covers: non-socket / non-root node refused (via injectable lstat/peercred), oversize line refused, `CLIENT_BUSY` and
`ACCESS_DENIED` framed refusals mapped, unknown op refused locally, a long op's result delivered as event; integration test with feature
`dev-socket` against the Python simulator helper (`tools/arltuner-helper.py` sim on a temp socket) runs `preflight`, `telemetry.start`, receives ≥ 2 telemetry events.

**G-205 · UI shell, design system, bridge** · S · deps G-202
Owns `desktop/app/ui/{index.html,app.js,bridge.js,store.js,styles/**,components/**,icons/**}`, `desktop/app/ui/fixtures/**`, `desktop/app/ui/tests/shell.*`.
Read: PLAN §3.2, §4.1, §4.5; `discovery/xtu-reference.md` (UI structure).
Goal: frame of §4.1 (nav rail, status bar, monitoring strip, pending-changes bar, toasts, drawer component), `bridge.js` with real (Tauri) and
`mock` (fixtures, `?bridge=mock`) backends sharing one interface, `store.js`. Fixtures generated from the simulator (preflight, telemetry stream,
tune.read result, plan, apply result) checked in as JSON.
Accept: `node --test` (store, bridge mock); Playwright smoke (`ARLTUNER_BROWSER_TESTS=1`) loads `index.html?bridge=mock`, navigates all 7 screens,
no console errors, CSP violations = 0.

**G-206 · Dashboard, System, Audit screens** · S · deps G-205
Owns `desktop/app/ui/views/{dashboard,system,audit}.js`, `desktop/app/ui/components/{coremap,chart,spark}.js`, tests for them.
Read: `web/app.js` dashboard/system/audit sections (cite lines in module headers); `discovery/repo-inventory.md` §4, §5.1.
Accept: node tests for formatting/scales; Playwright with mock bridge shows live tiles updating from the fixture stream; unavailable readings show `n/a` + reason.

**H1 · Live read-only connect** · H · deps G-204, G-206 — orchestrator runs the dev AppImage on the reference machine (read-only ops only:
preflight, telemetry, audit.tail, profiles.list, tune.last; no mailbox) with operator go; evidence to `captures/lnx/desktop-h1-*.txt`.

## D2 Tune core
**G-207 · Tune logic library** · S · deps G-205
Owns `desktop/app/ui/lib/{units,draft,predict,intent,limits,format}.js`, `desktop/app/ui/tests/lib_*.test.mjs`.
Read: `web/app.js` Tune sections (units from `tune.read`, draft, prediction, `buildRows`, intent); `src/arltuner/helper/ops_tune.py:1222-1320` (intent schema);
`docs/knowledge/platform-arl-s.md` (hardware rules).
Goal: units model from a `tune.read` result (P0..P7, EC0..EC3, ring), draft with multi-unit edits by point ratio, whole-curve + per-point offsets,
OC ratio/target, turbo tables; prediction (fused + offset, V(n) ≥ V(n−1) clamp); limits from the read (`limits`, `hw_limits`) with sources;
intent builder producing exactly the helper schema (ring field gated on G-212's schema flag); diff (pending count per unit).
Accept: node tests incl. golden intents for: one P point, all-E whole curve, mixed multi-select, OC ratio + target, turbo P/E, reset; every intent validates
against the Python schema (a test calls `python3 -c` on the helper's validator with the produced JSON).

**G-208 · Cores grid + inspector V/F editor** · S · deps G-207
Owns `desktop/app/ui/views/tune.js`, `desktop/app/ui/views/tune/{grid,inspector,curve-editor,oc}.js`, `desktop/app/ui/components/stepper.js`, tests.
Read: PLAN §4.2 (Cores), `discovery/xtu-reference.md`.
Accept: Playwright (mock): select P0 → edit point 4 by typing, arrows, wheel, drag on chart → same draft; multi-select All E shows "mixed" correctly;
pending bar counts; limit hints show policy source; keyboard-only path works.

**G-209 · Turbo ratio tables** · F · deps G-207
Owns `desktop/app/ui/views/tune/turbo.js` + tests. Accept: non-increasing enforced while typing; draft/intent golden test.

**G-210 · Apply drawer** · S · deps G-208
Owns `desktop/app/ui/views/tune/apply.js`, `desktop/app/ui/components/drawer.js` (extend), tests.
Read: PLAN §4.3; `web/app.js:3903-4388` (plan/apply/result).
Accept: Playwright (mock): Review & Apply → drawer with ordered writes + warnings; Apply sends `CONFIRM <nonce>`; per-unit progress from `progress`
events; Cancel sends `cancel`; mismatch result shown; plan error lands on the field; `PLAN_STALE` re-plans on request; window stays interactive.

**G-211 · Helper bookkeeping speed-up** · S · R · deps none (parallel with D1/D2)
Owns `src/arltuner/guard/{guard.py,audit.py}`, `src/arltuner/health/{snapshot.py,gate.py}`, `src/arltuner/controls/executor.py`, `tests/guard/`, `tests/health/`,
`tools/bench_guard.py` (new).
Read: PLAN §5; `docs/knowledge/apply-latency.md`; `docs/SAFETY.md`; the owned files.
Goal: first `tools/bench_guard.py` (simulator transport, 16 MB synthetic audit log, 300 ops) prints per-part timings; then incremental audit state,
session-held lock, cheaper snapshot implementation, no pacer slack at `min_dwell_s = 0`; semantics unchanged.
Accept: bench before/after in the report; ≤ 20 ms median overhead per op on the 16 MB log; full suite green; new tests prove: appended-only parse,
rescan on inode/size mismatch or truncation, lock still excludes a second process, health gate still trips on each injected fault, dwell still honoured when > 0.

**G-212 · Helper: ring rows in the intent** · S · R · deps none
Owns `src/arltuner/helper/ops_tune.py` (intent parse/resolve for ring only), `tests/` for it, `docs/ARCHITECTURE.md` (Contract 9 intent line).
Accept: intent `{ring:{points:{"4":-25}}}` plans ring P2 writes within policy; ring OC word never written; changed ring OC word refuses ring alone; tests.

**G-213 · Profiles screen** · S · deps G-210
Owns `desktop/app/ui/views/profiles.js` + tests. Accept: list, load into Tune (draft), compare two, save, set accepted (from read / from profile), fingerprint; Playwright (mock).

**H2 · Live speed check** · H · deps G-211 merged + installed — one `tune.read` timed (audit ts) with operator go; target ≤ 20 ms/op overhead.

## D4 New controls (helper first, then UI)
**G-214 · Power limits backend** · S · R · deps G-211
Owns `src/arltuner/controls/power.py` (new), helper op module `src/arltuner/helper/ops_power.py` (new), registration lines in `helper/ops.py`,
`src/arltuner/guard/policy.py` (`[power]` keys + plain-MSR read allowlist `0x606,0x610,0x614`), `policies/default.toml` (keys **commented out** until the
operator sets values), `docs/knowledge/registers.md` (power section, evidence-tagged), tests + fake sysfs.
Goal: `power.read` (powercap + MSR info, lock bit), intent field `power {pl1_w, pl2_w, tau_s}` planned/applied through the same nonce flow, powercap writes
only, read-back, audit, journal + revert to boot values.
Accept: sim tests for bounds, lock bit → read-only, read-back mismatch trips, revert; no write when policy keys absent (refusal names the key).

**G-215 · Limit-reason decode** · S · deps none
Owns `docs/knowledge/registers.md` (limit-reasons section), `src/arltuner/telemetry/limits.py` (new), tests.
Goal: document `0x64F`/`0x6B0` bit meanings with evidence tags (DOC/COMMUNITY sources; mark UNVERIFIED bits), decode only DOC/MEASURED bits.
Accept: decoder tests; registers.md entry cites sources.

**G-216 · Caps, parking, uncore backend** · S · R · deps G-211
Owns `src/arltuner/controls/{caps,parking,uncore}.py` (new), `src/arltuner/helper/ops_runtime.py` (new) + registration, policy keys (commented defaults), tests + fake sysfs.
Goal: intent fields `caps {cpu: ratio}`, `parking {cpu: bool}`, `uncore {min_ratio, max_ratio}`; journal + revert; conflicts with stress/measure caps refused by name;
CPU0 and last-of-kind parking refused; E-cluster caps as 4 CPUs.
Accept: sim tests per rule; journal restore at helper start.

**G-217 · UI: Power, Ring & uncore, Ratios & parking** · S · deps G-207, G-212, G-214, G-216
Owns `desktop/app/ui/views/tune/{power,ring,runtime}.js` + tests; `lib/intent.js` extension (coordinate: G-207 must be merged).
Accept: Playwright (mock) per sub-tab; intents validate against the helper schema.

**G-223 · Telemetry: 0x6B0 relabel + 0x64F core limit reasons** · F · deps G-215
Owns `src/arltuner/telemetry/{msrio.py,cpu.py,sampler.py}`, `docs/knowledge/telemetry-sources.md`, `web/index.html` (label only), tests in `tests/telemetry/`.
Goal: 0x6B0 is the graphics word (G-215): rename labels/keys; add `limit_reasons` = `decode_core_64f(rdmsr 0x64F)` (+ `limit_reasons_gfx` from 0x6B0) to the
telemetry snapshot as `{raw, status, log, unknown_bits}`; keep `limit_reasons_raw` for compatibility one release. Accept: tests; suite green.

**H3–H5 · Hardware gates** · H — H3 ring row edit (one point, read back, restore); H4 PL1/PL2/tau write + revert; H5 one cap, one park/unpark, uncore
max −1 bin + revert. Each with operator go, evidence in `captures/lnx/desktop-h*.txt`, gate recorded in SPEC §5 table.

## D5 Remaining screens + native
**G-218 · Curves + Stress screens** · S · deps G-205 — Owns `desktop/app/ui/views/{curves,stress}.js` + tests. Accept: parity checklist items for both (mock).
**G-219 · Native integration + setup** · S · deps G-204 — Owns `desktop/app/src-tauri/src/{tray,settings,notify}.rs`, `desktop/app/ui/views/setup.js`.
Accept: tray status colours by helper state; settings persisted to `~/.config/arltuner-desktop/settings.json`; setup screen for missing/not-allowed/busy with copyable command.
**G-220 · Parity checklist run** · F · deps D2–D5 — walk `progress/PARITY.md` against the mock + sim, file gaps as fix tasks.

## D6 Release
**G-221 · Release build + docs** · S · deps all — build script polish, sha256, `docs/guide/desktop-app.md`, README section, screenshots (read-only),
install to `~/AppImages/arltuner.AppImage` on operator request.
**G-222 · Final review** · O — adversarial review of the whole desktop diff (Rust link, capabilities, CSP, new helper ops); operator acceptance.

**G-224 · Dashboard follow-ups** · S · deps G-206
Owns `desktop/app/ui/views/dashboard.js`, `components/coremap.js`, new `views/dashboard/{cores,sensors}.js`, `styles/views.css` (+ one `<link>` in index.html).
Goal: port the web Cores table + Sensors sub-views; mem.read on screen open (read-only MCHBAR); move constructed-stylesheet CSS into `styles/views.css`;
core map 8 P tiles in one row at 1280 px; calmer tile colours (heat scale on a dark base). Accept: Playwright (mock) + orchestrator screenshot.

**G-225 · Rust allowlist: power.* + runtime.*** · S · R · deps G-214, G-216
Owns `desktop/app/src-tauri/src/helper/ops.rs` (+ link.rs plan binding), tests. Goal: add `power.read/plan/apply` and `runtime.read/plan/apply` with arg-shape
checks; bind `power.apply` / `runtime.apply` to plan_ids this connection got from the matching `*.plan` (separate bounded sets). Accept: cargo tests (unknown keys refused, cross-kind plan id refused).

**G-226 · Helper: CPU-enforced limits only (D-049)** · M · R · deps G-216, G-225
Owns `src/arltuner/guard/{policy,guard,planner,encoding}.py`, `src/arltuner/controls/{power,caps,uncore}.py`, `src/arltuner/helper/ops_tune.py` (limit paths
only), `policies/default.toml`, their tests. Goal: remove every operator-policy VALUE limit listed in desktop/progress/logs/cpu-rules-audit.md §B; enforce
only CPU/kernel rules (§A rows 1, 3, 5, 6, 9 lock/fields, 10, 11) read at runtime, fail closed when a CPU limit is unread; demote non-CPU rules to warnings;
keep every PROCESS invariant. Expose the limits as `cpu_limits` (contract in notes). Accept: tests per removed limit (now allowed + warned) and per CPU rule
(refused at the CPU value, source cited); Opus review.

**G-227 · Bug: tiles/inspector don't show applied offsets until a re-read** · S · deps G-210
Owns `desktop/app/ui/views/tune/apply.js` (afterApply), `views/tune.js` / inspector render paths as needed, `src/arltuner/helper/ops_tune.py`
(`_record_apply`/`op_tune_last` only), tests. Reported live by the operator 2026-10-18. Accept: a test that fails before the fix and passes after, at the layer
where the bug is; Playwright smoke shows the applied value in tile + inspector right after Apply with no Read.
