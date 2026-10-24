# Windows restore — accepted state good-p57-e47 (2026-10-24, for the Claude agent on Windows)

This SUPERSEDES `captures/win/accepted-20261020-p58-e47-2/` (the P0 58× OC state — do not restore that one any more). Read `AGENTS.md` and
`docs/SAFETY.md` first; host `win`. The general procedure (prerequisites, preflight, init-dir, carve, rules) is the same as in
`captures/win/accepted-offsets-20261020/WINDOWS-RESTORE-NOTE.md` — follow its steps 1–7, but copy THIS folder in step 4:

    robocopy captures\win\accepted-20261024-p57-e47 %ProgramData%\arltuner\accepted /E /PURGE

`/PURGE` removes the old p58 profile from the accepted directory. Then run `ts_driver.py check-dir` and `restore_tuning.ps1` as before.

## What changed from good-p58-e47-2, and why
- **P0 58× OC point removed.** P0 tops out at its fused 57× again; the P turbo table is back to the BIOS default `57,57,55×6`.
- Everything else is identical (`profiles.diff` on Linux: 4 rows — P0 OC ratio 58 → none, P0 OC target 1.426 V → none, P turbo buckets 0–1 58 → 57).
- Why (operator, 2026-10-24): the Linux boot that ran p58 for Oct 22–24 logged 23 corrected machine checks, including a `CPU0 BANK0 CMCI storm`
  [MEASURED lnx 2026-10-24]; 58× gave little performance. An earlier 24 h boot on p58 had none, so the OC is the suspect, not a proven cause.

## What it restores (copied from `/etc/arltuner/accepted` on Linux, made by the app's `profiles.accept` from a verified read after the apply)
- **P-cores:** the known-good undervolt, offsets as in the offsets-only note's table (P0/P3 57× −75.2 mV, clamps to V(56×)).
- **P0:** no OC point. Its OC word is `39` (57×, no voltage target) — the word a cold boot leaves; the compare treats it as OK / EQUIVALENT.
- **E-clusters:** OC point 47×: **EC0 (Linux CPUs 8–11) 1.097 V** (word `4632F`), **EC1–3 1.077 V** (word `44F2F`), slot `2F`; point 6 (46×) −99.6 mV on all.
- **Turbo tables:** P `57,57,55,55,55,55,55,55` (= BIOS, so no P write is expected) and E `47×8` (raised from the BIOS `46×8`). Plain MSR writes (0x1AD / 0x650)
  through the same guarded port.
- **Ring:** compared only, never written (BIOS default).
- `fingerprint.json`: the fused 0x09 words are identical to the p58 folder's. `favored` is now false on every CPU (Linux CPPC `highest_perf` is 92 on all P cores
  without the OC); the Windows restore does not use that flag.

Testing on Linux: the E 47× + P undervolt underneath passed 10/10 balanced and 10/10 hpc-compute-zram (llama.cpp b11748) with P0 at 58×; at 57× P0 runs
inside the fused curve with the 57× offset that passed 15/15 + stress-ng P0+P3 at 57× (GOOD-STATE.md). Applied on Linux 2026-10-24 14:21: all writes read back equal, MCE 0.

## What to expect
- **The run order:** the identity check, then the apply creates the E OC points and writes the E turbo table, each write read back. A cold boot gives the cleanest start.
- **After a warm reboot from a p58 state** (Windows or Linux): a residual P0 58× OC point (word `5B43A`) can survive. The apply's `--neutralize-residual`
  rewrites it; with P turbo at 57× the core cannot run 58× anyway. If the compare says BAD on P0 only, report it and suggest a cold boot.
- **Restore verdict:** the compare must report RESTORED. OK and EQUIVALENT lines both count.
- **Check under load in HWiNFO:**
  - All-core load: E-cores 47× at about 1.098 V (EC0) / 1.078 V (EC1–3) VID; P-cores 55× at about 1.27 V.
  - One thread pinned to the core Windows maps to Linux CPU 0: **57×** at about 1.37 V (no longer 58× / 1.43 V).
- **If an OC-word write is refused or trips,** stop and report. Do not retry.
- **Revert:** a cold boot returns the CPU to the BIOS state.
