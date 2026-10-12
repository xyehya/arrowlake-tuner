# G-138 — Windows restore for any Arrow Lake-S (no 285K constants)

- **Status:** ready · **Host:** lnx (code) / win (untested) · **Tier:** T0 · **Branch:** `task/G-138-windows-restore-generic`

## Problem (documenter finding, G-135)
`src/arltuner/msr/winport.py:44-52` hard-codes the reference chip's Linux CPU → Windows LP map and the P-core fused fingerprint
(`WINDOWS_P_FUSED_FINGERPRINT`); `cli.py` `IDENTITY_*` uses them. Any other chip is refused. D-036: nothing chip-specific in the code.

## Goal
1. The map is **derived at run time on Windows**: P LPs and E LPs from `GetSystemCpuSetInformation` efficiency class (existing `CpuSet`),
   E clusters from shared L2 (existing `L2Cache`), then:
   - P: read cmd 0x09 P1=0x11 on each Windows P LP (guarded, one at a time) and match each word to the Linux P-core with the same fused word
     **recorded in the accepted state**; all words must be distinct and every one must match exactly once, else refuse (nothing written).
   - E: clusters in ordinal order of their lowest LP ↔ Linux clusters in order; cross-check the cluster's 0x09 word against the recorded one;
     mismatch ⇒ refuse.
2. The accepted state records what that needs: on `profiles.accept` (Linux, helper) write `fingerprint.json` into the accepted dir — per
   Linux CPU its kind, cluster, and 0x09 word, plus CPU family/model and the CPU brand string. Old accepted dirs without it: the Windows restore
   refuses with "re-accept on Linux to record the fingerprint". (The orchestrator will re-accept on the reference machine.)
3. Move the 285K map/fingerprint to tests/fixtures (and `examples/reference-285k/` if useful as documentation); none in `src/`.
4. `tools/win/*.ps1` and docs/RUNBOOK-win.md: adjust any reference to the fixed map. Keep "untested on Windows from this repo" wording.
5. Tests with a fake Windows port: the reference map derived from fixture data; a shuffled P order still maps right; duplicate fused words
   refuse; missing fingerprint refuses; E mismatch refuses.

## Owns
`src/arltuner/msr/winport.py`, `src/arltuner/cli.py` (identity/--device-win only), `src/arltuner/topology/reference.py` (if only used here),
`src/arltuner/profiles/**` and the helper's `profiles.accept` handler (fingerprint write only), `tools/win/**`, `docs/RUNBOOK-win.md`,
`tests/**` (not tests/web). Not `src/arltuner/guard/**` or `helper/ops_tune.py` (G-137).

## Acceptance
`python3 tools/run_tests.py` green.
