# Web GUI parity checklist (desktop app must cover each before the web GUI is demoted)

Tick with the task that delivered it and how it was checked (mock / sim / live).

## Frame
- [ ] helper state + fix text (missing / not allowed / busy / restarted) · [ ] mode (write / read-only + reason) · [ ] applied vs accepted chip
- [ ] ops this boot · [ ] refresh interval setting · [ ] telemetry history window

## Dashboard
- [ ] core map (clock/busy/temp/VID, fixed/auto scale) · [ ] 6 tiles + sparklines · [ ] power + temp charts · [ ] memory card + all timings
- [ ] Cores table (filter, columns, sort, detail charts) · [ ] Sensors view · [ ] raw limit reasons (decoded after G-215)

## Curves
- [ ] measure quick/dense, units, cancel, progress, problems · [ ] chart/table/library · [ ] compare, presets, legend solo · [ ] rename / baseline / delete / export

## Tune
- [ ] Read with progress + cost text + cancel · [ ] whole-curve offset · [ ] per-point offsets P/E · [ ] OC ratio · [ ] OC target V · [ ] 48x offset helper
- [ ] undo per unit · [ ] discard · [ ] restore accepted · [ ] reset offsets to 0 · [ ] predicted VID / VccIA · [ ] turbo tables P/E · [ ] values as read (raw words)
- [ ] review: before/after, ordered writes, ops, warnings · [ ] apply + read-back table · [ ] cpufreq rows · [ ] residual / ring_refused notes
- [ ] NEW: ring row · [ ] NEW: cancel apply · [ ] NEW: multi-select edits · [ ] NEW: typed values

## Stress
- [ ] D-019 banner · [ ] cpu/memory, methods, MB · [ ] length presets + custom · [ ] ratio cap · [ ] live ring/clock/errors/temp/power · [ ] result card · [ ] history

## Profiles
- [ ] accepted state card · [ ] set current as accepted · [ ] fingerprint for Windows · [ ] list + summary · [ ] load into Tune · [ ] review and apply
- [ ] set as accepted · [ ] save current settings · [ ] compare two profiles

## System / Audit
- [ ] helper + ops ring · [ ] preflight list + re-run · [ ] limits table (policy vs CPU) · [ ] audit tail + filter + refresh
