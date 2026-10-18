# desktop — STATUS (live board; rewrite each round, ≤ 60 lines)

_Round 2 · 2026-10-18 · orchestrator Hermes (Opus 5.5)_

| Task | Model | State | Notes |
|---|---|---|---|
| G-201 decision + docs | O | **merged** | 90de1c0 |
| G-202 Tauri scaffold + AppImage | S | **merged** 98424ee | AppImage 0.0.1 builds (114 MB, AppRun + gtk hook, no Python), `--version` OK; cargo 5/5 |
| G-215 limit-reason decode | S | **merged** 0107311 | 0x6B0 is GRAPHICS (msr-index.h + SDM), not package — relabel follow-up below |
| G-211 helper speed-up | S (R) | **in Opus review** | bench 211.8 -> 2.8 ms/op overhead on 16 MB log; suite 3040 OK |
| G-212 ring intent | S (R) | running | committed 9e15518, worker finishing tests |
| G-204 Rust helper link | S (R) | dispatched R2 | |
| G-205 UI shell + bridge | S | dispatched R2 | |
| G-203 test hooks | F | dispatched R2 (deepseek-flash) | |
| others | — | waiting on deps | TASKS.md |

Toolchain: rustc 1.96, tauri-cli 2.12.3, webkit2gtk4.1 2.54.1, node 24 (tests only). Blockers: none.
Follow-ups (not yet tasks): relabel 0x6B0 as graphics in `telemetry/msrio.py`, `cpu.py`, `telemetry-sources.md`, `web/index.html`; add a 0x64F
reader to telemetry (core limit reasons for the strip); `apply-latency.md` after-numbers once G-211 is merged + H2 measured.
Main suite: OK (skipped=74), 6m24s after the G-202 merge.
Lessons: dispatcher allowlist must include `Bash(desktop/tools/*)` (fixed); never `pgrep -f` a string that is in the polling command itself.
Known dependency advisory (accepted, tracked): Dependabot #1 / RUSTSEC-2024-0429 — glib 0.18 `VariantStrIter` unsoundness (fixed in 0.20). Pinned by
Tauri 2's gtk-rs 0.18 stack (every Tauri 2 Linux app, incl. the Unsloth AppImage); our code never calls VariantStrIter. Re-check at each Tauri bump.
