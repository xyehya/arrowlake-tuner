// app.js: composition root. Builds the frame (PLAN §4.1), wires the bridge to the store, routes the 7 screens.
// A screen is a view object {id, mount(root, ctx), unmount()}; until its task lands, VIEWS[id] is the placeholder. Later tasks swap in
// `import { tuneView } from "./views/tune.js"` here. ctx = {store, bridge, toast(kind, text), openDrawer(title), closeDrawer(), drawer}.
import { selectBridge } from "./bridge.js";
import {
  createAppStore, SCREEN_IDS, setScreen, setLink, setPreflight, setLastRead, setPending, pushToast, openDrawer, closeDrawer, applyHelperEvent,
} from "./store.js";
import { h, clear } from "./components/dom.js";
import { SCREENS, createNavRail, screenForKey } from "./components/nav-rail.js";
import { createStatusBar } from "./components/status-bar.js";
import { createMonitorStrip } from "./components/monitor-strip.js";
import { createPendingBar } from "./components/pending-bar.js";
import { createToaster } from "./components/toast.js";
import { createDrawer } from "./components/drawer.js";
import { placeholderView } from "./components/placeholder.js";

const VIEWS = Object.fromEntries(SCREENS.map((s) => [s.id, () => placeholderView(s.id)]));

const TELEMETRY_INTERVAL_S = 1.0;

function screenFromHash(hash) {
  const id = String(hash || "").replace(/^#\/?/, "");
  return SCREEN_IDS.includes(id) ? id : null;
}

function isTyping(target) {
  return !!target && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

/** After a connect: the read-only start-up reads (preflight, tune.last) and the telemetry stream. No mailbox traffic (tune.read is never automatic). */
async function startUp({ store, bridge, toast }) {
  const pre = await bridge.call("preflight");
  if (pre.ok) setPreflight(store, pre.result);
  else toast("error", `preflight failed: ${pre.error.message}`);
  const last = await bridge.call("tune.last");
  if (last.ok && last.result && last.result.available) {
    const r = last.result;
    setLastRead(store, new Date(r.at * 1000).toISOString(), r.result ? r.result.accepted_match ?? null : null, r.result ? r.result.accepted_note : "");
  }
  const tel = await bridge.call("telemetry.start", { interval_s: TELEMETRY_INTERVAL_S });
  if (!tel.ok) toast("warn", `telemetry did not start: ${tel.error.message}`);
}

export async function boot(doc = document) {
  const store = createAppStore();
  const bridge = await selectBridge();

  const toast = (kind, text, ttlMs) => pushToast(store, { kind, text, ttlMs });
  const drawer = createDrawer(doc.getElementById("drawer"), store);
  const ctx = { store, bridge, toast, drawer, openDrawer: (title) => openDrawer(store, title), closeDrawer: () => closeDrawer(store) };

  const goto = (id) => {
    if (!setScreen(store, id)) return;
    if (screenFromHash(doc.defaultView.location.hash) !== id) doc.defaultView.location.hash = `#/${id}`;
  };

  createStatusBar(doc.getElementById("status-bar"), store);
  createNavRail(doc.getElementById("nav-rail"), store, { onSelect: goto });
  createMonitorStrip(doc.getElementById("strip"), store);
  createToaster(doc.getElementById("toasts"), store);
  createPendingBar(doc.getElementById("pending-bar"), store, {
    onDiscard: () => {
      setPending(store, 0, 0);
      toast("info", "Pending changes discarded.");
    },
    // G-210 replaces this with the real plan + apply drawer.
    onReview: () => {
      drawer.setContent(h("p", { class: "muted", text: "The ordered write list and the Apply button arrive with the apply drawer (G-210)." }));
      openDrawer(store, "Review & Apply");
    },
  });

  // ---- screens ----
  const screenRoot = doc.getElementById("screen");
  let current = null;
  store.watch(
    (s) => s.screen,
    (id) => {
      if (current) current.unmount();
      clear(screenRoot);
      screenRoot.dataset.screen = id;
      current = VIEWS[id]();
      current.mount(screenRoot, ctx);
      doc.title = `arltuner · ${SCREENS.find((s) => s.id === id).label}`;
    },
  );
  const fromHash = screenFromHash(doc.defaultView.location.hash);
  if (fromHash) setScreen(store, fromHash);
  doc.defaultView.addEventListener("hashchange", () => {
    const id = screenFromHash(doc.defaultView.location.hash);
    if (id) setScreen(store, id);
  });
  doc.addEventListener("keydown", (e) => {
    const id = screenForKey(e.key, { typing: isTyping(e.target), ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey });
    if (id) {
      e.preventDefault();
      goto(id);
    } else if (e.key === "Escape" && store.get().drawer.open) {
      closeDrawer(store);
    }
  });

  // ---- bridge -> store ----
  for (const ev of ["heartbeat", "state", "progress", "telemetry"]) bridge.on(ev, (p) => applyHelperEvent(store, ev, p));
  bridge.on("link", (l) => setLink(store, l));

  if (bridge.name === "mock") globalThis.__arltuner = { store, bridge, ctx }; // test seam for the Playwright smoke; never set for the real bridge

  try {
    const link = await bridge.connect();
    setLink(store, link);
    if (link.state === "connected") await startUp({ store, bridge, toast });
    else toast("warn", link.message || "The helper is not available.", 0);
  } catch (e) {
    setLink(store, { state: "error", message: String(e && e.message ? e.message : e) });
    toast("error", `startup failed: ${e && e.message ? e.message : e}`);
  }
  doc.documentElement.dataset.ready = "1";
  return ctx;
}

boot().catch((e) => {
  // Last resort: the frame itself failed. textContent only.
  const root = document.getElementById("screen");
  if (root) root.textContent = `arltuner could not start: ${e && e.message ? e.message : e}`;
  console.error(e);
});
