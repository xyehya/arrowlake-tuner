# Curves + Stress fixtures (G-218)

Recorded answers of the **simulator** helper for `vf.measure`, `curves.*` and `stress.*` (the G-205 mock answers `UNKNOWN_OP` for them). No hardware, no sudo, no installed helper.
Nothing here is a reference-machine measurement (D-036). They live in this sub-directory so that the G-205 check "`fixtures.js` equals the `*.json` files of `fixtures/`" stays about G-205's files.

Re-record: `python3 desktop/app/ui/fixtures/g218/gen_fixtures_g218.py` (private temp socket / audit / lock / state; reuses `../gen_fixtures.py`'s client; also rewrites `fixtures-g218.js`).
The browser loads `fixtures-g218.js` (a JS module, governed by `script-src 'self'`), never the JSON (see `../README.md`); `tests/g218.stress.test.mjs` fails if the two drift apart or a host path / real boot id leaks in.

| File | Recorded request | Contents |
|---|---|---|
| `curves-list-empty.json` | `curves.list` on a fresh store | `{curves: [], no_turbo: false}` |
| `vf-measure-stock.json` | `vf.measure {mode: quick, units: [P0,P1,EC0], name: stock}` | request + the `state`/`progress`/`vf.point` events + final response (`curve_id`, full `record`) |
| `vf-measure-dense.json` | `vf.measure {mode: dense, units: [P0,EC0], name: second}` | same, every ratio 8..top |
| `vf-measure-cancelled.json` | dense run, `cancel {op_id}` after 24 events | the events, the `cancel` answer and the final `CANCELLED` error with `detail.curve_id` + `detail.partial` |
| `curves-list.json`, `curves-list-after-cancel.json` | `curves.list` | rows (newest first), `no_turbo` |
| `curves-get.json`, `curves-update-rename.json`, `curves-update-baseline.json`, `curves-delete.json` | the matching ops | request + response |
| `stress-start.json` | `stress.start {cpu, integer, 30 s, [P0,EC0]}` | events + final result |
| `stress-start-memory.json` | `stress.start {memory, 30 s, 2048 MB}` | events + final result |
| `stress-start-cancelled.json` | cpu run, `stress.stop` after 4 events | events, the stop answer, the result (`stopped_by: "cancel"`) |
| `stress-list.json`, `stress-stop-idle.json` | `stress.list`, `stress.stop` with nothing running | as answered |

The 12-unit quick run is only recorded to check whether the helper omits units from a record at the 64 KiB line: it does not (16 KB), so `mergeOmitted` is tested with synthetic data.
The simulator's records carry no `fused_point`, so the "fused points only" chart branch is also tested synthetically.

## `../mock-backend-g218.js`
`wrapMockTransport(inner, {stepMs, seed, now})` adds these ops to a G-205 mock transport: it replays the recorded events with the same timer idiom as `replayLong`, keeps an in-memory curve store
and stress history (list / get / update / delete work like the helper's), and checks arguments like `ops.rs`. `seed: "recorded"` starts with the stock (baseline) and dense curves and the recorded stress history.
**Not simulator output (invented by the wrapper):** a measurement always yields the recorded points whatever `units` were asked; a stress run replays the recorded 30 s run with its clock
stretched to the asked length (`elapsed_s`, `of_s`, the result's `duration_s`); a Stop answers with the recorded cancelled result carrying the replay's elapsed time.
