# Windows restore — offsets only (for the Claude agent on Windows)

Written 2026-10-20 on Linux by the orchestrator. Repo copy: `C:\Users\yk\AI\arrowlake-tuner` (copied from Linux; `.git` included, `origin` = the GitHub repo).
Read `AGENTS.md` and `docs/SAFETY.md` first. You are on host `win`.

## What this restores
The accepted state in this folder (`captures\win\accepted-offsets-20261020\`):
- **V/F point offsets only** for the 8 P-cores and 4 E-clusters, as the CPU held them on Linux. That state passed 15/15 all-core compile builds.
- **OC words stay at stock (0).**
- **No turbo tables and no ring.** The operator sets those in the BIOS: P turbo 55× all cores, E 46×, PL1 250 W, PL2 295 W.

| Unit | Point 5 (53×) | Point 6 (55× P / 46× E) | Point 7 (56×) | Point 8 (57×) |
|---|---|---|---|---|
| P0, P3 (favored) | −49.8 | −28.3 | −14.6 | −75.2 |
| P1 | −49.8 | −36.1 | 0 | 0 |
| P2 | −71.3 | −95.7 | 0 | 0 |
| P4 | −49.8 | −48.8 | 0 | 0 |
| P5 | −60.5 | −87.9 | 0 | 0 |
| P6 | −49.8 | −76.2 | 0 | 0 |
| P7 | −71.3 | −106.4 | 0 | 0 |
| E-clusters 0–3 | 0 | **−99.6 (46×)** | — | — |

All values are mV. P points 1–4 and E points 1–5 are 0. The exact words are in `EXPECTED-words.txt`.

The files and how they were made:
- `gen.py` built the profile (`profiles\win-offsets-20261020.toml`) and `EXPECTED-words.txt` from a verified Linux vf-dump.
- `fingerprint.json` maps Linux CPUs to Windows logical processors. It holds the fused 0x09 words, which are fixed per chip.
- `policy.toml` is a copy of `/etc/arltuner/policy.toml`.

## Rules (non-negotiable)
- Every hardware step needs the operator's explicit "go" in chat. Mailbox reads are WRMSRs too. One run at a time; never loop or retry a failed write; never work around a refusal.
- Install nothing without naming it first (name, source, size) and getting a yes. This includes Python and ThrottleStop.
- The ThrottleStop driver is carved from the operator's own `ThrottleStop.exe` (9.7.3) and never committed. `restore_tuning.ps1` creates the service for its own run and deletes it afterwards.
- No XTU, OCCT, HWiNFO or ThrottleStop GUI running during the restore. Run OCCT unelevated afterwards.
- Revert = a full cold boot (power off for about 10 s).

## Steps (elevated PowerShell, in `C:\Users\yk\AI\arrowlake-tuner`)
1. **Prerequisites.** Windows was freshly formatted, so ask the operator to install or provide:
   - Python ≥ 3.12 (`py -3`) from python.org.
   - `ThrottleStop.exe` 9.7.3.
2. **Read-only check:** `powershell -ExecutionPolicy Bypass -File tools\win\preflight.ps1`. Fix anything marked FIX. Usually that means:
   - turning Memory integrity off;
   - turning the Vulnerable Driver Blocklist off (it needs a reboot);
   - stopping XtuService.
3. **Admin-only directory:** `py -3 tools\win\ts_driver.py init-dir`, which creates and locks `%ProgramData%\arltuner`.
4. **Copy the accepted state there** (elevated):
   `robocopy captures\win\accepted-offsets-20261020 %ProgramData%\arltuner\accepted /E`
   Then check it with `py -3 tools\win\ts_driver.py check-dir %ProgramData%\arltuner\accepted`, which must exit 0.
5. **Carve the driver:** `py -3 tools\win\ts_driver.py carve --exe <path>\ThrottleStop.exe`. It is hash-verified and goes into `%ProgramData%\arltuner\ThrottleStop.sys`.
6. **Restore.** This writes to the hardware, so get the operator's go first. Run `powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1`.
   - The operator types CONFIRM once. Use `-Orchestrated` only with a go in chat.
   - The script then:
     - runs the identity check (fused 0x09 words);
     - applies with `--neutralize-residual` (only the words that differ, each read back);
     - does a vf-dump read-back;
     - judges it with `compare-accepted` against `EXPECTED-words.txt`;
     - removes the driver.
7. **Report.** Capture files go to `captures\win\restore-<ts>-*.txt`. Report `RESTORED` (every core OK or EQUIVALENT) or `STOP` with the per-core lines. Commit the small logs with `G-NNN`-free messages, e.g. `win: offsets restore <date>`, and push if the operator wants it.

## What to expect
- **After a cold boot:** every line should read OK. A P0/P3 OC word of 0x39 shows as EQUIVALENT ("OC word without an OC point"), which counts as success.
- **After a warm reboot from Linux:** a residual E 48× OC point (word 0x4BD30) can survive. The apply's `--neutralize-residual` rewrites it so the clusters run the accepted voltage, and the compare should then say EQUIVALENT. If it says BAD on the E lines only, report it and suggest a cold boot.
- **First real Windows run of this code path** (`--device-win`, written on Linux): read the ts_driver and arltuner output carefully and stop at the first surprise.

## Verifying under load (after RESTORED)
- HWiNFO (unelevated is fine for reading) under an all-core load such as Cinebench or y-cruncher should show:
  - E-cores at 46× with VID about 1.058 V (EC0) and 1.038 V (EC1–3);
  - P-cores at 55× with about 1.27 V on the P rail.
- On Linux at the same settings: P 53× ≤ 1.200 V, 55× about 1.270 V.
- These are the voltages the CPU requests (VID), not Vcore. Windows load lines and the BIOS can make Vcore differ.
