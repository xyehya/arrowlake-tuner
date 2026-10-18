# desktop — ROUNDS (append-only)

## R0 · 2026-10-18 · discovery
- Unsloth AppImage = Tauri 2.12 + wry/WebKitGTK 4.1, linuxdeploy (gtk + gstreamer hooks), plugins single-instance, window-state, notification, ...
- D1 inventory (Sonnet) -> `discovery/repo-inventory.md`; XTU 10.0.1.188 research -> `discovery/xtu-reference.md`.
- Measured apply latency: 1 ms/transaction, 194 ms overhead between transactions; audit log 15.9 MB (`docs/knowledge/apply-latency.md`).
- Operator answers -> D-046 (Tauri 2 + vanilla JS, Rust speaks the socket, new controls: power/ring+uncore/caps+parking, helper speed-up, downloads ok).
- tauri-cli 2.12.3 installed.

## R1 · 2026-10-18 · dispatch D0 + independent backend tasks
- G-202, G-211, G-212, G-215 dispatched as Claude Code Sonnet 5.5 sessions in worktrees.
