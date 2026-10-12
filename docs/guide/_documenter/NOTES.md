# Documenter notes (private working memory of the documenter agent)

## Outline and status
| Page | Status | Last run |
|---|---|---|
| README.md (index) | complete (re-check "Supported hardware" if platform file changes) | 2026-10-12 |
| 01-requirements.md | complete | 2026-10-12 |
| 02-install.md | complete (D-038 "+3" wording depends on G-134 merge) | 2026-10-12 |
| 03-first-run.md | complete | 2026-10-12 |
| 04-arrow-lake-vf-primer.md | complete | 2026-10-12 |
| 05-measuring-curves.md | complete | 2026-10-12 |
| 06-undervolting.md | complete; Tune labels per G-133 (see recheck list) | 2026-10-12 |
| 07-overclocking.md | complete; OC model is the G-133 spec, not yet in code (see recheck list) | 2026-10-12 |
| 08-stability-testing.md | complete | 2026-10-12 |
| 09-profiles-and-restore.md | complete; Windows section "untested from this repo" | 2026-10-12 |
| 10-cli-reference.md | complete (every flag from `--help`) | 2026-10-12 |
| 11-troubleshooting-faq.md | complete (first version; grows with user reports) | 2026-10-12 |
| 12-safety-and-internals.md | complete | 2026-10-12 |
All 13 pages written in the first run. Nothing is draft, but items under "Recheck after merges" are provisional.

## Style decisions
- Evidence tags kept verbatim from the source; reference-machine numbers always "example values from one chip (Core Ultra 9 285K, ASRock Z890)".
- Tune page uses the G-133 names (All-Core / Per-Core, Whole Curve Offset / Per V/F point Offsets) even though the code still has Simple / Per unit / Whole curve / Per point.
- Short sentences, tables for limits, each page: purpose line, body, "Next / Previous" links. Cross-links relative. No emojis.
- Button names are quoted from `web/index.html` / `web/app.js`: "Read ratios & voltages", "Review changes", "Apply", "Restore accepted", "Reset offsets to 0",
  "Discard changes", "Show raw words", "Measure", "Set the current state as accepted", "Set as accepted", "Load into Tune", "Review and apply", "Save profile",
  "Measure again", "Validate with a stress screen", "Set baseline" / "Clear baseline".
- Source file names: platform file is `platforms/arl-s.toml` (ARCHITECTURE contract 3 and SPEC §3 still name the old `arl-s-285k.toml`).

## Verified facts (fact -> source file:section)
- Supported CPU: family 6, model 198, stepping [2]; unknown stepping = WARN -> `platforms/arl-s.toml`, `platform/preflight.py check_cpuid`.
- Preflight checks and modes (write / read-only / refuse): `platform/preflight.py` (`check_*`, `PreflightReport.mode`); UI banner text `web/app.js:388`.
- install.sh layout, flags (`--reseed-config`, `-h`, `DESTDIR`), refuses while helper runs, seeds policy only if absent, self-check -> `install.sh`; uninstall flags -> `uninstall.sh`.
- GUI launcher options, one-time token (TTL 900 s), session 30 min idle / 12 h, re-login via second run -> `tools/arltuner-gui.py`, `web/server.py Config`, `web/reissue.py`.
- CLI commands, flags, exit codes 0/1/2/3, env flag, `CONFIRM <token>` -> `cli.py` (`--help` output run 2026-10-12, no hardware).
- Restore script flags (`--check --from --policy --orchestrated`), "RESTORED: all cores/clusters match the accepted state." -> `tools/lnx/restore_tuning.sh`.
- Accepted layout (CURRENT, EXPECTED-words.txt, profiles/) and refusals NOT_APPLIED / NO_READ / NOT_REPRESENTABLE -> `profiles/accepted.py`, `helper/ops_accept.py`.
- Profile TOML format -> `profiles/schema.py` docstring.
- V/F word fields, P2 semantics, GT offset decode confirmation -> `knowledge/registers.md` (V/F data word, Measured via XTU level-4).
- Point tables P 8/20/30/48/53/55/56/57, E 8/15/23/30/40/46 (reference chip) and community agreement -> `knowledge/platform-285k.md §3`.
- Monotonic clamp, 54x midpoint, interpolation, OC point P2=7 on E, whole-curve P2=0 offset, target dropped without OC point -> `platform-285k.md §3`, `tuning-results.md` (First write sittings, Dense curve).
- Cold vs warm boot -> `platform-285k.md §7`. E 49x needs 0x11+0x1D+0x650 -> `§8`. Turbo tables -> `§9`, `registers.md`.
- VccIA model (+100-110 mV over highest allowed VID, hypothesis) -> `platform-285k.md §5`, `SPEC-v3 F6`.
- Silent failures, gate = Linpack 2021 60 min + SSE 60 min -> `tuning-results.md §2-3, §5`.
- Measurement method (pinning min=max, 0.3 s settle, 10 samples, reasons) -> `helper/ops_measure.py`, ARCHITECTURE G-132 addendum, `power-profile-capping.md`.
- Stress options and result wording -> `web/app.js` (ST_*, stSummary), ARCHITECTURE G-116 addendum.
- Guard check order and refusals -> `guard/guard.py` docstring; audit fields -> `guard/audit.py`; policy keys -> `policies/default.toml`.
- Windows restore: identity fingerprint hard-coded to the reference 285K -> `msr/winport.py:52`, `cli.py:527`.

## Open questions for the orchestrator
1. **Windows restore is reference-chip only.** `msr/winport.py` hard-codes the Linux->Windows LP map and the P-core fused fingerprint of the reference 285K; any other chip is
   refused by `--device-win`. Guide says so. Conflicts with the "public project, live data" goal (D-036)?
2. **Ratio ceiling:** D-038 says auto = 0x1C + 3; code (`guard/policy.py fused_ratio_cap`) and `policies/default.toml` header still say plain fused max until G-134 merges. Guide
   (02, 07, 12) states D-038 as the rule and flags the pre-G-134 behaviour. Edit if G-134 lands differently.
3. **G-133 not merged.** Guide 06/07 use the new labels and the OC-ratio-first model from the task file, and say "per the G-133 specification / recheck". See the recheck list.
4. **Stale file names in other docs:** ARCHITECTURE contract 3 and SPEC §3 name `platforms/arl-s-285k.toml` (file is `arl-s.toml`); SPEC-v3 §6 still says "ratio <= P 57 / E 49"
   and "Read tuning words" while the code says "Read ratios & voltages"; `docs/USER-GUIDE.md` still says "Read tuning words". Not edited (not my files).
5. `README.md` (root) says "all 17 checks passed"; the self-check prints the live count (`all N checks passed`), the guide says N.
6. Dense measurement: tooltip says "about 10 minutes", measured 12 minutes (2026-10-11). Guide gives both.
7. The helper's measure uses settle 0.3 s / 10 samples, the CLI `vf-dump --measure` uses 2.5 s / 30 samples (`vfdump.py`). R-001 notes mention "the app's 2.5 s" (CLI value). Guide documents them separately.
8. Package names for Debian/Ubuntu: repo only gives hints for perf, turbostat, lm-sensors (`OPTIONAL_TOOLS`). stress-ng / stressapptest names are given without distro tags.
9. Reading the E cluster grouping: SPEC-v3 tags "one regulator per cluster of four" [MEASURED]; registers.md tags it [USER][COMMUNITY]. Guide cites both.
10. `docs/ARCHITECTURE.md` stress addendum says the ratio cap sets EPP `performance`; the G-132 addendum says the EPP write is skipped under the performance governor. Guide does not mention EPP for Stress.

## Recheck after merges (mechanical list)
After G-133 merges, re-verify these strings in 06/07 and README:
- scope control: code now `Simple` / `Per unit` (`web/app.js:4040`) -> expected `All-Core` / `Per-Core`.
- offset control: code `Whole curve` / `Per point` (`web/app.js:2838`) -> expected `Whole Curve Offset` / `Per V/F point Offsets`.
- OC box texts ("No OC ratio configured", "OC ratio r x, uses fused point n (offset only)", "OC point n, r x, adaptive V"), ratio dropdown rules, prefilled target, tag
  "fused, offset only, above this core's 55x limit", header line "offsets ... OC ratio: none, runs up to 48x", live limits (the code still has a literal `POLICY` at `web/app.js:1193`).
- "(interpolated)" label on predictions for unmeasured points.
After G-134 merges: 02 "What the default policy allows", 07 §2, 12 §4 (`+3`).
After any web change: tab list, button names (style decisions above), Curves reason texts (`web/app.js:1824-1836`).

## Next
- Add screenshots only if the orchestrator provides them (none taken: no hardware, no browser by rule).
- Page 11: extend with real user problems; page 07: add P-core OC results when one is validated; page 05: add a Dense example if wanted.
- Convert `docs/USER-GUIDE.md` into a pointer (orchestrator's call).
- Consider a short glossary page (unit, OC point, DLVR, VID, VccIA, WRMSR) if readers ask.

## Log (newest first)
- 2026-10-12 (first run, branch task/G-135-user-guide): created NOTES.md and all 13 pages (README, 01..12) from the repo only (docs/knowledge, DECISIONS, SAFETY, SPEC-v3,
  ARCHITECTURE, install.sh, policies/default.toml, web/index.html + app.js labels, cli.py `--help`, scripts headers). Ran only `python3 tools/arltuner.py ... --help`; no hardware, no sudo.
  Link check over `docs/guide` found no broken relative links. Commits `docs(guide): ...` per page.
