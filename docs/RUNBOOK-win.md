# RUNBOOK — applying the accepted profile on Windows 11 (`win`)

> Carried over 2026-10-08 from `linuxmon@b01f78a` (`~/AI/linuxmon`). Paths `docs/research/*`, `docs/tasks/T-0xx` and `captures/*` cited here live in that repo (see `docs/PROVENANCE.md`); "linuxmon" = this project's former name.

How the operator loads ThrottleStop's signed driver, re-applies the accepted tuning (`profiles/CURRENT`) through `linuxmon --device-win`, then benchmarks.
Rules behind it: `AGENTS.md`, `docs/SAFETY.md`. Driver facts: `docs/research/win-throttlestop-fivr.md` §1, `win-throttlestop-9.7.3.md`.
All paths relative to the repo (`C:\Users\yk\AI\arrowlake-tuner`). Use an **elevated** PowerShell (Run as administrator). `py -3` below = `python` if you have no launcher.
The driver is never stored in the repo: it is carved from your own `ThrottleStop.exe` (hash-verified) into `%LOCALAPPDATA%\linuxmon\ThrottleStop.sys`.

## 0. Before any sitting
- Operator at the machine, explicit go-ahead. Only linuxmon may write the mailbox: no ThrottleStop GUI, OCCT, XTU client or HWiNFO running (HWiNFO read-only is tolerated but close it for the first apply).
- Warm or cold boot clears every per-point offset (re-apply after **every** boot). BIOS must already hold the settings in §6.

## 1. Get the repo
```
cd C:\Users\yk\AI\arrowlake-tuner          # first time: git clone <private GitHub URL> C:\Users\yk\AI\arrowlake-tuner
git pull
```

## 2. Preflight (read-only, reports only)
```
powershell -ExecutionPolicy Bypass -File tools\win\preflight.ps1
```
Fix every `FIX` line. The driver needs **Memory integrity OFF** and the **Microsoft Vulnerable Driver Blocklist OFF**
(Windows Security > Device security > Core isolation; reboot after each toggle). Re-enable both when the sitting is over.

## 3. Driver (carve + install once per Windows install)
```
py -3 tools\win\ts_driver.py carve --exe C:\Users\yk\Downloads\ThrottleStop_9.7.3\ThrottleStop.exe
py -3 tools\win\ts_driver.py install
py -3 tools\win\ts_driver.py status
```
`carve` refuses unless the exe is 9.7.3 (SHA-256 `52D86D3D…2BA2`) and the 50,216-byte cut is the TechPowerUp driver (`16F83F05…97F0`).
`install` = `sc create ThrottleStop type= kernel start= demand` + `sc start`; idempotent. `status` must show `RUNNING` and `\\.\ThrottleStop opens OK`.
If start fails with 1275 / 577 / "blocked": see the blocklist / Memory integrity note in §2.
**After each later boot** the service is stopped (demand start): `sc start ThrottleStop` (or re-run `install`).

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
- **Revert:** reboot (offsets are volatile; stock returns). To drop the driver without rebooting: `sc stop ThrottleStop` (does **not** undo applied offsets). Full removal: `py -3 tools\win\ts_driver.py remove`.
- Per boot: `sc start ThrottleStop`, `Stop-Service XtuService` (or `Set-Service XtuService -StartupType Manual` once), then step 4.2 and 4.3.

## 6. BIOS settings the tuning assumes (header of `profiles/tune-2026-10-02.toml`)
PL1 = PL2 = 295 W · board VccIA offset -100 mV · ring OC 40x with -16 mV on its 39x/40x points · DDR5 7200 · TVB voltage optimisations off · E-cores 48x OC word.
Accepted state: run 18, 15-min OCCT CPU SSE clean on Linux. Windows results go to `docs/research/win-*.md`.

## 7. Tools (`tools/win/`)
| Tool | What | Hardware access |
|---|---|---|
| `preflight.ps1` | admin, Memory integrity, blocklist, XtuService, other mailbox clients, Python, driver state | none |
| `ts_driver.py carve/install/status/remove` | carve the driver from your exe, manage the `ThrottleStop` kernel service | loads/unloads a kernel driver (operator only) |
| `reapply_current.ps1 [-Check]` | apply `profiles/CURRENT` via `--device-win`; `-Check` = read-only vf-dump | writes point offsets via the guard |
| `restore_tuning.ps1 [-Orchestrated]` | after any boot: checks → driver start → vf-dump → apply `profiles/CURRENT` → vf-dump compared word-for-word with the accepted state → driver stop | writes point offsets via the guard |

## Safety nets built in (2026-10-02)
- **Core identity check:** every `apply` / `point-offset` / `vf-dump` with `--device-win` first reads the fused V/F point (cmd `0x09 P1=0x11`) of P-cores
  0–7 and compares it with the values read on Linux (`captures/lnx/fused-0x09-cpu*.txt`: `0x5B439 0x53839 0x57439 0x59439 0x54439 0x56C39 0x56039 0x58039`,
  all distinct). A mismatch = the Linux→Windows core map is wrong ⇒ refused, nothing written. The E clusters / P-vs-E split are checked from Windows' own
  CPU-set and L2-cache data before the driver is even opened.
- **Health bracket:** new WHEA-Logger events in the System log during a session ⇒ fail (Windows' stand-in for the Linux taint/MCE check).
- `reapply_current.ps1` records each apply with `Start-Transcript` (Windows PowerShell 5.1 may not capture native output in a transcript); the complete
  record of every request is the audit log `%LOCALAPPDATA%\linuxmon\audit.jsonl`.
- Untested on real Windows: the first run of `reapply_current.ps1 -Check` (read-only) is the bring-up test. If the driver's RDMSR returns a size other than
  8 bytes, or pinning fails, it stops with an error — nothing is written.
