# G-145 — knowledge docs review: general Arrow Lake-S facts, chip-specific data moved out

- **Status:** queued (starts after G-144 merges) · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-145-knowledge-review` · **Agent:** Opus 5.5

## Goal (operator 2026-10-12)
"a full knowledge docs review … remove generic stuff and info relevant to my specific cpu and rely on general facts and info/knowledge we already
collected and validated or reported by a trustworthy source in case we didn't validate". Trigger: `favored-cores.md` needs a revamp.

Scope: every file in `docs/knowledge/` (registers, linux-path, windows-path, mailbox-recon-summary, memory-mchbar, platform-285k,
power-profile-capping, telemetry-sources, tuning-results, favored-cores).
1. **Keep** only facts that hold for Arrow Lake-S in general (or are clearly scoped, e.g. "on 285K"), each tagged: `[MEASURED host date]`
   (validated here), `[DECOMPILED …]`, `[DOC url]` (Intel/kernel/vendor docs), `[COMMUNITY url]` from a trustworthy source (SkatterBencher,
   kernel mailing lists, well-known reviewers) when we did not validate it, `[UNVERIFIED]` only if it is a useful open lead.
2. **Remove** generic filler (tutorial prose, restated common knowledge, speculation, hypotheses already settled, process/session narrative,
   "next steps" that are done, duplicated content).
3. **Move out** what is specific to the operator's chip/board (per-core fused voltages, offsets, tuning-gate history, sample VIDs, BIOS
   settings, the accepted profiles' numbers) to `examples/reference-285k/notes/` (one or two files, labelled "one sample, not a spec"). Keep at
   most a one-line example in the general doc when it illustrates a rule, labelled as an example from one chip.
4. **Revamp `favored-cores.md`**: what favored cores are (fused, CPPC/HWP highest perf, ITMT, 0x1C), how the app reads them live, what is
   known vs open, how to characterise cores — general, sourced; the 285K tables go to the examples notes.
5. Rename where a name is chip-specific (`platform-285k.md` → e.g. `platform-arl-s.md`) and **update every reference in the repo**
   (`git grep` for each old name: src comments, tests, docs/guide, README, ARCHITECTURE, SPEC*, DECISIONS keep history but fix links).
   `registers.md` stays the single source of truth for encodings (keep its name and section anchors that code cites; check `git grep
   "registers.md"` hits still resolve).
6. Contradictions between docs: resolve with the newest MEASURED fact; list the ones you could not resolve in the report.
7. Add `docs/knowledge/README.md`: index (one line per file), the evidence-tag legend, and the rule "general facts here; one chip's numbers in
   examples/reference-285k/notes/".

## Owns
`docs/knowledge/**`, `examples/reference-285k/**`, reference fixes in any file (link/path text only, no code logic).

## Acceptance
`python3 tools/run_tests.py` green; no broken relative links in docs (check them); report: per file what was kept/moved/removed, renames,
unresolved contradictions, facts that need validation.
