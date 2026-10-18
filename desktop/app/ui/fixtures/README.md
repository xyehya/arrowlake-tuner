# UI fixtures (G-205)

Recorded answers of the **simulator** helper, used by the `mock` bridge (`index.html?bridge=mock`) and by `node --test` / the Playwright smoke.
No hardware, no sudo, no installed helper was involved. Nothing here is a reference-machine measurement (D-036): the numbers are the simulator's own model.

## How they were made
`python3 desktop/app/ui/fixtures/gen_fixtures.py` starts `tools/arltuner-helper.py --sim --socket` on a private temp dir (socket, audit log, lock, state dir, a
copy of the shipped `policies/default.toml`, a `helper.toml` naming the current user), speaks the wire protocol (JSON lines) and writes the responses and events
it got, then deletes the temp dir. Re-run it to refresh; ids, timestamps and the nonce change on every run.

| File | Request | Contents |
|---|---|---|
| `preflight.json` | `preflight` | report: checks, topology (8 P + 4 E clusters), `writes_allowed: false` (unprivileged run) |
| `telemetry-start.json` / `telemetry-stop.json` | `telemetry.start {interval_s:1}` / `.stop` | the op answers |
| `telemetry-events.json` | (stream, ~10 s) | 9 `telemetry` + 5 `heartbeat` events exactly as sent |
| `tune-read.json` / `tune-read-events.json` | `tune.read` | final response + the `state`/`progress` events before it (132 steps) |
| `tune-plan.json` | `tune.plan` for P0 point 5 (53x) offset -20 mV | request + response (plan_id, nonce, steps, before/after) |
| `tune-apply.json` / `tune-apply-events.json` | `tune.apply` with the plan's nonce | request (nonce redacted) + response with read-back + events |
| `profiles-list.json`, `audit-tail.json`, `tune-last.json` | the read-only ops | as answered |
| `fixtures.js` | | **generated**: all of the above as one ES module, `export default {<file stem>: ...}`. The browser loads this, not the JSON (below) |

## Things you must know
- **Telemetry is raw and almost all `n/a`.** An unprivileged simulator run has no MSR / RAPL / hwmon access, so every leaf is `status: "unavailable"` with the helper's
  real `reason`. That is the honest recording, and it is what exercises the `n/a + reason` path. For demos, `?bridge=mock&motion=1` makes the **mock backend** overlay an
  *invented* deterministic waveform (clearly marked `INVENTED` in `mock-backend.js`) on the unavailable leaves; DRAM and core power stay `n/a`. The JSON is never edited.
- **Scrubbed after recording** (host leakage, not simulator data): `preflight.board` -> `"Simulated board"` (the helper reads DMI from the host even in `--sim`), `audit.tail.path` ->
  `/var/lib/arltuner/audit.jsonl`, the host boot id -> an all-zero UUID. Left as the simulator wrote it: `operator: "gui:uid<N>"` in audit records (the uid of whoever ran the generator).
- `tune.plan` / `tune.apply` replay exactly one recorded plan: the mock does not plan. `tune.apply` checks `plan_id` and `typed == "CONFIRM <nonce>"` like the helper and answers
  `PLAN_STALE` / `CONFIRM_MISMATCH` otherwise. `tune.read` stamps a fresh `read_at`.
- **Why `fixtures.js` and not `import ... with {type:"json"}` or `fetch()`:** Chrome checks both against `connect-src`, which the shipped CSP (`tauri.conf.json`) limits to
  `ipc: http://ipc.localhost`; a JS module is governed by `script-src 'self'`. This way the mock runs under the exact shipped CSP. `tests/shell.test.mjs` fails if `fixtures.js`
  and the JSON files drift apart.
- The directory ships inside the AppImage with the rest of `ui/` (`frontendDist: "../ui"`). Only `mock-backend.js` / `fixtures.js` are loaded, and only for `?bridge=mock`.

- Curves/Stress fixtures and the run-simulating wrapper: see `g218/README.md` (G-218).
## Profile fixtures (G-213)
`python3 desktop/app/ui/fixtures/gen_profiles_fixtures.py` (simulator, temp paths, never applies) adds, next to the files above, and rebuilds `fixtures.js` from every `*.json` here (the older recordings are not re-made):
`profiles-save-<name>.json` (request + response of `profiles.save` for `sim-p0-undervolt` and `sim-e-light`), `profiles-get-<name>.json` (what each holds: complete, 12 units, turbo tables),
`profiles-saved-list.json` (`profiles.list` with both saved), `profiles-save-exists.json` (the `EXISTS` refusal), `profiles-accept-unavailable.json` / `profiles-fingerprint-unavailable.json` (the unprivileged simulator keeps no
accepted-state directory: `NO_ACCEPTED_DIR`). The accepted state itself cannot be recorded, so the mock **invents** it (`profiles.accept`, `profiles.fingerprint`, the accepted entry of `profiles.list` / `get`,
plans of `{profile}` / `{restore: "accepted"}`; marked INVENTED in `mock-backend.js`). Query knobs: `profiles=none`, `profiles=accepted`, `accept=unavailable`, `fingerprint=incomplete`.
Note the recorded `tune-last.json` was read after the recorded apply (P0 53x at -20 mV), while the saved profiles were taken from the read before it.
