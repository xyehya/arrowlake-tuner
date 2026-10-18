# SPEC — arrowlake-tuner v1 (Linux local web GUI + Windows PowerShell restore) · v2 DRAFT 2026-10-08

_v1 was an early draft (superseded). v2.1 (2026-10-08, D-027): §4.2 SSE uses `Sec-Fetch-Site` instead of a required Origin; long ops 202 + SSE. v2 folds in the review `docs/review/2026-10-08-spec-v1-review.md` (37 findings; numbers cited as R#).
Open operator decisions are marked **OD-n** (§14) and block the phase named there. Phases: §12 / `docs/ROADMAP.md`. Tasks: just-in-time._

## 1. Operator decisions (2026-10-08)
| # | Decision |
|---|---|
| O-1 | **Linux in general** (any distro, packages named per distro). **Windows: PowerShell restore only**, no GUI. |
| O-2 | **Python (stdlib) + local web UI** in the browser. |
| O-3 | GUI **unprivileged**; a **root helper** (a socket-activated service with a per-connection user allowlist, D-043; no polkit) performs every privileged read and write. |
| O-4 | Apply = **before/after diff + type CONFIRM once** (D-020 style). |
| O-5 | **Live readings embedded.** |
| O-6 | **Stress tests embedded**: frequency caps for fixed-ratio stability, compute screens, a memory test (stressapptest). |
| O-7 | **v1 includes all controls**: offsets, target voltage, OC ratios, turbo ratio tables — each writable only after its hardware gate (§5). |
| O-8 | **Autostart apply** option: off / with CONFIRM / unattended — needs D-021 (§10). |

## 2. Scope and non-goals
- **In:** read/edit the V/F curve per P-core and E-cluster (offset, target V, ratio); OC ratios; turbo ratio tables `0x1AD` (P) / `0x650` (E);
  profiles (load / diff / save-as / apply / restore / compare); live telemetry; stress screens with frequency caps; audit/health log; autostart.
- **Windows:** keep `tools/win/restore_tuning.ps1` + `Restore-Tuning.cmd` working and in sync with the accepted state (§6.4).
- **Out of v1:** Windows GUI; static/override voltage and Power-Gate modes (D-016); ring/NGU writes; PL1/PL2; editing the policy (§7); CPUs without a
  platform file; bundling closed tools (OCCT, Intel Linpack, y-cruncher); an OCCT launcher (R29).

## 3. Platform support and preflight (R9, R32)
Runs on any distro; **writes only on a recognised platform** described in `platforms/arl-s.toml` (CPUID, topology, V/F point ratios, fused
words, turbo-table defaults, sensor chip). Recognition = CPUID match **and** the fused-word identity check (as Windows already does, D-020) — not
CPUID alone. Each failure is a named refusal, never a workaround; preflight re-runs immediately before every apply.
| Check | On failure |
|---|---|
| CPUID + fused-word identity match a platform file | read-only mode |
| not under a hypervisor (`hypervisor` cpuinfo flag) | read-only mode |
| lockdown: bracketed word in `/sys/kernel/security/lockdown` is `none` (Secure Boot lockdown blocks MSR writes) | read-only + explanation |
| `/dev/cpu/*/msr` present (module loaded, built in, or loadable); `/sys/module/msr/parameters/allow_writes` ≠ `off` | explanation |
| the helper socket `/run/arltuner/helper.sock` exists and `/etc/arltuner/helper.toml` `[access]` admits somebody (`helper_service`, D-043) | read-only; explanation |
| Python ≥ 3.12 (D-024) | refuse |
| optional tools: stress-ng, stressapptest, perf, turbostat, lm-sensors, nvidia-smi | feature shows "missing: install <pkg>" |
Measured here `[MEASURED lnx 2026-10-08]`: lockdown `[none]`, Secure Boot disabled, `allow_writes` default, `intel_pstate` active (HWP).

## 4. Architecture
```
browser ──HTTP 127.x.y.z:<port> (cookie)──► ui server  (user; stdlib ThreadingHTTPServer; static files from the install dir; SSE for live data)
                                                 │ JSON lines over the helper's Unix socket /run/arltuner/helper.sock (D-043)
                                                 ▼
                                     root helper (arltuner-helper.service, socket-activated; exactly one per machine, one client at a time)
                                     = library: controls → guard → msr.transport → mailbox; health; telemetry readers; freq-cap; stress launcher
```
### 4.1 Helper start (R21; D-043 replaced polkit)
`arltuner-helper.socket` (systemd, `/run/arltuner/helper.sock`, mode 0666) starts `arltuner-helper.service` on the first connection; without systemd the helper is started by hand with
`/usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket`. There is no polkit and no password. The gate is the helper's own `SO_PEERCRED` check of every connection against
the `[access]` allowlist of `/etc/arltuner/helper.toml` (the installer seeds the sudo user; `install.sh --add-user NAME`); one client at a time; idle exit after ~10 min. The helper supplies
`ARLTUNER_ALLOW_MAILBOX` itself for an admitted connection (the allowlist replaces the password that used to be the human act, D-021/D-043). The CLI keeps the env-flag rule unchanged.
### 4.2 Server hardening (R2, R3, R24)
- Bind 127.0.0.1, random port. A one-time **bootstrap token** (≥128 bit) is written to a 0600 file and opened via a `file://` launcher page — never
  in argv or a URL that reaches history; the first request swaps it for an `HttpOnly; SameSite=Strict` session cookie and redirects to a clean URL.
- Exact `Host` allowlist incl. port; `Origin` required and exact on every state-changing request and on SSE; JSON bodies only (`Content-Type:
  application/json`), size-capped; constant-time key compare; failed-auth rate limit.
- Headers: strict CSP (self only, no inline script), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- Static files from a fixed manifest (no path joins from the request) ⇒ no traversal.
### 4.3 Confirmation bound to the plan (R1)
`plan(profile)` returns `plan_id` = SHA-256 of the canonical step list + a fresh 6-character **nonce** that the page shows only in the confirm
dialog. `apply(plan_id, typed)` is accepted only if `typed == "CONFIRM <nonce>"`, the plan is unchanged and < 10 min old, and preflight still
passes. The helper calls the library through `confirm_fn` (`cli.py`), never `orchestrator_confirm.py`.
### 4.4 Trust boundary: root-owned files only (R4, OD-1)
The helper loads code, policy, accepted state and audit log **only from root-owned, non-group/world-writable paths with root-owned parents**:
`/usr/lib/arltuner/` (code), `/etc/arltuner/policy.toml` + `/etc/arltuner/accepted/` (CURRENT, profiles, EXPECTED words), `/var/lib/arltuner/`
(audit log, cap journal, autostart markers). Profiles from the user are **inputs to plan**, validated by the schema and bounded by the root policy;
never trusted for limits. Helper code is never executed from `~/.local` or a git checkout (that would be a local root escalation).
### 4.5 Helper protocol (R8, R22)
`{"id", "op", "args"}` → `{"id", "ok"|"error", ...}`; telemetry/progress as `{"event", ...}`. Max line 64 KiB; heartbeat every 2 s; ops: `preflight`,
`vf.dump`, `plan`, `apply`, `restore`, `cancel`, `freq.cap`, `freq.restore`, `stress.start`, `stress.stop`, `telemetry.start/stop`, `audit.tail`.
`cancel` takes effect **between** mailbox transactions only. On EOF / SIGTERM / SIGHUP: finish the current transaction, stop the apply at a safe
point, restore frequency caps from the journal, kill stress children, exit.
### 4.6 Concurrency, progress, budget (R17–R20)
- One machine-wide lock `/run/lock/arltuner-mailbox.lock` for uninstalled runs (the CLI's default, D-041; so CLI and GUI exclude each other).
- Helper state machine `idle | planning | applying | capped | stressing`; conflicting requests get a named refusal. Operations are addressed by id:
  a reload or second tab re-attaches to the running operation, never repeats or cancels it.
- Apply shows step progress, ops used / per-boot budget (600, from the audit log) before CONFIRM; Apply is disabled with the reason if the plan does
  not fit. There is no "raise cap" control.

## 5. Controls and their gates (O-7, R15)
Every control is shown; a control is **writable only after its gate is recorded** (§6.4 attestation). Until then it is read-only and says why.
| Control | Code | Hardware status | Gate |
|---|---|---|---|
| Per-point offset P (points 1–8) / E (1–6) | `point-offset`, profiles | **proven**: p-step7 both OSes, D-019 gate passed | — |
| Profile apply / restore / word compare | `apply`, restore scripts | **proven** | — |
| E OC ratio (`0x11`) + `0x1D` + E turbo table `0x650` | profiles, T-023 plain-MSR path | **written once** (E 49×, 2026-10-01, 1-min stress) | G4: supervised re-apply + D-019 gate at 49× |
| E OC-point target voltage | profiles + policy `v_target_max_mv` | **written once** (1.165 V, same run) | same as above |
| P OC ratio (`0x11`) + `0x1D` | library + simulator | **written once, ungated**: Windows 2026-10-06, P0 58× at V target 1429.69 mV, read back equal; no stability result; cleared by reboot | G5: supervised re-apply → measured clock → D-019 gate |
| P turbo table `0x1AD` | T-023 code built | **written once, ungated** (same 10-06 test: `0x373737373737393A`) | G5: supervised write + read-back + gate |
| P OC-point target voltage raise | library; accepted policy has no P ceiling (= fused) | **written once, ungated** (same test, the 2026-10-06 session policy) | operator adds a P ceiling to the root policy by hand, then G5 gate |
| Ring point offsets (P1 2, P2 1..9) | profiles/restore (D-045) + `tune.plan {ring}` (G-212) | **proven**: restore since 2026-10-15; editor intent H3 2026-10-18 (P2 7 −5 mV, read back, reverted) | — (D-046) |
| Package power limits PL1 / PL2 / tau (powercap; MSR 0x610 read-back) | `power.read/plan/apply/revert` (G-214) | **proven**: H4 2026-10-18 (PL2 295→275 W, revert to boot values, 275 W again; file and MSR agree) | policy `[power]` ceilings = BIOS values (D-047) |

## 6. Screens
1. **Dashboard:** live tiles + per-core table (§8); preflight; applied vs accepted ("matches accepted" / "equivalent: residual OC point (cleared by a cold boot); voltages as accepted" (G-147) / "differs: n" / "stock"); ops used / 600.
2. **V/F editor:** rows P0–P7 and E-clusters 0–3, columns = V/F points. Cell: fused word, offset (mV), target V, measured VID. Editing **target V**
   requires a measured stock VID for that point (`vf-dump --measure`, costs ops and minutes) — disabled otherwise (R33). Out-of-policy cells red
   with the limit; gated controls grey with the gate.
3. **Ratios & turbo tables:** per-core OC ratio, `0x1AD` / `0x650` buckets, same rules.
4. **Profiles:** list, diff (profile↔profile, profile↔live), save-as (name `^[a-z0-9._-]{1,64}$`, never overwrite), plan → diff → CONFIRM → apply →
   read-back → compare. **Accepting** a profile is an operator **attestation** (which gate, when, evidence file, e.g. OCCT/Linpack log path) —
   never inferred from the app's own screens (R15). Acceptance in the GUI produces a **proposal** (new CURRENT, EXPECTED words, and the matching
   `$Expected` block for `restore_tuning.ps1`) that the operator commits with git; only after a typed CONFIRM is it installed to
   `/etc/arltuner/accepted/` (hash in the audit log) for autostart (R16).
   **D-044 (G-148):** a profile is a **complete, independent** tuning state (every unit's OC word fields, every fused point's offset, both turbo tables); saving
   = this boot's read with the editor's rows over it. The accepted state is **one** profile (`CURRENT` has one line; profile and EXPECTED words come from the
   same read); restore = apply it in one run, writing only the words that differ. G-147: a residual OC point (an OC ratio the accepted state lacks; only a cold boot clears it) is rewritten so the unit runs the accepted voltage and is reported EQUIVALENT, not BAD; `0x1D` is never lowered as a side effect. Profiles saved earlier are "partial (legacy)"; an older multi-profile
   `CURRENT` is converted once by `arltuner accepted-migrate` (from its EXPECTED words, with a backup) and refused by the restore until then.
5. **Stress:** §9. 6. **Log:** audit tail, health snapshots, MCE / hardware-error kernel lines since boot. 7. **Settings:** autostart; policy shown
   read-only with its path.

## 7. Safety rules carried into the app
- No policy editing or raising in the GUI (ceilings, 55 mV/write, −100 mV floor, op caps); the operator edits `/etc/arltuner/policy.toml` by hand.
- All writes through the guard; a restore (or a complete profile) whose change exceeds the per-write step limits is split into guarded rounds inside the one
  apply, decided before the first write (D-044; no hand-made ramp profile); refusals shown, never worked around.
- Before apply: list processes holding `/dev/cpu/*/msr` open (root scan of `/proc/*/fd`) and warn by name for OCCT / other tuners (OCCT's Linux GUI
  writes MSR 0x150 at launch, SAFETY.md); the operator ticks the SAFETY.md pre-flight checklist (R30).
- Revert of everything = cold boot; shown next to every Apply.

## 8. Live telemetry (O-5, R12–R14)
Readers re-implemented here from documented facts (`docs/knowledge/linux-path.md`; new rows go to `docs/knowledge/telemetry-sources.md`: value,
source, privilege, package, platform-specific?). The operator's separate monitoring tools are reference only — facts in, no code copied.
- Values: per-core MHz/busy (APERF/MPERF `0xE8/0xE7`), core VID (`0x198` — semantics on ARL to confirm), temps (coretemp hwmon / `0x19C`/`0x1B1`),
  package/core power (RAPL powercap; readability differs by distro), throttle reasons, **Vcore via hwmon** (board-specific, read-only files; no direct
  Super-I/O access — R31 partly rejected: it is the operator's key number), DRAM GB/s (one long-lived `perf stat -I 1000` on `uncore_imc_free_running`,
  root; event names `[UNVERIFIED]` beyond this kernel), GPU (nvidia-smi, optional).
- Every telemetry MSR gets an allowlist entry (read kind) in `docs/knowledge/registers.md` before use. Telemetry runs on its own thread and never
  delays a mailbox transaction. 1 Hz default, 0.5 s optional; recording to `captures/lnx/` on demand.

## 9. Stress (O-6)
1. **Process rules (R7):** stress children run **as the user** (helper drops privileges), arguments from enumerated values only (CPU lists checked
   against topology, integers in ranges), no shell strings; children in their own process group, killed with the helper.
2. **Frequency cap (R10, R11):** per-CPU `scaling_max_freq` (+ `scaling_min_freq`, governor, EPP pinned to performance for the run). It is an upper
   bound under HWP: the page shows **achieved (APERF/MPERF) vs requested** and marks a run invalid if achieved is > 1 bin below the cap or the
   turbo table / power limits bind first. Originals journaled to `/var/lib/arltuner/capjournal.json` **before** the change; restored on stop, exit,
   EOF and on the next helper start.
3. **Screens (R27, R28):** stress-ng verifying methods ("vector-math-heavy", "integer", "cache") — not called SSE/AVX until the executed instructions
   are verified; stressapptest memory screen whose miscompare class **suggests** read path (re-read correct) vs stored data (re-read wrong) vs CRC-only —
   never "proves", since an undervolted CPU copy path can produce the same signature. Persistent banner: **"Quick screen — NOT an acceptance gate
   (D-019: Linpack 2021 60 min + SSE 60 min)."** No "pass" wording.
4. **External (R29):** launch Linpack Xtreme **as the user** (path chosen by the operator), show its log; the operator attests the result. Never
   executed by the root helper.

## 10. Autostart (O-8) — D-021 draft, unsigned
Modes: **off** (default) · **with CONFIRM** (user autostart entry opens the GUI on the plan diff; the user must be in the helper allowlist) · **unattended** (system unit
at boot, root). Unattended:
- applies only `/etc/arltuner/accepted/CURRENT` (root-owned, installed by a confirmed acceptance, §6.4) and only a profile with a recorded gate;
  health bracket before/after; one apply that splits into rounds within the step limits (D-044); ends with the word-for-word compare;
- **crash-loop guard:** a marker in `/var/lib/arltuner/` is set when autostart starts and cleared by a timer unit after N minutes of healthy uptime;
  a leftover marker, an MCE / hardware-error line in the previous boot's kernel log, or a **non-persistent journal** (cannot check ⇒ skip) disables
  autostart until the operator re-enables it in the GUI;
- on refusal or mismatch mid-apply: stop, apply the revert plan (offsets of touched points back to the read baseline); if the revert fails, leave a
  banner + audit entry and advise a cold boot (partial ≠ stock, R6);
- result reported by the audit log + a banner at next GUI start (no session-bus notification from root, R35).
D-021 must name what it supersedes: SAFETY.md step 5 ("nothing auto-applied at boot until the human enables it deliberately" — satisfied by the
written opt-in), AGENTS.md "explicit go-ahead per write session" (the opt-in is a standing go-ahead for this one action), D-020/D-020a "never
unattended", the `orchestrator_confirm.py` docstring, the env-flag rule for GUI/autostart (polkit / the opt-in replace it).

## 11. Packaging (O-1, R26, OD-1)
System install only for the helper: `/usr/lib/arltuner/` (code), `/usr/share/arltuner/web/` (static), `/etc/systemd/system/arltuner-helper.{socket,service}`,
`/etc/arltuner/`, `/var/lib/arltuner/`, optional `arltuner-autostart.service` + timer, desktop entry; user data `~/.config/arltuner/profiles/`.
`install.sh` / `uninstall.sh` (root, idempotent). Packages (Fedora `[MEASURED]`; others `[UNVERIFIED]`):
| Need | Fedora | Debian / Ubuntu | Arch |
|---|---|---|---|
| python ≥ 3.12 | python3 | python3 (Debian 13+, Ubuntu 24.04+) | python |
| systemd (recommended; other inits start the helper by hand) | systemd | systemd | systemd |
| stress-ng · stressapptest | stress-ng · stressapptest | stress-ng · stressapptest | stress-ng · stressapptest (AUR) |
| perf · turbostat | perf · kernel-tools | linux-perf (Debian) / linux-tools-$(uname -r) (Ubuntu) · ? | perf · ? |
| lm-sensors · nvidia-smi | lm_sensors · nvidia driver | lm-sensors · nvidia-utils-* | lm_sensors · nvidia-utils |

## 12. Phases (vertical slices; detail in `docs/ROADMAP.md`)
**Status 2026-10-12: G1–G5 delivered (v3, `SPEC-v3.md`); G6 autostart and G7 portability remain.** G1 skeleton (install layout, helper + server + security, preflight, platform file, read-only dashboard) → G2 offsets (editor, profiles, plan/
CONFIRM/apply/restore, acceptance proposal) → G3 stress (caps, screens, Linpack launcher) → G4 E ratios → G5 P ratios + `0x1AD` → G6 autostart
(after D-021) → G7 portability. **W** (any time): keep the Windows restore working after renames; sync check of `$Expected`.
G1 security tests: Host/Origin/cookie/bootstrap, CSRF simple-POST, oversize body, path traversal, slow client, plan-bound nonce replay.
G6 test: simulated leftover marker + a documented human-run hard-reset check (itself a supervised event, SAFETY.md).

## 13. Risks
Accepted-state drift between Linux and Windows copies · telemetry contention with other monitors (one owner per Super-I/O controller) · per-boot
budget shared with the CLI via the audit log (OD-1 moves it to `/var/lib`: the CLI must follow) · large untested-on-hardware surface in G4/G5.

## 14. Open decisions (operator)
| # | Question | Proposed | Blocks |
|---|---|---|---|
| OD-1 | Root-owned install for helper, policy, audit, accepted state — also during development (CLI keeps running from the checkout with sudo; it moves its audit log and lock to the same `/var/lib` / `/run/lock` paths) | **yes (D-024)** | — |
| OD-2 | Python ≥ 3.12 (excludes Ubuntu 22.04, Debian 12) or ≥ 3.11 | **yes (D-024)** | — |
| OD-3 | Autostart applies only the one accepted profile and only with a recorded gate (the reference machine: `accepted-2026-10-04`, built from p-step7) | deferred to G6 | G6 |
| OD-4 | Sign D-021 (unattended autostart, §10) | deferred to G6 | G6 |
| OD-5 | GitHub remote: `xyehya/arrowlake-tuner` (https://github.com/xyehya/arrowlake-tuner); public after a privacy scrub (G-140)? | **yes (D-024)** | — |
