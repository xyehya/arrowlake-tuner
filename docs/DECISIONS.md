# Decisions (append-only; one short entry each)

> Carried over 2026-10-08 from `linuxmon@b01f78a` (`~/AI/linuxmon`). Paths `docs/research/*`, `docs/tasks/T-0xx` and `captures/*` cited here live in that repo (see `docs/PROVENANCE.md`); "linuxmon" = this project's former name.

Format: `D-NNN · date · decision — why`. Supersede by adding a new entry, not editing.

- **D-001 · 2026-09-30 · Two-host model.** `win` (Windows + XTU) does recon; `lnx` (Linux, same machine) builds and
  tests, with Claude Code running on both. Git is the only handoff channel. — Keeps recon and implementation
  separate and every session resumable from the repo.
- **D-002 · 2026-09-30 · Hardware writes: human + orchestrator only.** Worker/worktree agents never write
  hardware. Per-session go-ahead in chat. — OCCT already tainted a kernel; MSR/mailbox writes can hang the box.
  Protocol in `docs/SAFETY.md`.
- **D-003 · 2026-09-30 · Milestone 1 scope.** Per-core V/F offsets (P and E) + core max / turbo ratio limits.
  Power limits and ring/NGU are later. — Highest-value gaps in the BIOS.
- **D-004 · 2026-09-30 · Docs-as-handoff.** `AGENTS.md` is canonical (CLAUDE.md imports it); `docs/STATE.md` is the
  single live handoff; tasks are written just-in-time; findings are evidence-tagged. — Continuity without
  context bloat.
- **D-005 · 2026-09-30 · LF everywhere.** `.gitattributes` forces LF; repo-local `core.autocrlf=false`. — Repo is
  consumed on Linux.
- **D-006 · 2026-09-30 · Language of the app: OPEN.** Existing reader code is Python; the control layer's language is
  decided after the register/mailbox mechanics are known.
- **D-007 · 2026-09-30 · Sync via private GitHub.** The existing Fedora readers repo and this repo travel between hosts
  through private GitHub remote(s). Repo names/URLs TBD; creating a remote and pushing needs the human's go-ahead each time.
- **D-008 · 2026-09-30 · Third-party tuning binaries are static-analysis only.** ThrottleStop (and any similar tool with
  its own kernel driver) is analysed offline under git-ignored `captures/thirdparty/`; agents never launch it. Launching it
  is a human T2 action.
- **D-009 · 2026-09-30 · ~~M1 voltage semantics~~ — SUPERSEDED by D-016 (2026-10-01): its "undervolt-first / no overvolt / ratio at or below stock" limit is removed; its hardware facts (only the OC-ratio point takes an adaptive voltage, fused points take offsets only) still hold.** Original text (from the user): On Arrow Lake only the OC-ratio point takes an adaptive voltage; fused lower points accept offsets only. M1 therefore exposes per P-core / per E-cluster:
  ratio limit, adaptive OC-point voltage, offset (primary undervolt control). Static/override mode, Power-Gate mode and absolute voltages for fused points are out of scope; M1 is undervolt-first (no overvolt).
  Per-point offsets are a stretch goal pending a measured probe. — Matches the hardware topology (`docs/research/arl-voltage-topology.md`) and XTU's own controls.
- **D-010 · 2026-09-30 · turbostat+ relationship and access.** The operator's `turbostat+` (read-only monitor) and linuxmon (tuner) are separate projects; linuxmon borrows facts and decoders with attribution. Access to the copied repo is
  **orchestrator-only, read-only**; workers never touch it. Its read-only / no-Tier-4 rules are not inherited here. Open for the operator: whether linuxmon later imports its read layer or consumes its captures.
- **D-011 · 2026-09-30 · Mailbox discipline.** Mailbox transactions are single, human-attended, bracketed by health snapshots with a dwell; no sweeps. — 2026-08-26 incident (`docs/SAFETY.md`).
- **D-012 · 2026-09-30 · App language: Python ≥ 3.12, standard library only** (resolves D-006). Same language as the operator's readers/turbostat+; stdlib-only keeps privileged code auditable; `unittest` (no pytest install); `tomllib` for profiles.
  The mailbox layer is isolated so a later port is possible. Set by the orchestrator on the operator's "proceed"; revisit on request.
- **D-013 · 2026-09-30 · Build the core on Windows against a simulator; hardware later.** Phase P1 (codec → topology/profiles → MSR port + simulator + transport → guard → controls/CLI) needs no hardware and runs in worktrees on `win`; Linux bring-up (T-007) follows.
  T-003 (Linux baseline) is **deferred**: the operator reported the facts it would establish (kernel 7.2, Secure Boot off, `msr` present, OC unlocked, UVP disabled, OCCT only runs when opened manually); its flag-MSR reads fold into the T-007 pre-flight.
- **D-014 · 2026-10-01 · Verify on Windows first, Linux last (from the operator).** Before linuxmon sends any hardware traffic on Fedora: (1) derive from the Windows tools (XTU/IPF, ThrottleStop, decompiles) the complete list of reads/writes — mailbox, MSR, MMIO, PCI — the product needs (the **access manifest**, T-017);
  (2) observe XTU performing them live on Windows by a **passive trace** of its IPF IOCTL stream (T-018 decoder, T-019 capture script, T-020 sessions; the human drives XTU, the trace is decoded offline), so requests *and* hardware responses are MEASURED on this CPU; (3) test linuxmon's planner/codec against the traced sequences; (4) document; (5) only then replay the verified subset on Fedora.
  Supersedes the D-013 ordering "P2 Linux bring-up next": T-007 is deferred and re-baselined on the Windows-measured values. Windows-side hardware writes stay human-only (XTU "Apply" by the operator); agents only observe, and never attach to or launch anything except their own self-test process.
- **D-015 · 2026-10-01 · linuxmon writes the way XTU was measured to write; offset budget counts the BIOS offset.** From the Windows XTU traces (`win-xtu-live-trace.md`): a per-core Apply sends `0x11` (full word: ratio = `0x1C[7:0]` or the non-zero baseline ratio, V target = fused `0x09 P1=0x11` voltage, adaptive, only the offset changed), `0x1D` (= ratio) and `0x27` (= the `0x26` read), never a ratio/V target of 0. linuxmon's planner/guard/simulator follow that shape (T-021); every field remains `[UNVERIFIED]` on Linux until read back.
  The runtime offset adds to the BIOS per-core V/F offset (P-Core 0: ≈ −40…−50 mV; −15 mV runtime → VID −14 mV), so the guard budgets the **effective** undervolt: default `effective_offset_floor_mv = −100` with `assumed_bios_offset_mv = {P: −50, E: 0}` ⇒ runtime floor P −50 mV, E −100 mV; overridable only in a reviewed policy file. Orchestrator's conservative default; the operator can change it.
- **D-016 · 2026-10-01 · Scope: full per-core control, both directions (from the operator; supersedes D-009's undervolt-first limit).** linuxmon controls, per P-core and per E-cluster, **ratio, adaptive OC-point voltage (raise or lower) and V/F offset (either sign)** — "the whole point": the BIOS shows no E-core voltages or V/F curves and offers a per-core V/F offset for P-cores only.
  **E-core and P-core turbo ratio tables** (MSR `0x1AD/0x1AE/0x650/0x651`; also visible in the BIOS, but a worthwhile addition) join later, behind a guarded non-mailbox MSR write path. **Unchanged:** static/override mode and Power-Gate mode stay out of scope unless the operator asks; absolute voltages for fused points are not settable; hardware writes only by the human + orchestrator under the `SAFETY.md` session protocol.
  **Safety moves from direction to bounds.** The guard stops refusing a change for being an increase and enforces an explicit, reviewed policy instead: per-kind ratio ceiling · adaptive V-target ceiling (hardware maximum from `0x28`: E ≈ 1.355 V, P ≈ 1.625 V on this CPU) · offset floor/ceiling counting the BIOS offset (D-015) · per-operation step limits (ratio, mV) · dwell, session and per-boot caps · health bracket · read-back · revert path. **Shipped default ceilings equal today's fused/baseline values; the operator raises them deliberately in a reviewed policy file** (so an overclock is always a human-set number, never a default). Raising ratio and voltage together is done voltage-first, as XTU does. Implemented by T-022 after T-021 (the code still has the old undervolt-only check until then).
  *D-016 implementation choices (T-022, merged; the operator may change any of them in a reviewed policy file):* default `max_ratio_step` 1, `max_v_step_mv` 20 (symmetric: also limits drops), `max_offset_step_mv` 25 (so `-30 mV` on a P core is reached as `-25` then `-5`, two sessions); `offset_ceiling_mv` default 0 with a +100 mV sanity bound; sanity bounds ratio P 62 / E 60, V target P 1625 mV / E 1355 mV; `0x28` is read only when the V target rises above the no-raise value; a rising ratio and V target are two `0x11` words, **voltage first** (falling: frequency first) and the guard refuses one `0x11` that moves both the same way; a step beyond the limits stops the plan with `NeedsRamp` and prints the next allowed profile; read-backs count in the plan's WRMSR budget (53 for an E-cluster raise, 13 for one P core) but not in the guard's counters.
- **D-017 · 2026-10-01 · Turbo ratio tables: Linux-first, bundled with the per-core apply (from the operator).** Facts that prompted it (`lnx-first-read.md`, `[MEASURED lnx 2026-10-01]`): after a warm reboot XTU's E `0x11` word (ratio 49, V target) persisted but `0x1D` (`0x1C` = 48) and MSR `0x650` (48 ×8) were BIOS-restored, so the E-cores ran 48×. Hence:
  (1) **T-023 builds the guarded plain-MSR write path on Linux without a Windows trace** (an exception to D-014 for documented, plain turbo-ratio MSRs): whole-64-bit RMW on CPU 0 as XTU/IPF does (`decomp`), only ratio bytes change, read-back on CPU 0 and on one E CPU, BIOS value = revert. Pre-checks measured on lnx: `0xCE[28]` = 1 (programmable ratio limits), `0xCE[24]` = 1, `0x194[20]` = 0 (OC unlocked), `0x650` identical on CPU 0 and 20.
  (2) Scope v1: ratio bytes of **`0x650` (E) and `0x1AD` (P)**; bucket core-count MSRs `0x651`/`0x1AE` stay read-only.
  (3) **E ratio ceiling 49** in the reviewed policy for now (step +1 per attended session unchanged).
  (4) **One bundled apply** like XTU: per-core `0x11`/`0x1D`/`0x27` on every core of the cluster **plus** the turbo-table write, one plan, one CONFIRM, whole budget checked before anything is sent, read-backs, revert plan. Needs a session-raised policy (caps).
- **D-018 · 2026-10-01 · Guarded bundled applies may run as a paced burst (from the operator; narrows D-011).** D-011's "one transaction at a time, never a sweep" still governs **sittings**: one sitting = one guarded command, followed by the ≥ 5-minute watch and health check before the next. **Inside** one guarded, budget-checked `apply` (whole plan validated before anything is sent, read-backs, health bracket, revert plan) the dwell between operations is a policy number; the operator chose **5 s** (`min_dwell_s = 5.0` in session policies) and approved the full E-core bundle (16 cores + `0x650`, ≈130 operations ≈ 11 min) as the first write. Blind reads / enumerations stay forbidden. Note: the revert plan's `0x1D` restores the ratio of the revert `0x11` word (49 on the current state), not the pre-session `0x1C` (48); a reboot or the `0x650` revert is the real undo.
- **D-019 · 2026-10-04 · Accepted state = p-step7 at BIOS LLC 6; 60-min OCCT Linpack 2021 is the acceptance gate (from the operator).** p-step4 — and
  every deeper variant tried on 10-04 (p-step6 B: 53× ~1.181 V on P2/5/6/7; p-step6-half A; p-fav57 = p-step4 + 57× −75) — failed OCCT Linpack 2021
  (24 threads, 2048 MB) with "CPU error" and no WHEA (silent wrong results; p-fav57: 50 min at LLC 7, ~2 min at LLC 6). The earlier gates (OCCT SSE
  10–15 min, CPU+RAM 20 min, clock cycler) never caught it. The operator's table **p-step7** — 48× stock on all P-cores, 53× −50 uniform, 55× set by
  target voltage (1.270 V P0/P3/P1, 1.285 V P4/P5/P6/P2/P7), P0 56/57× 1.360/1.385 V, P3 1.330/1.365 V; E split3 and ring unchanged — passed Linpack 2021
  60 min and OCCT SSE variable 60 min (WHEA unchanged). Reasons: margin back in the 48→53× blend where all-core AVX runs (P 51–52×) and on the favored
  boost points; 55× capped by voltage instead of the deepest offsets (P7 −97 → −91). LLC 7 → 6: little practical difference, p-step7 validated at 6.
  `profiles/CURRENT`, `profiles/EXPECTED-words.txt` and `tools/win/restore_tuning.ps1 $Expected` updated together (offline check: read-back overlay =
  both tables; boot-ramp → p-step7 largest step 41 mV ≤ 55). Linux re-confirmation pending.
- **D-020 · 2026-10-05 · One CONFIRM per restore run (from the operator).** `tools/win/restore_tuning.ps1` (and the Desktop launcher
  `tools/win/Restore-Tuning.cmd`, self-elevating) asks once "Type CONFIRM" (case-sensitive; anything else ⇒ nothing sent) and then answers the guard's
  per-command tokens via `tools/lnx/orchestrator_confirm.py`, as `-Orchestrated` already did. Still never unattended; the guard is unchanged (identity
  check, policy limits, per-write read-back, final word-for-word compare). The pre-apply `vf-dump` became opt-in (`-Before`, ~132 ops) so two restores
  fit in one boot's 600-op budget (an interrupted run + a rerun exhausted it on 10-05: the final compare was refused, all 57 writes had read back equal).
- **D-020a · 2026-10-08 · One CONFIRM per restore run on Linux too (from the operator).** `tools/lnx/restore_tuning.sh` now shows policy/profiles,
  asks once "Type CONFIRM" (exact; anything else ⇒ nothing sent), then answers the guard's per-command tokens via `orchestrator_confirm.py` — on par
  with `tools/win/restore_tuning.ps1`. Guard unchanged; still never unattended.

## arrowlake-tuner (new repo, 2026-10-08)
- **D-022 · 2026-10-08 · New repo `arrowlake-tuner` for the product (from the operator).** The GUI (Linux) + PowerShell restore (Windows) continue
  here; `linuxmon` stays as the research/evidence archive (read-only, `docs/PROVENANCE.md`). Package renamed `linuxmon` → `arltuner`, env flag
  `LINUXMON_ALLOW_MAILBOX` → `ARLTUNER_ALLOW_MAILBOX`. Kept on purpose: audit log `~/.local/state/linuxmon/audit.jsonl` (Windows
  `%LOCALAPPDATA%\linuxmon\`) and lock `/run/lock/linuxmon-mailbox.lock`, so the per-boot op cap and the lock stay shared with the old CLI.
  Fresh git history; source commit `linuxmon@b01f78a`.
- **D-023 · 2026-10-08 · Product decisions O-1…O-8 (from the operator)** — `docs/SPEC.md` §1: any Linux distro; Windows PowerShell only; stdlib Python +
  local web UI; unprivileged GUI + polkit root helper; diff + one CONFIRM per apply; embedded telemetry and stress screens; all controls in v1
  behind hardware gates; autostart option (off / with CONFIRM / unattended) subject to D-021.
- **D-021 · DRAFT, NOT SIGNED · Unattended autostart apply.** Text: `docs/SPEC.md` §10. Signing it supersedes, for that mode only: SAFETY.md step 5,
  AGENTS.md per-sitting go-ahead, D-020/D-020a "never unattended", `tools/lnx/orchestrator_confirm.py` "never scheduled", the env-flag rule.
- **D-024 · 2026-10-08 · OD-1, OD-2, OD-5 resolved (from the operator).** Root-owned install for the helper, policy, audit log and accepted state,
  also during development (SPEC §4.4/§11); Python ≥ 3.12; private GitHub `xyehya/arrowlake-tuner`. OD-3/OD-4 (autostart scope, D-021) deferred to
  G6. BIOS PL1 = 250 W (operator; resolves the 295/250 conflict in the records).
- **D-025 · 2026-10-08 · Autonomous orchestration (from the operator).** The top-level session orchestrates the project A to Z: Sonnet workers
  (worktrees) do all coding and test runs, read-only Sonnet reviewers check security-relevant work, the orchestrator reviews/merges/decides and
  keeps its context clean, consults the `advisor` tool at defined points, and asks the operator only for hardware access, limits, downloads,
  scope and operator decisions. Detail: `docs/ORCHESTRATION.md`.
- **D-026 · 2026-10-08 · Telemetry reads use their own read-only allowlist, not the guard.** The sampler reads plain MSRs through
  `telemetry.msrio.TelemetryMsr` (rdmsr only, fixed `TELEMETRY_MSRS`, refuses 0x150; AST test bars imports of mailbox/guard/controls). The guard's
  `PLAIN_MSR_READABLE` is not widened (that is an operator item, ORCHESTRATION §4). The first real telemetry read is T1, orchestrator, with a go-ahead.
- **D-027 · 2026-10-08 · Web↔helper clarifications (G-103 findings).** Long ops = 202 + SSE `result`; SSE authenticated by cookie + Host +
  `Sec-Fetch-Site: same-origin` (SPEC §4.2 "Origin on SSE" unimplementable in browsers for same-origin GET); helper's root preflight is the authority
  (refuse writes on `UNKNOWN`); `/api/call` op allowlist. Detail: ARCHITECTURE contract 8 addendum.
- **D-028 · 2026-10-08 · G-103 accepted residual risks (after 2 Sonnet review rounds).** (1) Cookies are not port-isolated on 127.0.0.1: another
  local service receives the session cookie; mitigated by idle 30 min / absolute 12 h expiry, logout, exact Host/Origin, `Sec-Fetch-Site`, op
  allowlist, polkit at helper start — not eliminated (a cookie holder can fetch a plan nonce). (2) A local process can hold all 16 connection slots by
  reconnecting (local DoS only). (3) The server's `plan` args check is shallow; the helper is the authority and must schema-validate profiles.
  Re-login without restart: second `arltuner-gui.py` run → SIGUSR1 → fresh single-use token.
  (4) The rate limiters do not slow guessing (a correct token/cookie is checked before the limiter and always accepted, by design, so an
  attacker cannot lock the operator out); brute-force resistance rests on 256-bit token / session-id entropy. Flagged by the automated commit scan
  as "rate-limit bypass" — accepted.
- **D-029 · 2026-10-08 · Root helper hardening (G-102, 2 review rounds + automated scan).** Root helper accepts no argv but `--device`; all paths
  and the taint-bit allowance are root-owned constants (`/etc/arltuner/helper.toml`); file opens symlink/FIFO/hardlink-safe (guard lock/audit too).
  Carried: `check_code_tree` is a tripwire (runs after import) — the gate is G-106's polkit `exec.path` with no argv; lock stays in `/run/lock` (DoS
  only) until G-106; latent: a future non-long write op would skip `machine.begin` — G2 must mark write ops long or fix the gate.
- **D-030 · 2026-10-08 · Path migration rule + G1 tail.** CLI and helper switch from the legacy lock/audit (`/run/lock/linuxmon-mailbox.lock`,
  `~/.local/state/linuxmon/audit.jsonl`) to `/run/arltuner/mailbox.lock` + `/var/lib/arltuner/audit.jsonl` **only when installed**: the CLI treats
  "installed" = `/etc/arltuner/policy.toml` present and passing the helper's trust check; present-but-untrusted ⇒ named refusal, never fallback; absent
  ⇒ legacy unchanged. No two-lock transition (a user-owned legacy log read for the cap would be truncatable ⇒ cap bypass). `install.sh` seeds the new
  audit log from the legacy one (root 0600) so the per-boot count survives a mid-boot install; tmpfiles.d creates `/run/arltuner` (0755 root).
  Consequence: the first restore after the install go-ahead is the first run on the new paths. Dashboard budget/accepted data come from the helper
  (contract 8 addendum G-107).
- **D-031 · 2026-10-08 · G-106 install merged (2 review rounds); operator CLI path stays as is.** `install.sh` copies only, runs only the installed
  tree as root (`-I`, cwd /), refuses symlinked source dirs, seeds the audit log as the invoking user (64 MiB cap), atomic code swap, self-check
  (code tree, launcher hash, polkit rules.d overrides). `uninstall.sh --purge` needs `--yes-reset-budget`. Restore scripts no longer pass `--audit`
  and pass `HOME` through sudo (pre-install path unchanged). Known and accepted: the operator restore scripts (`tools/lnx/*.sh`) still run the
  checkout's Python and the checkout `--policy` as root via sudo — that is the human/orchestrator CLI path (D-022), outside the helper trust rule
  (SPEC §4.4 applies to the pkexec helper). A later phase may move restore onto the installed helper.
- **D-032 · 2026-10-11 · Advanced-user limits (operator yes).** Operator, verbatim: "remove the limitations on the mailbox writes and the amount
  of offsets or target voltages - this is for advanced users to they knw what they are doing. although in the drop down menus i dont think we can go
  above/below 200mv in offsets or the fused max of the p and e cores in the cpu". New root policy `policies/advanced-2026-10-11.toml` (seeded with
  `install.sh --reseed-config`): runtime offset **−200…+200 mV** (`effective_offset_floor_mv = -200`, `offset_ceiling_mv = 200`, code sanity
  ceiling raised 100 → 200); per-write steps opened to the code maxima (offset 200 mV, V 200 mV, ratio 8); V target ceiling = the **hardware**
  maximum from cmd 0x28 minus one (P 1625 mV, E 1355 mV) [MEASURED win 2026-10-01]; ratio ceiling P 57 (fused max, `0x1C[15:8]` on the favored
  cores [MEASURED]), E 49 (D-017, already run; `0x1C` 46 is not an E cap [MEASURED]); **op caps become indicators**: per-boot/per-session
  100000, dwell 0. **Kept (not limits, safety mechanisms):** one mailbox transaction at a time (2026-08-26 incident: sweeps, not budgets), the
  guard + allowlist, the health gate, the audit log, the plan-bound nonce, read-back. Consequence: the restore scripts read the same root policy,
  so they also lose the 55 mV step (the boot ramp profile still exists but is no longer required). Static/override voltage stays out (D-016).
- **D-033 · 2026-10-11 · Product v3 flow: Measure → Store → Tune (SPEC-v3).** Operator: "first we lay the ground, then we tune according to what we
  have in hands". (a) "Read V/F curve" now means **measure** volts vs frequency per P-core and per E-cluster (12 curves): cpufreq cap per ratio +
  pinned load + VID from `0x198`, no mailbox (the T-026/T-027 method as a helper op); results are stored root-owned and comparable. (b) The mailbox
  word read (`vf.dump`) is renamed **"Read tuning words"** and lives on the Tune page. (c) SPEC §5 hardware gates become informational badges, not
  locks. (d) O-4 changes: apply = before/after diff modal + one **Apply** click that carries the plan nonce (no typing); plan-binding, expiry and
  single-use stay. (e) The orchestrator still asks for a go before the first mailbox write from the new UI.
- **D-034 · 2026-10-11 · Orchestrator fully autonomous (operator).** Operator, verbatim: "u have the go to do anything to keep thie project going
  plz. dont wait any more ok from my side. orchestrator can go autonomously." This is the standing go for every sitting the orchestrator runs
  (installs, sudo, read and write sittings, merges). Unchanged: workers never touch hardware; guard, one transaction at a time, health bracket,
  smallest step first, honest reporting; stop on a traceback, health delta, MCE/WHEA. Operator sequencing: reads + UI to standard first, then writes
  (OC point → adaptive voltage → offsets).
