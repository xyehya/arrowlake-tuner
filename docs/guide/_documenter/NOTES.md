# Documenter notes (private working memory of the documenter agent)

## Outline and status
| Page | Status | Last run |
|---|---|---|
| README.md (index) | complete; has the "Limits in one paragraph" box | 2026-10-12 (update) |
| 01-requirements.md | complete | 2026-10-12 (update) |
| 02-install.md | complete; limits table with hard ceilings | 2026-10-12 (update) |
| 03-first-run.md | complete; random loopback URL | 2026-10-12 (update) |
| 04-arrow-lake-vf-primer.md | complete | 2026-10-12 (update) |
| 05-measuring-curves.md | complete | 2026-10-12 (update) |
| 06-undervolting.md | complete; G-133 labels are now real | 2026-10-12 (update) |
| 07-overclocking.md | complete; OC model matches the merged page | 2026-10-12 (update) |
| 08-stability-testing.md | complete | 2026-10-12 (update) |
| 09-profiles-and-restore.md | complete; Windows section rewritten (fingerprint, %ProgramData%), still "untested on Windows" | 2026-10-12 (update) |
| 10-cli-reference.md | complete (flags from `--help` after G-138/G-141) | 2026-10-12 (update) |
| 11-troubleshooting-faq.md | complete (first version) | 2026-10-12 (update) |
| 12-safety-and-internals.md | complete; section 4 = limits (canonical), section 7 = D-042 hardening | 2026-10-12 (update) |
Also owned now: `docs/USER-GUIDE.md` (a pointer to `docs/guide/README.md`).

## Style decisions
- Evidence tags kept verbatim from the source; reference-machine numbers always "example values from one chip (Core Ultra 9 285K, ASRock Z890)".
- Cite only files that exist in this repo (D-041). Vendor-decompile claims use `[DECOMPILED registers.md "section"]`. No names of private repos, personal paths or the earlier tool name.
- Words: "you" for the user, not "operator"; `--operator` and the audit field `operator` keep the name.
- **Limits wording (D-039), used on README, 02, 07, 10, 11, 12:** every limit is the user's policy in `/etc/arltuner/policy.toml`; defaults are conservative; the program hard-codes none;
  hard ceilings = the CPU's own limits read live (cmd `0x01` bits 7:0 ratio, cmd `0x28` minus one step voltage; unread = no raise) and the field encodings (offset -1000...+999 mV, V target 0.5...3.999 V,
  ratio 1...255, steps up to 1000 mV / 255); extreme-cooling users can raise the defaults at their own risk; a change applies from the next read or plan (no restart). Canonical text: 12 section 4.
- Button names quoted from `web/index.html` / `web/app.js`: "Read ratios & voltages", "Review changes", "Apply", "Restore accepted", "Reset offsets to 0", "Discard changes",
  "Show raw words", "Measure", "Set the current state as accepted", "Record CPU fingerprint for Windows", "Set as accepted", "Load into Tune", "Review and apply", "Save profile",
  "Measure again", "Validate with a stress screen", "Set baseline" / "Clear baseline"; scope "All-Core" / "Per-Core"; offsets "Whole Curve Offset" / "Per V/F point Offsets"; Audit filter "All" / "Writes & refusals";
  System card "Tuning limits".
- The platform file is `platforms/arl-s.toml`.

## Verified facts (fact -> source file:section)
- Supported CPU: family 6, model 198, stepping [2]; unknown stepping = WARN -> `platforms/arl-s.toml`, `platform/preflight.py check_cpuid`.
- Preflight checks and modes -> `platform/preflight.py`; banner text `web/app.js` ("Read-only mode. Mailbox requests are disabled ...").
- install.sh/uninstall.sh layout and flags; audit seeded from `~/.local/state/arltuner/audit.jsonl` (D-041) -> `install.sh`, `uninstall.sh`.
- GUI: random loopback `127.x.y.z`, fallback `127.0.0.1` + warning, token TTL 900 s, session 30 min / 12 h, read/full scope, socketpair, undumpable -> `tools/arltuner-gui.py`, `web/server.py` (`READ_OPS`, `SCOPES`), D-042.
- CLI commands/flags/exit codes -> `python3 tools/arltuner.py <cmd> --help` (re-run 2026-10-12): new `--fingerprint FILE`; audit default `$XDG_STATE_HOME/arltuner/audit.jsonl`; lock `/run/lock/arltuner-mailbox.lock`.
- Restore scripts: `restore_tuning.sh` flags unchanged, runs `python3 -I`; `restore_tuning.ps1` / `reapply_current.ps1` use `%ProgramData%\arltuner` and require `fingerprint.json`; `ts_driver.py init-dir|carve|start|install|status|remove|check-dir`.
- Accepted layout now includes `fingerprint.json`; `profiles.accept` refuses `STALE_READ`; `profiles.fingerprint` -> `helper/ops_accept.py`, ARCHITECTURE G-141.
- Limits: `policies/default.toml` header (D-039), ARCHITECTURE G-133/G-137/G-141 addenda, `registers.md` "Limits in the tool"; `System > Tuning limits` card `web/app.js renderSysLimits`.
- tune.read cost about 140 on 8P+16E (112 V/F + 12 fused + 8 favored + 4 limits + 4 E 0x1C) -> ARCHITECTURE G-137 addendum; the page computes it from the unit list (G-139).
- Everything from the first run (V/F word, points, clamp, interpolation, turbo tables, cold/warm boot, silent failures) still stands: `registers.md`, `platform-arl-s.md`, `tuning-results.md`.

## Open questions for the orchestrator
1. **Windows is untested.** Guide 09 §4 describes the G-138/G-142 flow from the script headers and `docs/RUNBOOK-win.md`, labelled untested. After the first real run, correct anything that differs.
2. **Driver blocklist conflict:** `docs/knowledge/windows-path.md` says the driver loaded with the Microsoft Vulnerable Driver Blocklist ON (measured); `tools/win/preflight.ps1` marks the blocklist ON/default as FIX.
   Guide 09 says "start as it is, follow preflight only if `start` fails with 1275/577/blocked". Decide which is right.
3. **Stale lines in files I do not own** (not edited): `docs/knowledge/windows-path.md` §3 still describes the hard-coded 285K identity check, a 55 mV step and 600 ops/boot; `docs/SAFETY.md` still says default dwell 60 s
   and cap 12 in places; `docs/ARCHITECTURE.md` contract 3 / `SPEC.md` §3 name `platforms/arl-s-285k.toml` (the file is `arl-s.toml`); `docs/SPEC-v3.md` §6 "ratio <= P 57 / E 49" and "Read tuning words";
   `docs/STATE.md` mentions "all 17 passed" and the pre-D-041 log name; `docs/DECISIONS.md` D-033 "Read tuning words".
4. `tools/win/preflight.ps1` shows "driver service ThrottleStop" as FIX until a run has started the service (the restore script creates it). Guide says so; a clearer preflight wording would help.
5. Dense measurement: tooltip "about 10 minutes", measured about 12 (2026-10-11); guide gives both.
6. Helper measure uses settle 0.3 s / 10 samples, CLI `vf-dump --measure` 2.5 s / 30 samples; documented separately.
7. Debian/Ubuntu package names: repo gives hints only for perf, turbostat, lm-sensors (`OPTIONAL_TOOLS`).
8. E-cluster regulator grouping: SPEC-v3 tags [MEASURED], `registers.md` tags [USER][COMMUNITY]; guide cites both.
9. ARCHITECTURE stress addendum (G-116: cap sets EPP `performance`) vs G-132 addendum (EPP write skipped under performance governor): guide does not mention EPP for Stress.
10. The CLI's default policy (no `--policy`) is still the built-in conservative M1 policy with "no raise"; the restore scripts pass `/etc/arltuner/policy.toml`. Guide tells users to pass it.

## Recheck after merges (all done 2026-10-12 against main c5eba92)
- Tune labels All-Core/Per-Core, Whole Curve Offset/Per V/F point Offsets, OC box texts (with `·` and `×`), "No OC ratio" dropdown rules, "above 55× limit" tag, header "runs up to N×", live limits: verified in `web/app.js`.
- D-038 +3: `policies/default.toml` header and ARCHITECTURE G-137; clamped by cmd `0x01`.
- Curves reason texts unchanged; interpolated points are hollow markers with a caption.
Next time: re-grep button names in `web/index.html`, `--help` of every CLI command, `ts_driver.py` argparse, `policies/default.toml` header, and `git log` since the last run.

## Next
- A short glossary page (unit, OC point, DLVR, VID, VccIA, WRMSR) if readers ask.
- Page 11: add real user problems as they arrive. Page 07: add a P-core OC result when one is validated.
- After the first Windows run: correct 09 §4.

## Log (newest first)
- 2026-10-12 (update run, branch task/G-143-guide-update from main c5eba92): checked the recheck list against merged G-133..G-142 code; updated 01, 02, 03, 04, 05, 06, 07, 08, 09 (Windows fingerprint flow,
  %ProgramData%, new button, STALE_READ), 10 (--fingerprint, D-041 paths, Windows scripts table, screenshots tool), 11, 12 (limits section, D-042 hardening, socketpair, health-gate limitation), README
  limits paragraph; `docs/USER-GUIDE.md` is now a pointer; root `README.md` lines on setup/URL/limits/self-check refreshed (Screenshots section untouched). Scrubbed private-repo / predecessor / operator wording and
  non-existent citation file names. Commands re-verified with `--help` only; no hardware, no sudo.
- 2026-10-12 (first run, branch task/G-135-user-guide): created NOTES.md and all 13 pages from the repo; link check clean.
