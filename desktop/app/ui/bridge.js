// bridge.js: the only file that touches window.__TAURI__ (PLAN §3.2). One interface, two backends behind it:
//   - Tauri backend: `invoke('helper_status' | 'helper_connect' | 'helper_call')` and `listen('helper://<event>')` (PLAN §3.1).
//     UNTESTED against the real Rust side until G-204 lands; the command argument/return shapes below follow PLAN §3.1 and are the contract G-204 must meet.
//   - mock backend (`?bridge=mock`): replays the JSON fixtures from fixtures/ (loaded lazily, so the AppImage never parses them).
// Interface: connect(), status(), call(op, args), on(event, cb), off(event, cb) (+ wait(id), run(op, args), close()).
//   - call() resolves with the helper's response `{id?, ok:true, result}` / `{ok:false, error:{code,message,detail?}}`.
//     A long op (tune.read, tune.apply, vf.measure, stress.start) resolves at once with `{ok:true, pending:true, id}`; its final response arrives as
//     the `result` event ({id, ok, result|error}); wait(id) / run() hide that.
//   - events: heartbeat state progress telemetry vf.point result (payloads exactly as the helper sends them, contract 9) and `link` ({state,kind,message,fix}).

export const EVENTS = ["heartbeat", "state", "progress", "telemetry", "vf.point", "result", "link"];
const RESULT_KEEP = 32; // final responses remembered so wait(id) works even if the result event arrived first

export const LINK_STATES = ["connecting", "connected", "busy", "not_installed", "not_allowed", "error"];

/** Fix text per failure kind (named fixes, same wording as the web GUI's helperlink; the app never runs sudo itself). */
export const FIXES = {
  not_installed: "sudo bash install.sh",
  not_allowed: "sudo bash install.sh --add-user $USER",
  busy: "Close the other arltuner window or client, then retry.",
};

/** Map whatever a backend reports to {state, kind, message, fix}. Accepts the helper-link `kind` names (missing untrusted denied unavailable busy not_allowed closed timeout). */
export function normalizeLink(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  let state = r.state;
  if (!LINK_STATES.includes(state)) {
    const kind = r.kind;
    if (r.connected === true) state = "connected";
    else if (kind === "busy") state = "busy";
    else if (kind === "missing" || kind === "unavailable") state = "not_installed";
    else if (kind === "not_allowed" || kind === "denied") state = "not_allowed";
    else state = "error";
  }
  const fix = r.fix ?? (state === "connected" || state === "connecting" ? null : FIXES[state] || null);
  return { state, kind: r.kind ?? null, message: String(r.message ?? ""), fix };
}

function isObject(x) {
  return x !== null && typeof x === "object" && !Array.isArray(x);
}

export class Bridge {
  #t;
  #name;
  #link = { state: "connecting", kind: null, message: "", fix: null };
  #subs = new Map(); // event -> Set(cb)
  #unlisten = new Map(); // event -> unlisten fn | Promise<fn>
  #results = new Map(); // id -> final response
  #waiters = new Map(); // id -> [resolve]

  constructor(transport, name = "custom") {
    this.#t = transport;
    this.#name = name;
  }

  get name() {
    return this.#name;
  }

  async connect() {
    await this.#ensure("result");
    await this.#ensure("link");
    let raw;
    try {
      raw = await this.#t.connect();
    } catch (e) {
      raw = { state: "error", message: e && e.message ? e.message : String(e) };
    }
    this.#setLink(normalizeLink(raw));
    return this.#link;
  }

  async status() {
    try {
      if (this.#t.status) this.#setLink(normalizeLink(await this.#t.status()));
    } catch (e) {
      this.#setLink(normalizeLink({ state: "error", message: e && e.message ? e.message : String(e) }));
    }
    return this.#link;
  }

  async call(op, args = {}) {
    if (typeof op !== "string" || !op) throw new TypeError("bridge.call: op must be a non-empty string");
    if (!isObject(args)) throw new TypeError("bridge.call: args must be an object");
    await this.#ensure("result");
    let res;
    try {
      res = await this.#t.call(op, args);
    } catch (e) {
      return { ok: false, error: { code: "BRIDGE_ERROR", message: e && e.message ? e.message : String(e) } };
    }
    if (isObject(res) && res.ok === undefined && typeof res.id === "string") return { ok: true, pending: true, id: res.id }; // long op: the result comes as an event
    if (!isObject(res) || typeof res.ok !== "boolean") return { ok: false, error: { code: "BRIDGE_ERROR", message: "malformed reply from the backend" } };
    return res;
  }

  /** Final response of a long op (resolves from the `result` event; immediately if it already arrived). */
  wait(id) {
    if (this.#results.has(id)) return Promise.resolve(this.#results.get(id));
    return new Promise((resolve) => {
      const list = this.#waiters.get(id) || [];
      list.push(resolve);
      this.#waiters.set(id, list);
    });
  }

  /** call() and, for a long op, wait for its final response. */
  async run(op, args = {}) {
    const first = await this.call(op, args);
    return first.pending ? this.wait(first.id) : first;
  }

  on(event, cb) {
    if (typeof cb !== "function") throw new TypeError("bridge.on: callback must be a function");
    let set = this.#subs.get(event);
    if (!set) this.#subs.set(event, (set = new Set()));
    set.add(cb);
    this.#ensure(event).catch(() => {});
    return () => this.off(event, cb);
  }

  off(event, cb) {
    const set = this.#subs.get(event);
    if (!set) return;
    if (cb) set.delete(cb);
    else set.clear();
    if (set.size === 0 && event !== "result" && event !== "link") void this.#drop(event);
  }

  async close() {
    for (const ev of [...this.#unlisten.keys()]) await this.#drop(ev);
    this.#subs.clear();
    if (this.#t.close) await this.#t.close();
  }

  // ---- internals ----
  #setLink(link) {
    const same = link.state === this.#link.state && link.kind === this.#link.kind && link.message === this.#link.message;
    this.#link = link;
    if (!same) this.#dispatch("link", link);
  }

  #dispatch(event, payload) {
    if (event === "result" && isObject(payload) && typeof payload.id === "string") {
      this.#results.set(payload.id, payload);
      if (this.#results.size > RESULT_KEEP) this.#results.delete(this.#results.keys().next().value);
      const waiting = this.#waiters.get(payload.id);
      if (waiting) {
        this.#waiters.delete(payload.id);
        for (const resolve of waiting) resolve(payload);
      }
    }
    const set = this.#subs.get(event);
    if (set) for (const cb of [...set]) {
      try {
        cb(payload);
      } catch (e) {
        console.error("bridge listener for", event, "threw", e);
      }
    }
  }

  async #ensure(event) {
    if (event === "link") return; // `link` is produced by the bridge itself, not by a backend event
    if (this.#unlisten.has(event)) return this.#unlisten.get(event);
    const p = Promise.resolve(this.#t.listen(event, (payload) => this.#dispatch(event, payload)));
    this.#unlisten.set(event, p);
    p.then((fn) => this.#unlisten.set(event, fn), () => this.#unlisten.delete(event));
    return p;
  }

  async #drop(event) {
    const u = this.#unlisten.get(event);
    this.#unlisten.delete(event);
    if (!u) return;
    const fn = await u;
    if (typeof fn === "function") fn();
  }
}

// ---- Tauri backend ----
export function createTauriTransport(tauri = globalThis.__TAURI__) {
  if (!tauri || !tauri.core || typeof tauri.core.invoke !== "function") throw new Error("window.__TAURI__ is not available");
  const invoke = tauri.core.invoke.bind(tauri.core);
  return {
    connect: () => invoke("helper_connect"),
    status: () => invoke("helper_status"),
    call: (op, args) => invoke("helper_call", { op, args }),
    listen: (event, cb) => tauri.event.listen(`helper://${event}`, (e) => cb(e.payload)),
  };
}

/** What a plain browser gets without ?bridge=mock: no helper to talk to, says so in the status bar. */
function createUnavailableTransport() {
  const message = "no desktop runtime here: open the arltuner app, or add ?bridge=mock to try the UI with fixtures";
  return {
    connect: async () => ({ state: "error", kind: "no_runtime", message }),
    status: async () => ({ state: "error", kind: "no_runtime", message }),
    call: async () => ({ ok: false, error: { code: "NOT_CONNECTED", message } }),
    listen: () => () => {},
  };
}

/** `search` is location.search. bridge=mock[&motion=1][&tick=<ms>][&helper=busy|not_installed|not_allowed|error] */
export async function selectBridge(search = globalThis.location?.search ?? "", tauri = globalThis.__TAURI__) {
  const q = new URLSearchParams(search);
  if (q.get("bridge") === "mock") {
    const { createMockTransport } = await import("./fixtures/mock-backend.js");
    const tick = Number(q.get("tick"));
    return new Bridge(
      createMockTransport({ motion: q.get("motion") === "1", intervalMs: Number.isFinite(tick) && tick >= 10 ? tick : 1000, helper: q.get("helper") || "ok" }),
      "mock",
    );
  }
  if (tauri && tauri.core) return new Bridge(createTauriTransport(tauri), "tauri");
  return new Bridge(createUnavailableTransport(), "none");
}
