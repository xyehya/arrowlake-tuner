# desktop/ — the arltuner desktop app (Tauri 2 AppImage)

Decision D-046 (2026-10-18). The primary front-end for arltuner becomes a native AppImage, inspired by Intel XTU 10's layout but covering
everything this project already does (every V/F point per P-core, E-cluster and ring). The root helper, guard, policy and CLI are unchanged
and stay Python stdlib; the AppImage is an unprivileged client of `/run/arltuner/helper.sock`.

| File | What | Changes |
|---|---|---|
| `PLAN.md` | vision, architecture, screen design, safety, phases, orchestration model | only when a decision changes (static) |
| `TASKS.md` | task catalog G-201+ with Owns / acceptance / model | only when scope changes (static) |
| `discovery/` | repo inventory, XTU 10 reference (images local only) | frozen inputs |
| `progress/STATUS.md` | live board: task status, current round, blockers | every round |
| `progress/ROUNDS.md` | append-only log of orchestration rounds | every round |
| `progress/PARITY.md` | web-GUI parity checklist | as features land |
| `progress/logs/` | worker prompts (tracked) and raw outputs (untracked) | per dispatch |
| `app/` | the Tauri project: `src-tauri/` (Rust), `ui/` (HTML/CSS/JS, no npm) | code |
| `tools/build-appimage.sh` | release build | code |

Build (once G-202 lands): `desktop/tools/build-appimage.sh` → `desktop/dist/arltuner-desktop-<ver>-x86_64.AppImage`.
Prerequisite on the machine: the helper installed with `sudo bash install.sh --add-user $USER` (the app shows this when it is missing).
