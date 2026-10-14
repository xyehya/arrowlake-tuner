# 09 Profiles and restore

This page covers keeping a tuning you trust and getting it back after a reboot: profiles, the **accepted state**, **Restore accepted**, the Linux
restore script and the Windows PowerShell restore. It is for anyone whose tuning has passed their gate ([08](08-stability-testing.md)).

**Nothing is applied automatically at boot.** The tool never touches the CPU until you start it and confirm a plan. A cold boot returns the CPU to
stock ([04 §11](04-arrow-lake-vf-primer.md)), so after every boot you restore deliberately.

## 1. Three words
| Term | Meaning |
|---|---|
| **Profile** | a **complete** tuning state (D-044): for every P-core and E-cluster the OC word (OC ratio, adaptive voltage, whole-curve offset), the offset of **every** fused V/F point (0 included), and both turbo tables when they were read. Stored under `/var/lib/arltuner/profiles/`. Profiles stand alone: none builds on another. Saving writes nothing to the CPU |
| **Applied state** | what the CPU holds right now. The tool shows it from the last read this boot (the "applied" chip) |
| **Accepted state** | the tuning *you* decided is good: **one** complete profile, plus the exact V/F words the CPU read back (the same words, recorded together). It lives in `/etc/arltuner/accepted/`. This is what "Restore accepted" brings back, in one run |

Profiles saved before this version hold only the rows that were changed; the app lists them as **partial (legacy)**. They still load and apply (over what the
CPU holds), and **Set as accepted** on one records the complete state the CPU holds after you applied it, never the partial file.

A fresh install has no accepted state ("No accepted state yet"). It is user data (D-036): the repository ships none. One chip's tuning is kept only as an
illustration in `examples/reference-285k/`, and the app never reads that directory.

## 2. The Profiles tab
| Card | What it does |
|---|---|
| **Accepted state** | shows the one accepted profile, its kind and a summary. Button **Set the current state as accepted** records what the CPU holds now (the last read) as the accepted state; nothing is written to the CPU. Button **Record CPU fingerprint for Windows** adds `fingerprint.json` to an existing accepted state (section 4). An accepted state from an older version that is still a list of several profiles is shown with the command that converts it (section 3, "An older accepted state") |
| **Profiles** | the list of profiles with a badge **complete** or **partial (legacy)** and a short summary. Per profile: **Load into Tune** (fills the Tune editor; a complete profile fills every unit; nothing is sent), **Review and apply** (plan flow; a complete profile is applied like the accepted state: only the words that differ, in rounds if a step limit needs them), **Set as accepted** |
| **Save the current Tune settings** | saves a complete profile: this boot's read of every unit with the unapplied choices from Tune over it (no changes = the read as it is). It needs a read this boot (`NO_READ`, "read first") and refuses `STALE_READ` when a write came after that read (read again first); a value a profile file cannot hold exactly is refused `NOT_REPRESENTABLE`. Name: lowercase letters, digits, `.`, `_`, `-`, 1 to 64 characters. A saved profile is never overwritten (an existing name is refused: `EXISTS`) |
| **Compare two profiles** | a diff table of what the two profiles hold; with a partial (legacy) profile, "–" marks a value it does not set |

### Making a state accepted
The helper's rule (`ops_accept.py`): **accepted = what the CPU holds now.**

1. Apply the tuning (Tune → Review changes → Apply, read-back matches), and stress-test it.
2. Either press **Set the current state as accepted** (it takes the state from this boot's last read), or choose **Set as accepted** on a saved profile. Either way
   the helper records the **complete** state of the last read (every unit, every fused point, both turbo tables when read) as one profile, and the same words as
   `EXPECTED-words.txt`: the two cannot disagree.
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
/etc/arltuner/accepted/CURRENT                 exactly one line:  profile = profiles/<name>.toml
/etc/arltuner/accepted/EXPECTED-words.txt      one line per core / E-cluster:  <cpu> <OC word> <point 1> ... <point n>   (hex, no 0x)
/etc/arltuner/accepted/profiles/<name>.toml    the complete profile (the same words as EXPECTED-words.txt)
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
complete = true        # the whole tuning state (the app writes this)

[[core]]               # the OC word of a unit; ONE per P-core and per E-cluster
kind = "P"             # "P" | "E"
index = 4              # XTU numbering: P 0..7, E 0..15 (an E entry is its whole cluster)
fused_top_ratio = 57   # the unit's top fused ratio where the profile was made: on another CPU the profile is refused (PROFILE_TOPOLOGY)
ratio = 60             # optional: an OC point (a ratio above the top fused ratio) with its adaptive voltage;
oc_voltage_mv = 1400   #   absent = no OC point
offset_mv = -30        # whole-curve offset, always stated (0 included)

[[point]]              # every fused V/F point of every unit, 1..n, the last one at fused_top_ratio
kind = "E"
index = 8
point = 6              # P points 1..8, E points 1..6 (read from your CPU)
offset_mv = -20        # 0 included
expect_ratio = 46      # refuse if the point's ratio read from the hardware differs

[turbo]                # both tables or none (none = not part of this profile, left as they are); bucket 0 = one active core first
p = [57, 57, 55, 55, 55, 55, 55, 55]
e = [48, 48, 48, 48, 48, 48, 48, 48]
```
Values are examples, and a real complete profile has a `[[core]]` for every unit and a `[[point]]` for every fused point. The app writes these files for you; you
rarely edit one by hand. A file without `complete = true` is a partial profile: only what it names is changed.

## 3. After every boot: Restore accepted
**In the GUI.** Start the GUI ([03](03-first-run.md)), open **Tune**, press **Restore accepted** (the button is disabled until an accepted state exists),
then **Review changes**, check the before/after and the operation count, and **Apply**. It is **one run**: the apply reads every word of the accepted profile
first and writes only the words that differ, so it works from a cold boot (all stock) and from a warm reboot (a mix) alike. An OC ratio left over from a warm
reboot that the accepted state does not have (it cannot be removed at runtime) is rewritten so the unit runs the accepted voltages and reported as **equivalent**
([below](#a-left-over-oc-point-equivalent)).

If a word is further from the accepted value than your policy's per-write step limit (`max_offset_step_mv`, `max_v_step_mv`, `max_ratio_step`), the apply splits
the change into **rounds** within the same run: read, write every word one step closer, read again, until the accepted values are reached. No intermediate profile
and no second sitting is needed. How many rounds are needed, and whether they fit the policy's operation caps and the tool's own bound of 16 rounds (a design bound,
not a hardware one), is decided before the first write. If they do not fit, nothing is sent and the plan is refused `STEP_LIMIT`, naming the unit and the field that
needs the most rounds; raise the step limits or the operation caps in `/etc/arltuner/policy.toml`, or bring the CPU closer first ([11 §3](11-troubleshooting-faq.md)).
Afterwards the Dashboard's "applied" chip and the comparison with the accepted state tell you whether every word matches.

**Write order (G-151).** Within each unit the apply writes in a transition-safe order: no ratio's voltage goes below the lower of its voltage before and after the plan while
the words are written. Writes that raise a voltage go before writes that lower one. So when the accepted state has an OC point and a lowered top point (for example an E-cluster
with 48x held at its old voltage and point 6 lowered), the OC word is written before the point-6 offset, from a cold boot as well. A unit with no such order is refused by name
(`write_order`) and not written ([07 how-to](07-overclocking.md)).

**The OC point's table slot.** Once an OC point exists the CPU also shows a table slot for it (on the reference E-clusters: point 7, ratio 48, offset 0). It is never written as a
fused point. The compare treats a missing slot, on a unit whose OC word has not been written yet, as part of the OC word's difference, not as a separate failure; after the OC word
it must read the OC ratio with offset 0 (task G-151, `controls/residual.py` `oc_slot_pending`).

Operation count: the plan reserves the worst case (every word read, every word written and read back, plus two reads per unit that has no OC point in the accepted
state, to look for a left-over OC point). For the reference layout (8 P-cores, 4 E-clusters of 4 CPUs) the simulator reserves 651 operations for a one-round plan
(24 of them the scan) [simulator test, `tests/helper/test_complete_profiles.py`]. Only the words that differ are written, so a restore of a state the CPU already holds sends reads only: a
live one-shot restore on the reference machine was 281 reads and 0 writes (orchestrator's run, docs/STATE.md 2026-10-13). The default policy allows 100000 operations per
session; a stricter policy refuses up front (`SESSION_LIMIT`).

### A left-over OC point: "equivalent"
You may meet this after OC write tests or after a warm reboot. Setting an OC ratio on a unit (a P-core or an E-cluster) adds an OC point above its top fused point.
The CPU does not accept an OC ratio of 0 at runtime, so that ratio, and the adaptive voltage next to it, stay in the unit until a **cold boot** [MEASURED lnx 2026-10-11,
platform-arl-s.md §3]. The accepted state has no OC point there, so the unit keeps an OC point that the accepted tuning does not describe.

Why it matters (example values from one chip, the reference 285K): after OC tests an E-cluster held 48x with an adaptive target of 1.137 V. Its 48x therefore ran at the
stock voltage of its top point, while the accepted tuning has -25 mV on that point (1.1125 V) [MEASURED lnx 2026-10-13, examples/reference-285k/notes/tuning-history.md §5].
The words on the other points matched; only the OC word differed.

What Restore accepted does about it, for every unit whose accepted state has no OC point but whose live word holds an OC ratio above the top fused ratio:

| Part of the OC word | Written |
|---|---|
| ratio | the one the unit already holds (it cannot be removed; it is never raised and never 0) |
| whole-curve offset | the accepted one |
| adaptive target | 25 mV **below the accepted top-point voltage** (the unit's fused voltage plus the accepted offset of its top point; the fused voltage itself if that offset is positive), never above the fused voltage, and at most one step limit below what the unit holds now |

A target below the top fused point is clamped up to that point by the hardware [MEASURED lnx 2026-10-11, 2026-10-13], so the unit runs the accepted voltages. Example
from one chip: a target of 1.100 V read back as `0x00046630`, and 48x measured 1.1125 V, the accepted value [MEASURED lnx 2026-10-13, tuning-history.md §5].
If the ratio the unit holds is above your policy's `ratio_max`, or no such target is reachable in one step of your policy, the unit is left alone (or moved as far as one step
allows), the plan notes say why, and the result is a real difference.

The result is reported as **EQUIVALENT** with the text `residual OC point (cleared by a cold boot); voltages as accepted`, not as a failure. EQUIVALENT counts as success:
the restore scripts exit 0, and the Dashboard comparison says "Matches the accepted state ..." and adds "cpu N is equivalent: ...". The script ends:
```
  cpu12 EQUIVALENT <the live words>
         residual OC point (cleared by a cold boot); voltages as accepted
         accepted <the accepted words>
RESTORED: all 12 cores/clusters match the accepted state (1 equivalent: residual OC point (cleared by a cold boot); voltages as accepted).
```
(The numbers and words are illustrative.) A cold boot clears the left-over point; then the next restore reports plain `OK`. Only a real difference (a different
point word, an offset that is not the accepted one, a target above the accepted top point) is `BAD` and ends with `STOP: N core(s)/cluster(s) differ`.

Two related rules (G-147):

- **The favored ratio is never lowered as a side effect.** The tool does not send a favored-ratio word (`0x1D`) that merely restates a ratio below the unit's current
  favored maximum (`0x1C` bits 7:0): on the reference chip `0x1D` = 46 took an E-cluster from 48x / 4.9 GHz to 46x / 4.7 GHz [MEASURED lnx 2026-10-13]. A deliberate
  lowering below the ratio in force still sends it, and an OC raise sends the new ratio.
- **The cpufreq cap follows a favored-ratio change.** After an apply that wrote a `0x1D` (the GUI, `arltuner apply --device` and so the Linux restore script), the
  `scaling_max_freq` of the unit's CPUs follows the new `cpuinfo_max_freq` if it sat at the old maximum. A cap you set below the old maximum is never raised; a cap above a
  lowered maximum is lowered. This is Linux only: Windows has no cpufreq step.

**From a terminal.** The restore script restores the accepted state and proves it word for word:
```
bash tools/lnx/restore_tuning.sh            # shows policy + profiles, asks once: Type CONFIRM
bash tools/lnx/restore_tuning.sh --check    # read-only: dump and compare, no writes
bash tools/lnx/restore_tuning.sh --from DIR # restore another accepted directory
bash tools/lnx/restore_tuning.sh --policy FILE
```
| Option | Meaning |
|---|---|
| (none) | prints the policy, the accepted profile and the expected-words file, then asks you to type `CONFIRM` (exactly; anything else sends nothing) |
| `--check` | mailbox reads only (they are still WRMSRs), then compare; nothing is written |
| `--from DIR` | accepted state to restore; default `/etc/arltuner/accepted` |
| `--policy FILE` | the root policy for the guarded writes; default `/etc/arltuner/policy.toml` |
| `--orchestrated` | for the project's automation after a go-ahead in chat; skips the prompt. Do not use it unless you know why |

What it does: records a health snapshot (kernel taint, PCI devices, modules), applies the one profile named in `CURRENT` in one guarded run with `sudo`
(it asks for your `sudo` password once), dumps the V/F words, compares every one with `EXPECTED-words.txt`, and takes a second health snapshot. It stops at the first
failure and retries nothing. A good run ends:
```
RESTORED: all cores/clusters match the accepted state.
```
and a bad one with `STOP: N core(s)/cluster(s) differ`. It refuses to start when no accepted state exists, the policy is missing, or OCCT is running as root.
Captures are written to `captures/lnx/`. It uses the checkout's Python with `sudo`, in isolated mode (`python3 -I`), as the command-line path (D-031), and sets
`ARLTUNER_ALLOW_MAILBOX=1` itself for the guarded commands.

The planner budgets the whole plan (all its rounds) before sending the first request. `tools/lnx/reapply_current.sh` is only another name for `restore_tuning.sh`
([10 §5](10-cli-reference.md)).

### An older accepted state (a list of profiles)
Before profiles were complete (D-044), `CURRENT` could list several profiles applied in order. **Restore accepted**, both restore scripts and the Windows scripts
now refuse such a state and name the conversion; `install.sh` prints the same hint. Convert it once, as root, from the repository:
```
sudo python3 -I tools/arltuner.py accepted-migrate --dry-run   # prints the profile it would write and how it differs from the old list
sudo python3 -I tools/arltuner.py accepted-migrate             # writes it; the old directory is kept as /etc/arltuner/accepted.bak-<UTC time>/
```
The new profile is built from `EXPECTED-words.txt` (the words the old list was verified to leave on the chip), never from a read: after a warm reboot the CPU can
hold a left-over OC ratio that must not become the accepted state. `EXPECTED-words.txt` and `fingerprint.json` are kept unchanged; the top fused ratio of each unit
comes from `fingerprint.json` (or, without one, from the last point in `EXPECTED-words.txt`). If the old list set no turbo tables, the new profile has none either
(they stay as the CPU holds them); the next **Set the current state as accepted** records both. It sends nothing to the CPU. `--dir DIR` converts another directory.

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
- The Windows side refuses, before any write: no `fingerprint.json` or one without CPU identity or without a fused word for every P-core and E-cluster; another CPU (vendor, family,
  model, brand) than the one recorded; a layout that differs (logical-processor counts per class, E-cluster sizes, processor groups other than 0, no L2 data); a P-core word read twice or
  not recorded; an E-cluster reading another word than recorded (`docs/RUNBOOK-win.md`). The command-line `read-vf` and `read` with `--device-win` send one request and run no identity check
  (they print a note); `apply`, `point-offset` and `vf-dump` settle the map first.
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
including `fingerprint.json`), `policy.toml`, the audit log `audit.jsonl` and the run's read-back file. The scripts refuse a `-From` or `-Policy` outside such a directory. Copy the accepted state in from an Administrator terminal (for example `xcopy /e /i <where you put it> %ProgramData%\arltuner\accepted`).
Copies of earlier versions under `%LOCALAPPDATA%\arltuner` are no longer read: delete the old driver copy and move the accepted state.

```
# once per Windows install (elevated PowerShell, in the repository)
py -3 tools\win\ts_driver.py init-dir                                  # create and lock down %ProgramData%\arltuner (carve also does it)
py -3 tools\win\ts_driver.py carve --exe C:\path\to\ThrottleStop.exe     # -> %ProgramData%\arltuner\ThrottleStop.sys (hash-checked)
powershell -ExecutionPolicy Bypass -File tools\win\preflight.ps1        # read-only checks; fix every FIX line (see the note below)
# from an Administrator terminal, copy the Linux files in:
#   /etc/arltuner/accepted/*   ->  %ProgramData%\arltuner\accepted\   (CURRENT, EXPECTED-words.txt, profiles\, fingerprint.json)
#   /etc/arltuner/policy.toml  ->  %ProgramData%\arltuner\accepted\policy.toml   (or pass -Policy FILE)

# after every boot
powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1   # type CONFIRM once; or double-click tools\win\Restore-Tuning.cmd
```
The preflight's line "driver service ThrottleStop" shows FIX until a run has started the service; that is expected, because the restore script creates and starts the service itself. A leftover
`%LOCALAPPDATA%\arltuner\ThrottleStop.sys` from an older version is flagged and must be deleted (a non-admin could replace it).

What a run does: checks (administrator, XTU service stopped, no OCCT/XTU, admin-only directories, `fingerprint.json` present), then one `CONFIRM`, then `ts_driver.py start`
(verifies the directory, its ACL, reparse points and the driver's SHA-256, writes a **new uniquely named copy** of the driver for this run, creates the service on it, re-verifies, starts it),
then the guarded identity reads, the one profile of `CURRENT` applied in one run (only the words that differ), every V/F word read back and compared with `EXPECTED-words.txt`, and finally `ts_driver.py remove` (stops and deletes the
service and the copy). The verdict `RESTORED: all <n> cores/clusters match the accepted state.` comes from this run's own read-back file, never from the audit log. Any mismatch stops
it; nothing is retried. As on Linux, an EQUIVALENT unit (a left-over OC ratio that runs the accepted voltages) counts as success.

Options of `restore_tuning.ps1`: `-From DIR`, `-Policy FILE`, `-Before` (also dump the state before applying; about 132 extra mailbox reads), `-Orchestrated`. `reapply_current.ps1 [-Check]
[-From DIR] [-Policy FILE]` is the simpler twin (one confirmation, no compare; `-Check` is a read-only dump). Both refuse an accepted state that still lists several profiles:
convert it on Linux (`accepted-migrate`, section 3) and copy it again. The Windows health check counts new WHEA events in the System log.
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
