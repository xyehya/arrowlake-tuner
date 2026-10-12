# arrowlake-tuner user guide

This guide covers installing and using arrowlake-tuner, and the facts about Arrow Lake voltage/frequency (V/F) tuning that the tool is built on.
It is for people who tune their own CPU: you know what an undervolt is, you accept that a bad value can crash the machine or silently corrupt
results, and you want a Linux tool that does what Intel XTU does on Windows.

## What the tool is
arrowlake-tuner reads telemetry from, and writes tuning controls to, an Intel Core Ultra 200S (Arrow Lake-S) desktop CPU at runtime, from Linux.
It has a local web GUI, a command line and restore scripts. It can:

- show the machine live: per-core clock, load, temperature, requested voltage (VID), package power, memory timings;
- **measure** the voltage-against-frequency curve of every P-core and every E-core cluster, store it and compare it;
- **tune**: per-V/F-point and whole-curve voltage offsets, an OC ratio with its adaptive voltage, and the turbo ratio tables;
- run short **stress screens** (CPU or memory) and count errors;
- keep a **profile** you trust as the *accepted state* and put it back after a reboot.

Everything privileged runs in a small root helper, a systemd socket-activated service that only the users in its allowlist can use (no polkit, no password prompt). Every write passes a guard, is sent one mailbox transaction at a time,
is read back and is written to an audit log. A **cold boot reverts every runtime change.** The tool applies nothing by itself at boot.

## Supported hardware
| Item | Supported |
|---|---|
| CPU | Intel Core Ultra 200S, Arrow Lake-S desktop: CPU family 6, model 198 (0xC6), stepping 2 (`platforms/arl-s.toml`). An unlisted stepping gives a warning, not a refusal. Any other CPU is read-only or refused. |
| OS | Linux on bare metal (not a VM), with systemd (recommended; any init works with a manual command). Windows gets a PowerShell restore script only, driven by a fingerprint recorded on Linux (see [09 §4](09-profiles-and-restore.md); not yet run on real Windows in its current form). |
| Board | Any board for the CPU whose BIOS lets you disable **Undervolt Protection** and **OC Lock**. The board-specific part is only the Vcore reading, which the app looks up by the board's DMI name. It is known for the reference board (ASRock Z890 Steel Legend WiFi); on an unknown board the dashboard shows no Vcore rather than a guess (`platforms/arl-s.toml`). |

**Example values.** The project was developed and measured on one machine: a Core Ultra 9 285K on an ASRock Z890 Steel Legend WiFi. Where this
guide quotes numbers from it (fused voltages, point ratios, undervolt offsets) they are labelled *example values from one chip*. The app reads
everything live from your CPU. Your chip has its own V/F curve; values that are stable on the reference chip can crash yours.

## Limits in one paragraph
**Every limit is your policy.** The voltage-offset window, the adaptive-voltage ceiling, the OC-ratio ceilings and the step sizes are numbers in `/etc/arltuner/policy.toml`. The shipped values are
conservative defaults; the file is yours (root-owned, never overwritten by the installer), the program hard-codes none of them, and a change applies from the next read or plan (D-039). The only
ceilings you cannot edit are your CPU's own (the OC-ratio limit from mailbox command `0x01`, the maximum voltage from `0x28`, both read live) and the register field widths (offset -1000 ... +999 mV).
Extreme-cooling users (liquid nitrogen, binned chips) can therefore raise the defaults, entirely at their own risk. Details and the exact refusal texts: [12 §4](12-safety-and-internals.md).

## Safety in four lines
1. Writing CPU voltages can crash the machine or silently corrupt data. Only you are responsible for what you apply.
2. Change one thing at a time, in small steps, and stress-test before you trust it. A quick screen is not an acceptance gate.
3. A cold boot (power off, then on) undoes everything the tool wrote. That is the universal undo.
4. Never work around a refusal from the guard. Read why it refused.

Details: [12 Safety and internals](12-safety-and-internals.md) and [`../SAFETY.md`](../SAFETY.md).

## Reading order
| Page | Read it when |
|---|---|
| [01 Requirements](01-requirements.md) | before anything: CPU, BIOS settings, kernel, packages |
| [02 Install](02-install.md) | to install, update, reset or remove |
| [03 First run](03-first-run.md) | to start the GUI and read the preflight |
| [04 Arrow Lake V/F primer](04-arrow-lake-vf-primer.md) | before your first write: the hardware facts everything else rests on |
| [05 Measuring curves](05-measuring-curves.md) | to record your stock curve (do this first) |
| [06 Undervolting](06-undervolting.md) | to lower voltage with offsets |
| [07 Overclocking](07-overclocking.md) | to raise ratios, set an adaptive voltage, edit turbo tables |
| [08 Stability testing](08-stability-testing.md) | to decide whether a tuning is stable |
| [09 Profiles and restore](09-profiles-and-restore.md) | to keep a tuning and bring it back after every boot |
| [10 CLI reference](10-cli-reference.md) | for the command line and scripts |
| [11 Troubleshooting and FAQ](11-troubleshooting-faq.md) | when something is refused, read-only or unstable |
| [12 Safety and internals](12-safety-and-internals.md) | to understand the guard, the audit log and the helper |

## Conventions
- **Evidence tags** follow the project rules: `[MEASURED host date]` (read on a real machine), `[DECOMPILED file]` (read from vendor code),
  `[DOC url]` (public documentation), `[COMMUNITY url]` (forum/blog claim), `[UNVERIFIED]`. Only the first three are treated as fact by the code.
  `lnx` is the Linux host, `win` the Windows install on the same reference machine.
- **Names.** A *unit* is one tuning domain: a P-core, or an E-core cluster (E-cores share a regulator in clusters of four). The reference chip
  has 12 units (8 P + 4 E); the app derives the list from your CPU.
- **Button and tab names** are quoted from the web page (for example **Read ratios & voltages**, **All-Core** / **Per-Core**, **Whole Curve Offset** / **Per V/F point Offsets**, **Set as accepted**).
  Screenshots of the pages are in the repository [README](../../README.md#screenshots); they show one example machine.
- Commands are for a normal user shell unless they start with `sudo`.

## License
MIT (see [`../../LICENSE`](../../LICENSE)).

## Other documents
[`../../README.md`](../../README.md) (short project README), [`../SPEC-v3.md`](../SPEC-v3.md)
(product spec), [`../DECISIONS.md`](../DECISIONS.md), [`../knowledge/`](../knowledge/) (distilled hardware facts; `registers.md` is the single source for encodings).
