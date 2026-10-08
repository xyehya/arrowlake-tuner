# Task index (G1 written just-in-time; later phases get tasks when G1 is done)

| ID | Title | Host | Tier | Status | Depends on |
|---|---|---|---|---|---|
| G-101 | platform file `platforms/arl-s-285k.toml` + loader; preflight checks (SPEC §3) | lnx | T0 | **merged** 2026-10-08 | — |
| G-102 | root helper: JSON-lines protocol, state machine, lock, read-only ops (`preflight`, `vf.dump` via guard, `audit.tail`) | lnx | T0 build | **merged** 2026-10-08 | G-101 |
| G-103 | web server: bootstrap token → cookie, Host/Origin/CSP, static manifest, SSE; security test suite (SPEC §4.2, §12) | lnx | T0 | **merged** 2026-10-08 | — |
| G-104 | telemetry readers (APERF/MPERF, VID, temps, RAPL, hwmon Vcore, perf IMC) + `docs/knowledge/telemetry-sources.md` + allowlist rows | lnx | T0 build (fake sysfs/MSR) | **merged** 2026-10-08 | — (parallel to G-101; uses `topology/`) |
| G-105 | dashboard page (plain JS): live table, preflight, applied-vs-accepted, ops budget | lnx | T0 | **merged** 2026-10-08 | G-102, G-103, G-104 |
| G-106 | install layout: `install.sh`/`uninstall.sh`, polkit action, root-owned paths; CLI audit/lock path move | lnx | T0 build / T1 install | in progress | G-102 |
| G-107 | helper: budget in heartbeat, `accepted_match` in `vf.dump` (contract 8 addendum) | lnx | T0 | in progress | G-102 |

Tiers: T0 = no hardware; T1 = privileged read-only on the real machine (orchestrator); T2/T3 = writes (human + orchestrator, SAFETY.md).
