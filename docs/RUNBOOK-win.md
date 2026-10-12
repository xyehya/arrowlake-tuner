# RUNBOOK — applying the accepted profile on Windows 11 (`win`)

> Carried over 2026-10-08 from the project's earlier research phase. Paths `docs/research/*`, `docs/tasks/T-0xx` and `captures/*` cited here name notes of that phase, which are not part of this repository.

How the operator loads ThrottleStop's signed driver, re-applies the accepted tuning (`profiles/CURRENT`) through `arltuner --device-win`, then benchmarks.
Rules behind it: `AGENTS.md`, `docs/SAFETY.md`. Driver facts: `docs/research/win-throttlestop-fivr.md` §1, `win-throttlestop-9.7.3.md`.
All paths relative to the repo (`%USERPROFILE%\AI\arrowlake-tuner`). Use an **elevated** PowerShell (Run as administrator). `py -3` below = `python` if you have no launcher.
The driver is never stored in the repo: it is carved from your own `ThrottleStop.exe` (hash-verified) into `%ProgramData%\arltuner\ThrottleStop.sys`.
**Admin-only directory (G-142, D-042):** the scripts run elevated, so everything they trust lives in `%ProgramData%\arltuner` (owner Administrators, access for Administrators and
SYSTEM only): the carved driver, the accepted state (`accepted\`: `CURRENT`, `EXPECTED-words.txt`, `fingerprint.json`, `policy.toml`, `profiles\`), the audit log and the read-back
file the RESTORED verdict is computed from. Create it once from an Administrator terminal: `py -3 tools\win\ts_driver.py init-dir`; then copy the accepted state in
(`xcopy /e /i <where you put it> %ProgramData%\arltuner\accepted`). Scripts refuse a `-From` / `-Policy` directory that is not admin-only (`ts_driver.py check-dir DIR`), and a
pre-existing `%ProgramData%\arltuner` that is not (inspect it, `rmdir /s /q` it, run `init-dir`). The per-user copies of earlier versions (`%LOCALAPPDATA%\arltuner\ThrottleStop.sys`,
`...\accepted`, `audit.jsonl`) are no longer read: delete the driver copy; move the accepted state. Moving the audit log restarts the per-boot op count on Windows
for the boot you move it in (the cap is an indicator, D-032). **Untested on Windows** - written on Linux; the first run of each changed script is its bring-up test.

## 0. Before any sitting
- Operator at the machine, explicit go-ahead. Only arltuner may write the mailbox: no ThrottleStop GUI, OCCT, XTU client or HWiNFO running (HWiNFO read-only is tolerated but close it for the first apply).
- Warm or cold boot clears every per-point offset (re-apply after **every** boot). BIOS must already hold the settings in §6.

## 1. Get the repo
```
cd %USERPROFILE%\AI\arrowlake-tuner          # first time: git clone https://github.com/xyehya/arrowlake-tuner %USERPROFILE%\AI\arrowlake-tuner
git pull
```

## 2. Preflight (read-only, reports only)
```
powershell -ExecutionPolicy Bypass -File tools\win\preflight.ps1
```
Fix every `FIX` line. The driver needs **Memory integrity OFF** and the **Microsoft Vulnerable Driver Blocklist OFF**
(Windows Security > Device security > Core isolation; reboot after each toggle). Re-enable both when the sitting is over.

## 3. Driver (carve once per Windows install; the service is created per run)
```
py -3 tools\win\ts_driver.py init-dir
py -3 tools\win\ts_driver.py carve --exe %USERPROFILE%\Downloads\ThrottleStop_9.7.3\ThrottleStop.exe
py -3 tools\win\ts_driver.py start
py -3 tools\win\ts_driver.py status
```
`carve` (elevated) refuses unless the exe is 9.7.3 (SHA-256 `52D86D3D…2BA2`) and the 50,216-byte cut is the TechPowerUp driver (`16F83F05…97F0`), and writes it into the
admin-only directory. `start` (elevated; `install` is the same) checks the directory's ACL and the file's SHA-256, `sc create ThrottleStop type= kernel start= demand binPath=` the
admin-only copy when the service is absent or points elsewhere, checks both again **immediately before** `sc start`. `status` must show `RUNNING` and `\\.\ThrottleStop opens OK`.
`restore_tuning.ps1` does `start` before and `remove` (stop + delete) after every run: no service lingers pointing at a driver file.
If start fails with 1275 / 577 / "blocked": see the blocklist / Memory integrity note in §2.
**After each later boot** run `start` again (or just `restore_tuning.ps1`).

## 4. Check, apply, check
Stop the Intel XTU service first (it may re-apply its own settings; real name `XtuService`):
```
Stop-Service XtuService
```
1. **Read-only first** — a fresh boot should show stock words (P points around 0x35/0x37/0x38/0x39, offsets +0.0 mV; CPU 0 OC word 0x39):
```
powershell -ExecutionPolicy Bypass -File tools\win\reapply_current.ps1 -Check
```
Type the `CONFIRM <token>` it prints (reads are WRMSRs to MSR 0x150 and are guarded too).
2. **Apply** — one CONFIRM per profile in `profiles/CURRENT` (`e-p6-split3`, `p-step3` = `profiles/tune-2026-10-02.toml`); output is also saved to `captures\win\reapply-<name>-<ts>.txt`:
```
powershell -ExecutionPolicy Bypass -File tools\win\reapply_current.ps1
```
3. **Check again** — the point offsets must equal the Linux readback `captures/lnx/vfdump-accepted-run18.txt` (same offsets, Linux CPU numbering):
```
powershell -ExecutionPolicy Bypass -File tools\win\reapply_current.ps1 -Check > captures\win\vfdump-after-apply.txt
```
If anything differs or a command errors, stop and report; do not retry blindly (one transaction at a time, per-boot write cap in the guard).

## 5. Benchmark, revert
- Benchmark now (OCCT / Cinebench / games). Do not start XTU or the ThrottleStop GUI, and do not let OCCT apply its own voltage/ratio settings: they write the same mailbox and would overwrite or fight the profile.
- **Revert:** reboot (offsets are volatile; stock returns). To drop the driver without rebooting: `py -3 tools\win\ts_driver.py remove` (stop + delete the service; does **not** undo applied offsets).
- Per boot: `py -3 tools\win\ts_driver.py start`, `Stop-Service XtuService` (or `Set-Service XtuService -StartupType Manual` once), then step 4.2 and 4.3.

## 6. BIOS settings the tuning assumes (header of `profiles/tune-2026-10-02.toml`)
PL1 = PL2 = 295 W · board VccIA offset -100 mV · ring OC 40x with -16 mV on its 39x/40x points · DDR5 7200 · TVB voltage optimisations off · E-cores 48x OC word.
Accepted state: run 18, 15-min OCCT CPU SSE clean on Linux. Windows results go to `docs/research/win-*.md`.

## 7. Tools (`tools/win/`)
| Tool | What | Hardware access |
|---|---|---|
| `preflight.ps1` | admin, Memory integrity, blocklist, XtuService, other mailbox clients, Python, driver state | none |
| `ts_driver.py init-dir/carve/start/status/remove/check-dir` | create the admin-only directory, carve the driver from your exe, manage the `ThrottleStop` kernel service (hash + ACL re-checked before every start) | loads/unloads a kernel driver (operator only) |
| `reapply_current.ps1 [-Check]` | apply `profiles/CURRENT` via `--device-win`; `-Check` = read-only vf-dump | writes point offsets via the guard |
| `restore_tuning.ps1 [-Orchestrated]` | after any boot: checks → driver start → vf-dump → apply `profiles/CURRENT` → vf-dump compared word-for-word with the accepted state → driver stop | writes point offsets via the guard |

## Safety nets built in (2026-10-02)
- **Core map and identity check (G-138):** nothing about a particular chip is stored in the code or the scripts. The accepted state carries `fingerprint.json`
  (written by *Profiles > Set as accepted* on Linux: per Linux CPU its kind, E cluster and fused V/F point word, plus the CPU's family / model / brand); copy the whole
  accepted directory, it must be in `-From DIR`. Without it the scripts stop with "re-accept on Linux to record the fingerprint" (an accepted state from before this
  existed has none: set it as accepted again on Linux). At run time `--device-win` (`--fingerprint FILE`, the scripts pass it):
  1. refuses unless Windows reports the same CPU family / model / brand as the fingerprint, before the driver is opened;
  2. derives a first guess of the Linux CPU -> Windows logical-processor map from Windows' own data: P LPs = the higher EfficiencyClass (ascending), E clusters =
     LP sets sharing one L2 (ordered by lowest LP), and holds it against what Windows reports (P private L2, E cluster = one L2, counts);
  3. every `apply` / `point-offset` / `vf-dump` then reads the fused V/F point (cmd `0x09 P1=0x11`) through the guard on each P LP and the first LP of each E cluster
     (8 + 4 reads on an 8P + 16E part; the audit log notes them as provisional slots) and matches the words with the recorded ones: every P word distinct, each
     matching exactly one recorded P-core (this settles the P order, which Windows cannot tell), each E cluster reading its recorded word. Any other outcome is a refusal and
     nothing is written.
  `read-vf` and `read` with `--device-win` send one request and run no identity check (they print a note): the P-core they hit is only the first guess.
- **What the Windows restore refuses** (all before any write): no `fingerprint.json` ("re-accept on Linux to record the fingerprint"); a fingerprint without CPU identity or
  without a fused word for every P-core / E cluster; two P-cores with the same recorded word; another CPU (vendor / family / model / brand) or one Windows will not name;
  a layout that differs from the fingerprint (LP counts per class, E cluster sizes, processor groups other than 0, no L2 data); P words read twice, not recorded, or not
  all read; an E cluster reading another word than recorded.
- **Health bracket:** new WHEA-Logger events in the System log during a session ⇒ fail (Windows' stand-in for the Linux taint/MCE check).
- `reapply_current.ps1` records each apply with `Start-Transcript` (Windows PowerShell 5.1 may not capture native output in a transcript); the complete
  record of every request is the audit log `%ProgramData%\arltuner\audit.jsonl`.
- Untested on real Windows: the first run of `reapply_current.ps1 -Check` (read-only) is the bring-up test. If the driver's RDMSR returns a size other than
  8 bytes, or pinning fails, it stops with an error — nothing is written.
