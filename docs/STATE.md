# STATE — live handoff (rewrite at end of every session; ≤ 80 lines)

_Updated: 2026-10-19 17:10 · lnx · G-236..G-241 merged + installed, AppImage rebuilt. Suite **3455 Python OK (74 skipped), node 367, cargo OK**
(`python3 tools/run_tests.py`). Operator is rebooting into Windows for unrelated tasks; resume on Linux with a fresh Re-read._

## Where we are
- **Desktop app (D-046):** Tauri 2 AppImage `desktop/dist/arltuner-desktop-0.0.1-x86_64.AppImage` (Dashboard, Tune [Cores/Turbo/Power/Ring & uncore/
  Ratio caps], Curves, Stress, Profiles, System, Audit) over the root helper socket (`arltuner-helper.socket`, D-043). Board: `desktop/progress/
  {STATUS,ROUNDS}.md`; tasks `desktop/TASKS.md`. The old browser GUI (`tools/arltuner-gui.py`) still exists but lacks power/runtime ops.
- **Safety model D-049:** no operator value policy; only CPU/kernel limits (0x01 ratio, 0x28 voltage, field encodings), read live, fail closed while unread.
- **Remote:** `github.com/xyehya/arrowlake-tuner` (`origin`, only `main`).

## Today (2026-10-19) — decisions and findings
- **D-050:** OC ratio and turbo tables are decoupled (no derived 0x1AD/0x650 raise; (i) notes on the OC field and Turbo ratios tab). G-236.
- **G-237:** a new OC point's V target lands a few ms after its ratio; the guard re-reads the P1=0 P2=0 word up to 4x (2/5/10/20 ms) — proven live
  (P0 58x verified after 1 re-read, 3.2 ms). **G-238:** OC point shown in the V/F table + chart. **G-239:** "Complete the OC ratio" for a half-applied OC.
- **G-240:** Undervolt Protection (UVP, Intel BIOS default) refuses negative offsets with rc 0x13 [MEASURED 13:12Z]; helper names it, remembers it per
  boot, refuses negative-offset plans at Review once seen; amber status chip. No read-only UVP detection exists (0x09 identical on/off).
- **G-241:** an OC point is created by ONE 0x11 word (ratio + adaptive target) — operator: a new point takes its own voltage; guard exemption only when
  nothing was sent to that core yet, ratio <= fused top < new ratio, and 0x1C[7:0] <= fused top (review f1). Review now raises apply-time PlanErrors;
  "Nothing was written" when no write went out.
- **Fused V/F curve read:** not possible via the mailbox on ARL-S (0x09 per-point floors carry ratio only, 0x07 unrecognised) [MEASURED] —
  `docs/knowledge/vf-fused-read.md`. Curves > Measure stays the method. Temporary allowlist entries were removed again.
- **E-core +1 ratio display** (kernel kHz one bin high) fixed in the UI (G-233); `power-profile-capping.md` §6.
- Live results before the BIOS reset: P0 58x single-thread holds 58x most of the time (dips = turbo buckets from background load); E 50x all-core
  1.175 V, 110 W, MCE 0. `captures/lnx/stress-*-20261019T12*.txt`.

## Machine state (reference 285K) — at 17:10
- **Operator reset the BIOS to defaults + XMP** (UVP on, PerCore Voltage Enable off at boot, E 0x1C 0x2E2E = 46x, P 57/57/55x6, PL1 250 / PL2 295 W).
  This boot: 0x15 PerCore enable was written (0x14 = 8); no offsets or OC words landed (UVP refusal, then a G-124 PlanError). Accepted state NOT applied
  (operator: "don't restore anything"). `accepted-20261015` + `baseline-20261019` profiles exist (undervolts: need UVP off in BIOS).
- Policy `/etc/arltuner/policy.toml` trimmed per D-049 (backups `.bak-d049-*`, `.bak-fused-20261019T090226`). Helper installed (21/21 self-checks).
- MCE 0 all day.

## Next (on return to Linux)
1. Fresh Re-read (Windows/XTU may have written OC words).
2. Retry the operator's E-clusters draft: OC 50x @ 1.1846 V → Review must show ONE 0x11 word per E CPU then 0x1D/0x27, no turbo write (raise the E
   table on the Turbo ratios tab separately). Watch for the G-237 re-read in the audit.
3. With UVP on: one negative P offset → apply refused with the UVP message + chip; a second negative plan refused at Review.
4. Optional: EPP `performance` on P0 to explain the 57x samples without a limit-reason bit.

## Open / backlog
- CLI planner lacks a P `TURBO_BELOW_OC` warning (G-236 review f3). UI: mock still shows E 49x (re-record fixtures).
- WebKitGTK AT-SPI crash risk when driving the window via AT-SPI: drive by desktop coordinates only (orchestrator skill).
- Windows restore path untested on Windows; restore scripts still run from the checkout (ROADMAP). G6 autostart, G7 portability.

## How we work
Orchestrator + Sonnet workers in worktrees (`docs/ORCHESTRATION.md`, `desktop/tools/dispatch.sh`); Opus read-only reviews for helper/guard
changes; workers never touch hardware. Decisions in `docs/DECISIONS.md` (latest D-050).

## Tools
AppImage above · CLI `python3 tools/arltuner.py …` · `tools/lnx/restore_tuning.sh` · tests `python3 tools/run_tests.py`.
