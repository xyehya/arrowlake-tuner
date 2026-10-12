# 09 Profiles and restore

This page covers keeping a tuning you trust and getting it back after a reboot: profiles, the **accepted state**, **Restore accepted**, the Linux
restore script and the Windows PowerShell restore. It is for anyone whose tuning has passed their gate ([08](08-stability-testing.md)).

**Nothing is applied automatically at boot.** The tool never touches the CPU until you start it and confirm a plan. A cold boot returns the CPU to
stock ([04 §11](04-arrow-lake-vf-primer.md)), so after every boot you restore deliberately.

## 1. Three words
| Term | Meaning |
|---|---|
| **Profile** | a saved set of intended changes (offsets, OC ratio, adaptive voltage, turbo tables) stored as a TOML file under `/var/lib/arltuner/profiles/`. Saving writes nothing to the CPU |
| **Applied state** | what the CPU holds right now. The tool shows it from the last read this boot (the "applied" chip) |
| **Accepted state** | the tuning *you* decided is good: a profile, plus the exact V/F words the CPU read back when it was applied. It lives in `/etc/arltuner/accepted/`. This is what "Restore accepted" brings back |

A fresh install has no accepted state ("No accepted state yet"). It is user data (D-036): the repository ships none. One chip's tuning is kept only as an
illustration in `examples/reference-285k/`, and the app never reads that directory.

## 2. The Profiles tab
| Card | What it does |
|---|---|
| **Accepted state** | shows the chain of accepted profiles in the order **Restore accepted** applies them. Button **Set the current state as accepted** records what the CPU holds now (the last read) as the accepted state; nothing is written to the CPU |
| **Profiles** | the list of saved profiles with a short summary. Per profile: **Load into Tune** (fills the Tune editor; nothing is sent), **Review and apply** (plan flow), **Set as accepted** |
| **Save the current Tune settings** | saves the unapplied choices from Tune as a profile. Name: lowercase letters, digits, `.`, `_`, `-`, 1 to 64 characters. A saved profile is never overwritten (an existing name is refused: `EXISTS`) |
| **Compare two profiles** | a diff table; the toggle chooses **As applied** (an accepted profile counts as the state after its step of the chain) or **This file only** |

### Making a state accepted
The helper's rule (`ops_accept.py`): **accepted = what the CPU holds now.**

1. Apply the tuning (Tune → Review changes → Apply, read-back matches), and stress-test it.
2. Either press **Set the current state as accepted** (it takes the state from this boot's last read), or choose **Set as accepted** on a saved profile.
3. Confirm the dialog ("Set as accepted?"); it shows what it replaces. The helper writes `/etc/arltuner/accepted/` as a whole, atomically, and records the
   change in the audit log first (it refuses if it cannot).

Refusals you may meet:

| Message | Meaning |
|---|---|
| `NOT_APPLIED` | the saved profile is not what the CPU holds now: apply it first, then set it as accepted. A profile that was never applied cannot be recorded as the state the chip was verified to hold |
| `NO_READ` | nothing was read this boot: use **Read ratios & voltages** in Tune first |
| `NOT_REPRESENTABLE` | a word of the current state cannot be expressed exactly in a profile file (profiles hold whole millivolts); the tool refuses rather than round, because the restore would write a different word |

Layout of the accepted state (readable by the restore scripts and the Dashboard comparison):
```
/etc/arltuner/accepted/CURRENT                 one line per profile, applied in order:  profile = profiles/<name>.toml
/etc/arltuner/accepted/EXPECTED-words.txt      one line per core / E-cluster:  <cpu> <OC word> <point 1> ... <point n>   (hex, no 0x)
/etc/arltuner/accepted/profiles/<name>.toml    the profiles
```
Back it up if you value it (`sudo cp -a /etc/arltuner/accepted ~/accepted-backup`).

### Profile file format
Strict TOML; unknown keys are rejected (`src/arltuner/profiles/schema.py`). The file stores intent, not safety limits: the policy ([12](12-safety-and-internals.md))
judges every plan.

```toml
[meta]
name = "example"
platform = "arl-s"

[[core]]               # the OC word of a unit: optional ratio, adaptive OC voltage, whole-curve offset
kind = "P"             # "P" | "E"
index = 4              # XTU numbering: P 0..7, E 0..15 (E entries are cluster-wide)
ratio = 55
oc_voltage_mv = 1300
offset_mv = -30

[[point]]              # one fused V/F point's offset
kind = "E"
index = 8
point = 6              # P points 1..8, E points 1..6 (read from your CPU: the OC point is [[core]])
offset_mv = -20
expect_ratio = 46      # optional: refuse if the point's ratio read from the hardware differs

[turbo]                # optional; bucket 0 = one active core first, 8 values each
p = [57, 57, 55, 55, 55, 55, 55, 55]
e = [48, 48, 48, 48, 48, 48, 48, 48]
```
Values are examples. The app writes these files for you; you rarely edit one by hand.

## 3. After every boot: Restore accepted
**In the GUI.** Start the GUI ([03](03-first-run.md)), open **Tune**, press **Restore accepted** (the button is disabled until an accepted state exists),
then **Review changes**, check the before/after and the operation count, and **Apply**. The planner reads the live words first and writes only what differs,
so it works from a cold boot (all stock) and from a warm reboot (a mix) alike. Afterwards the Dashboard's "applied" chip and the comparison with the
accepted state tell you whether every word matches.

On the reference machine restoring the whole accepted state took 172 operations and 57 read-back rows, all matching
[MEASURED lnx 2026-10-11, 2026-10-12, tuning-results.md]. Example value from one chip.

**From a terminal.** The restore script restores the accepted state and proves it word for word:
```
bash tools/lnx/restore_tuning.sh            # shows policy + profiles, asks once: Type CONFIRM
bash tools/lnx/restore_tuning.sh --check    # read-only: dump and compare, no writes
bash tools/lnx/restore_tuning.sh --from DIR # restore another accepted directory
bash tools/lnx/restore_tuning.sh --policy FILE
```
| Option | Meaning |
|---|---|
| (none) | prints the policy, profiles and expected-words file, then asks you to type `CONFIRM` (exactly; anything else sends nothing) |
| `--check` | mailbox reads only (they are still WRMSRs), then compare; nothing is written |
| `--from DIR` | accepted state to restore; default `/etc/arltuner/accepted` |
| `--policy FILE` | the root policy for the guarded writes; default `/etc/arltuner/policy.toml` |
| `--orchestrated` | for the project's automation after a go-ahead in chat; skips the prompt. Do not use it unless you know why |

What it does: records a health snapshot (kernel taint, PCI devices, modules), applies each profile listed in `CURRENT` through the guard with `sudo`
(it asks for your password once), dumps the V/F words, compares every one with `EXPECTED-words.txt`, and takes a second health snapshot. It stops at the first
failure and retries nothing. A good run ends:
```
RESTORED: all cores/clusters match the accepted state.
```
and a bad one with `STOP: N core(s)/cluster(s) differ`. It refuses to start when no accepted state exists, the policy is missing, or OCCT is running as root.
Captures are written to `captures/lnx/`. It uses the checkout's Python with `sudo`, as the operator command-line path (D-031), and sets
`ARLTUNER_ALLOW_MAILBOX=1` itself for the guarded commands.

`bash tools/lnx/reapply_current.sh [--check] [--from DIR] [--policy FILE]` is the older, simpler script: it applies each profile in `CURRENT` with one `CONFIRM <token>` per
profile; `--check` dumps the words read-only and compares them.

Operation counts depend on how much differs; the planner budgets the whole plan before sending the first request.

## 4. Windows restore (reference-chip path; untested from this repository)
Windows has no GUI. `tools\win\restore_tuning.ps1` restores the accepted state through a signed third-party driver. **Status: the scripts were used on the
reference machine in the predecessor repository (October 2026); the first run from this repository is still pending, so treat this section as
untested here** (STATE.md).

Important limits:

- The Windows port uses a Linux-to-Windows CPU map and a P-core identity fingerprint **measured on the reference 285K**
  (`src/arltuner/msr/winport.py`: `WINDOWS_LP_OF_CPU`, `WINDOWS_P_FUSED_FINGERPRINT`, the check at `cli.py` `IDENTITY_CPUS`). On any other chip the
  identity check will not match and the tool **refuses and writes nothing**. That is by design (nothing is written to a core it cannot identify), and it means
  the Windows restore is currently usable on the reference chip only.
- It restores; it does not tune. The accepted state comes from Linux: copy `/etc/arltuner/accepted` to `%LOCALAPPDATA%\arltuner\accepted` (or pass `-From DIR`)
  and put a `policy.toml` next to it (copy `/etc/arltuner/policy.toml`, or pass `-Policy FILE`).
- It needs an elevated PowerShell, Python (`py -3`), the repository checkout, Windows **Memory integrity (HVCI/VBS) off**, the Intel XTU service stopped
  (`Stop-Service XtuService`), and OCCT, XTU, ThrottleStop, HWiNFO not running.
- It needs the ThrottleStop kernel driver, which this project **never ships**: you carve it from your own `ThrottleStop.exe` (version 9.7.3; the `carve` command
  checks the exe's SHA-256 `52D86D3D...2BA2` and the driver's `16F83F05...97F0` and refuses on a mismatch). The script starts the driver service for the run
  and stops it afterwards. Obtain ThrottleStop yourself; this project downloads nothing.

```
# once per Windows install (elevated PowerShell, in the repository)
py -3 tools\win\ts_driver.py carve --exe C:\path\to\ThrottleStop.exe
py -3 tools\win\ts_driver.py install
powershell -ExecutionPolicy Bypass -File tools\win\preflight.ps1       # read-only checks; fix every FIX line

# after every boot
powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1  # type CONFIRM once; or double-click tools\win\Restore-Tuning.cmd
```
Options: `-From DIR`, `-Policy FILE`, `-Before` (also dump the state before applying; about 132 extra mailbox reads), `-Orchestrated`. Success prints
`RESTORED: all <n> cores/clusters match the accepted state.`; any mismatch stops it. The audit log is `%LOCALAPPDATA%\linuxmon\audit.jsonl` (legacy name kept on
purpose). The Windows health check counts new WHEA events in the System log.

The predecessor's notes record a conflict about the Microsoft Vulnerable Driver Blocklist: the driver loaded with it on, on Windows 11 build 26200/26300
[MEASURED win 2026-10-02], while an older runbook says to switch it off. `preflight.ps1` reports what it finds (`windows-path.md` §1).

## 5. Per-boot routine
1. Boot (cold if you changed BIOS settings or had a crash).
2. Start the GUI, check **System → Preflight** says `write`.
3. **Tune → Restore accepted → Review changes → Apply**, or `bash tools/lnx/restore_tuning.sh`.
4. Confirm "all cores/clusters match", then run your normal workload. Re-run a short Stress screen if you changed anything else.

Next: [10 CLI reference](10-cli-reference.md) · Previous: [08 Stability testing](08-stability-testing.md)
