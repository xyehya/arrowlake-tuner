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
| **Accepted state** | shows the chain of accepted profiles in the order **Restore accepted** applies them. Button **Set the current state as accepted** records what the CPU holds now (the last read) as the accepted state; nothing is written to the CPU. Button **Record CPU fingerprint for Windows** adds `fingerprint.json` to an existing accepted state (section 4) |
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
| `STALE_READ` | a mailbox or plain MSR write happened after the read: read again first, then accept |
| `NOT_REPRESENTABLE` | a word of the current state cannot be expressed exactly in a profile file (profiles hold whole millivolts); the tool refuses rather than round, because the restore would write a different word |

Layout of the accepted state (readable by the restore scripts and the Dashboard comparison):
```
/etc/arltuner/accepted/CURRENT                 one line per profile, applied in order:  profile = profiles/<name>.toml
/etc/arltuner/accepted/EXPECTED-words.txt      one line per core / E-cluster:  <cpu> <OC word> <point 1> ... <point n>   (hex, no 0x)
/etc/arltuner/accepted/profiles/<name>.toml    the profiles
/etc/arltuner/accepted/fingerprint.json        which core is which (kind, cluster, favored, fused top-point word) and the CPU model; used by the Windows restore
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
Captures are written to `captures/lnx/`. It uses the checkout's Python with `sudo`, in isolated mode (`python3 -I`), as the operator command-line path (D-031), and sets
`ARLTUNER_ALLOW_MAILBOX=1` itself for the guarded commands.

`bash tools/lnx/reapply_current.sh [--check] [--from DIR] [--policy FILE]` is the older, simpler script: it applies each profile in `CURRENT` with one `CONFIRM <token>` per
profile; `--check` dumps the words read-only and compares them.

Operation counts depend on how much differs; the planner budgets the whole plan before sending the first request.

## 4. Windows restore (any Arrow Lake-S through the fingerprint; untested on Windows)
Windows has no GUI. `tools\win\restore_tuning.ps1` restores the accepted state through a signed third-party driver. **Status: the scripts were used on the reference
machine in earlier research (October 2026), before the changes of G-138 (any chip, via `fingerprint.json`) and G-142 (admin-only directory, per-run driver copy). The new code
was written and tested on Linux only; the first run on Windows is the bring-up test. Treat this section as untested.**

### How it finds your cores: `fingerprint.json`
Linux CPU numbers mean nothing on Windows, and Windows cannot tell the P-cores apart. So **Set as accepted** on Linux also writes `fingerprint.json` into
`/etc/arltuner/accepted/`: for every Linux CPU its kind, E-cluster, favored flag and its **fused top V/F point word** (mailbox command `0x09`; distinct per physical P-core,
shared inside an E-cluster), plus the CPU's vendor, family, model and name. On Windows the tool derives the Linux-CPU-to-logical-processor map at run time from
Windows' own data and settles it with guarded reads of those fused words before any write. A mismatch refuses the whole session and nothing is written. No constants of a
particular chip are built in, so the Windows restore is meant to work on any Arrow Lake-S, but only for the machine the accepted state was recorded on.

- A state accepted **before** this existed has no `fingerprint.json`: the Windows scripts stop with "re-accept on Linux". You do not have to re-accept the tuning: in
  **Profiles** press **Record CPU fingerprint for Windows** (helper op `profiles.fingerprint`). It needs a **read this boot** (Tune, Read ratios & voltages), writes only
  `fingerprint.json` next to the existing accepted state (the accepted tuning and its words are not touched, nothing is written to the CPU) and reports `complete` or
  `incomplete: <why>`. An incomplete fingerprint is written all the same, and the Windows restore refuses it.
- **Set as accepted** also refuses `STALE_READ` ("read again first") when the audit log shows a mailbox or plain MSR write after the read it would record, so the accepted
  words are what the CPU really holds.

### Prerequisites
- Windows 11, an **elevated** PowerShell, Python (`py -3`), the repository checkout.
- Windows **Memory integrity (HVCI/VBS) off**. Check with `tools\win\preflight.ps1`, which also looks at the driver blocklist, the Intel XTU service (stop it:
  `Stop-Service XtuService`) and other mailbox clients. OCCT, XTU, ThrottleStop and HWiNFO must not run.
- The ThrottleStop kernel driver, which this project **never ships**. You carve it from your own `ThrottleStop.exe` (version 9.7.3; `carve` checks the exe's SHA-256 `52D86D3D...2BA2`
  and the driver's `16F83F05...97F0` and refuses on a mismatch). Obtain ThrottleStop yourself; this project downloads nothing.
- The accepted state copied from Linux into the **admin-only directory** `%ProgramData%\arltuner` (below).

### The admin-only directory (G-142, D-042)
The script runs elevated, so everything that decides what it does must be writable only by Administrators and SYSTEM. That is `%ProgramData%\arltuner` (owner
Administrators, ACL Administrators + SYSTEM; no path component may be a junction or symlink). It holds the driver, `accepted\` (the copy of `/etc/arltuner/accepted`,
including `fingerprint.json`), `policy.toml`, the audit log `audit.jsonl` and the run's read-back file. The scripts refuse a `-From` or `-Policy` outside such a directory.

```
# once per Windows install (elevated PowerShell, in the repository)
py -3 tools\win\ts_driver.py init-dir                                  # create and lock down %ProgramData%\arltuner (carve also does it)
py -3 tools\win\ts_driver.py carve --exe C:\path\to\ThrottleStop.exe     # -> %ProgramData%\arltuner\ThrottleStop.sys (hash-checked)
powershell -ExecutionPolicy Bypass -File tools\win\preflight.ps1        # read-only checks; fix every FIX line
# from an Administrator terminal, copy the Linux files in:
#   /etc/arltuner/accepted/*   ->  %ProgramData%\arltuner\accepted\   (CURRENT, EXPECTED-words.txt, profiles\, fingerprint.json)
#   /etc/arltuner/policy.toml  ->  %ProgramData%\arltuner\accepted\policy.toml   (or pass -Policy FILE)

# after every boot
powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1   # type CONFIRM once; or double-click tools\win\Restore-Tuning.cmd
```
What a run does: checks (administrator, XTU service stopped, no OCCT/XTU, admin-only directories, `fingerprint.json` present), then one `CONFIRM`, then `ts_driver.py start`
(verifies the directory, its ACL, reparse points and the driver's SHA-256, writes a **new uniquely named copy** of the driver for this run, creates the service on it, re-verifies, starts it),
then the guarded identity reads, each profile of `CURRENT` applied, every V/F word read back and compared with `EXPECTED-words.txt`, and finally `ts_driver.py remove` (stops and deletes the
service and the copy). The verdict `RESTORED: all <n> cores/clusters match the accepted state.` comes from this run's own read-back file, never from the audit log. Any mismatch stops
it; nothing is retried.

Options of `restore_tuning.ps1`: `-From DIR`, `-Policy FILE`, `-Before` (also dump the state before applying; about 132 extra mailbox reads), `-Orchestrated`. `reapply_current.ps1 [-Check]
[-From DIR] [-Policy FILE]` is the simpler twin (one confirmation per profile; `-Check` is a read-only dump). The Windows health check counts new WHEA events in the System log.
Applied offsets stay until a cold boot even after the driver service is removed.

Driver blocklist: on the reference machine the driver loaded with Windows' Microsoft Vulnerable Driver Blocklist **on** (Windows 11 build 26200/26300) [MEASURED win 2026-10-02],
while `preflight.ps1` flags the blocklist as something to turn off. Start with the blocklist as it is; if `ts_driver.py start` fails with 1275, 577 or "blocked", follow the
preflight advice (Memory integrity off and reboot; then the blocklist). If the driver is still blocked the route is closed for that Windows build (`docs/knowledge/windows-path.md`).

## 5. Per-boot routine
1. Boot (cold if you changed BIOS settings or had a crash).
2. Start the GUI, check **System → Preflight** says `write`.
3. **Tune → Restore accepted → Review changes → Apply**, or `bash tools/lnx/restore_tuning.sh`.
4. Confirm "all cores/clusters match", then run your normal workload. Re-run a short Stress screen if you changed anything else.

Next: [10 CLI reference](10-cli-reference.md) · Previous: [08 Stability testing](08-stability-testing.md)
