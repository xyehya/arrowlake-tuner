# arrowlake-tuner — user guide (Linux, Intel Core Ultra 200S)

**Supported:** Arrow Lake-S desktop (Core Ultra 200S; CPU family 6, model 198) on Linux. Anything else is refused or read-only. Everything the page shows
comes from your machine, read live (CPU, topology, V/F words, favored cores, limits); the repository ships no tuning, policy or measurements of any chip.

An XTU-style tuner for Arrow Lake on Linux: see the machine, measure its V/F curves, tune offsets / adaptive voltage / OC ratios / turbo tables,
validate with stress screens, keep what works. Everything privileged runs in a root helper started through polkit; every write goes through a
guard, one mailbox transaction at a time, with read-back and an audit log. **A cold boot reverts every runtime change.**

## Install and start
```
sudo bash install.sh                 # code to /usr/lib/arltuner, a generic policy to /etc/arltuner (only if absent), state to /var/lib/arltuner; self-check must pass
python3 tools/arltuner-gui.py --open # one polkit password prompt; the browser opens the dashboard (one-time login link)
```
A fresh install has **no accepted state**: that is yours, created in the app (Profiles > Set as accepted, below). Re-running `install.sh` keeps your
accepted state and your `/etc/arltuner/policy.toml` (`--reseed-config` replaces only the policy and `helper.toml`).

New login link for a running GUI: run the same command again. Undo: `sudo bash uninstall.sh` (keeps policy/audit) or `--purge --yes-reset-budget`.

## The tabs, in the order you use them
| Tab | What you do there |
|---|---|
| **Dashboard** | Live core map (clock / busy / temp / VID), package & core power, board Vcore, DRAM bandwidth, memory card (MT/s, gear, command rate, full trained timings, DIMM temps), refresh slider (0.25–5 s), pause, chart window |
| **Curves** | **Measure V/F curve** — one curve per P-core and per E-cluster (E-cores share one regulator per cluster of four). Quick (fused points, ~2 min) or Dense (every ratio, ~12 min). Caps each unit to each ratio, loads it, records the requested VID; no mailbox traffic; caps restored afterwards. Mark one as the **stock baseline** (measured with all offsets at 0 = the fused table as the CPU applies it). Chart (overlay, toggle units, compare two curves, Δ mV), table, library |
| **Tune** | **Read tuning words** first (≈ 124–136 mailbox reads; the page keeps the last read of this boot). Then per unit (one per P-core, one per E-cluster, as your CPU has them) or Simple (all P / all E): voltage offset whole-curve or per point (−200…+200 mV), adaptive target on the **OC point** (the point after the last fused one), OC ratio, turbo tables. Prediction chart + VccIA panel; **Review** shows before/after, warnings, ops → **Apply** → read-back. Reset offsets to 0, Restore accepted (disabled until an accepted state exists) |
| **Stress** | CPU (integer / vector-math / cache) or memory (stressapptest) screen, optional ratio cap, 1–60 min. Errors, achieved clocks, MCE count, health. *A quick screen is not an acceptance gate.* |
| **Profiles** | The accepted state, saved profiles, **Set as accepted**, compare, Load into Tune, Review and apply |
| **System / Audit** | Preflight checks, helper state, mailbox ops this boot, audit log tail |

## Arrow Lake rules the app enforces or explains (SPEC-v3 §9)
- **Fixed V/F points:** each P-core has 8 fused points and each E-cluster 6 (offset only); the ratios are read from your CPU. The **OC point** (the point
  after the last fused one) exists only when an OC ratio above the top fused ratio is set; it carries the adaptive target.
- **Monotonic clamp:** a point can never be below the previous one — a deeper offset is clamped up (shown as "clamped to Vn−1").
  Ratios between points interpolate; an E OC target below the last fused point is clamped to it.
- **Favored cores:** the P-cores your CPU reports as favored (highest ACPI CPPC performance) reach the top ratio with few active cores; the others top out lower.
  E-cores above their last fused point run on that point's voltage.
- **Turbo tables** must not increase with more active cores (refused otherwise).
- **VccIA** (board Vcore) ≈ highest allowed VID of any unit or the ring + ~0.1 V: lowering a unit that doesn't set the maximum won't lower Vcore.
- **Be careful with E-cores:** very low voltage on the lower E points and high E voltages can produce errors and WHEA reports on some chips.

## A first tuning session
1. Cold boot → Curves: Quick, name it "stock", mark baseline.
2. Tune: Read tuning words → pick a unit and one point, e.g. −20 mV → Review → Apply → read-back matches.
3. Curves: Measure again (that unit only) → compare to the baseline (Δ mV).
4. Stress: CPU 5–15 min on the changed units, then your real gate (e.g. Linpack 60 min + SSE 60 min) before calling it stable.
5. Profiles: Save the settings, and when they are the ones you trust **Set as accepted** (the profile must already be applied; the helper writes
   `/etc/arltuner/accepted` from what the CPU holds and asks you to confirm what it replaces). After every boot use **Restore accepted** (or
   `tools/lnx/restore_tuning.sh`, which reads `/etc/arltuner/accepted`; `--from DIR` names another). The dashboard then compares the CPU with it.

## Limits (D-032, operator policy `/etc/arltuner/policy.toml`)
Offsets ±200 mV; the adaptive-target and ratio ceilings are `"auto"`: read from your CPU in the session (its own voltage maximum, its fused ratios), never
from a table. The file is yours: edit it to allow more (for example a fixed E ratio); `install.sh` never overwrites it. The op counter is an indicator.
Kept regardless: one mailbox transaction at a time, guard allowlist, health check, read-back, audit log.
