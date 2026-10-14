# Documenter notes (private working memory of the documenter agent)

## Outline and status
| Page | Status | Last run |
|---|---|---|
| README.md (index) | complete; has the "Limits in one paragraph" box | 2026-10-12 (update) |
| 01-requirements.md | complete; no polkit, systemd recommended | 2026-10-12 (G-146) |
| 02-install.md | complete; helper units, allowlist, `--add-user`, without systemd | 2026-10-12 (G-146) |
| 03-first-run.md | complete; Profiles tab row reworded | 2026-10-13 (G-147/148) |
| 04-arrow-lake-vf-primer.md | complete; §6 defines OC ratio vs OC point (G-150) and notes the left-over OC point + restore | 2026-10-14 (G-150) |
| 05-measuring-curves.md | complete | 2026-10-12 (update) |
| 06-undervolting.md | complete; G-133 labels are now real; top-point row points to the 07 how-to | 2026-10-14 (G-150) |
| 07-overclocking.md | complete; section 1 rewritten for G-150 (OC ratio vs OC point, header/box texts, "48x offset" control, warnings), how-to "keep the OC ratio, lower the top point", E-cluster cap bullet | 2026-10-14 (G-150) |
| 08-stability-testing.md | complete; decision table says apply, read, then accept; cap-all-four-E-cores bullet | 2026-10-14 (G-150) |
| 09-profiles-and-restore.md | complete; D-044 model (complete profiles, one accepted profile, one-shot restore with rounds, accepted-migrate), new "A left-over OC point: equivalent" section (G-147); Windows still "untested" | 2026-10-13 (G-147/148) |
| 10-cli-reference.md | complete (flags from `--help`); apply `--neutralize-residual`, compare-accepted, accepted-migrate, exit codes; `--accepted-before` gone | 2026-10-13 (G-147/148) |
| 11-troubleshooting-faq.md | complete; rows for STEP_LIMIT (two kinds), SESSION/BOOT_LIMIT, ACCEPTED_CHAIN, PROFILE_TOPOLOGY, NO_READ/STALE_READ on save, NOT_REPRESENTABLE, EXISTS, EQUIVALENT FAQ | 2026-10-13 (G-147/148) |
| 12-safety-and-internals.md | complete (checked clean for D-044 on 2026-10-13); §4 limits (canonical), §7 = D-043 access model (new), §8 = web hardening (was §7), sections 9-11 renumbered | 2026-10-12 (G-146) |
Also owned now: `docs/USER-GUIDE.md` (a pointer to `docs/guide/README.md`).

## Style decisions
- Evidence tags kept verbatim from the source; reference-machine numbers always "example values from one chip (Core Ultra 9 285K, ASRock Z890)".
- Cite only files that exist in this repo (D-041). Vendor-decompile claims use `[DECOMPILED registers.md "section"]`. No names of private repos, personal paths or the earlier tool name.
- No polkit anywhere (D-043): the helper is a socket-activated systemd service (`arltuner-helper.socket` -> `/run/arltuner/helper.sock`, 0666); the gate is the `[access]` allowlist (SO_PEERCRED) in `/etc/arltuner/helper.toml`; `sudo bash install.sh --add-user NAME`; without systemd run `sudo /usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket`. Say "sudo password" only for the restore script and installs.
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
- D-044 layout: `CURRENT` = exactly one `profile =` line; profile = `complete = true` (one `[[core]]` per unit with `fused_top_ratio` + explicit `offset_mv`, a `[[point]]` for every fused point with `expect_ratio`, `[turbo]` both or neither); `EXPECTED-words.txt` and the profile derive from the same read; `fingerprint.json` beside them (op `profiles.fingerprint` = "Record CPU fingerprint for Windows", writes only that file) -> `profiles/schema.py` (`check_complete`), `profiles/complete.py`, `helper/ops_accept.py`, ARCHITECTURE "Addendum G-148".
- Refusals: `ACCEPTED_CHAIN` (`ops_tune.py` `_accepted_profile`, `CHAIN_HINT`), `PROFILE_TOPOLOGY` (`_check_complete`: unit missing/twice, or top fused ratio differs), `NO_READ`/`STALE_READ`/`NOT_REPRESENTABLE`/`EXISTS` on `profiles.save` (`op_profiles_save`), `STEP_LIMIT` from `NeedsRamp` (partial profile) or `NeedsRounds` (complete/restore; `controls/model.py`, `planner.check_rounds`, `MAX_RAMP_ROUNDS = 16`), `SESSION_LIMIT`/`BOOT_LIMIT` (`_preflight_error`).
- Rounds: `executor.apply_profile(auto_ramp=True)`, `planner.ramp_rounds` (ceil(delta/step)); judged before the first write; earlier rounds' results have negative step numbers.
- Residual rule (G-147): `controls/residual.py` -- ratio kept, offset = accepted, target = min(fused V, fused V + accepted top-point offset) - 25 mV (`TARGET_MARGIN_MV`), floor = held target - one step limit, never above fused V; EQUIVALENT text `RESIDUAL_NOTE`; scan = 2 reads per unit without an accepted OC point (`SCAN_READS_PER_UNIT`). `0x1D` not sent when ratio <= top fused, not below the ratio in force, and below `0x1C[7:0]` (`planner.py` ~1042-1051). cpufreq follow rule `controls/cpufreq.py follow_cap` (GUI `tune.apply` + CLI `apply --device`; Linux only).
- CLI (re-run `--help` 2026-10-13): `apply [--neutralize-residual]` (always on for complete profiles), `compare-accepted --readback --expected [--fingerprint]` (exit 0/2/1; prints OK/EQUIVALENT/BAD, `RESTORED: ... (K equivalent: ...)` or `STOP: N ...`), `accepted-migrate [--dir] [--dry-run] [--sim]` (exit 0/2/1); `--accepted-before` removed (grep of src/tools/web: none). `reapply_current.sh` = `exec restore_tuning.sh`; `.ps1` twins refuse a multi-line CURRENT. `install.sh:332` prints the migrate hint.
- Web: badges "complete" / "partial (legacy)", accepted card note for a chain (`web/app.js` ~4731-4780), Dashboard text "cpu N is equivalent: ..." (~3743); no chain UI, no "As applied" toggle.
- Live numbers: one-shot restore of an already matching state = 281 reads, 0 writes (orchestrator's run, docs/STATE.md 2026-10-13; no MEASURED tag there); simulator worst-case reserve 651 ops (reference layout) [`tests/helper/test_complete_profiles.py:72`]. EC1/EC2 incident values -> `examples/reference-285k/notes/tuning-history.md` §5.
- Limits: `policies/default.toml` header (D-039), ARCHITECTURE G-133/G-137/G-141 addenda, `registers.md` "Limits in the tool"; `System > Tuning limits` card `web/app.js renderSysLimits`.
- tune.read cost about 140 on 8P+16E (112 V/F + 12 fused + 8 favored + 4 limits + 4 E 0x1C) -> ARCHITECTURE G-137 addendum; the page computes it from the unit list (G-139).
- GUI connect errors (exit status 2) -> `src/arltuner/web/helperlink.py` `HelperConnectError` texts and `tools/arltuner-gui.py`; helper start/units -> `install.sh -h`, `uninstall.sh -h`, `packaging/arltuner-helper.{socket,service}`; allowlist file keys `allow_taint_bits`, `idle_exit_s`, `[access] users|group` -> `helper/settings.py`; preflight `helper_service` -> `platform/preflight.py`.
- G-150 Tune texts (`web/app.js` ocInfo ~2734, nowText ~3364, refreshOcOffset ~3262, eVfFact ~3310, refreshRowWarn ~3324, fillRatioSelect ~3086, OC_TIP ~2696): header `OC ratio: 48× (fused top 46×) · no OC point · runs up to 48×` or `OC ratio: 48× · OC point 7`; OC box `48× (OC ratio set at boot) · no OC point`; point label `Point 6 · 46× (also 47–48×)`; ratio dropdown "No OC point", "· lowers the max from 48×"; "48× offset" options = offsets strictly above the edited point-6 offset on a 5 mV grid up to `v_target_max_mv`, target = fused 0x09 V + offset; disabled without `v_target_max_mv` or when `ratio_max` < the OC ratio; warnings quoted. Task: `docs/tasks/G-150-*.md`.
- Planner order in one plan: point writes, then per-core (OC) words (`planner.py` docstring ~line 51), so lowering point 6 and creating the OC point in one draft briefly runs 48x on the lowered voltage; the reference run used two plans (`tuning-history.md` §5). E-cluster cap-all-four -> `platform-arl-s.md` §3.
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

3a. (resolved by G-145/G-146) knowledge citations now point at `platform-arl-s.md`, `examples/reference-285k/notes/*`; `[USER]` tags removed from the sources and from guide 04/01.

11. (G-146) Stale polkit wording in files I do not own: `docs/ARCHITECTURE.md:227` (G-116 addendum `PKEXEC_UID`; the G-144 addendum supersedes it), `docs/STATE.md:7`, `docs/ORCHESTRATION.md:38`, `docs/SPEC.md` §10 (D-021 draft).
    `docs/SAFETY.md` has no paragraph on the D-043 access model (any process of an allowed user can drive the helper); guide 12 §7 carries it. Consider a short paragraph there.
12. (G-146) "21 self-check checks" is from the coordinator; I did not run the installed self-check. Behaviour when the helper service is stopped while the GUI is open (page shows `helper: n/a`) is inferred from the code, not observed.

13. (G-147/148) Code vs task files: (a) `residual.py` docstring says the default step limit is 20 mV but `policies/default.toml` has `max_v_step_mv = 200` (the built-in M1 policy may differ); guide quotes no default. (b) G-147 suggested "fused top - 100 mV" for the target; code uses -25 mV with a floor of one step limit (documented in `residual.py`; guide follows the code). (c) A neutralised target limited by the floor (tight step limit) is written but still above the accepted top point, so compare says BAD until a further restore; the guide says "moved as far as one step allows ... real difference" (not observed). (d) `docs/knowledge/platform-arl-s.md` says "no 0x1D is sent" for neutralising, broader than `planner.py` (~1042-1051: not sent only when the ratio is at/below the top fused ratio and below `0x1C[7:0]`). (e) G-148 decision 4 names only `--dry-run`; the CLI also has `--dir` and `--sim` (documented). (f) `web/app.js:161/166` has a duplicate `NO_READ` key and the `STEP_LIMIT` text there ("change fewer units at once") predates the rounds model; the helper's own message is quoted in the guide.
14. (G-148) The first-run Windows scripts' EQUIVALENT handling and chain refusal are static-checked only (no pwsh); guide 09 section 4 stays "untested".
15. (G-150) Order of a combined draft (point 6 lowered + 48x offset): one plan writes points before the OC word, so 48x briefly sits on the lowered point-6 voltage. The "keep 48x, lower 46x" how-to documents the one-draft flow and states the ordering. Can the GUI create the OC point at the old voltage first (a two-apply flow)? The 48x offset list starts above point 6's edited offset, so with point 6 unchanged the old voltage is the "same as point 6" option (no draft); the ratio dropdown + absolute target (5 mV grid) may not hit it. Not verified; guide says nothing about a GUI two-step.
    Also: `docs/STATE.md` says the split was "applied live, not accepted"; guide says so. "Unverified" whether the Stress tab's cap covers all four cores of an E-cluster (guide 08 says to check "cap held").

## Recheck after merges (all done 2026-10-12 against main c5eba92)
- Tune labels All-Core/Per-Core, Whole Curve Offset/Per V/F point Offsets, OC box texts (with `·` and `×`), "No OC ratio" dropdown rules, "above 55× limit" tag, header "runs up to N×", live limits: verified in `web/app.js`.
- D-038 +3: `policies/default.toml` header and ARCHITECTURE G-137; clamped by cmd `0x01`.
- Curves reason texts unchanged; interpolated points are hollow markers with a caption.
Next time: re-grep button names in `web/index.html`, `--help` of every CLI command, `ts_driver.py` argparse, `policies/default.toml` header, and `git log` since the last run.

## Next
- A short glossary page (unit, OC point, DLVR, VID, VccIA, WRMSR) if readers ask.
- Page 11: add real user problems as they arrive. Page 07: add a P-core OC result when one is validated.
- After the first Windows run: correct 09 §4.
- After the operator's next cold boot + restore: compare the real script output with the EQUIVALENT/OK example in 09 §3 (the example there is illustrative), and add the real operation count of a cold-boot one-shot restore if the orchestrator records one.

## Log (newest first)
- 2026-10-14 (G-150 run, worktree branch): 07 section 1 rewritten (two terms, box table, header examples, "48x offset" control table and warnings, "Point 6 ... (also 47-48x)"), new how-to with the reference example, 3 step-3 pointer, cap-all-four bullet; 04 §6 terms; 06 table row; 08 cap bullet. Read app.js, task file, planner docstring; no hardware, no commands run except link checks.
- 2026-10-13 (G-147/G-148 run, branch docs/guide-g147-g148 from main e73819d): the G-148 worker had already rewritten 09/10 (commit c799ddb); I audited them against the code and fixed/extended:
  09 (rounds bound of 16, new section "A left-over OC point: equivalent" with the target rule, EQUIVALENT output, favored-ratio and cpufreq rules, Save refusals, live restore count replaces unsourced op counts, no "ramp" wording),
  10 (apply/compare-accepted/accepted-migrate exit codes, `--accepted-before` gone, restore script rows), 11 (new refusal rows, EQUIVALENT rows and FAQ, partial (legacy) FAQ), 04 §6, 07 (follow rule, "After an OC test", old 172-op chain line replaced), 03, 08.
  Checked clean: 01, 02, 05, 06, 12, README (no chain/ramp/As applied wording; link check clean). Commands re-verified with `--help` only; nothing run on hardware, `accepted-migrate` not run.
- 2026-10-12 (G-146, G-143 and first runs, summarised): G-146 removed polkit/pkexec from 01, 02, 03, 09, 10, 11, 12, README and SPEC (O-3, preflight, section 4, section 10), added 12 section 7 (D-043), the connect-error table (11 section 2) and `--add-user`/no-systemd text (02).
  G-143 update run rebuilt 01-12 against merged G-133..G-142 (limits section, D-042 hardening, Windows fingerprint flow, `--fingerprint`, D-041 paths); first run (G-135) created all 13 pages.
