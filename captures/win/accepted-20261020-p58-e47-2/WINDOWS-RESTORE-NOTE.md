# Windows restore — accepted state good-p58-e47-2 (2026-10-20, for the Claude agent on Windows)

> **SUPERSEDED 2026-10-24 — do not restore this folder.** Use `captures/win/accepted-20261024-p57-e47/WINDOWS-RESTORE-NOTE.md`
> (the same state without the P0 58× OC point; D-051). Kept for history only.

This SUPERSEDES `captures/win/accepted-offsets-20261020/` (the offsets-only state). Read `AGENTS.md` and `docs/SAFETY.md` first; host `win`.
The general procedure (prerequisites, preflight, init-dir, carve, rules) is the same as in
`captures/win/accepted-offsets-20261020/WINDOWS-RESTORE-NOTE.md` — follow its steps 1–7, but copy THIS folder in step 4:

    robocopy captures\win\accepted-20261020-p58-e47-2 %ProgramData%\arltuner\accepted /E /PURGE

`/PURGE` removes the old offsets-only profile from the accepted directory. Then run `ts_driver.py check-dir` and `restore_tuning.ps1` as before.

## What it restores (copied from `/etc/arltuner/accepted` on Linux, made by the app's `profiles.accept`)
This is the full state, not offsets only:
- **P-cores:** the known-good undervolt. Point offsets are the same as in the offsets-only note's table.
- **P0:** OC point 58× at 1.426 V, its fused 57× voltage. The word is `5B43A`, plus the OC table slot `3A`.
- **E-clusters:** OC point 47×: **EC0 (Linux CPUs 8–11) 1.097 V** (word `4632F`), **EC1–3 1.077 V** (word `44F2F`), slot `2F`; point 6 (46×) −99.6 mV on all.
- **Turbo tables:** P `58,58,55,55,55,55,55,55` and E `47×8`. These are plain MSR writes (0x1AD / 0x650) through the same guarded port. The BIOS has P `57,57,55×6` and E `46×8`, so the restore raises them.
- **Ring:** compared only, never written (BIOS default).

Testing on Linux, all-core llama.cpp compile builds:
- this state passed 10/10 under tuned balanced and 10/10 under hpc-compute-zram (llama.cpp b11748); the uniform 1.077 V variant failed on EC0 under hpc-compute-zram;
- the P/E undervolt underneath it passed 15/15.

The operator also plans OCCT/Linpack runs.

## What to expect
- **The run order:** the identity check runs first, then the apply creates the two OC points and writes the turbo tables, each write read back. A cold boot gives the cleanest start.
- **Restore verdict:** the compare must report RESTORED. OK and EQUIVALENT lines both count.
- **Check under load in HWiNFO:**
  - All-core load: E-cores 47× at about 1.098 V (EC0) / 1.078 V (EC1–3) VID; P-cores 55× at about 1.27 V.
  - One thread pinned to the core Windows maps to Linux CPU 0: 58× at about 1.43 V.
- **The 58× and 47× OC points are new on Windows.** If an OC-word write is refused or trips, stop and report. Do not retry.
- **Revert:** a cold boot returns the CPU to the BIOS state.
