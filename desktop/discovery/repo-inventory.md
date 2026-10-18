# arrowlake-tuner — capability inventory for a Tauri/AppImage desktop front-end

_Discovery worker D1, 2026-10-18, host lnx, **read-only** (no hardware, no helper, no sudo, no /dev). Repo state: `main` @ `de21a80` (G-154), STATE.md says 3017 tests OK._
_Paths are repo-relative `file:line`. Evidence tags per AGENTS.md. `docs/ARCHITECTURE.md` Contract 8/9 (lines 134-388) is the written wire contract; code cited where it is the authority._
_Things I could not verify read-only are marked `[UNVERIFIED]` (notably real per-transaction latency; see §5.4)._

**Headline for the desktop design (read first):**
1. Everything privileged lives behind ONE root helper on `/run/arltuner/helper.sock`, JSON lines, **one client at a time**. A desktop app is just another client of that socket; the web server (`src/arltuner/web/server.py`) is a thin cookie/CSRF/SSE gateway in front of it.
2. A Tauri app that talks to the socket directly (Rust or a Python sidecar) **drops the whole HTTP token/cookie/Origin layer**; `SO_PEERCRED` + `/etc/arltuner/helper.toml [access]` becomes the only gate (`service.py:361-407`). It also **excludes the web GUI** (`CLIENT_BUSY`, `service.py:389-392`).
3. Reusing `web/` unchanged inside a webview means keeping the Python server as a sidecar. The server is stdlib-only, hardened, and serves a fixed 3-file manifest (`server.py:40-44`).
4. D-012 (stdlib-only Python), O-2 (local web UI) and "no CDN/npm" are **not preserved by a Tauri front-end — they are superseded** and need a new `DECISIONS.md` entry/operator yes (§8).

---

## 1. HTTP / JSON surface (`src/arltuner/web/server.py`, 854 lines)

### 1.1 Routes (all fixed; `_route` `server.py:657-675`)
| Method | Path | Request | Response | Helper? | Auth / CSRF | Streaming |
|---|---|---|---|---|---|---|
| POST | `/bootstrap` (`:701-730`) | `application/x-www-form-urlencoded` `token=<256-bit>[&scope=full\|read]`, body ≤ 1 KiB (`:208`) | 200 HTML meta-refresh to `/` + `Set-Cookie: arltuner_session=<sid>; Path=/; HttpOnly; SameSite=Strict` | no | Host exact; `Origin` must be our origin **or `null`** (file:// launcher, `:703`); token single-use, 900 s TTL (`:206-207,346-363`); own rate limiter `boot_limiter` (10 wrong/60 s, `:290,719-724`) | no |
| GET | `/`, `/app.js`, `/style.css` (`MANIFEST` `:40-44`) | none | static bytes read once at server start (`:297-301`); `Cache-Control: no-store`, CSP `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'` (`:45-46`) | no | session cookie (`_static` `:678-684`) | no |
| GET | `/api/status` (`:686-690`) | none | `{ok, helper_alive, sse_clients}` (triggers a one-shot helper reconnect via `live_helper`, `:439-455`) | liveness only | cookie | no. Page uses it only after an SSE close, `app.js:5299` |
| POST | `/api/logout` (`:692-699`) | none | 200 + cookie cleared | no | Origin exact + cookie | no. **The page never calls it** (grep) |
| POST | `/api/call` (`:732-781`) | `application/json` `{op, args}`, body ≤ 32 KiB (`:210`), strict JSON (NaN/Infinity → 400, `:226,745`) | short op: `200 {id, ok:true, result}` or `{id, ok:false, error:{code, message, detail?}}` with HTTP status from `_STATUS_FOR_CODE` (`:183-184`); long op: `202 {id}` (`:783-794`) | yes (allowlisted ops, §1.2) | Origin exact (required on POST, `:733`) + session cookie + `may_call` scope check (`:755`); `Content-Type` exact | long-op result arrives over SSE |
| GET | `/api/events` (`:796-854`) | none; headers `Sec-Fetch-Site: same-origin` required (`:801`), `Origin` optional-but-exact | `text/event-stream`: `retry: 3000`, `: keepalive` every 15 s, events `heartbeat state telemetry progress vf.point result` | subscribes to helper events | cookie + Host + Sec-Fetch-Site | **SSE**, ≤ 8 clients (`:215`), write timeout 5 s (`:213`), per-session `result` delivery (`helperlink.py:165-172`) |

Other methods → 405 / 403 `bad_host` (`:649-655`). No WebSocket, no long-poll.

### 1.2 `/api/call` op allowlist (`OPS` `server.py:145-171`; arg validators `:58-143`; helper re-validates, `ops*.py`)
`L` = long (202 + SSE `result`). `W` = helper `OpSpec(write=True)` (write-gated by `write_gate`, `ops.py:171-184`). `R` = in `READ_OPS` (`:175-176`, usable by a `scope=read` session).

| op | args (server-side schema) | result (Contract 9, ARCHITECTURE.md) | L | W | R |
|---|---|---|---|---|---|
| `preflight` | `{}` | report `{mode, platform, checks[], topology{units[]..}, writes_allowed}` (`ops.py:187-189`) | | | R |
| `audit.tail` | `{n?: 1..200}` (`:62-63`) | `{path, records[], truncated}`; last 256 KiB only (`ops.py:53,208-241`) | | | R |
| `telemetry.start` | `{interval_s?: 0.2..60}` (`:73-75`) | `{running, already_running, restarted, op_id, interval_s}` (`core.py:549-576`); a different interval restarts the sampler | | | R |
| `telemetry.stop` | `{}` | `{stopped}` | | | |
| `cancel` | `{op_id?}` | `{cancelled, op_id, state}` (`core.py:539-547`); acts **between** mailbox transactions | | | |
| `vf.dump` | `{cpus?: [int ≤64], all_e?: bool}` | `vfdump.to_json` + `accepted_match` + `ops_done` (`ops.py:591-740`) | L | **W** | |
| `tune.read` | same as `vf.dump` | `vf.dump` + decoded `tune`, `fused`, `turbo{P,E,favored}`, `tables`, `hw_limits`, `limits`, `ring`, `ops_done`, `read_at` (`ops_tune.py:930-988`) | L | **W** | |
| `tune.plan` | `{intent}` ≤ 16 KiB (`:87-88`); intent forms: `{scope:"simple"\|"unit", rows[], turbo?}`, `{turbo}`, `{profile}`, `{reset:"stock"}`, `{restore:"accepted"}` (`ops_tune.py:1273-1320`) | `{plan_id(sha256), nonce(6×[A-Z2-9]), steps[], writes, reads, ops_needed, ops_left, expires_s:600, before, after, predicted, vccia, warnings[{code,text}], words_assumed, complete, rounds}` (`ops_tune.py:2261-2343`). Pure, no hardware, `slow` thread | | | |
| `tune.apply` | `{plan_id(64 hex), typed:"CONFIRM <nonce>"}` (`:82-84`) | `{plan_id, steps, readback[{unit,point,cpu,expected,actual,match}], ops_used, msr_writes, complete, writes_sent, warnings, cpufreq[], residual[], ring_refused[], stop?}` (`ops_tune.py:2585-2612`) | L | **W** | |
| `tune.last` | `{}` | `{available:false}` or `{available, boot_id, at, applied_at, result:<tune.read result>}` (ARCH 251-261) | | | R |
| `profiles.list` | `{}` | `{profiles:[{name, source, created, complete, chain?, note?}]}` | | | R |
| `profiles.get` | `{name}` | `{name, source, created, complete, contents{rows,turbo}, summary}` (ARCH 262-268) | | | R |
| `profiles.save` | `{name, intent}` (`:91-93`); never overwrites; max 500 (`store.py:26`) | stores a **complete** profile = last read + rows overlay (D-044) | | | |
| `profiles.accept` | `{name}` or `{from:"read"}` (`:96-97`) | writes `/etc/arltuner/accepted/` (CURRENT, profile, EXPECTED-words.txt, fingerprint.json); refuses `STALE_READ`/`NOT_APPLIED` (ARCH 293-295,339-340) | | | |
| `profiles.fingerprint` | `{}` | `{recorded, cpus, accepted[], fingerprint}` (ARCH 296-300) | | | |
| `vf.measure` | `{mode:"quick"\|"dense", units?: [unit≤256], name?, baseline?}` (`:116-118`) | `{curve_id, record}`; events `progress{step,of,unit,ratio,eta_s}` + `vf.point{unit,ratio,mhz,vid_v,status}` (ARCH 176-182). cpufreq only, journaled | L | | |
| `curves.list/get/update/delete` | `{}` / `{id, units?}` / `{id, name?, baseline?}` / `{id}` (`:132-142`); id `^\d{8}T\d{6}Z-[a-z0-9]{4}$` | list rows; record (ARCH 180-185) | | | R (list,get) |
| `mem.read` | `{}` | `{available, mts, qclk_mhz, gear, cmd_rate, bclk_mhz, timings{18}, turnaround, dimms[], cross_check, ...}` (ARCH 172-175) | | | R |
| `stress.start` | `{kind:"cpu"\|"memory", method?, units?, ratio_cap?:8..60\|null, duration_s:30..3600 (required), memory_mb?:256..}` (`:121-129`) | `{id, kind, started, ended, elapsed_s, stopped_by, errors, error_lines[], achieved{}, cap_valid, max_temp_c, max_pkg_w, mce_new, health, record_path}`; **no "pass" field** (D-019) (ARCH 213-228) | L | | |
| `stress.stop` / `stress.list` | `{}` | `{cancelled,...}` / `{results[]≤50]}` | | | R (list) |

Note: `tune.read` and `vf.dump` are **write-gated ops** (`ops_tune.py:2756-2757`, `write=True`) because every OC-mailbox request, reads included, is a WRMSR to MSR 0x150 (`docs/SAFETY.md:22`, `registers.md` "request model"). Only `tune.apply` actually changes CPU state.

### 1.3 Errors, limits, session model
- Helper error codes → HTTP: `BAD_REQUEST/UNKNOWN_OP` 400, `BUSY/PLAN_STALE/CONFIRM_MISMATCH/CANCELLED` 409, `REFUSED` 403, `PREFLIGHT_FAILED` 412, `INTERNAL` 500, unknown 422 (`server.py:183-184`). Server-only: 401 unauthorized, 403 bad_host/bad_origin/bad_fetch_site/read_only_session, 411/413/415, 429 rate_limited / too_many_pending / too_many_streams(503), 503 helper_unavailable, 504 helper_timeout.
- Only `error.detail.{state, check, curve_id}` reach the browser (`_DETAIL_ALLOW` `:185`, `public_response` `:188-200`).
- `Config` (`:203-224`): JSON body 32 KiB (helper line cap 64 KiB), ≤ 16 connections, ≤ 4 pending long ops, ≤ 8 SSE clients, short-op timeout 30 s, session idle 30 min / absolute 12 h, 10 wrong creds per 60 s.
- Long ops have **no wall-clock timeout**: a slot is held until the helper answers, the link dies, or 3 consecutive `idle` heartbeats while pending → `helper_lost` (`:418-437,457-479`).
- Server binds a **random `127.x.y.z` loopback** (cookie jar isolation, D-042; fallback `127.0.0.1`, `:316-327`). Process sets `PR_SET_DUMPABLE 0` (`helperlink.py:365-372`). `Protocol HTTP/1.0`, one request per connection (`:486`).
- Sessions have scope `full` or `read` (`READ_OPS`, `READ_EVENTS=heartbeat/state/telemetry` `:180`). `tools/screenshots.py` uses `read`.
- Reconnect: after a helper restart `live_helper()` makes one serialized reconnect attempt per call (G-153, `:439-455`); an op in flight is reported lost, never replayed.
- The GUI server starts via `tools/arltuner-gui.py` (112 lines): launcher is a 0600 `file://` page in `$XDG_RUNTIME_DIR/arltuner/` carrying the one-time token (`web/launcher.py:39-57`, `arltuner-gui.py:55-62`); a 2nd run signals the 1st via SIGUSR1 for a fresh token (`web/reissue.py`). `--helper CMD…` = simulator/dev path (socketpair, `helperlink.py:276-289`); `--no-helper` = 503 everywhere.

**Consequence for a desktop client:** (a) talking HTTP to this server keeps the launcher/token/cookie dance (needs an HTTP client with a cookie jar, `Origin` exact on POST, `Sec-Fetch-Site: same-origin` on SSE — a Tauri webview's `tauri://`/`http://tauri.localhost` origin will **not** pass the exact-Origin checks without server changes); (b) talking to the socket directly removes all of it; see §2.

---

## 2. Root helper protocol (`src/arltuner/helper/`, `src/arltuner/web/helperlink.py`)

| Item | Fact | Cite |
|---|---|---|
| Socket | `/run/arltuner/helper.sock`, `AF_UNIX` stream, root-owned, **mode 0666**; `/run/arltuner` root 0755 (tmpfiles `d /run/arltuner 0755 root root -`) | `service.py:49-50`, `packaging/arltuner-helper.socket`, `packaging/arltuner.tmpfiles.conf` |
| Activation | systemd socket unit (`ListenStream`, `SocketMode=0666`, `Accept=no`) starts `arltuner-helper.service` on first connect (`ExecStart=/usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket`); or helper makes its own socket under umask 0177 → chmod 0666 → listen | `service.py:116-223`, `packaging/arltuner-helper.service` |
| Framing | UTF-8 JSON, **one object per line**, ≤ 64 KiB per line both ways (oversize → `BAD_REQUEST` id null, stream stays usable). Compact, ASCII-only, `allow_nan=False` | `protocol.py:20,66-80,131-136`, `helperlink.py:24` |
| Request | `{"id": str 1..64, "op": str, "args": object}`; unknown top-level key, duplicate JSON key, NaN → `BAD_REQUEST`. Ids unique per connection (LRU 1024); a repeat of an in-flight `(id,op,args)` re-attaches silently | `protocol.py:96-117`, `core.py:433-445,54` |
| Response | exactly one per request: `{id, ok:true, result}` or `{id, ok:false, error:{code, message, detail?}}`. Codes: `BAD_REQUEST UNKNOWN_OP BUSY REFUSED PREFLIGHT_FAILED PLAN_STALE CONFIRM_MISMATCH CANCELLED INTERNAL` | `protocol.py:24-32,120-128` |
| Events (no `id`) | `heartbeat` every **2 s** `{op_id, state, ops_used, ops_budget}`; `state {op_id,state}`; `progress {op_id, step, of, ops_used, ops_budget}` (+ unit/ratio/eta for measure, `elapsed_s/of_s/errors` for stress); `telemetry {op_id, t, values}`; `vf.point` | `core.py:404-408,172-173,594-606`, `config.py:35`, `ops.py:75-76` |
| Authorization | **`SO_PEERCRED` of every accept** → uid must be admitted by `/etc/arltuner/helper.toml [access]` (`users` by name, optional `group`; root only if listed by name; missing/empty/untrusted/invalid ⇒ nobody; re-read through trust check when file stat changes). Refused peer gets ONE framed `{"id":null,"ok":false,"error":{"code":"REFUSED","detail":{"check":"ACCESS_DENIED"\|"CLIENT_BUSY"}}}` then close | `service.py:327-335,361-407`, `settings.py:3-12,87-117` |
| Client side check | `connect_helper` lstat's the node (must be a socket owned by root), connects, verifies the *listener's* uid via `SO_PEERCRED` on its own end, then sends `preflight` as handshake; maps refusals to `HelperConnectError.kind` (`missing untrusted denied not_allowed busy unavailable closed timeout`) with fix text | `helperlink.py:305-362,38-44` |
| One client | second client while one is connected → `CLIENT_BUSY`. Next client accepted only after **every** thread of the previous connection ended. Client that stops reading is dropped (`SO_SNDTIMEO` 8 s) | `service.py:389-392,60,409-422`, `core.py:244-273` |
| Idle exit | socket-activated helper exits after `idle_exit_s` (default 600; 0 = never; `helper.toml`) with no client and no op | `settings.py:27`, `service.py:425-430` |
| Operator identity | audit `operator = gui:uid<N>` for the connection; stress child runs as that uid; root peer → `NO_INVOKER` for stress | `core.py:250-251`, ARCH 367 |
| State machine | `idle \| planning \| applying \| measuring \| stressing`. Write/long ops only in `idle`, else `BUSY` (`detail.state/op_id/op`). Read-only (`slow`) ops allowed in every state but ≤ 4 concurrent (`SLOW_MAX`, 5th → `BUSY`); request queue 64 | `core.py:447-456,56,55`, `state.py` |
| Cancel / disconnect | `cancel` and EOF/SIGTERM set the op's cancel flag; the in-flight transaction completes (read-back / restore), no new one starts, op answers once | `core.py:186-192,342-364,539-547` |
| Write gate (D-027 c) | every `write=True` op re-runs the helper's own `preflight` and refuses unless mode is `write` with no `UNKNOWN` check (`PREFLIGHT_FAILED`) — skipped for the simulator | `ops.py:171-184`, `core.py:508-513` |
| Root argv | as root only: nothing, `--device`, or `--device --socket`; `--sim`, `--audit`, `--lock`, `--policy`, `--socket-path`, … refused by name | `helper/main.py:56-57,102-115` |
| Trust | root-owned, non-g/o-writable paths with root-owned parents; `O_NOFOLLOW`, fstat regular, nlink 1; code tree check at start | `helper/trust.py`, SPEC §4.4, D-029 |
| Machine-wide | one helper per machine (`InstanceLock` flock `<lock>.helper`, exit 75 if busy); mailbox lock `/run/arltuner/mailbox.lock` (installed) | `core.py:64-98` |

### 2.1 Which helper ops reach hardware
| op | Tier | Evidence |
|---|---|---|
| `tune.apply` | **T2 volatile write**: `0x11/0x15/0x1D/0x27` mailbox writes + plain `wrmsr 0x650/0x1AD`, each read back; plus `scaling_max_freq` writes (`_lift_cpufreq`) | `ops_tune.py:2761,2518-2612` |
| `vf.dump`, `tune.read` | mailbox **reads** = WRMSR 0x150 (T2-tier traffic, no state change) + plain `rdmsr`. `tune.read` also reads the ring and 4 turbo MSRs | `ops_tune.py:2756-2757,695-815`, SAFETY.md:22 |
| `vf.measure` | cpufreq sysfs only (`scaling_min/max_freq` pinned, journaled `/run/arltuner/measure-journal.json`, restored on every exit and at helper start); `rdmsr 0x198` via guard allowlist | ARCH 176-179,200-202, `ops_measure.py:1099` |
| `stress.start` | cpufreq cap journal (optional) + spawns `/usr/bin/stress-ng` / `/usr/bin/stressapptest` **as the invoking user**; no MSR/mailbox | `ops_stress.py:1-15`, `stress/cmd.py:15-18` |
| `profiles.accept/fingerprint` | root writes **files** in `/etc/arltuner/accepted/`; no hardware | `ops_tune.py:2766-2768`, `profiles/accepted.py:1-20` |
| `profiles.save`, `curves.update/delete` | root-owned files under `/var/lib/arltuner/` | `profiles/store.py:1-10`, ARCH 185 |
| `telemetry.*`, `mem.read`, `preflight`, `audit.tail`, `*.list/get`, `tune.plan`, `tune.last` | read-only (rdmsr allowlist, sysfs, perf, MCHBAR via `mem.read`) | `telemetry/msrio.py:29-33` |

All mailbox traffic goes through `WriteGuard` (`guard/guard.py`, "only path that may call `MailboxTransport.transact`"). Order of checks, first failure refuses and audits: `TRIPPED, NO_ENV_FLAG, BAD_CONFIRMATION, CPU_NOT_ALLOWED, NOT_ALLOWLISTED, BASELINE_MISSING, CHECK_FAILED, SESSION_LIMIT, AUDIT_UNAVAILABLE, BOOT_LIMIT, DWELL, LOCKED, HEALTH_GATE` (`guard.py:10-14,268-281`).

---

## 3. Tunable controls: read / write / limit matrix

Mailbox encodings: `docs/knowledge/registers.md` sections cited as "reg:§". Policy = `/etc/arltuner/policy.toml` (seeded from `policies/default.toml`, schema `guard/policy.py:14-53`). **D-039: the code keeps only the CPU's own limits (read live) and the field encodings; every other number is the user's policy.**

| Control | Units | READ source | WRITE path (exists today) | Encoding | Policy key(s) / allow entry | Guard check | Status |
|---|---|---|---|---|---|---|---|
| **Whole-curve voltage offset** (OC word `P2=0`, bits [31:21]) | per P-core, per E-cluster (4 CPUs written separately) | mailbox `0x10 P1=0 P2=0` (`default.toml:103 voltage_freq_read`) | `0x11 P1=0 P2=0` (`:125 voltage_freq_write`) via `tune.apply`; CLI `apply` | 11-bit signed, LSB 1/1024 V, −1000…+999 mV (reg:"V/F data word") | `offset_ceiling_mv` (def +200), `effective_offset_floor_mv` (def −200), `assumed_bios_offset_mv`, `max_offset_step_mv` (def 200) | `vf_bounded` | **R+W** |
| **Per-V/F-point offset** | P pts 1..8 (8,20,30,48,53,55,56,57×), E pts 1..6 (8,15,23,30,40,46×); count read live | `0x10 P1=0 P2=1..10` until rc `0x11` (`:110 vf_point_read`) | `0x11 P1=0 P2=1..8` (`:149 vf_point_write`) | same offset field; ratio/target are fused (hardware ignores them) | same offset keys | `vf_point_offset_bounded` | **R+W** (GUI: "Per V/F point Offsets") |
| **OC ratio** (ratio field [7:0] of OC word + favored `0x1D`) | per unit | `0x10`, `0x1C` (favored, `:75`), `0x09 P1=0x11` fused top (`:68`) | `0x11` ratio + `0x1D` (`:133 favored_core_write`) + `0x26/0x27` granular echo (`:82,141`) | integer ×BCLK (BCLK stays 100 MHz); 1..255 | `[ratio_max] P/E` (`"auto"` = fused `0x1C[15:8]`+3, D-038; or number) clamped by CPU `cmd 0x01[7:0]`; `max_ratio_step` (def 8) | `vf_bounded` (ratio ceiling+step), `favored_ratio_follows_vf`, `granular_equals_read` | **R+W** |
| **OC-point adaptive target voltage** ([19:8], mode adaptive) | per unit (only meaningful when ratio > fused top) | OC word `0x10`; fused V `0x09`; hw max `0x28` (`:89`) | `0x11 P2=0` | 12-bit S2.10, V=raw/1024; page grid 0.900 V + 5 mV | `[v_target_max_mv] P/E` (`"auto"` = CPU `0x28` max − 1 LSB), `max_v_step_mv` (def 200) | `vf_bounded` | **R+W**; static/override mode never written (D-016) |
| **Per-core voltage enable** (`0x15` bit 3) | package | `0x14` (`:96 misc_global_config_read`) | `0x15` (`:117`) only to set bit 3 | reg:"XTU's write procedure" | none | `percore_enable_only` | side effect of apply; no UI |
| **Turbo ratio table — P** (8 buckets) | package (MSR on CPU 0) | plain `rdmsr 0x1AD`, counts `0x1AE` (`:184,189`) | plain `wrmsr 0x1AD` on CPU 0 (`:206`) | 1 byte/bucket, bucket 0 = 1 active core | `ratio_max` P; `max_ratio_step` | `turbo_ratio_bounded` (non-zero, non-increasing, ≤ ratio_max, step from table read this session) | **R+W**; counts R only (`PLAIN_MSR_WRITABLE={0x1AD,0x650}` `policy.py:173`) |
| **Turbo ratio table — E** | package | `rdmsr 0x650`, counts `0x651` (`:179,194`) | `wrmsr 0x650` CPU 0 (`:199`) | same | `ratio_max` E | same | **R+W** |
| **Ring (cache) V/F point offsets** (`P1=2`) | 1 domain, via CPU 0; points 1..9 (9 = the 40× slot the BIOS ring OC ratio creates) | `0x10 P1=2 P2=1..10` (`:157 ring_point_read`) | `0x11 P1=2 P2=1..9` (`:164 ring_point_write`, operator yes 2026-10-15) | offset only | policy ring entry (reference machine: P2 1..9); offset keys as above | `vf_point_offset_bounded` | **R+W in profile / restore / CLI only** — **no editor row in the GUI** (§9). Ring **OC word** is BIOS-owned: read+compared, never written (D-045) |
| **Ring ratio** | — | `0x10 P1=2 P2=0` read-only | **none** (needs BIOS) | — | — | — | **read-only** |
| **CPU limits (informational)** | P and E | `0x01 P1=0` fused max OC ratio; `0x28 P1=0` max V; `0x1C[15:8]` (`ops_tune.py:756-772`) | n/a | — | clamp ceilings (D-039) | — | **read-only** (feeds `limits`, `hw_limits`) |
| **cpufreq caps** `scaling_max_freq` / `scaling_min_freq` / EPP | per CPU | sysfs | **no direct user control.** Written only as side effects: (a) `vf.measure` pins min=max (journaled, restored); (b) `stress.start` `ratio_cap` (journaled); (c) `tune.apply` lifts `scaling_max_freq` after a `0x1D` raised cpuinfo_max (`_lift_cpufreq` `ops_tune.py:2502-2515`, rule `controls/cpufreq.py`) | kHz | none | journal-based | no user-facing control |
| **PL1 / PL2 / PL4, tau, powercap vs MSR 0x610/0x601, MMIO 0x59A0..B0** | package | **telemetry reads only**: power from powercap `energy_uj` or MSR `0x611` (`telemetry/rapl.py`, `sampler.py:46-48,92-98`) | **NO WRITE PATH IN THE APP.** `0x610/0x601/0x606` are not in `PLAIN_MSR_READABLE` (`policy.py:170`) nor writable. PL2 was set by the orchestrator **by hand via powercap sysfs** (`captures/lnx/ring-pl2-20261015.txt`: `constraint_1` 295→275 W, `0x610` decoded) `[MEASURED lnx 2026-10-15]`; BIOS restores 295 W at boot (STATE.md) | PL1 `[14:0]`, PL2 `[46:32]`, lock `[63]`, 0.125 W units (reg:"Measured") | none | none | **no read UI for limits, no write** |
| **TVB** (`0x24/0x25`, `0x18/0x19`) | per bucket / CCP | none in app (probed by hand: `captures/lnx/tvb-*.txt` `[MEASURED lnx 2026-10-13]`) | **none**: `Cmd` enum only (`mailbox/commands.py:33-45`), no allow entry; `0x25` and `0x19` are in `WRITE_COMMANDS` so the guard refuses by default-deny | reg:`0x24/0x25` | — | NOT_ALLOWLISTED | **no path** |
| **IccMax** (`0x04/0x16/0x17`) | VR | none | **none** (enum only) | U9.2, 0.25 A | — | NOT_ALLOWLISTED | **no path** |
| **AVX offset** (`0x1A/0x1B`), **BCLK** (`0x22/0x23`), SVID, AVX scale | — | none | **none** (enum only; BCLK read-back unproven on ARL) | — | — | NOT_ALLOWLISTED | **no path** |
| **Uncore/ring frequency** (MSR `0x620/0x621`) | package | not in the app (ring VID was measured by hand through `intel_uncore_frequency`, `ring-pl2-20261015.txt`) | none | `0x620` [6:0] max, [14:8] min | — | — | **no path** |
| **GT / SA / NGU domains** (`P1=1,3,4,7`) | — | `0x10 P1=7` and `0x28 P1=7` readable per `default.toml:89,103` | `POINT_P1_ALLOWED={0,2}` only (`policy.py:607`) | — | — | refused | **read-only, no UI** |
| **Memory (DDR timings, MCHBAR)** | package | `mem.read` (MCHBAR via `memory-mchbar.md`, perf; SMBIOS + spd5118 temps) | **none** | — | — | — | **read-only** (Dashboard Memory card) |
| **Power profile / governor / turbo on-off** | — | `no_turbo` read by measure (`curves.list` field `no_turbo`) | none | — | — | — | read-only note only |

**What the guard/policy bound for every write** (summary): CPU allowlist (`cpus`/`e_cpus` = `"auto"` live topology), default-deny allowlist, per-kind ratio and V ceilings (policy ∩ CPU limit), offset window, per-write step limits (larger changes split into guarded **rounds** inside one apply, `MAX_RAMP_ROUNDS=16`, `planner.py:756`; refused `STEP_LIMIT` before any write), `max_operations_per_session/boot` (shipped 100000 = indicators, D-032) and `min_dwell_s` (shipped 0.0), health gate before/after each transaction, per-write read-back (mismatch trips the guard; no retry), plan nonce (§5), `BASELINE_MISSING` (a write needs a read of the same word earlier in the session). No loop-retry anywhere (SAFETY.md:45).

**Cross-cutting facts the UI relies on** (`SPEC-v3.md` §9, `docs/knowledge/platform-arl-s.md`): P points `8,20,30,48,53,55,56,57×`, E points `8,15,23,30,40,46×`; hardware clamps V(n) ≥ V(n−1); OC point cannot be cleared at runtime (cold boot only; G-147 "residual OC point" rewrite → EQUIVALENT); E clusters share one ratio; never offset E 40× `[MEASURED lnx 2026-10-02]`; turbo buckets non-increasing (`TURBO_ORDER`).

**Intent schema (what `tune.plan` accepts; NO ring field)** — `ops_tune.py:1222-1320`: row `{target: "P"\|"E" (scope simple) \| "P0".."EC3" (scope unit), offset: null \| {kind:"curve", mv:int} \| {kind:"points", mv:{ratio:int}}, v_target_v: float\|null, ratio: int\|null}`; ≤ 32 rows; `turbo: {P?:[8], E?:[8]}`; whole mV only; mV window −1000..999; unknown keys → `BAD_REQUEST`. Ring is reachable only through a complete profile / `{restore:"accepted"}`.

---

## 4. Telemetry (`src/arltuner/telemetry/`)

| Metric (JSON key in `telemetry.values`) | Source | Privilege | Cite |
|---|---|---|---|
| `cores[cpu]{name,mhz,busy,vid,ratio}` | APERF/MPERF `0xE8/0xE7` deltas (+ TSC MHz from `0xCE`), `0x198` VID `[47:32]/8192` and ratio `[15:8]` | root `rdmsr` via `TelemetryMsr` | `sampler.py:70-77`, `cpu.py` |
| `temps{label→{value,name,cpu}}` | coretemp hwmon; MSR `0x19C/0x1B1`+Tjmax `0x1A2` fallback | hwmon: user; fallback root | `sampler.py:78-87`, `hwmon.py` |
| `vcore` | board hwmon channel chosen per DMI board name (`platforms/arl-s.toml [boards]`); unknown board = none | user | `sampler.py:96` |
| `power.package` / `power.core` | powercap `energy_uj` of `intel-rapl:0` / `:0:0`, fallback MSR `0x611` | root-only since kernel 5.10 | `sampler.py:88-93`, `rapl.py` |
| `dram.read/write` GB/s | **one long-lived** `perf stat -a -I … uncore_imc_free_running/data_read/,/data_write/` | root; `/usr/bin/perf`, env scrubbed | `imc.py:1-25` |
| `limit_reasons_raw` | MSR `0x6B0` raw (decode open) | root | `cpu.py:113`, `sampler.py:99` |
| `ts` | wall clock of the sample | | `sampler.py:96` |
Every leaf is `Reading {value\|null, unit, source, status:"ok"\|"unavailable", reason?}`; unavailable = `n/a` + reason, never a guess (`readings.py`, `app.js:96-102`). Allowed MSRs: `TELEMETRY_MSRS` (`msrio.py:29-33`; refuses `0x150`; AST test bars importing mailbox/guard, D-026). Memory: separate `mem.read` (static, once + on demand).

- **Rate:** `telemetry.start{interval_s}` 0.2–60 s, default 1 s (`ops.py:55`, `server.py:73-75`). Sampler thread is push-only: the helper emits an event when `ts` changes, polling the snapshot every `max(20 ms, interval/4)` (`core.py:594-606`).
- **Page:** subscribes via SSE `/api/events` once; calls `telemetry.start` on every SSE open (`app.js:5282`); slider steps `[0.25,0.5,1,2,5]` s (`app.js:13`, default 1 s, stored in `localStorage`, `:60-66`); each change → `telemetry.start` after 250 ms (`:1435-1452`, restarts the sampler). Client history 15 min (`HISTORY_MS` `app.js:10`, cap 7000 samples). Heartbeat staleness 7 s (`:8`). So: **push over SSE, not polling**; the only polled data is on demand (`audit.tail`, `mem.read`, `tune.last`, `*.list`).
- A client leaving stops telemetry in the helper (`core.py:353`).

---

## 5. Current web page (`web/index.html` 518 lines, `web/app.js` 5374 lines, `web/style.css` 856 lines)

Single IIFE, no framework, **all text via `textContent`, no inline style**, SVG charts hand-built (CSP). Tabs are an ARIA tablist (`index.html:26-34`), active tab in `location.hash` (`app.js:216-230`).

### 5.1 Tabs and panels
| Tab (`index.html`) | Controls / panels |
|---|---|
| **Header** (11-45) | brand + mode tag (`reading preflight / guarded writes / writes off`); tab bar; **Refresh slider** 0.25–5 s; pills `events: connecting/connected` and `helper: n/a`; banner for read-only/refuse/session-ended |
| **Dashboard** (51-175) | sub-views Overview / Cores / Sensors; applied-state chip (stock / words / unknown); chart window 1/5/15 min; Pause. **Overview:** SVG **core map** (metric Clock/Busy/Temp/VID, fixed/auto scale, scale lock, probe line); 6 tiles with sparklines (package power, hottest sensor, highest clock, Vcore (board), core power, DRAM R/W) + limit-reasons raw; power and temp history charts; **Mailbox ops this boot** ring + session sparkline + cap note; **Memory card** (DDR5 headline, timings, DIMMs + temps, "All timings" dialog, "Read again"). **Cores:** filter, column toggles (Busy/Ratio/VID/Temp/Trend), sortable table, detail panel with 4 charts. **Sensors:** temperature list + 4 charts |
| **Curves** (178-242) | Measure card: Resolution (Quick/Dense), Name, Units chips (12), Measure/Cancel, turbo-off note, progress bar, problems list. Curve card: views Chart/Table/Library; curve picker; **Compare with**; presets (All P/All E/Favored/All), legend chips (Shift = solo), options; Δ-vs-baseline table; Library (rename, set baseline, delete with confirm dialog, export JSON). Tuning-state badge "stock baseline" |
| **Tune** (245-331) | Read card: **Read ratios & voltages** + Cancel + progress + cost text ("each request is a WRMSR"); steps list; applied chip. Console (after a read): scope **All-Core / Per-Core**; "Show raw words"; per-unit rows: **Voltage offset** (Whole Curve dropdown / Per V/F point dropdowns, 5 mV grid inside the policy window), **OC ratio** dropdown ("No OC point", stockMax+1 … policy ratio_max), **Adaptive voltage (OC point)** dropdown (0.900 V + 5 mV … policy max), **48x offset** dropdown (G-150), Undo per row, clamp/predicted-voltage badges; foot: Discard, **Restore accepted**, **Reset offsets to 0**, **Review changes**. Cards: Highest predicted VID (VccIA), **Turbo ratio tables** (8 dropdowns P and E, step chart, counts read-only), Preview (measured curve + prediction overlay), Values as read (decoded/raw table + accepted comparison) |
| **Stress** (334-400) | permanent "Quick screen — NOT an acceptance gate" banner (D-019); Screen CPU/Memory; CPU load integer/vector-math/cache; memory MB select; Length presets 1/5/15/60 min + custom slider 30–3600 s; Ratio cap select; unit map; Start/Stop; Live ring + clock + errors + temp/power sparklines; result card; history table (50) |
| **Profiles** (403-440) | Accepted-state card (**Set the current state as accepted**, **Record CPU fingerprint for Windows**); profile list (summary; per profile **Load into Tune**, **Review and apply**, **Set as accepted**, `app.js:4968-4979`; no delete for profiles); **Save the current Tune settings** (name `[a-z0-9._-]{1,64}`); Compare two profiles table |
| **System** (443-480) | Helper/ops budget ring + state + last heartbeat; Preflight list (Re-run); Tuning limits table (policy vs "your CPU allows") |
| **Audit** (483-497) | audit tail table (200 rows, filter All / Writes & refusals, Refresh) |
| Dialogs (500-515) | All memory timings; **Review changes** (apply dialog); generic confirm |

### 5.2 Page start-up traffic (`onOpen` `app.js:5278-5289`)
On every SSE open: `telemetry.start`, `preflight`, `audit.tail(200)`, `mem.read`, `curves.list`, `profiles.list`, `tune.last` = **7 POSTs** at once (+ `profiles.get`/`curves.get` as needed). The helper runs ≤ 4 slow ops concurrently, so the page re-asks BUSY read-only ops with 0.3→8 s back-off (`RETRY_BUSY_OPS` `app.js:121-132`, G-139). Static files: 3 GETs.

### 5.3 The apply flow, step by step (Tune)
Precondition: a **`tune.read` this boot** (else `tune.plan` → `REFUSED NO_READ`, `app.js:3914`). Stored in `/var/lib/arltuner/state/last-read.json`, restored lazily after a helper restart (`tune.last`), so normally once per boot.

| # | Actor / action | Round trip | Cite |
|---|---|---|---|
| 0 | (once per boot) **Read ratios & voltages**: `POST tune.read` → 202; SSE `progress` per mailbox op; SSE `result`. ≈112 V/F word reads + 12 fused + 12 favored + 4 limits + 4 plain rdmsr + ring ≈ **130+ mailbox WRMSRs** (ARCH 271-272,283) | 1 POST + N SSE events | `app.js:1561-1578`, `ops_tune.py:695-715,930-988` |
| 1 | User edits dropdowns (client-side `T.draft`; prediction drawn locally from a stored curve) | 0 | `app.js:2945-2968` |
| 2 | **Review changes** → `POST tune.plan {intent}` (sync, ≤ 30 s). Helper resolves intent against the last read, checks policy/preflight/rounds/budget, stores plan (≤ 16, **expires 600 s**), returns nonce | **1 POST** | `app.js:3903-3927`, `ops_tune.py:2261-2343,129` |
| 3 | Review dialog opens (before/after table per unit/point, ops needed, warnings, VccIA prediction, turbo diff, "Revert: cold boot"); focus on **Cancel**. **Apply** button is enabled only by `applyGate` (SSE up, preflight mode `write`, `writes_allowed`, **fresh heartbeat ≤ 7 s with ops budget**, no op running, helper `idle`, ops fit) and "plan not expired" | 0 (blocking gates, heartbeat cadence **2 s**) | `app.js:3984-4000,4282-4298,1401-1411`, `config.py:35` |
| 4 | **Apply** click → `POST tune.apply {plan_id, typed:"CONFIRM <nonce>"}` (nonce is returned to the client, one click, no typing: D-033 d) → 202 | **1 POST** | `app.js:4300-4323` |
| 5 | Helper (op thread, state `applying`): `claim` plan (nonce single-use, 3 wrong → drop), re-check against policy now, `write_gate` (fresh helper preflight), `_guarded_session` (audit/lock trust checks, read whole audit log for budget, health gate factory), `apply_profile`: re-read baselines, `PLAN_STALE` check, then per word write → read-back → ..., fold results, `_lift_cpufreq` poll | SSE `progress` events | `ops_tune.py:2518-2612,485-569`, `controls/executor.py` |
| 6 | SSE `result` → `onCommitResult` shows read-back table, cpufreq rows, "Validate with a stress screen", "Measure again" | 0 | `app.js:4334-4388` |
| 7 | After **every** long-op result: `audit.tail(200)` (`loadAudit`, `app.js:1335`) and, after apply, `tune.last` (`refreshTablesAfterApply` `:1593-1602`) | **2 POSTs** | |

**Totals for one edit→apply:** 2 user-visible POSTs (`tune.plan`, `tune.apply`) + 2 follow-up refresh POSTs (`audit.tail`, `tune.last`) + 1 SSE stream. Plus the once-per-boot `tune.read`. Only one confirmation (the Apply click). That is **not** many HTTP requests; the web layer is not why apply feels slow.

### 5.4 Why apply is slow / clunky (concrete, from code; per-op latency NOT measured — `[UNVERIFIED]`)
I could not time a mailbox transaction without touching the helper; **ask the orchestrator to derive real latency from the `ts` fields of `attempt`/`result`/`readback` records in `/var/lib/arltuner/audit.jsonl`** (root 0600). The following are structural costs, each a candidate:

| # | Cause | Evidence |
|---|---|---|
| a | **Op count is large.** Per CPU a V/F rewrite is `READS_PER_CPU=4` (`0x10,0x1C,0x09,0x26`) + `WRITES_PER_CPU=3` (`0x11,0x1D,0x27`) + `VMAX_EXTRA_READS=1` (`0x28`) + `SPLIT_EXTRA_WRITES=1`, **and each write is read back by an extra WRMSR** (not counted by the guard, counted by the planner). An E cluster = 4 CPUs written one by one. Documented: one P core = 13 WRMSRs, **an E-cluster raise = 53** (SAFETY.md:87); a real restore = **37 writes, 353 ops** (STATE.md, 2026-10-13). A restore additionally scans 2 reads per unit for residual OC points and may run up to 16 rounds | `controls/model.py:82-89`, `ops_tune.py:2273,2283-2287`, `planner.py:756` |
| b | **Strictly serial, one transaction at a time** (safety rule from the 2026-08-26 incident; D-032 keeps it). Per transaction the guard does: create `O_EXCL` lock file + unlink; `read_state` parse of the **whole audit log** (cross-process dwell + per-boot cap), repeated once the lock is held; audit `attempt`, `result`, `readback` records each `fsync`'d; **health snapshot twice** (before and after: taint file, PCI device-dir listing, `/proc/modules`, thermal zones, machinecheck banks) | `guard/guard.py:19-34,954-975`, `guard/audit.py:219-220`, `health/snapshot.py:1-18`, `health/gate.py:151-170` |
| c | Mailbox transport polls MSR 0x150 bit 63 in 10 ms steps (timeout 10 s) before and after each WRMSR | `msr/transport.py:39-47` |
| d | Policy `min_dwell_s` is a hard floor between operations (code default 60 s when a policy states nothing; shipped `default.toml` sets 0.0). With the executor's `_Pacer` waiting `dwell + 0.05 s` | `controls/executor.py:48-60`, `default.toml:33`, SAFETY.md:66 |
| e | Audit log has **no size cap and is never truncated** (SAFETY.md:67-73) while `read_state` re-parses it per operation → cost grows with history (install seeds with a 64 MiB cap check, D-031). `[UNVERIFIED]` how large it is on the reference machine | `guard/audit.py`, `ops.py:247-412` (BudgetMeter exists specifically to avoid rescanning per heartbeat) |
| f | After an OC-ratio change the helper polls `cpuinfo_max_freq` up to 4 × 0.25 s to lift `scaling_max_freq` | `controls/cpufreq.py` `POLL=(4,0.25)`, `ops_tune.py:2490` |
| g | `tune.read` is a prerequisite for planning, costs ≥ 130 WRMSRs, and re-planning after any helper restart/plan expiry (600 s) repeats the review. `PLAN_STALE` forces re-plan if any word changed | `app.js:3914`, `ops_tune.py:2370-2391` |
| h | Heartbeat-gated UI: Apply stays disabled until a fresh heartbeat (2 s cadence) with the ops budget has been seen; any helper reconnect/`idle` transition re-evaluates it | `app.js:1375-1411`, `config.py:35` |
| i | UX: the apply dialog cannot be closed or cancelled while running (`btn-plan-close` disabled, ESC blocked, `app.js:4287,4481`); **no Cancel button for apply** (the page's Cancel is wired only to reads, `app.js:3889-3892`; the helper supports `cancel` between transactions); progress is a single bar + text (`app.js:4325-4332`); result needs a reading pass of a long read-back table; 7 gates produce one text reason |
| j | After results, `loadAudit` re-reads and re-renders up to 200 audit rows (up to 256 KiB read in the helper, `ops.py:53,208-241`) | |

Real numbers to get from the orchestrator (cheap, read-only): audit timestamps per op; `ops_used` / duration of the last restore (`captures/lnx/restore-20261018T075130-*` holds only the plan text, no timing).

---

## 6. Profiles, accepted state, restore, stress, health gate, audit

| Area | Facts | Cite |
|---|---|---|
| Profile schema | TOML: `[meta]`, `[[core]]` (kind, index, ratio, oc_voltage_mv, offset_mv, `fused_top_ratio`), `[[point]]` (per V/F point), `[turbo]` p/e arrays, `[ring]` (oc_word + points). **Complete** profile (`[meta] complete=true`, D-044): one core per unit, all points, both turbo tables or neither. Partial = "legacy" delta | `profiles/schema.py:1-25`, ARCH 323-355 |
| Saved profiles | root-owned `/var/lib/arltuner/profiles/<name>.json` (0600, dir 0700), name `^[a-z0-9._-]{1,64}$`, never overwritten, ≤ 500, ≤ 32 KiB, `{name, created, profile:<TOML>}` or legacy `{intent}` | `profiles/store.py:1-30` |
| Accepted state | `/etc/arltuner/accepted/`: `CURRENT` (exactly one `profile = profiles/<n>.toml`; chain refused `ACCEPTED_CHAIN`, fix `arltuner accepted-migrate`), `EXPECTED-words.txt` (`<cpu> <OC word> <points…>`, plus `ring …`), `fingerprint.json` (CPU identity + fused `0x09` words for Windows), `profiles/`. Dir replaced atomically; written only by `profiles.accept` (root helper), never by install | `profiles/accepted.py:1-20`, `ops.py:56-58` |
| Accept freshness | `accept` refuses `STALE_READ` if the audit log shows a write after the read; `NOT_APPLIED` if a saved profile ≠ CPU; `{from:"read"}` records what the CPU holds | ARCH 293-295,339-340 |
| Restore | `intent {restore:"accepted"}` → plan/apply of the one profile: writes **only words that differ**, residual-OC-point rewrite (G-147, result EQUIVALENT), transition-safe write order (G-151), step-limit **rounds** (G-148), ring unit refused alone if its OC word changed (G-154). Same logic for the CLI scripts: `tools/lnx/restore_tuning.sh` (CONFIRM once, `--check`, `--orchestrated`) and Windows `tools/win/restore_tuning.ps1` | `ops_tune.py:1316-1319,2272`, `restore_tuning.sh:1-30` |
| Compare | `accepted_match` per core `{cpu, match, diffs[], equivalent?, equivalents[]}` + ring entry; CLI `compare-accepted` | `ops.py:488-543`, `cli.py:442` |
| Last read | `/var/lib/arltuner/state/last-read.json` (0700/0600, boot-id bound, ≤ 256 KiB); words follow verified read-backs after an apply | ARCH 251-261 |
| Curves | `/var/lib/arltuner/curves/<id>.json`, root 0700/0600; `baseline` flag on at most one; record has per-unit points `{ratio,mhz,vid_v,vid_min,vid_max,kept,total,status:ok\|mismatch\|unreachable\|interpolated,reason…}` | ARCH 180-185,203-212 |
| Stress | `stress-ng` (cpu: `--cpu N --taskset … --verify`; methods integer/vector-math→matrixprod/cache) and `stressapptest` (`-s T -M MB -W`) at fixed absolute paths, trust-checked, run as the invoking user (`Popen(user=…)`, own session/process group), killed on stop/exit/timeout; optional ratio cap through the measure journal (E clusters capped as 4 cores, G-152); stores `/var/lib/arltuner/stress/<id>.json` (≤ 200); result has errors/health/MCE but **never a pass verdict** (D-019) | `ops_stress.py:1-15`, `stress/cmd.py:15-18`, ARCH 213-239 |
| Health gate | `make_health_gate`: snapshot **before** and **after** every mailbox transaction; clean delta required: new taint bits (except `allow_taint_bits` of `helper.toml`, default `[2]` from install), cleared taint, lost/added PCI devices, module changes; fails closed on unreadable data. **Known limit:** sees only its window (an instability that needs load later is not seen: SAFETY.md:68-73, D-042 (6)) | `health/gate.py:1-60,129-170`, `helper/settings.py:3-8` |
| Audit | `AuditLog` append-only JSONL, `O_APPEND|O_NOFOLLOW`, fsync per record; events `attempt`, `result`, `readback`, `msr_read` (+ refusal records); fields incl. `ts`, `seq`, `boot_id`, `cpu`, `cmd/p1/p2/data`, `rc`, `reason`, `operator` (`gui:uid<N>`). Path installed `/var/lib/arltuner/audit.jsonl` (0600), dev `$XDG_STATE_HOME/arltuner/audit.jsonl`. Counts `ops_used` per `boot_id` (heartbeat). Never delete/truncate to get past `AUDIT_UNAVAILABLE` | `guard/audit.py`, `ops.py:247-412`, SAFETY.md:66-73 |
| Budget | heartbeat `ops_used`/`ops_budget` (policy `max_operations_per_boot`, shipped 100000 → indicator, D-032) | `ops.py:247-300` |

---

## 7. Packaging today

| Item | Facts | Cite |
|---|---|---|
| Installer | `sudo bash install.sh [--reseed-config] [--add-user NAME]`; copies only; the only Python that runs (compileall, self-check) is the installed root-owned tree under `-I`, cwd `/`; idempotent; `DESTDIR=` for tests; `uninstall.sh` (`--purge` needs `--yes-reset-budget`) | `install.sh:1-62`, D-031 |
| Installed layout (root:root) | `/usr/lib/arltuner/arltuner/` (precompiled package) · `/usr/lib/arltuner/platforms/*.toml` · `/usr/lib/arltuner/arltuner-helper` (0755 launcher, `#!/usr/bin/python3 -I`) · `…/arltuner-selfcheck` · `…/arltuner-access` · `/usr/share/arltuner/web/` (static GUI) · `/etc/systemd/system/arltuner-helper.{socket,service}` · `/usr/lib/tmpfiles.d/arltuner.conf` · `/etc/arltuner/{policy.toml,helper.toml}` · `/etc/arltuner/accepted/` (user data, never touched by install) · `/var/lib/arltuner/{audit.jsonl 0600, state/, curves/, profiles/, stress/}` · runtime `/run/arltuner/{helper.sock, mailbox.lock, measure-journal.json}` | `install.sh:10-27`, units in `packaging/` |
| What runs as root | **only** `python3 -I /usr/lib/arltuner/arltuner-helper --device --socket` (systemd-activated). Stress children drop to the invoking uid. The GUI server and CLI run as the user (the CLI's `apply --device` is run via `sudo` by the operator/orchestrator — separate path, D-022/D-031) | `arltuner-helper.service`, `helper/main.py:1-20` |
| Unit hardening | `NoNewPrivileges, RestrictSUIDSGID, LockPersonality`; deliberately **no** `PrivateDevices/ProtectKernelTunables/ProtectSystem/ProtectHome/PrivateTmp/PrivateNetwork` (needs `/dev/cpu/*/msr`, cpufreq sysfs, `/etc/arltuner`, stress workdirs). `TimeoutStopSec=300` ≥ `_SESSION_JOIN_S` | `packaging/arltuner-helper.service` |
| User-side launcher | `tools/arltuner-gui.py`: starts `WebServer`, writes `$XDG_RUNTIME_DIR/arltuner/launcher.html` (0600 file:// form POST with the token), optional `--open`; second invocation reissues a token (SIGUSR1). Web root = checkout `web/` if present, else `/usr/share/arltuner/web` | `arltuner-gui.py:37-97`, `web/launcher.py`, `web/reissue.py` |
| Python entry points | `pyproject.toml` has `dependencies = []`, `requires-python >= 3.12`; no console_scripts: tools are `tools/arltuner.py` (CLI), `tools/arltuner-gui.py`, `tools/arltuner-helper.py` (dev/sim) | `pyproject.toml`, `tools/` |
| Windows | PowerShell **restore only** (`tools/win/`); never run on Windows from this repo (STATE.md) | AGENTS.md |

**Must stay OUTSIDE any AppImage (mounted read-only, user-owned, a FUSE mount is not a root-trusted path):**
1. `/usr/lib/arltuner/**` code tree and `arltuner-helper` launcher — SPEC §4.4 / D-029: root helper never executes code from a user-writable path or a checkout; `check_code_tree` enforces it (`main.py:4-13`).
2. Both systemd units, tmpfiles, the root-owned socket `/run/arltuner/helper.sock` (0666, root), `/etc/arltuner/{policy.toml,helper.toml,accepted/}`, `/var/lib/arltuner/**` — all trust-checked as root-owned with root-owned parents.
3. Anything that writes MSRs, runs `stress-ng` as the user with the helper's `/usr/bin/…` trust check, or reads `/dev/cpu/*/msr`.
**What an AppImage may contain:** an unprivileged client only (UI + optionally the Python `web/` gateway as a sidecar). It needs a way to tell the user to run `sudo bash install.sh --add-user <name>` when `helper_service`/`HelperConnectError` says so (messages already exist, `helperlink.py:305-362`). Per-user data today: only `localStorage` prefs (refresh interval, chart window; `app.js:60-66`) — nothing in `~/.config`.

---

## 8. Hard constraints a new GUI must preserve (or consciously supersede)

### 8.1 Preserve (non-negotiable)
| Constraint | Source |
|---|---|
| Only the **human and the orchestrator** perform hardware writes, with an explicit chat go-ahead per sitting (standing autonomy D-034 for the orchestrator); worker/worktree agents work against the simulator/fake sysfs only | AGENTS.md, D-002, D-034, SAFETY.md |
| **One mailbox transaction at a time; never a sweep/loop/"enumerate"**; no automatic retry of a failed write; health bracket around every transaction; cold-power recovery plan | SAFETY.md:55-66, D-032 "kept" list |
| Every request, reads included, is a WRMSR → goes through the guard; default-deny allowlist; read-back of every write; guard refusals are shown, **never worked around** | `guard.py`, AGENTS.md, SPEC §7 |
| Plan-bound confirmation: `plan_id` = sha256 of canonical plan + fresh 6-char nonce, `typed == "CONFIRM <nonce>"`, plan < 600 s, single use; D-033(d): returned to the client, **one Apply click, no typing** | SPEC §4.3, D-033 (d), `ops_tune.py:129,2313-2319` |
| Root code/policy/accepted/audit only from root-owned, non-g/o-writable paths with root-owned parents; helper code never run from `~/.local`, a checkout, or an AppImage | SPEC §4.4, D-029, D-031 |
| Helper access = `SO_PEERCRED` allowlist in `/etc/arltuner/helper.toml`; one client; `127.x.y.z` cookie isolation if the HTTP gateway is kept; session scope full/read | D-043, D-042 |
| **Limits are the user's policy** (`/etc/arltuner/policy.toml`), shown with their source; the GUI never edits or raises policy; code keeps only CPU limits (read live) + field encodings; unknown limit ⇒ no raise | D-039, D-032, SPEC §7, AGENTS.md ("Never widen the operator's policy") |
| **Live data only** — no reference-machine lore at runtime (topology, fused words, favored cores, limits all read live; reference numbers only in tests/examples) | D-036 |
| Profiles are complete & independent; accepted state = ONE profile restored in one shot (only differing words, rounds, residual rule); ring is part of the accepted state, ring OC word never written | D-044, D-045 (G-147/G-148/G-154) |
| Adaptive voltage mode only, no static/override | D-016 |
| Stress is a *quick screen*, **never "pass"**; permanent banner | D-019, SPEC §9 |
| Revert = cold boot, shown next to every Apply | SPEC §7 |
| No network/CDN at runtime; nothing downloaded/installed without naming it and a yes; OCCT never elevated | AGENTS.md |
| Audit log append-only, never truncated; `AUDIT_UNAVAILABLE` blocks everything | SAFETY.md:67 |
| Tests: `python3 tools/run_tests.py` green before merge (3017 tests; browser tests opt-in `ARLTUNER_BROWSER_TESTS=1`, Playwright + Chrome, fake helper `tests/web/fake_helper.py`, `fake_v3.py`) | AGENTS.md, STATE.md |
| Python ≥ 3.12 stdlib only for everything **root or backend** | D-012, D-024 |
| All strings via `textContent`/no HTML injection, strict CSP — current design; keep equivalent XSS discipline in any new front-end | `web/app.js:4-6`, `server.py:45-46` |

### 8.2 Superseded by a Tauri/AppImage front-end — needs a NEW `DECISIONS.md` entry and operator yes
| Item | Where it is stated | Why a Tauri front-end changes it |
|---|---|---|
| **D-012** "App language: Python ≥ 3.12, standard library only" | DECISIONS.md:33 | Tauri = Rust core + webview toolchain. D-012's rationale ("keeps privileged code auditable") still holds **only if** all privileged code stays Python/stdlib; the front-end is unprivileged. Decide explicitly |
| **O-2** "Python (stdlib) + local web UI in the browser" | SPEC §1 | a native window replaces the browser |
| **"No CDN/npm; plain HTML/CSS/JS shipped in the repo"** | AGENTS.md "Working conventions" | Tauri normally needs npm/cargo; either keep `web/` as-is (no bundler) or amend the convention |
| **SPEC §4.2** loopback HTTP + bootstrap-token file:// launcher + cookie + exact Origin | SPEC §4.2, D-027/D-028/D-042 | a desktop client would use the socket or a Tauri-owned channel instead |
| **SPEC §4.1/§11** "GUI unprivileged; browser; desktop entry optional" | SPEC §11 | AppImage replaces `tools/arltuner-gui.py` start-up; `install.sh` still must run for the helper |
| **CSP / `connect-src 'self'`** | `server.py:45-46` | webview origin ≠ server origin unless the page is served by the same server |
Not a "must-change": the helper protocol (JSON lines over the socket) is a clean seam — a native client can speak it as-is.

---

## 9. Gaps

### 9.1 Backend capabilities with no (or partial) GUI
| Capability | Backend | GUI today |
|---|---|---|
| **Ring (cache) point offsets 1..9** | read (`tune.read` → `ring`), compared (`accepted_match` ring entry), restored, `RING_NOT_RESTORED` warning; CLI `point-offset --p1 2 --p2 N` | **No editor row** (`UNIT_IDS` excludes it; `RING_UNIT` appears only in plan diff / read-back, `app.js:3937,3963`). `buildRows` has no ring row. Cannot be edited except via a hand-made profile or the CLI |
| Ring OC word / ratio | read-only | not shown as a control |
| Turbo bucket **core counts** `0x1AE/0x651` | read | read-only note (by policy; `PLAIN_MSR_WRITABLE`) |
| `read`/`read-vf`/`point-offset`/`vf-dump`/`plan`/`apply`/`compare-accepted`/`accepted-migrate`/`topology` | `cli.py:358-470` | CLI-only. `accepted-migrate` has no GUI (profiles page just badges "partial (legacy)") |
| `Reset offsets to 0` (`{reset:"stock"}`) | yes | button exists; **does not reset OC ratio, voltage, turbo** (tooltip says so) — not a "reset to stock" |
| `vf.dump` with `cpus`/`all_e` subset | yes | page only calls `tune.read {}` (all units); partial reads can't be requested; partial read leaves ring alone |
| `profiles.get` for older chain members, `profiles.fingerprint` (Windows) | yes | fingerprint button exists; chain members visible only as legacy |
| `cancel` of `tune.apply` | helper supports (between transactions) | **no Cancel button** while applying (§5.4 i) |
| `/api/logout`, `scope=read` bootstrap | yes | page never logs out; read scope only for screenshots |
| Curves export | JSON download exists; **no import** | |
| Autostart (G6, D-021 unsigned), Settings tab | not built (SPEC §10, §6.7) | none |
| PL1/PL2/PL4/tau, IccMax, TVB, AVX offset, BCLK, uncore, memory timings | **no backend write path; PL limits not even read** (§3) | none — dashboard shows only power draw and raw `0x6B0` limit-reason bits |
| Autostart/crash-loop guard, scheduling restore at login | none (`tools/lnx/reapply_current.sh` exists as a script, not wired) | none |
| Live `telemetry` for GPU/NVIDIA | listed in SPEC §8 (optional) | not implemented in the sampler |

### 9.2 GUI pieces that are hard to use
| Issue | Evidence |
|---|---|
| Opening the app = a `file://` launcher page + one-time token click; second tab/reload without the pidfile signal needs a fresh launcher; session dies at 30 min idle / 12 h and shows "Open a fresh launcher link" | `launcher.py`, `app.js:201-204`, `server.py:218-219` |
| **All-Core vs Per-Core** scope: rows merge to `P`/`E` only if *every* unit's row is identical, otherwise silently falls back to `unit` scope | `app.js:2958-2967` |
| 5-step dependency chain before first edit: install → Measure a stock baseline (Curves) for predictions → Read (130+ WRMSR) → edit → Review → Apply; "NO_READ" handling is a banner + focus jump | `app.js:3914-3918`, SPEC-v3 §3-4 |
| Three overlapping "state" indicators (header mode tag, applied chip, preflight pill, ops ring) and a separate heartbeat/SSE gate that can leave Apply grey with only a text reason | `app.js:1375-1423,4282-4298` |
| Tune console is a wide row of 4 dropdowns per unit + collapsible per-point panels + preview + VccIA card + turbo card on one scrolling page; 12 rows in Per-Core | `index.html:245-331`, `app.js:3008-3082` |
| Every value is a `<select>` grid (5 mV / 5 mV / 1×): no typed entry, no keyboard stepping, no "apply to selected units" multi-select besides the 2 scopes | `app.js:2970-2999` |
| Apply dialog: modal, no cancel while running, closes only on done; read-back table is long; only a progress bar for hundreds of ops (no per-unit status or ETA) | `app.js:4282-4332,4481` |
| Dashboard `n/a` cells and "applied: unknown" until the first read of the boot; `tune.last` can answer BUSY on load and the page retries silently | `app.js:121-132,1604-1628` |
| One helper client: opening the page in a second tab is fine (same server), but running the CLI via the helper, a second GUI, or a desktop app at the same time → `CLIENT_BUSY` | `service.py:389-392` |
| `localStorage` is the only preference store (no config file); prefs vanish with a new random `127.x.y.z` origin each run (a different origin ⇒ different `localStorage`) | `app.js:60-66`, `server.py:316-327` |

---

## 10. Summary (10 lines)
1. One root helper (`/run/arltuner/helper.sock`, JSON lines, 64 KiB cap, one client, `SO_PEERCRED` + `helper.toml [access]`) holds ALL hardware access; `web/server.py` is a gateway with 5 routes and a 24-op allowlist.
2. A desktop front-end should either spawn the unchanged Python gateway as a sidecar, or speak the socket directly (dropping the token/cookie/Origin layer, and excluding the web GUI/CLI because of the single-client rule).
3. Writable today: whole-curve + per-point offsets, OC ratio (+`0x1D`), OC-point target V, turbo tables P/E, ring points 1..9 (profile/restore/CLI only), all guard- and policy-bounded; read-only: CPU limits, ring OC word, memory, telemetry.
4. **No write path at all:** PL1/PL2/PL4/tau (not even read; set by hand via powercap `[MEASURED lnx 2026-10-15]`), IccMax, TVB, AVX offset, BCLK, uncore, cpufreq caps (side-effects only).
5. Telemetry is pushed over SSE (1 s default, 0.2–60 s) from one helper sampler: per-core MHz/busy/VID/ratio, temps, Vcore, package/core W, DRAM GB/s, raw limit reasons.
6. Apply = `tune.plan` (sync) → review dialog → `tune.apply` (202+SSE) → `tune.last` + `audit.tail`: only 4 POSTs and one click, preceded once per boot by a ~130-WRMSR `tune.read`.
7. The slowness is helper-side: dozens-to-hundreds of strictly serial WRMSRs, each with lock file, two fsync'd audit writes, two health snapshots, a whole-audit-log parse and a read-back (SAFETY: E-cluster raise = 53 ops; a real restore = 353 ops) — real latency `[UNVERIFIED]`, measure from audit `ts`.
8. UI clunk: heartbeat-gated Apply, non-cancellable modal, dropdown-only edits, hidden dependency chain (baseline curve → read → edit), scope-merge quirks, ring has no editor, `file://` token launcher.
9. Must stay outside an AppImage: `/usr/lib/arltuner` root tree, units, socket, `/etc/arltuner`, `/var/lib/arltuner`; the AppImage is an unprivileged client only (D-043, SPEC §4.4, D-029).
10. Tauri supersedes D-012 / O-2 / "no CDN/npm" (needs a new D-entry + operator yes); everything else (one-at-a-time, guard, nonce, policy-as-limits D-039, live data D-036, D-043/44/45) must be preserved.
