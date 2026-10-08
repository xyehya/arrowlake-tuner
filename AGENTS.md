# arrowlake-tuner — agent rules (canonical; CLAUDE.md imports this)

**Goal:** a Linux app (local web GUI + CLI) that READs telemetry from, and WRITEs tuning controls to, an Intel Core Ultra 9 285K (Arrow Lake-S) on
an ASRock Z890 Steel Legend WiFi — per-core V/F point offsets and target voltages, OC ratios, turbo ratio tables — replicating Intel XTU at runtime.
Windows: PowerShell restore of the accepted state only. Spec: `docs/SPEC.md`. Predecessor (research, raw evidence): `~/AI/linuxmon` (`docs/PROVENANCE.md`).

**Roles and autonomy:** `docs/ORCHESTRATION.md` (orchestrator runs the project end to end with Sonnet workers; what it decides alone vs asks).

## Session start (stop as soon as you have enough)
1. `docs/STATE.md` — done / in-flight / next / blockers / open decisions. Trust it over memory.
2. Your task file `docs/tasks/G-NNN-*.md` (if assigned); read only what its **Read first** lists.
3. Facts: `docs/knowledge/` (registers.md is the single source of truth for encodings). Do not re-research what is there; for raw evidence go to
   the predecessor path named in `docs/PROVENANCE.md`, read-only.

## Session end
Commit · rewrite `docs/STATE.md` (≤ 80 lines) · append to `docs/DECISIONS.md` only for real decisions · findings to `docs/knowledge/<topic>.md`,
never only in chat · `git push` (remote in STATE).

## Hosts
`lnx` = Linux (Fedora on the reference machine): build, test, GUI, hardware experiments. `win` = Windows 11 on the same machine: PowerShell restore
(`tools/win/`). Every task/finding declares its host. Cross-host handoff via git only.

## Hardware safety — non-negotiable (full protocol: `docs/SAFETY.md`)
- Only the **human** and the **orchestrator session** perform hardware WRITES (`wrmsr`, OC-mailbox requests, MMIO/PCI writes, BIOS settings,
  driver load, cpufreq caps). **Worker/worktree agents NEVER do** — they work against the simulator and fake sysfs only.
- The orchestrator needs an explicit go-ahead in chat for each write sitting. Every mailbox request — reads too — is a WRMSR.
- Mailbox traffic: one transaction at a time, never a sweep (`docs/SAFETY.md`, 2026-08-26 incident). Reads: allowlisted registers only.
- Never widen the policy (55 mV max change per write, −100 mV floor, per-boot op cap 600) without the operator's yes. Never work around a guard refusal.
- Never download or install software/drivers without naming it (name, source, size) and a yes. OCCT never elevated. The Windows ThrottleStop
  driver is carved from the operator's own exe, never committed, stopped after applying.
- The operator's monitor repos `~/AI/turbostat` and `~/AI/turbostat+` are **orchestrator-only, read-only**; workers never get access; facts come in
  via `docs/knowledge/`.
- Root helper code is never run from a user-writable path (SPEC §4.4).

## Evidence rules
Tag every hardware claim: `[MEASURED host date]` `[DECOMPILED file]` `[DOC url]` `[COMMUNITY url]` `[UNVERIFIED]`. Only MEASURED / DECOMPILED /
DOC facts become constants in code. Register/mailbox encodings live in `docs/knowledge/registers.md`; code cites the entry.

## Working conventions
- Python ≥ 3.12, standard library only (D-012); the web page is plain HTML/CSS/JS shipped in the repo, no CDN, no npm.
- One task = one branch `task/G-NNN-slug` = one worktree; tasks own disjoint files (**Owns**). Commits `G-NNN: summary`. LF everywhere.
- `python3 tools/run_tests.py` green before any merge. Raw captures go to `captures/` (only `captures/lnx|win` small text logs are tracked).
- Don't create files speculatively; task files are written just-in-time for the next phase only.
- Legacy names kept on purpose (shared with the predecessor CLI so the per-boot op cap and lock stay machine-wide): audit log
  `~/.local/state/linuxmon/audit.jsonl` (Windows `%LOCALAPPDATA%\linuxmon\`), lock `/run/lock/linuxmon-mailbox.lock`. SPEC OD-1 moves them.

## How the operator works
Short honest status, tables, one variable at a time; "proceed"/"lets go" = start. Design work: raise the decisions that change the design, take a
position, get alignment before building; adversarial review of designs (Sonnet reviewer) with honest reporting, including own errors.
