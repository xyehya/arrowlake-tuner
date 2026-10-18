# desktop — ROUNDS (append-only)

## R0 · 2026-10-18 · discovery
- Unsloth AppImage = Tauri 2.12 + wry/WebKitGTK 4.1, linuxdeploy (gtk + gstreamer hooks), plugins single-instance, window-state, notification, ...
- D1 inventory (Sonnet) -> `discovery/repo-inventory.md`; XTU 10.0.1.188 research -> `discovery/xtu-reference.md`.
- Measured apply latency: 1 ms/transaction, 194 ms overhead between transactions; audit log 15.9 MB (`docs/knowledge/apply-latency.md`).
- Operator answers -> D-046 (Tauri 2 + vanilla JS, Rust speaks the socket, new controls: power/ring+uncore/caps+parking, helper speed-up, downloads ok).
- tauri-cli 2.12.3 installed.

## R1 · 2026-10-18 · dispatch D0 + independent backend tasks
- G-202, G-211, G-212, G-215 dispatched as Claude Code Sonnet 5.5 sessions in worktrees.
- R1 results: G-202 done (scaffold; AppImage built by orchestrator because the worker's allowlist missed `desktop/tools/*` — fixed); G-215 done
  (0x64F/0x6B0 decode; 0x6B0 = graphics); G-211 done (2.8 ms/op overhead, was 211.8) -> Opus review; G-212 committed, finishing.

## R2 · 2026-10-18
- Merged G-215, G-202; main suite OK. Dispatched G-204 (Rust link, S), G-205 (UI shell, S), G-203 (test hooks, deepseek-flash); G-211 Opus review running.
- G-211 Opus review: APPROVE WITH FIXES (med: same-size mid-log rewrite undetected by a long-running helper; low: anchor bytes source, anchor
  top-up, tests). Fix round dispatched to Sonnet. Session-held lock + pacer change deliberately not done (lock 0.05 ms/op; pacer already 0 at dwell 0).
- G-212 done (ring intent, intent_features ["ring"], 3031 OK; one unexplained earlier failure) -> Opus review (PLAN_STALE does not cover ring words).
- G-212 Opus review: APPROVE WITH FIXES — no unsafe write path (P2=0 blocked 3x, P1 always 2, policy + guard re-check); med: ring not PLAN_STALE-checked
  (end state = confirmed "after" but the shown "before"/diff can be wrong); low: point-key parsing (non-ASCII digits -> INTERNAL, "04"/"4" dup).
  Two full runs OK (3031); the worker's flaky failure not reproduced (only a logged ConnectionResetError in a web test thread). Fix round dispatched.
  Note for G-213/G-217: profiles.save refuses ring, so a ring edit can be saved only after it is applied.

## R3 · 2026-10-18 · D-047 standing autonomy
- G-211 merged (Opus fixes verified). Installed (`sudo install.sh`, 21/21). **H2 live:** gap 194 -> 5 ms; tune.read 1.06 s (155 ops); after-reboot
  restore of the accepted state 1.85 s (311 ops, 6 writes, 6/6 read back); accepted 13/13; MCE 0. (A reboot ~08:49 had reset P0 pts 5-8 + ring 8/9.)
- G-204 committed (49 cargo tests); orchestrator wired the commands into build.rs + capability; Opus review running.
- Dispatched G-214 (power backend, S) and G-223 (telemetry relabel + 0x64F, deepseek-flash). G-216 waits for G-214 (shared ops/policy files).
- G-203 merged (deepseek-flash; node --test + cargo test in run_tests.py; Node 24 needs explicit file list). Main: 3054 OK + node 2 + cargo 5.
- G-212 merged + installed; **H3 passed** (ring P2 7 −5 mV in 0.06 s, read back, reverted by restore 1.83 s, 13/13, MCE 0). G-204 Opus: APPROVE WITH FIXES (low) -> fix round.
- G-205 merged (UI shell; node 36, smoke 11; screenshot checked by orchestrator). Orchestrator error: `pkill -f 'http.server 8765'` killed its own
  shell (self-match), so the merge + notes did not happen while G-206/G-207 were already dispatched; both stopped within seconds (no work), worktrees
  removed, redone correctly. dispatch.sh now refuses a missing notes file. Rule: kill by PID (ss/lsof), never pkill -f with a string in the command.
- G-223 merged (deepseek-flash) + installed; live decode of 0x64F OK (idle: no active limit; sticky log thermal/VR/PL1/PL2/max-turbo). Readings may hold a decoded dict.
- G-204 merged (Opus fixes; release guard verified). AppImage from main built. **H1 passed** live: helper connected · writes allowed · accepted matches.
- G-207 merged (tune logic lib; node 88; golden intents == helper canonical_intent). Dispatched G-208 (cores grid + inspector, S) and G-209 (turbo sub-tab, deepseek-flash) on a shared mount(el,ctx) sub-tab contract. Follow-up: tune.read should expose the ring policy window.
- G-206 merged + wired into app.js by orchestrator (views were not routed: app.js outside Owns). Dashboard verified by screenshot. G-224 follow-ups filed.
- G-214 done (power.read/plan/apply as separate ops sharing PlanStore nonce semantics — deliberate deviation from 'intent field'; UI drawer applies tune + power plans in sequence). 82 tests, suite 3137. Opus review running (guard bypass for powercap, journal trust, audit cost).
- G-209 merged (turbo sub-tab, deepseek-flash, 18 tests). Needs <link> styles/turbo.css + tune view wiring at G-208 merge.
