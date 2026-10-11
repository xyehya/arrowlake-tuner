# arrowlake-tuner — user guide (Linux, Core Ultra 200S / 285K)

An XTU-style tuner for Arrow Lake on Linux: see the machine, measure its V/F curves, tune offsets / adaptive voltage / OC ratios / turbo tables,
validate with stress screens, keep what works. Everything privileged runs in a root helper started through polkit; every write goes through a
guard, one mailbox transaction at a time, with read-back and an audit log. **A cold boot reverts every runtime change.**

## Install and start
```
sudo bash install.sh                 # code to /usr/lib/arltuner, policy to /etc/arltuner, state to /var/lib/arltuner; self-check must pass
python3 tools/arltuner-gui.py --open # one polkit password prompt; the browser opens the dashboard (one-time login link)
```
New login link for a running GUI: run the same command again. Undo: `sudo bash uninstall.sh` (keeps policy/audit) or `--purge --yes-reset-budget`.

## The tabs, in the order you use them
| Tab | What you do there |
|---|---|
| **Dashboard** | Live core map (clock / busy / temp / VID), package & core power, board Vcore, DRAM bandwidth, memory card (MT/s, gear, command rate, full trained timings, DIMM temps), refresh slider (0.25–5 s), pause, chart window |
| **Curves** | **Measure V/F curve** — 12 curves (8 P-cores + 4 E-clusters; E-cores share one regulator per cluster of four). Quick (fused points, ~2 min) or Dense (every ratio, ~12 min). Caps each unit to each ratio, loads it, records the requested VID; no mailbox traffic; caps restored afterwards. Mark one as the **stock baseline** (measured with all offsets at 0 = the fused table as the CPU applies it). Chart (overlay, toggle units, compare two curves, Δ mV), table, library |
| **Tune** | **Read tuning words** first (≈ 124–136 mailbox reads; the page keeps the last read of this boot). Then per unit (P0–P7, EC0–EC3) or Simple (all P / all E): voltage offset whole-curve or per point (−200…+200 mV), adaptive target on the **OC point** (P point 9 / E point 7), OC ratio, turbo tables. Prediction chart + VccIA panel; **Review** shows before/after, warnings, ops → **Apply** → read-back. Reset offsets to 0, Restore accepted |
| **Stress** | CPU (integer / vector-math / cache) or memory (stressapptest) screen, optional ratio cap, 1–60 min. Errors, achieved clocks, MCE count, health. *A quick screen is not an acceptance gate.* |
| **Profiles** | The accepted chain, saved profiles, compare, Load into Tune, Review and apply |
| **System / Audit** | Preflight checks, helper state, mailbox ops this boot, audit log tail |

## Arrow Lake rules the app enforces or explains (SPEC-v3 §9)
- **Fixed V/F points:** P-cores 8, 20, 30, 48, 53, 55, 56, 57× (points 1–8, fused, offset only); E-clusters 8, 15, 23, 30, 40, 46× (points 1–6).
  The **OC point** (P point 9 / E point 7) exists only when an OC ratio above the top fused ratio is set; it carries the adaptive target.
- **Monotonic clamp:** a point can never be below the previous one — a deeper offset is clamped up (shown as "clamped to Vn−1").
  Ratios between points interpolate (54× = midpoint of 53/55×); an E OC target below point 6 is clamped to point 6.
- **Favored cores:** P0/P3 reach 57× with ≤ 2 active cores; the other P-cores top out at 55×. E-cores run 47–48× on point 6's voltage.
- **Turbo tables** must not increase with more active cores (refused otherwise).
- **VccIA** (board Vcore) ≈ highest allowed VID of any unit or the ring + ~0.1 V: lowering a unit that doesn't set the maximum won't lower Vcore.
- **Do not offset E 40×** (errors observed); E above ~1.2 V draws WHEA reports.

## A first tuning session (what the reference machine did)
1. Cold boot → Curves: Quick, name it "stock", mark baseline.
2. Tune: Read tuning words → choose e.g. P7 point 6 (55×) −20 mV → Review → Apply → read-back matches.
3. Curves: Measure again (P7 only) → compare to baseline (Δ mV).
4. Stress: CPU 5–15 min on the changed units, then your real gate (e.g. Linpack 60 min + SSE 60 min) before calling it stable.
5. Profiles: save; after every boot use Restore accepted (or `tools/lnx/restore_tuning.sh`).

## Limits (D-032, operator policy `/etc/arltuner/policy.toml`)
Offsets ±200 mV, adaptive target ≤ hardware max (P 1.625 V, E 1.355 V), ratio ≤ P 57 / E 49, op counter is an indicator. Kept regardless: one
mailbox transaction at a time, guard allowlist, health check, read-back, audit log.
