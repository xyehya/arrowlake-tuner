# Knowledge base

Facts about Intel Core Ultra 200S (Arrow Lake-S) runtime tuning on Linux and Windows, each with its evidence. **General facts live here; one chip's
numbers live in `examples/reference-285k/notes/`** (one sample, not a spec). A measured fact here was measured on that one chip and is scoped as such;
SKU-specific values (point ratios, fused voltages, maxima) are labelled as examples and must be read live on any other CPU.

| File | Contents |
|---|---|
| [registers.md](registers.md) | Single source of truth for encodings: OC mailbox wire format, V/F word, commands, return codes, MSRs, MMIO, BIOS gates (code cites its section names) |
| [platform-arl-s.md](platform-arl-s.md) | Hardware behaviour: topology, V/F points and OC point, clamp/interpolation, VccIA model, reboot persistence, ratio raises, turbo tables, powercap |
| [favored-cores.md](favored-cores.md) | What favored cores are, how the app reads them live, what is known about the criterion, how to characterise cores |
| [tuning-results.md](tuning-results.md) | Stability lessons (silent failures, the gate) and what the hardware did in the first write sittings |
| [power-profile-capping.md](power-profile-capping.md) | Power profiles, EPP, cpufreq cap pinning and granularity for V/F measurement |
| [telemetry-sources.md](telemetry-sources.md) | One documented source per telemetry value; the telemetry read allowlist |
| [memory-mchbar.md](memory-mchbar.md) | Live DDR5 frequency and timings from MCHBAR (read-only) |
| [linux-path.md](linux-path.md) | `/dev/cpu/N/msr` privilege, taint and lockdown, mailbox sequence, health bracket, kernel interfaces |
| [windows-path.md](windows-path.md) | ThrottleStop.sys as a raw MSR port: driver carving, IOCTLs, safety checks, restore flow |
| [mailbox-recon-summary.md](mailbox-recon-summary.md) | How XTU/IPF and ThrottleStop drive the mailbox; open questions (C-n) |

## Evidence tags
| Tag | Meaning | Can become a code constant |
|---|---|---|
| `[MEASURED host date]` | read or written and read back on the reference machine (`lnx` = Linux, `win` = Windows) | yes |
| `[DECOMPILED file]` | read from a vendor tool for interoperability (the named notes are earlier research, not in this repository) | yes, after a MEASURED read-back |
| `[DOC url]` | Intel / kernel / vendor documentation | yes |
| `[COMMUNITY url]` | trustworthy third party (SkatterBencher, kernel lists, known reviewers), not validated here | no |
| `[UNVERIFIED]` | open lead worth testing | no |
Short forms in `registers.md`: `measured`, `decomp`, `doc`, `lead`, `seen`, `trace` (legend at its top).

## Rules
- Register and mailbox encodings go into `registers.md` only; other files cite it.
- A new finding goes into the file of its topic, tagged; numbers that only describe one chip go to `examples/reference-285k/notes/`.
- When a newer MEASURED fact contradicts an older one, replace the old statement (say "superseded" if code or the guide cited it).
