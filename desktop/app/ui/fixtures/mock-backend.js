// fixtures/mock-backend.js: the `mock` transport of bridge.js. Replays the JSON fixtures recorded from the simulator helper
// (gen_fixtures.py; see README.md). Same transport interface as the Tauri backend: connect, status, call, listen (+ close).
// Short ops answer with the recorded response; long ops (tune.read, tune.apply) answer {id}, replay the recorded state/progress events on a timer
// and finish with a `result` event, exactly as PLAN §3.1 describes the real link.
// Loaded lazily by bridge.js only for ?bridge=mock, so the AppImage never parses it. The data comes from fixtures.js (the *.json files bundled as one ES
// module by gen_fixtures.py): Chrome checks JSON module imports and fetch() against connect-src, which the strict CSP restricts to ipc:, whereas a JS
// module is governed by script-src 'self'. So the mock runs under the exact CSP of tauri.conf.json.

import F from "./fixtures.js";

export const FIXTURES = {
  preflight: F["preflight"], telemetryStart: F["telemetry-start"], telemetryStop: F["telemetry-stop"], telemetry: F["telemetry-events"], tuneRead: F["tune-read"],
  tuneReadEvents: F["tune-read-events"], tunePlan: F["tune-plan"], tuneApply: F["tune-apply"], tuneApplyEvents: F["tune-apply-events"], profilesList: F["profiles-list"],
  auditTail: F["audit-tail"], tuneLast: F["tune-last"],
  // G-213: recorded from the simulator by gen_profiles_fixtures.py (saved profiles, their contents, the refusals); the accepted state is INVENTED below
  // (one entry per recorded file: shell.test.mjs checks the count)
  profilesSaveP0: F["profiles-save-sim-p0-undervolt"], profilesSaveELight: F["profiles-save-sim-e-light"], profilesSaveExists: F["profiles-save-exists"],
  profilesGetP0: F["profiles-get-sim-p0-undervolt"], profilesGetELight: F["profiles-get-sim-e-light"], profilesSavedList: F["profiles-saved-list"],
  profilesAcceptUnavailable: F["profiles-accept-unavailable"], profilesFingerprintUnavailable: F["profiles-fingerprint-unavailable"],
};
const {
  preflight, telemetryStart, telemetryStop, telemetry, tuneRead, tuneReadEvents, tunePlan, tuneApply, tuneApplyEvents, profilesList, auditTail, tuneLast,
  profilesGetP0, profilesGetELight, profilesSavedList, profilesAcceptUnavailable, profilesFingerprintUnavailable,
} = FIXTURES;
const profilesGet = [profilesGetP0, profilesGetELight];

const clone = (x) => JSON.parse(JSON.stringify(x));
const ok = (id, result) => ({ id, ok: true, result });
const fail = (id, code, message, detail) => ({ id, ok: false, error: detail ? { code, message, detail } : { code, message } });

const LINK_FAILURES = {
  busy: { state: "busy", kind: "busy", message: "another client is connected to the helper (another arltuner GUI?)" },
  not_installed: { state: "not_installed", kind: "missing", message: "the helper is not installed or not started: /run/arltuner/helper.sock does not exist" },
  not_allowed: { state: "not_allowed", kind: "not_allowed", message: "the helper refused this user: not in the allowlist (/etc/arltuner/helper.toml)" },
  error: { state: "error", kind: "closed", message: "the helper closed the connection right away" },
};

// INVENTED, not from any hardware and not from the simulator: with `motion` on, the telemetry leaves that the unprivileged simulator reports as
// "unavailable" get a deterministic waveform so sparklines and tiles can be seen moving. DRAM and core power stay unavailable on purpose (n/a path).
function invent(values, tick) {
  const v = clone(values);
  const put = (leaf, value) => {
    leaf.value = Math.round(value * 1000) / 1000;
    leaf.status = "ok";
    delete leaf.reason;
  };
  for (const [cpu, core] of Object.entries(v.cores || {})) {
    const n = Number(cpu);
    const busy = 50 + 45 * Math.sin(tick * 0.7 + n * 0.9);
    const ratio = Math.round((n < 8 ? 55 : 46) - 3 * (1 - busy / 100));
    put(core.busy, busy);
    put(core.ratio, ratio);
    put(core.mhz, ratio * 100);
    put(core.vid, 0.95 + 0.0035 * busy);
  }
  if (v.temps && v.temps.Package) put(v.temps.Package, 48 + 10 * Math.sin(tick * 0.5));
  if (v.power && v.power.package) put(v.power.package, 85 + 30 * Math.sin(tick * 0.6));
  if (v.vcore) put(v.vcore, 1.15 + 0.05 * Math.sin(tick * 0.4));
  if (v.limit_reasons_raw) put(v.limit_reasons_raw, tick % 8 === 5 ? 0x10 : 0);
  return v;
}

// INVENTED, not from any hardware and not from the simulator (the simulator's curve store is empty): a stock-baseline curve for the Tune predictions (G-208).
// Per unit a straight ramp that ends at the fused top voltage of the recorded read (0.0105 V per ratio step), `status: "ok"` on every read point. It is what
// `curves.list` / `curves.get` answer in the mock; `?bridge=mock&baseline=none` answers an empty library (the "no baseline curve" path).
function inventedBaseline() {
  const units = {};
  for (const u of tuneRead.result.units) {
    const cpu = u.cpus[0];
    const top = tuneRead.result.fused[String(cpu.cpu)];
    const ratios = cpu.points.map((p) => p.ratio);
    const topRatio = top ? top.ratio : Math.max(...ratios);
    const topV = top ? top.v : 1.1;
    units[u.id] = { points: ratios.map((r) => ({ ratio: r, status: "ok", vid_v: Math.round((topV - (topRatio - r) * 0.0105) * 10000) / 10000 })) };
  }
  return { id: "mock-baseline", name: "Mock stock baseline (invented)", baseline: true, created: "2026-01-01T00:00:00Z", units };
}
const noBaselineParam = () => new URLSearchParams(globalThis.location?.search ?? "").get("baseline") === "none";

// INVENTED, not from any hardware and not from the simulator (G-210): the mock plans from the intent so the apply drawer has something to show. It is the recorded plan
// (warnings, ops left, expiry) with `steps` / `before` / `after` / `units` derived from the intent rows and the recorded read, in the helper's step order (the conditional
// per-core enable first, then one write per point). Knobs (query string, like `baseline=none`): `apply=mismatch` (the last read-back word differs), `apply=stale` (the first
// tune.apply answers PLAN_STALE and the plan is forgotten), `step=<ms>` (gap between replayed progress events; a slow apply for the cancel test).
const searchParam = (name) => new URLSearchParams(globalThis.location?.search ?? "").get(name);
const stepParam = () => {
  const n = Number(searchParam("step"));
  return Number.isFinite(n) && n >= 1 ? n : null;
};
// G-240: `?uvp=on` makes the mock BIOS have Undervolt Protection on: the first tune.apply with a negative offset is refused (REFUSED, check UNDERVOLT_PROTECTION, the helper's message), the mock
// then reports uvp "on" in tune.read / tune.last / tune.plan, and a later negative-offset plan is refused at the plan like the helper's. `?uvp=off` reports "off" from the start; default "unknown".
export const UVP_MESSAGE = "The CPU refused a negative voltage offset: Undervolt Protection is enabled in the BIOS (Intel's default). Disable 'Undervolt Protection' in the BIOS and reboot to undervolt. Only negative offsets are refused; OC ratios and turbo ratios are not affected.";
const negativeOffsets = (intent) => {
  const out = [];
  for (const r of Array.isArray(intent && intent.rows) ? intent.rows : []) {
    const o = r && r.offset;
    if (!o) continue;
    if (o.kind === "curve" && o.mv < 0) out.push([r.target, o.mv]);
    if (o.kind === "points") for (const mv of Object.values(o.mv || {})) if (mv < 0) out.push([r.target, mv]);
  }
  return out;
};
const MOCK_FLOOR_MV = -150; // a mock CPU limit tighter than the editor's own clamp (the CPU's offset field, -1000..+999), so a refusal is reachable from the UI
const MOCK_CEIL_MV = 150;
const rawOf = (mv) => Math.round(mv * 1.024);
const mvOfRaw = (raw) => Math.round((raw / 1.024) * 1000) / 1000;
const wordOf = (mv, ratio) => "0x" + ((((rawOf(mv) & 0x7ff) << 21) | ratio) >>> 0).toString(16).padStart(8, "0");
const readUnit = (id) => tuneRead.result.units.find((u) => u.id === id) || null;
const signed = (n) => (n >= 0 ? "+" : "-") + Math.abs(n);
const outside = (mv) => mv < MOCK_FLOOR_MV || mv > MOCK_CEIL_MV;
const WINDOW = `${MOCK_FLOOR_MV}.0..+${MOCK_CEIL_MV}.0 mV`;

/** A CPU-limit refusal message (D-049: the CPU's own limits are the only bound) for the first offset of the intent outside the mock CPU limit, else null. */
function mockRefusal(intent) {
  for (const row of intent.rows || []) {
    const o = row.offset;
    if (!o) continue;
    for (const mv of o.kind === "curve" ? [o.mv] : Object.values(o.mv || {})) {
      if (outside(mv)) {
        return `${row.target}: offset ${signed(mv)} mV (raw ${rawOf(mv)}) is outside the CPU limit ${WINDOW} (mock CPU limit; the field encoding allows -1000..999 mV). Nothing was sent.`;
      }
    }
  }
  for (const [no, mv] of Object.entries((intent.ring && intent.ring.points) || {})) {
    if (outside(mv)) return `ring point ${no}: offset ${signed(mv)} mV (raw ${rawOf(mv)}) is outside the CPU limit ${WINDOW} (mock CPU limit). Nothing was sent.`;
  }
  return null;
}

const isNum = (x) => typeof x === "number" && Number.isFinite(x);
const TABLE_MSR = { P: "0x1AD", E: "0x650" };

/** D-050, as the helper plans it (ops_tune._oc_below_table_warnings): an OC ratio above the top bucket of its kind's table (the intent's own table, else the one read) is planned
 *  as asked, no table is derived, and a TURBO_BELOW_OC warning says so. */
function turboBelowOc(intent) {
  const out = [];
  for (const row of intent.rows || []) {
    if (!isNum(row.ratio)) continue;
    const kind = row.target.startsWith("E") ? "E" : "P";
    const table = (intent.turbo && intent.turbo[kind]) || tuneRead.result.tables[kind.toLowerCase()];
    if (table && row.ratio > Math.max(...table)) {
      out.push({ code: "TURBO_BELOW_OC", text: `${row.target} OC ratio ${row.ratio}x is above the ${kind} turbo table (${table.map((b) => b + "x").join(", ")}): the core runs at most the table's bucket for the active-core count until you raise it on the Turbo ratios tab (MSR ${TABLE_MSR[kind]})` });
    }
  }
  return out;
}

/** intent -> {steps, before, after, units, writes}; `writes` 0 = nothing to synthesize from (the caller answers the recorded plan). */
function synthPlan(intent) {
  const steps = [{ conditional: true, kind: "write_percore_enable", n: 1 }];
  const before = {};
  const after = {};
  const units = [];
  const add = (unit, point, ratio, fromMv, toMv, cpu) => {
    (before[unit] ||= {})[point] = { clamped_by: null, effective_v: null, offset_mv: fromMv, ratio, v_target_v: null };
    (after[unit] ||= {})[point] = { clamped_by: null, effective_v: null, offset_mv: mvOfRaw(rawOf(toMv)), ratio, v_target_v: null };
    if (!units.includes(unit)) units.push(unit);
    steps.push({ cpu, kind: "write_vf_point", n: steps.length + 1, offset_mv: toMv, point, unit });
  };
  for (const row of intent.rows || []) {
    const u = readUnit(row.target);
    if (!u) continue;
    const cpu = u.cpus[0];
    if (isNum(row.ratio)) {
      // the OC word (point 0): the ratio as asked. D-050: it never adds a turbo-table write; only an explicit `turbo` in the intent does
      const was = cpu.oc || {};
      (before[row.target] ||= {})["0"] = { clamped_by: null, effective_v: null, offset_mv: was.offset_mv ?? 0, ratio: was.ratio ?? null, v_target_v: was.v_target_v ?? null };
      (after[row.target] ||= {})["0"] = { clamped_by: null, effective_v: null, offset_mv: was.offset_mv ?? 0, ratio: row.ratio, v_target_v: row.v_target_v ?? was.v_target_v ?? null };
      if (!units.includes(row.target)) units.push(row.target);
      steps.push({ cpu: cpu.cpu, kind: "write_vf", n: steps.length + 1, point: 0, unit: row.target });
    }
    if (!row.offset) continue;
    if (row.offset.kind === "curve") {
      for (const p of cpu.points) add(row.target, p.point, p.ratio, p.offset_mv, row.offset.mv, cpu.cpu);
    } else {
      for (const [ratio, mv] of Object.entries(row.offset.mv || {})) {
        const p = cpu.points.find((q) => q.ratio === Number(ratio));
        add(row.target, p ? p.point : 1, Number(ratio), p ? p.offset_mv : 0, mv, cpu.cpu);
      }
    }
  }
  for (const [no, mv] of Object.entries((intent.ring && intent.ring.points) || {})) add("ring", Number(no), null, 0, mv, 0);
  for (const kind of Object.keys(intent.turbo || {})) {
    steps.push({ kind: "write_msr", n: steps.length + 1, table: kind, unit: "turbo " + kind });
    units.push("turbo " + kind);
  }
  return { steps, before, after, units, writes: steps.length - 1 };
}

// ---- profiles (G-213) ---------------------------------------------------------------------------------------------------------------------------------
// RECORDED from the simulator (gen_profiles_fixtures.py): the two saved profiles (profiles.get contents), the profiles.list answer, the EXISTS refusal, and the NO_ACCEPTED_DIR refusals of
// profiles.accept / profiles.fingerprint. INVENTED here (the unprivileged simulator keeps no accepted state): a working profiles.accept / profiles.fingerprint, the accepted entry of
// profiles.list / profiles.get, a profiles.save of any new name, "the words the CPU holds" (the recorded read, plus every plan applied in this mock session) and the plans of
// {profile} / {restore: "accepted"} intents (one write per point that differs from what is held, in the order of synthPlan). Knobs (query string): `profiles=none` (empty library),
// `profiles=accepted` (an accepted state equal to the recorded read exists from the start), `accept=unavailable` (accept / fingerprint answer the recorded refusal),
// `fingerprint=incomplete` (the fingerprint note says what the simulator would: no CPU identity).
const PROFILE_NAME_RE = /^[a-z0-9._-]{1,64}$/;
const readUnits = () => tuneRead.result.units.filter((u) => u.cpus && u.cpus[0] && Array.isArray(u.cpus[0].points));
const pointsOfUnit = (id) => (readUnit(id) ? readUnit(id).cpus[0].points : []);
const copyHeld = (held) => ({ units: Object.fromEntries(Object.entries(held.units).map(([k, v]) => [k, { ...v }])), turbo: { P: held.turbo.P.slice(), E: held.turbo.E.slice() } });

/** What the mock CPU holds: unit -> {point number: mV}, and the turbo tables. Starts as the recorded read. */
function initialHeld() {
  const units = {};
  for (const u of readUnits()) units[u.id] = Object.fromEntries(u.cpus[0].points.map((p) => [p.point, p.offset_mv]));
  return { units, turbo: { P: tuneRead.result.tables.p.slice(), E: tuneRead.result.tables.e.slice() } };
}

/** A complete profile's contents (profiles.get shape) from held words: a unit whose points are all equal is a whole-curve row. */
function contentsOfHeld(held) {
  const rows = readUnits().map((u) => {
    const mvs = pointsOfUnit(u.id).map((p) => held.units[u.id][p.point]);
    const offset = mvs.every((x) => x === mvs[0]) ? { kind: "curve", mv: mvs[0] } : { kind: "points", mv: Object.fromEntries(pointsOfUnit(u.id).map((p) => [String(p.ratio), held.units[u.id][p.point]])) };
    return { target: u.id, offset, ratio: null, v_target_v: null };
  });
  return { rows, turbo: { E: held.turbo.E.slice(), P: held.turbo.P.slice() }, complete: true };
}

const DIFF_UNITS = ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "EC0", "EC1", "EC2", "EC3", "ring", "P turbo", "E turbo"];

/** G-228: the settings a profile's contents hold, as profiles.diff counts them: [{unit, order, label, kind, value}] (a whole-curve row stands for every point of its unit). */
function diffFacts(contents) {
  const out = [];
  for (const row of contents.rows) {
    const o = row.offset;
    for (const p of pointsOfUnit(row.target)) {
      const value = !o ? null : o.kind === "curve" ? o.mv : String(p.ratio) in o.mv ? o.mv[String(p.ratio)] : null;
      if (value !== null) out.push({ unit: row.target, order: 10 + p.point, label: `Offset at ${p.ratio}x`, kind: "mv", value });
    }
    out.push({ unit: row.target, order: 1, label: "OC ratio", kind: "x", value: row.ratio });
    out.push({ unit: row.target, order: 2, label: "OC target voltage", kind: "v", value: row.v_target_v });
  }
  for (const k of ["P", "E"]) if (contents.turbo && Array.isArray(contents.turbo[k])) contents.turbo[k].forEach((value, i) => out.push({ unit: k + " turbo", order: i, label: "Bucket " + i, kind: "x", value }));
  return out;
}

function summarize(contents) {
  let deepest = null;
  let points = 0;
  for (const r of contents.rows) {
    if (!r.offset) continue;
    const values = r.offset.kind === "curve" ? [r.offset.mv] : Object.values(r.offset.mv);
    if (r.offset.kind === "points") points += values.length;
    deepest = deepest === null ? Math.min(...values) : Math.min(deepest, ...values);
  }
  return {
    units: contents.rows.length, points, deepest_offset_mv: deepest, oc_ratios: contents.rows.filter((r) => r.ratio !== null).length,
    v_targets: contents.rows.filter((r) => r.v_target_v !== null).length, turbo: !!contents.turbo, complete: contents.complete === true,
  };
}

/** The offset a profile row asks for at a point, or null when the row says nothing about it. */
function wantedMv(row, p) {
  if (!row.offset) return null;
  if (row.offset.kind === "curve") return row.offset.mv;
  return String(p.ratio) in row.offset.mv ? row.offset.mv[String(p.ratio)] : null;
}

/** Held words with the rows (and turbo) of an intent laid over them. */
function heldAfter(held, intent) {
  const next = copyHeld(held);
  for (const row of (intent && intent.rows) || []) {
    if (!next.units[row.target] || !row.offset) continue;
    for (const p of pointsOfUnit(row.target)) {
      const want = wantedMv(row, p);
      if (want !== null) next.units[row.target][p.point] = want;
    }
  }
  for (const k of Object.keys((intent && intent.turbo) || {})) next.turbo[k] = intent.turbo[k].slice();
  return next;
}

/** The tune.plan intent that takes held words to a profile's contents: only what differs (the helper writes only what differs from the CPU). */
function intentToReach(contents, held) {
  const rows = [];
  for (const row of contents.rows) {
    if (!held.units[row.target]) continue;
    const mv = {};
    for (const p of pointsOfUnit(row.target)) {
      const want = wantedMv(row, p);
      if (want !== null && want !== held.units[row.target][p.point]) mv[String(p.ratio)] = want;
    }
    if (Object.keys(mv).length) rows.push({ target: row.target, offset: { kind: "points", mv }, v_target_v: null, ratio: null });
  }
  const intent = {};
  if (rows.length) Object.assign(intent, { scope: "unit", rows });
  const turbo = {};
  for (const k of ["P", "E"]) if (contents.turbo && Array.isArray(contents.turbo[k]) && contents.turbo[k].some((x, i) => x !== held.turbo[k][i])) turbo[k] = contents.turbo[k].slice();
  if (Object.keys(turbo).length) intent.turbo = turbo;
  return intent;
}

/** "" when the contents equal the held words, else the first difference. */
function heldDifference(contents, held) {
  const intent = intentToReach(contents, held);
  if (intent.rows && intent.rows.length) {
    const [ratio, mv] = Object.entries(intent.rows[0].offset.mv)[0];
    return `${intent.rows[0].target} at ${ratio}x the profile has ${mv} mV`;
  }
  return intent.turbo ? "the " + Object.keys(intent.turbo)[0] + " turbo table differs" : "";
}

/** G-227: a tune.last result (cloned) with the held words laid over its V/F points (raw and decoded copy) and its turbo tables, like the helper's op_tune_last after an apply. */
function overlayHeld(result, held) {
  const put = (p, mv) => Object.assign(p, { offset_mv: mv, ...(typeof p.offset_raw === "number" ? { offset_raw: rawOf(mv) } : {}), ...(typeof p.word === "string" && typeof p.ratio === "number" ? { word: wordOf(mv, p.ratio) } : {}) });
  for (const u of result.units || []) {
    const mvs = held.units[u.id];
    if (!mvs) continue;
    for (const cpu of u.cpus) for (const p of cpu.points) if (p.point in mvs) put(p, mvs[p.point]);
  }
  for (const [id, t] of Object.entries(result.tune || {})) {
    const mvs = held.units[id];
    if (!mvs) continue;
    for (const cpu of t.cpus) for (const p of cpu.points) if (p.point > 0 && p.point in mvs) p.offset_mv = mvs[p.point];
  }
  if (result.tables) Object.assign(result.tables, { p: held.turbo.P.slice(), e: held.turbo.E.slice() });
  if (result.turbo && result.turbo.P && result.turbo.E) {
    result.turbo.P.ratios = held.turbo.P.slice();
    result.turbo.E.ratios = held.turbo.E.slice();
  }
}

const isoSeconds = (ms) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");

/** options: intervalMs (telemetry period, default 1000), stepMs (gap between replayed progress events, default 12; `?step=<ms>`), motion (see invent), helper ('ok' | key of LINK_FAILURES), now,
 *  baseline (curves.* answer the invented baseline; default on, `?baseline=none` turns it off), applyMode ('mismatch' | 'stale' | null; `?apply=`; see synthPlan above). */
function createBaseMockTransport({ intervalMs = 1000, stepMs = stepParam() ?? 12, motion = false, helper = "ok", now = () => Date.now(), baseline = !noBaselineParam(), applyMode = searchParam("apply"), profiles = searchParam("profiles"), accept = searchParam("accept"), fingerprint = searchParam("fingerprint"), read = searchParam("read"), diff = searchParam("diff"), uvp = searchParam("uvp") } = {}) {
  const listeners = new Map(); // event -> Set(cb)
  const timers = new Set();
  let link = { state: "connecting", kind: null, message: "" };
  let seq = 0;
  let telemetryTimer = null;
  let tick = 0;
  let busyOp = null;
  let running = null; // the replay of the long op in flight ({id, cancelled, done, sent})
  // G-213 profiles state (see the block above createMockTransport)
  const profilesMode = profiles;
  const acceptUnavailable = accept === "unavailable";
  const readStale = read === "stale"; // `?read=stale`: profiles.save {from: "read"} / profiles.accept refuse STALE_READ until a tune.read is answered (G-228)
  let readFresh = !readStale;
  let unreadable = accept === "unreadable"; // `?accept=unreadable`: the accepted state cannot be read: profiles.accept refuses ACCEPTED_UNREADABLE until set_aside_unreadable is given (G-228)
  const diffExtras = diff === "extras"; // `?diff=extras`: the state of the last read differs from the accepted one ONLY in a unit's OC-word offset and a ring point (profiles.diff rows, no point change)
  let held = initialHeld(); // the words the mock CPU holds
  const uvpBios = uvp === "on"; // the mock BIOS has Undervolt Protection on (G-240)
  let uvpSeen = uvp === "off" ? "off" : "unknown"; // what the mock helper reports for this boot
  let appliedHere = false; // a tune.apply finished in this mock session: tune.last then answers the held words, as the helper's does after an apply (G-227)
  const saved = new Map(); // name -> {created, contents}
  if (profilesMode !== "none") for (const g of profilesGet) saved.set(g.result.name, { created: g.result.created, contents: clone(g.result.contents) });
  let accepted = profilesMode === "accepted" && !unreadable ? { name: "read-20261018-120000", contents: contentsOfHeld(held) } : null;
  const planIntents = new Map(); // plan_id -> the intent it was made from (what an apply puts into `held`)

  const emit = (event, payload) => {
    for (const cb of [...(listeners.get(event) || [])]) cb(payload);
  };
  const later = (fn, ms) => {
    const t = setTimeout(() => {
      timers.delete(t);
      fn();
    }, ms);
    if (t.unref) t.unref();
    timers.add(t);
    return t;
  };
  const connected = () => link.state === "connected";

  function startTelemetry(id) {
    if (telemetryTimer) clearInterval(telemetryTimer);
    const frames = telemetry.events.filter((e) => e.event === "telemetry");
    const beats = telemetry.events.filter((e) => e.event === "heartbeat");
    telemetryTimer = setInterval(() => {
      const i = tick++;
      const src = frames[i % frames.length];
      const values = motion ? invent(src.values, i) : clone(src.values);
      values.ts = now() / 1000;
      emit("telemetry", { op_id: id, t: now() / 1000, values });
      if (i % 2 === 0 && beats.length) emit("heartbeat", { ...clone(beats[(i / 2) % beats.length | 0]), op_id: busyOp });
    }, intervalMs);
    if (telemetryTimer.unref) telemetryTimer.unref();
  }

  /** Replay recorded state/progress events for a long op, then the final `result`. Resolves nothing; the caller already answered {id}. */
  function replayLong(id, events, finalResponse) {
    busyOp = id;
    const run = { id, cancelled: false, done: false, sent: 0 };
    running = run;
    const finish = (response) => {
      if (run.done) return;
      run.done = true;
      busyOp = null;
      running = null;
      emit("state", { op_id: id, state: "idle" });
      emit("result", response);
    };
    // `cancel` takes effect at the next boundary between events, like the helper's between-transactions check (G-210)
    const cancelled = () => finish(fail(id, "CANCELLED", `cancelled between transactions after ${run.sent} progress step(s); the requests already sent are in the audit log`));
    let delay = 0;
    for (const ev of events) {
      const { event, ...payload } = ev;
      delay += stepMs;
      later(() => {
        if (run.done) return;
        if (run.cancelled) return cancelled();
        if (event === "progress") run.sent++;
        emit(event, { ...payload, op_id: id });
      }, delay);
    }
    later(() => (run.cancelled ? cancelled() : finish(finalResponse)), delay + stepMs);
  }

  // ---- plans (G-210): the recorded one plus the ones synthesized from an intent; a plan is single use and `apply=stale` forgets them all once ----
  const plans = new Map(); // plan_id -> plan result
  let planSeq = 0;
  let staleFired = false;
  let recordedGone = false; // the recorded plan was applied or forgotten: the next plan of an empty intent gets a fresh id
  const nonceOf = (n) => "M" + n.toString(8).padStart(5, "0").replace(/\d/g, (d) => String(Number(d) + 2));
  function issuePlan(intent, { whole = false } = {}) {
    const synth = synthPlan(intent);
    const recorded = tunePlan.response.result;
    if (!synth.writes && !recordedGone && !whole) {
      if (!plans.has(recorded.plan_id)) plans.set(recorded.plan_id, clone(recorded)); // the recorded one-point plan
      planIntents.set(recorded.plan_id, tunePlan.request.args.intent);
      return plans.get(recorded.plan_id);
    }
    const n = ++planSeq;
    const plan = clone(recorded);
    plan.plan_id = String(n).padStart(8, "0") + "c0ffee".repeat(9).slice(0, 56);
    plan.nonce = nonceOf(n);
    plan.name = "tune-mock" + n;
    if (synth.writes) {
      const writes = synth.steps.length;
      Object.assign(plan, { steps: synth.steps, before: synth.before, after: synth.after, units: synth.units, writes, reads: 2, ops_needed: writes * 2 + 2, rounds: 1 });
      // INVENTED prediction (the recorded plan has none): P0 sets the maximum
      plan.vccia = { predicted_v: 1.28, label: "prediction, not a measurement", set_by: { unit: "P0", ratio: 57 }, active_cores: "all", margin_v: 0.05, before_v: 1.29 };
      const mvs = synth.steps.map((s) => s.offset_mv).filter((x) => typeof x === "number");
      plan.warnings.unshift(...turboBelowOc(intent));
      if (mvs.some((x) => x > 0)) plan.warnings.unshift({ code: "POSITIVE_OFFSET", text: "a positive offset raises the voltage of the point: more heat, not more stability headroom" });
      if ((intent.rows || []).some((r) => r.target.startsWith("EC") && r.offset && r.offset.kind === "points" && "40" in r.offset.mv)) plan.warnings.unshift({ code: "E40_OFFSET", text: "an offset on the E-cluster 40x point has been reported to destabilise the cluster [COMMUNITY]" });
    } else if (whole) {
      // a profile that equals what the CPU holds: nothing to write
      Object.assign(plan, { steps: [], before: {}, after: {}, units: [], writes: 0, reads: 0, ops_needed: 0, rounds: 0, warnings: [] });
    }
    plans.set(plan.plan_id, plan);
    planIntents.set(plan.plan_id, intent);
    return plan;
  }

  // {profile: name} / {restore: "accepted"}: the plan of the writes that take the held words to the profile's contents
  function planWholeState(intent) {
    let contents;
    if (intent.restore !== undefined) {
      if (!accepted) return { error: { code: "REFUSED", message: "there is no usable accepted state yet (save a profile as accepted on the Profiles tab). Nothing was sent.", detail: { check: "NO_ACCEPTED_STATE" } } };
      contents = accepted.contents;
    } else if (accepted && accepted.name === intent.profile) contents = accepted.contents;
    else if (saved.has(intent.profile)) contents = saved.get(intent.profile).contents;
    else return { error: { code: "BAD_REQUEST", message: `no such profile: ${intent.profile}`, detail: { check: "PROFILE_NOT_FOUND" } } };
    const target = intentToReach(contents, held);
    const refused = mockRefusal(target);
    if (refused) return { error: { code: "REFUSED", message: refused, detail: { check: "POLICY_RANGE" } } };
    return { plan: issuePlan(target, { whole: true }) };
  }

  const acceptedMatch = () => {
    if (!accepted) return null;
    return [{ cpu: 0, match: heldDifference(accepted.contents, held) === "", diffs: [] }];
  };
  const getResult = (name) => {
    if (accepted && accepted.name === name) return { name, source: "accepted", created: null, complete: true, contents: clone(accepted.contents), summary: summarize(accepted.contents) };
    const rec = saved.get(name);
    if (!rec) return null;
    return { name, source: "saved", created: rec.created, complete: rec.contents.complete === true, contents: clone(rec.contents), summary: summarize(rec.contents) };
  };
  /** The final response and progress events of an apply of `plan` (the recorded ones for the recorded plan). */
  function applyOutcome(id, plan) {
    if (plan.plan_id === tunePlan.response.result.plan_id) return { events: tuneApplyEvents, response: ok(id, clone(tuneApply.response.result)) };
    const done = plan.steps.filter((s) => !s.conditional);
    const readback = done.map((s) => {
      const mv = s.offset_mv;
      const ratio = plan.after[s.unit]?.[s.point]?.ratio ?? 0;
      const word = s.kind === "write_msr" ? "0x" + (0x3a3a3a3a).toString(16) : wordOf(mv, ratio ?? 0);
      return { actual: word, cpu: s.cpu ?? 0, expected: word, match: true, point: s.kind === "write_msr" ? "turbo" : s.point, unit: s.table || s.unit };
    });
    if (applyMode === "mismatch" && readback.length) {
      const last = readback[readback.length - 1];
      last.match = false;
      last.actual = "0x" + ((parseInt(last.expected, 16) ^ 0x20) >>> 0).toString(16).padStart(8, "0");
    }
    const result = {
      ...clone(tuneApply.response.result),
      plan_id: plan.plan_id,
      readback,
      steps: done.map((s) => ({ cpu: s.cpu ?? 0, kind: s.kind, n: s.n, point: s.point ?? "turbo", status: "ok", unit: s.unit })),
      ops_used: plan.ops_needed,
      writes_sent: done.length,
    };
    const events = [{ event: "state", state: "applying" }];
    for (let step = 1; step <= plan.ops_needed; step++) events.push({ event: "progress", of: plan.ops_needed, ops_budget: 100000, ops_used: 152 + step, step });
    return { events, response: ok(id, result) };
  }

  return {
    async connect() {
      const fail_ = LINK_FAILURES[helper];
      link = fail_ ? { ...fail_ } : { state: "connected", kind: null, message: "simulated helper (mock fixtures, no hardware)" };
      return link;
    },
    async status() {
      return link;
    },
    async call(op, args = {}) {
      const id = `m${++seq}`;
      if (!connected()) return fail(id, "NOT_CONNECTED", "the mock helper is not connected");
      switch (op) {
        case "preflight":
          return ok(id, clone(preflight.result));
        case "telemetry.start":
          startTelemetry(id);
          return ok(id, { ...clone(telemetryStart.result), op_id: id, interval_s: intervalMs / 1000 });
        case "telemetry.stop":
          if (telemetryTimer) clearInterval(telemetryTimer);
          telemetryTimer = null;
          return ok(id, { ...clone(telemetryStop.result) });
        case "tune.last": {
          const r = clone(tuneLast.result);
          if (r.available) r.result.uvp = uvpSeen;
          if (r.available) r.at = now() / 1000 - 60; // pretend the recorded read happened a minute ago
          if (r.available && appliedHere && searchParam("last") === "gone") return ok(id, { available: false }); // `?last=gone`: the helper kept no record of the apply (the page must still show the applied words)
          if (r.available && appliedHere) overlayHeld(r.result, held);
          if (r.available && accepted) Object.assign(r.result, { accepted_match: acceptedMatch(), accepted_note: null }); // G-213: redone against the (invented) accepted state
          return ok(id, r);
        }
        case "curves.list":
          return ok(id, { curves: baseline ? [{ id: "mock-baseline", name: "Mock stock baseline (invented)", baseline: true, created: "2026-01-01T00:00:00Z" }] : [], no_turbo: false });
        case "curves.get":
          if (!baseline || args.id !== "mock-baseline") return fail(id, "NOT_FOUND", "no such curve in the mock");
          return ok(id, inventedBaseline());
        case "profiles.list": {
          const entries = [...saved].map(([name, rec]) => ({ name, source: "saved", created: rec.created, complete: true }));
          const result = { profiles: [...(accepted ? [{ name: accepted.name, source: "accepted", created: null, complete: true }] : []), ...entries], accepted: !!accepted };
          if (!accepted) result.note = profilesSavedList.result.note;
          return ok(id, result);
        }
        case "profiles.get": {
          if (typeof args.name !== "string" || !PROFILE_NAME_RE.test(args.name)) return fail(id, "BAD_REQUEST", "'name' must be 1..64 characters of a-z 0-9 . _ -");
          const got = getResult(args.name);
          return got ? ok(id, got) : fail(id, "BAD_REQUEST", `no profile named '${args.name}'`, { check: "PROFILE_NOT_FOUND" });
        }
        case "profiles.save": {
          if (typeof args.name !== "string" || !PROFILE_NAME_RE.test(args.name)) return fail(id, "BAD_REQUEST", "'name' must be 1..64 characters of a-z 0-9 . _ -");
          if (args.from !== undefined) { // G-228: the complete state of the last read, no rows, never accepted
            if (args.from !== "read" || args.intent !== undefined) return fail(id, "BAD_REQUEST", "give 'intent' (the editor's rows) or 'from': 'read' (the state of the last read), not both");
            if (saved.has(args.name) || (accepted && accepted.name === args.name)) return fail(id, "REFUSED", `a profile named '${args.name}' already exists (profiles are never overwritten)`, { check: "EXISTS" });
            if (!readFresh) return fail(id, "REFUSED", "the words of the last read may no longer be what the CPU holds: a write came after it. Read again first (Tune > Read), then save. Nothing was saved.", { check: "STALE_READ" });
            const contents = contentsOfHeld(held);
            const created = isoSeconds(now());
            saved.set(args.name, { created, contents });
            const sum = summarize(contents);
            return ok(id, { name: args.name, source: "saved", created, complete: true, units: sum.units, points: sum.points, turbo: true, ring_points: 0, from: "read" });
          }
          const intent = args.intent;
          if (!intent || typeof intent !== "object" || Array.isArray(intent) || Object.keys(intent).some((k) => k !== "scope" && k !== "rows")) return fail(id, "BAD_REQUEST", "only a {scope, rows} intent can be saved as a profile");
          if (saved.has(args.name) || (accepted && accepted.name === args.name)) return fail(id, "REFUSED", `a profile named '${args.name}' already exists (profiles are never overwritten)`, { check: "EXISTS" });
          const refused = mockRefusal(intent);
          if (refused) return fail(id, "REFUSED", refused, { check: "POLICY_RANGE" });
          const contents = contentsOfHeld(heldAfter(held, intent));
          const created = isoSeconds(now());
          saved.set(args.name, { created, contents });
          const sum = summarize(contents);
          return ok(id, { name: args.name, source: "saved", created, complete: true, units: sum.units, points: sum.points, turbo: true, ring_points: 0 });
        }
        case "profiles.diff": {
          const refOk = (v) => typeof v === "string" && PROFILE_NAME_RE.test(v);
          if (!refOk(args.a) || !refOk(args.b) || Object.keys(args).some((k) => k !== "a" && k !== "b")) return fail(id, "BAD_REQUEST", "give 'a' and 'b': 'accepted', 'read' or a saved profile name");
          const side = (ref) => {
            if (ref === "read") {
              return readFresh
                ? { source: "read", name: null, facts: diffFacts(contentsOfHeld(held)) }
                : { error: ["STALE_READ", "the words of the last read may no longer be what the CPU holds: a write came after it. Read again first (Tune > Read)."] };
            }
            if (ref === "accepted" || (accepted && accepted.name === ref)) {
              return accepted ? { source: "accepted", name: accepted.name, facts: diffFacts(accepted.contents) } : { error: ["NO_ACCEPTED_STATE", "No accepted state yet: save a profile as accepted (Profiles)."] };
            }
            const rec = saved.get(ref);
            return rec ? { source: "saved", name: ref, facts: diffFacts(rec.contents) } : { error: ["PROFILE_NOT_FOUND", `no saved profile named '${ref}'`] };
          };
          const sa = side(args.a);
          const sb = side(args.b);
          const bad = sa.error || sb.error;
          if (bad) return fail(id, bad[0].startsWith("PROFILE_") ? "BAD_REQUEST" : "REFUSED", bad[1], { check: bad[0] });
          if (diffExtras) {
            if (args.a === "accepted") sa.facts.push({ unit: "P1", order: 0, label: "OC word: whole-curve offset", kind: "mv", value: 0 });
            if (args.b === "read") sb.facts.push({ unit: "P1", order: 0, label: "OC word: whole-curve offset", kind: "mv", value: -5 }, { unit: "ring", order: 24, label: "Offset at 14x", kind: "mv", value: -25 });
          }
          const key = (f) => `${f.unit}|${f.order}`;
          const fa = new Map(sa.facts.map((f) => [key(f), f]));
          const fb = new Map(sb.facts.map((f) => [key(f), f]));
          const rows = [...new Set([...fa.keys(), ...fb.keys()])]
            .map((k) => {
              const x = fa.get(k);
              const y = fb.get(k);
              const f = x || y;
              return { unit: f.unit, order: f.order, label: f.label, kind: f.kind, a: x ? x.value : null, b: y ? y.value : null };
            })
            .filter((r) => r.a !== r.b)
            .sort((p, q) => DIFF_UNITS.indexOf(p.unit) - DIFF_UNITS.indexOf(q.unit) || p.order - q.order)
            .map(({ order, ...r }) => r);
          const units = [];
          for (const r of rows) {
            const last = units[units.length - 1];
            if (last && last.unit === r.unit) last.n++;
            else units.push({ unit: r.unit, n: 1 });
          }
          const info = (x, ref) => ({ ref, name: x.name, source: x.source, partial: false });
          return ok(id, {
            a: info(sa, args.a), b: info(sb, args.b), rows, units, truncated: 0,
            compared: ["each unit's OC word (whole-curve offset, OC ratio, OC target voltage)", "every fused point's offset", "the turbo tables"],
            not_compared: [{ what: "the ring", why: "neither state records it" }],
          });
        }
        case "profiles.accept": {
          if (acceptUnavailable) return { ...clone(profilesAcceptUnavailable), id };
          if (args.set_aside_unreadable !== undefined && args.set_aside_unreadable !== true) return fail(id, "BAD_REQUEST", "'set_aside_unreadable' must be true when given");
          if (unreadable && args.set_aside_unreadable !== true) {
            return fail(id, "REFUSED", "the accepted state that this would replace cannot be read (accepted CURRENT lists no profile), so it cannot be kept as a profile. You can have the whole directory /etc/arltuner/accepted moved aside to /etc/arltuner/accepted.unreadable-20261018-120000 (nothing is deleted) and accept anyway. Nothing was changed.", { check: "ACCEPTED_UNREADABLE" });
          }
          if (!readFresh) return fail(id, "REFUSED", "the words of the last read may no longer be what the CPU holds: a write came after it. Read again first (Tune > Read), then accept. Nothing was changed.", { check: "STALE_READ" });
          let name;
          if (args.from !== undefined) {
            if (args.from !== "read") return fail(id, "BAD_REQUEST", "'from' must be 'read'");
            name = "read-" + isoSeconds(now()).replace(/[-:]/g, "").replace("T", "-").replace("Z", "");
          } else {
            if (typeof args.name !== "string" || !PROFILE_NAME_RE.test(args.name)) return fail(id, "BAD_REQUEST", "'name' must be 1..64 characters of a-z 0-9 . _ -");
            const got = getResult(args.name);
            if (!got) return fail(id, "BAD_REQUEST", `no profile named '${args.name}'`, { check: "PROFILE_NOT_FOUND" });
            const difference = heldDifference(got.contents, held);
            if (difference) return fail(id, "REFUSED", `'${args.name}' is not what the CPU holds now (${difference}). Apply it first (Profiles > Review and apply), then set it as accepted.`, { check: "NOT_APPLIED" });
            name = args.name;
          }
          const replaced = accepted ? [accepted.name] : [];
          let previous = null; // G-228: the replaced state is kept as a saved profile first
          const setAside = unreadable ? "/etc/arltuner/accepted.unreadable-20261018-120000" : null;
          if (accepted) {
            const base = "accepted-before-" + isoSeconds(now()).replace(/[-:]/g, "").replace("T", "-").replace("Z", "");
            previous = base;
            for (let n = 2; saved.has(previous); n++) previous = `${base}-${n}`;
            saved.set(previous, { created: isoSeconds(now()), contents: clone(accepted.contents) });
          }
          accepted = { name, contents: contentsOfHeld(held) };
          unreadable = false;
          const sum = summarize(accepted.contents);
          return ok(id, { name, source: "accepted", from: args.from !== undefined ? "read" : "saved", accepted_at: isoSeconds(now()), replaced, previous_saved_as: previous, previous_saved_names: previous ? [previous] : [], set_aside_as: setAside, cpus: 24, core_entries: sum.units, points: sum.points, turbo: true, complete: true, ring_points: 0, fingerprint: "complete" });
        }
        case "profiles.fingerprint": {
          if (acceptUnavailable) return { ...clone(profilesFingerprintUnavailable), id };
          if (!accepted) return fail(id, "REFUSED", "there is no accepted state to record a fingerprint for; accept a profile first (Profiles > Set as accepted).", { check: "NO_ACCEPTED_STATE" });
          const incomplete = fingerprint === "incomplete";
          return ok(id, { recorded: isoSeconds(now()), cpus: 24, accepted: [accepted.name], fingerprint: incomplete ? "incomplete: no CPU identity under the simulator" : "complete" });
        }
        case "audit.tail":
          return ok(id, clone(auditTail.result));
        case "tune.plan": {
          if (!args.intent || typeof args.intent !== "object") return fail(id, "BAD_REQUEST", "tune.plan needs {intent: {...}}");
          if (args.intent.profile !== undefined || args.intent.restore !== undefined) {
            const whole = planWholeState(args.intent);
            return whole.error ? fail(id, whole.error.code, whole.error.message, whole.error.detail) : ok(id, clone(whole.plan));
          }
          const refused = mockRefusal(args.intent);
          if (refused) return fail(id, "REFUSED", refused, { check: "POLICY_RANGE" });
          const negative = negativeOffsets(args.intent);
          if (uvpSeen === "on" && negative.length) return fail(id, "REFUSED", `${negative[0][0]}: offset ${negative[0][1]} mV: ${UVP_MESSAGE} Nothing was sent.`, { check: "UNDERVOLT_PROTECTION" });
          return ok(id, { ...clone(issuePlan(args.intent)), uvp: uvpSeen }); // an empty intent answers the recorded one-point plan; anything else is synthesized (synthPlan)
        }
        case "cancel": {
          if (args.op_id !== undefined && args.op_id !== null && args.op_id !== busyOp) return fail(id, "BAD_REQUEST", `${JSON.stringify(args.op_id)} is not the running operation`, { state: busyOp ? "applying" : "idle", op_id: busyOp });
          if (!running) return ok(id, { cancelled: false, state: "idle" });
          running.cancelled = true;
          return ok(id, { cancelled: true, op_id: running.id, state: "applying" });
        }
        case "tune.read": {
          if (busyOp) return fail(id, "BUSY", "another long op is running", { state: "applying", op_id: busyOp });
          const result = clone(tuneRead.result);
          result.read_at = new Date(now()).toISOString().replace(/\.\d+Z$/, "Z");
          result.uvp = uvpSeen;
          readFresh = true;
          replayLong(id, tuneReadEvents, ok(id, result));
          return { id };
        }
        case "tune.apply": {
          if (busyOp) return fail(id, "BUSY", "another long op is running", { state: "applying", op_id: busyOp });
          const plan = plans.get(args.plan_id);
          if (!plan) return fail(id, "PLAN_STALE", "no such plan (the mock only knows the plans it issued, once each)");
          if (applyMode === "stale" && !staleFired) {
            staleFired = true;
            recordedGone = true;
            plans.clear();
            return fail(id, "PLAN_STALE", "a word changed since this plan was made, or the plan is gone. Nothing was written. Plan again.");
          }
          if (args.typed !== `CONFIRM ${plan.nonce}`) return fail(id, "CONFIRM_MISMATCH", "the typed confirmation does not match the plan's nonce");
          plans.delete(plan.plan_id); // single use
          if (uvpBios && negativeOffsets(planIntents.get(plan.plan_id)).length) {
            uvpSeen = "on";
            return fail(id, "REFUSED", UVP_MESSAGE, { check: "UNDERVOLT_PROTECTION", ops_done: 8 });
          }
          held = heldAfter(held, planIntents.get(plan.plan_id)); // G-213: what the mock CPU holds now (profiles.accept compares with it)
          appliedHere = true;
          if (plan.plan_id === tunePlan.response.result.plan_id) recordedGone = true;
          if (!uvpBios && uvpSeen === "unknown" && negativeOffsets(planIntents.get(plan.plan_id)).length) uvpSeen = "off"; // a negative offset went in: Undervolt Protection is off
          const outcome = applyOutcome(id, plan);
          if (outcome.response && outcome.response.result) outcome.response.result.uvp = uvpSeen;
          replayLong(id, outcome.events, outcome.response);
          return { id };
        }
        default:
          return fail(id, "UNKNOWN_OP", `the mock bridge has no fixture for ${op}`);
      }
    },
    listen(event, cb) {
      let set = listeners.get(event);
      if (!set) listeners.set(event, (set = new Set()));
      set.add(cb);
      return () => set.delete(cb);
    },
    close() {
      if (telemetryTimer) clearInterval(telemetryTimer);
      telemetryTimer = null;
      for (const t of timers) clearTimeout(t);
      timers.clear();
      listeners.clear();
    },
  };
}

// G-218: Curves/Stress fixtures and run simulation are layered on top of the base transport.
import { wrapMockTransport } from "./mock-backend-g218.js";
// G-217: power.* / runtime.* (Tune → Power, Ring & uncore, Ratio caps) and `cpu_limits` on the reads, layered on top
import { wrapMockTransportG217 } from "./mock-backend-g217.js";
export function createMockTransport(opts = {}) {
  // the recorded simulator curves seed the library (incl. a baseline for Tune predictions) unless ?nobaseline asks for none
  const seed = opts.seed ?? (noBaselineParam() ? "none" : "recorded");
  return wrapMockTransportG217(wrapMockTransport(createBaseMockTransport(opts), { ...opts, seed }), opts);
}
