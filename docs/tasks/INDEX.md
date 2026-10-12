# Task index (G1 written just-in-time; later phases get tasks when G1 is done)

| ID | Title | Host | Tier | Status | Depends on |
|---|---|---|---|---|---|
| G-101 | platform file `platforms/arl-s-285k.toml` + loader; preflight checks (SPEC §3) | lnx | T0 | **merged** 2026-10-08 | — |
| G-102 | root helper: JSON-lines protocol, state machine, lock, read-only ops (`preflight`, `vf.dump` via guard, `audit.tail`) | lnx | T0 build | **merged** 2026-10-08 | G-101 |
| G-103 | web server: bootstrap token → cookie, Host/Origin/CSP, static manifest, SSE; security test suite (SPEC §4.2, §12) | lnx | T0 | **merged** 2026-10-08 | — |
| G-104 | telemetry readers (APERF/MPERF, VID, temps, RAPL, hwmon Vcore, perf IMC) + `docs/knowledge/telemetry-sources.md` + allowlist rows | lnx | T0 build (fake sysfs/MSR) | **merged** 2026-10-08 | — (parallel to G-101; uses `topology/`) |
| G-105 | dashboard page (plain JS): live table, preflight, applied-vs-accepted, ops budget | lnx | T0 | **merged** 2026-10-08 | G-102, G-103, G-104 |
| G-106 | install layout: `install.sh`/`uninstall.sh`, polkit action, root-owned paths; CLI audit/lock path move | lnx | T0 build / T1 install | **merged** 2026-10-08 | G-102 |
| G-107 | helper: budget in heartbeat, `accepted_match` in `vf.dump` (contract 8 addendum) | lnx | T0 | **merged** 2026-10-08 | G-102 |
| G-108 | uninstall keeps lock dir while `/etc/arltuner` stays; CLI refuses on missing lock dir | lnx | T0 | **merged** 2026-10-08 | G-106 |
| G-109 | telemetry gaps from the first real GUI run: Vcore (nct6686 VIN16), DRAM perf wiring, temp names, mode header | lnx | T0 | **merged** 2026-10-11 | G-104/105/107 |
| G-110 | dashboard redesign: futuristic, tabbed, diagrams, interactive (Opus + frontend-design) | lnx | T0 | **merged** 2026-10-11 | G-105 |
| G-111 | helper: V/F curve measurement (cpufreq caps + VID), curve store, mem.read (MCHBAR), telemetry restart | lnx | T0 | **merged** 2026-10-11 | SPEC-v3 |
| G-112 | D-032 advanced policy + tune ops (read/plan/apply/reset/restore) + profiles store | lnx | T0 | **merged** 2026-10-11 | SPEC-v3 |
| G-113 | web v3: Dashboard+memory+slider, Curves, Tune, Profiles | lnx | T0 | **merged** 2026-10-11 | SPEC-v3 |
| G-114 | backend CPU facts (SPEC-v3 §9): sim tables, fused/turbo in tune.read, clamp/VccIA/turbo order in plan, measure reasons, mem fix | lnx | T0 | **merged** 2026-10-11 | v3 |
| G-115 | UI CPU facts: real point grid + OC point, clamp badges, turbo editor, VccIA panel, fused markers, warnings | lnx | T0 | **merged** 2026-10-11 | v3 |
| G-116 | helper stress/validation ops (stress-ng cpu, stressapptest memory, ratio cap, health bracket, results) | lnx | T0 | **merged** 2026-10-11 | v3 |
| G-117 | turbo-only plans (planner), UI on warning codes, sim offsets at stock | lnx | T0 | **merged** 2026-10-11 | G-114/G-115 |
| G-118 | measure fixes: max ratio from turbo tables, retry pass, baseline/stock tagging, fused_point labels | lnx | T0 | **merged** 2026-10-11 | G-114 |
| G-119 | Stress tab + one-click Apply + chart spans + profile/turbo/msr_writes gaps | lnx | T0 | **merged** 2026-10-11 | G-116/G-117 |
| G-120 | planner fixes from real writes: OC word shape, CLAMPED warnings, OC prediction, stock rule, accepted equivalence | lnx | T0 | **merged** 2026-10-11 | G-119 |
| G-121 | stress fixes from real run: user-writable temp/cwd, child abort reason in error_lines | lnx | T0 | **merged** 2026-10-11 | G-116 |
| G-122 | G-120 follow-ups: revert word shape, E whole-curve without OC point, caveat text, durable tuned_this_boot | lnx | T0 | **merged** 2026-10-11 | G-120 |
| G-123 | UX continuity: last read per boot (tune.last), profiles.get + compare/load, indicator budget, defaults | lnx | T0 | **merged** 2026-10-11 | G-121 |
| G-124 | no-OC-point OC words always target 0; plan steps = sent ops | lnx | T0 | **merged** 2026-10-12 | G-122 |
| G-125 | final polish: OC point in Simple grid, current caveat texts, no false 'not read' warning, unit cell | lnx | T0 | **merged** 2026-10-12 | G-124 |
| G-126 | OC ratio raise lifts scaling_max_freq (and lowers on revert), respecting user caps and journals | lnx | T0 | **merged** 2026-10-12 | G-125 |
| G-127 | Tune page live truth (D-036): plain button names, read time, favored ★ live, reach display, labelled predictions, no lore | lnx | T0 | **merged** 2026-10-12 | — |
| G-128 | backend live topology/limits/points (D-036): sysfs topology + favored, live point ratios/turbo/limits, no 285K fingerprint, lore out | lnx | T0 | **merged** 2026-10-12 | — |
| G-129 | repo/install de-personalisation (D-036): user-owned accepted state, author profiles/policies/captures out | lnx | T0 | proposed | G-128 |
| G-130 | web follow-ups: apply race, preflight warn, VccIA nulls, unit lists from topology | lnx | T0 | **merged** 2026-10-12 | G-127 |

Tiers: T0 = no hardware; T1 = privileged read-only on the real machine (orchestrator); T2/T3 = writes (human + orchestrator, SAFETY.md).
