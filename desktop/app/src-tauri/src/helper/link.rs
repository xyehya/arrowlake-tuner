//! One live connection to the helper: request/response matching, id allocation, event emission (PLAN §3.1; mirrors
//! `src/arltuner/web/helperlink.py` `HelperLink`).
//!
//! * Every request passes [`ops::check`] right before it is written: nothing outside the allowlist reaches the socket.
//! * A reader thread owns the read half. It dispatches responses by id, publishes events as `helper://<name>` (dots become `-`:
//!   Tauri event names allow only alphanumerics, `-`, `/`, `:` and `_`), and on EOF fails every waiter. Malformed, non-object or
//!   oversize lines are dropped; they never end the reader.
//! * Short ops are waited for by the caller. Long ops return their id at once; the final response is emitted as `helper://result`.
//! * An op in flight when the link dies is reported lost (`LINK_LOST`), never replayed.

use std::collections::{HashMap, VecDeque};
use std::io::{BufReader, Write};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::Duration;

use serde_json::{json, Map, Value};

use super::frame::{encode_line, read_frame, Frame, MAX_LINE};
use super::ops::{self, Refusal};

pub const EVENT_PREFIX: &str = "helper://";
/// Long ops (`server.py` `max_pending_long`) a client may have in flight at once.
pub const MAX_PENDING_LONG: usize = 4;
/// A send that blocks this long means the helper stopped reading (it drops such clients itself after the same time).
const WRITE_TIMEOUT: Duration = Duration::from_secs(8);
/// `error.detail` keys that reach the webview (`server.py` `_DETAIL_ALLOW`).
const DETAIL_ALLOW: [&str; 3] = ["state", "check", "curve_id"];
/// Plan ids remembered per connection (`tune.apply` is accepted only for one of these).
const MAX_PLANS: usize = 8;
/// Request ids come from one process-wide counter: an id is never reused across reconnects, so a late answer to an old
/// connection's request can never match a new one.
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

/// Where the link publishes events. The Tauri build wraps an `AppHandle`; tests collect.
pub trait EventSink: Send + Sync + 'static {
    /// `name` is the full event name (`helper://state`).
    fn emit(&self, name: &str, payload: Value);
}

#[derive(Debug)]
pub enum CallError {
    /// Refused before anything was written (not allowlisted / wrong argument shape).
    Refused(Refusal),
    /// The request would not fit in 64 KiB.
    TooLarge,
    /// [`MAX_PENDING_LONG`] long ops are already in flight.
    TooManyLong,
    /// The link is closed, or the write failed: the request may not have been sent and is not retried.
    Unavailable,
    /// No answer in time (the helper may still be working: watch the events).
    Timeout,
}

impl CallError {
    /// `(code, message)` as the webview sees them.
    pub fn code_message(&self) -> (String, String) {
        match self {
            CallError::Refused(r) => (r.code.to_string(), r.message.clone()),
            CallError::TooLarge => ("BAD_REQUEST".into(), "refused locally: the request exceeds the 64 KiB line limit".into()),
            CallError::TooManyLong => ("BUSY".into(), format!("refused locally: {MAX_PENDING_LONG} long operations are already running")),
            CallError::Unavailable => (
                "LINK_LOST".into(),
                "the helper connection is closed; the request was not replayed. Retry to reconnect.".into(),
            ),
            CallError::Timeout => ("TIMEOUT".into(), "no answer from the helper in time; it may still be working (watch the events)".into()),
        }
    }
}

/// Outcome of [`Link::call`].
#[derive(Debug)]
pub enum Reply {
    /// A short op's raw helper response (`{id, ok, result|error}`).
    Done(Value),
    /// A long op was sent; its final response arrives as the `helper://result` event.
    Started(String),
}

enum Waiter {
    Short(Sender<Option<Value>>),
    Long,
}

#[derive(Default)]
struct State {
    alive: bool,
    finished: bool,
    pending: HashMap<String, Waiter>,
    latest: HashMap<String, Value>,
    refusal: Option<Value>,
    /// `plan_id`s of successful `tune.plan` responses on this connection, oldest first.
    plans: VecDeque<String>,
}

struct Shared {
    state: Mutex<State>,
    finished: Condvar,
    sink: Arc<dyn EventSink>,
    stream: UnixStream,
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn emit(&self, name: &str, payload: Value) {
        self.sink.emit(&format!("{EVENT_PREFIX}{name}"), payload);
    }

    /// The pipe is over: fail every waiter (short: `None`; long: a `result` event saying the op was lost).
    fn fail_all(&self, reason: &str) {
        let pending = {
            let mut st = self.lock();
            st.alive = false;
            std::mem::take(&mut st.pending)
        };
        for (id, waiter) in pending {
            match waiter {
                Waiter::Short(tx) => {
                    let _ = tx.send(None);
                }
                Waiter::Long => self.emit(
                    "result",
                    json!({"id": id, "ok": false, "error": {"code": "LINK_LOST", "message":
                        "the helper connection was lost while this operation was running; its outcome is unknown and it was not replayed"}}),
                ),
            }
        }
        let _ = self.stream.shutdown(Shutdown::Both);
        self.emit("closed", json!({"reason": reason}));
        self.lock().finished = true;
        self.finished.notify_all();
    }

    fn dispatch(&self, obj: Map<String, Value>) {
        match obj.get("id") {
            Some(Value::String(id)) => {
                let waiter = self.lock().pending.remove(id);
                match waiter {
                    Some(Waiter::Short(tx)) => {
                        let _ = tx.send(Some(Value::Object(obj)));
                    }
                    Some(Waiter::Long) => self.emit("result", public_response(&Value::Object(obj))),
                    None => {} // answered after a timeout, or an id we never sent: dropped
                }
            }
            Some(Value::Null) => {
                // The socket service's one framed line before it closes (D-043): ACCESS_DENIED / CLIENT_BUSY. Kept for the connect error.
                let refused = obj.get("ok") == Some(&Value::Bool(false))
                    && obj.get("error").and_then(|e| e.get("code")).and_then(Value::as_str) == Some("REFUSED");
                if refused {
                    let mut st = self.lock();
                    if st.refusal.is_none() {
                        st.refusal = obj.get("error").cloned();
                    }
                }
            }
            Some(_) => {} // a non-string id matches nothing
            None => {
                let Some(name) = obj.get("event").and_then(Value::as_str) else { return };
                // "result" is the link's own event name: a helper line cannot impersonate a long op's outcome.
                if !event_name_ok(name) || name == "result" || name == "closed" {
                    return;
                }
                let mapped = name.replace('.', "-");
                if name == "state" || name == "heartbeat" {
                    self.lock().latest.insert(name.to_string(), Value::Object(obj.clone()));
                }
                self.emit(&mapped, Value::Object(obj));
            }
        }
    }
}

/// `[a-z0-9_.]{1,32}` (`server.py` `_EVENT_RE`).
fn event_name_ok(s: &str) -> bool {
    (1..=32).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'.')
}

pub struct Link {
    shared: Arc<Shared>,
    writer: Mutex<UnixStream>,
}

impl Link {
    /// Take over a connected stream and start the reader thread.
    pub fn start(stream: UnixStream, sink: Arc<dyn EventSink>) -> std::io::Result<Link> {
        stream.set_write_timeout(Some(WRITE_TIMEOUT))?;
        let reader = stream.try_clone()?;
        let writer = stream.try_clone()?;
        let shared = Arc::new(Shared {
            state: Mutex::new(State { alive: true, ..State::default() }),
            finished: Condvar::new(),
            sink,
            stream,
        });
        let sh = Arc::clone(&shared);
        thread::Builder::new().name("helper-reader".into()).spawn(move || read_loop(&sh, reader))?;
        Ok(Link { shared, writer: Mutex::new(writer) })
    }

    pub fn alive(&self) -> bool {
        self.shared.lock().alive
    }

    /// Long ops currently in flight.
    pub fn pending_long(&self) -> usize {
        self.shared.lock().pending.values().filter(|w| matches!(w, Waiter::Long)).count()
    }

    /// Last `state` / `heartbeat` events seen (replayed to a UI that attaches late).
    pub fn latest(&self) -> Map<String, Value> {
        self.shared.lock().latest.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
    }

    /// The framed `REFUSED` error (`{code, message, detail}`) the service sent before closing, if any.
    pub fn refusal(&self) -> Option<Value> {
        self.shared.lock().refusal.clone()
    }

    /// Wait until the reader has ended (the framed refusal arrives before the EOF). `true` if it ended in time.
    pub fn wait_finished(&self, timeout: Duration) -> bool {
        let guard = self.shared.lock();
        let (guard, _) = self.shared.finished.wait_timeout_while(guard, timeout, |s| !s.finished).unwrap_or_else(|p| p.into_inner());
        guard.finished
    }

    /// Send one request. Short ops wait up to `timeout`; long ops return [`Reply::Started`] at once.
    pub fn call(&self, op: &str, args: &Value, timeout: Duration) -> Result<Reply, CallError> {
        let info = ops::check(op, args).map_err(CallError::Refused)?;
        if op == "tune.apply" && !self.shared.lock().plans.iter().any(|p| args.get("plan_id").and_then(Value::as_str) == Some(p)) {
            return Err(CallError::Refused(Refusal {
                code: "BAD_REQUEST",
                message: "refused locally: plan not made by this app".to_string(),
            }));
        }
        let (id, rx) = self.submit(op, args, info.long)?;
        if op == "tune.apply" {
            // A plan is spent by the apply that names it (the helper consumes it too).
            let spent = args.get("plan_id").and_then(Value::as_str);
            self.shared.lock().plans.retain(|p| Some(p.as_str()) != spent);
        }
        let Some(rx) = rx else { return Ok(Reply::Started(id)) };
        match rx.recv_timeout(timeout) {
            Ok(Some(resp)) => {
                if op == "tune.plan" {
                    self.remember_plan(&resp);
                }
                Ok(Reply::Done(resp))
            }
            Ok(None) | Err(RecvTimeoutError::Disconnected) => Err(CallError::Unavailable),
            Err(RecvTimeoutError::Timeout) => {
                self.shared.lock().pending.remove(&id);
                Err(CallError::Timeout)
            }
        }
    }

    fn remember_plan(&self, resp: &Value) {
        if resp.get("ok") != Some(&Value::Bool(true)) {
            return;
        }
        let Some(plan) = resp.get("result").and_then(|r| r.get("plan_id")).and_then(Value::as_str) else { return };
        let mut st = self.shared.lock();
        st.plans.retain(|p| p != plan);
        st.plans.push_back(plan.to_string());
        while st.plans.len() > MAX_PLANS {
            st.plans.pop_front();
        }
    }

    fn submit(&self, op: &str, args: &Value, long: bool) -> Result<(String, Option<Receiver<Option<Value>>>), CallError> {
        let id;
        let mut rx = None;
        {
            let mut st = self.shared.lock();
            if !st.alive {
                return Err(CallError::Unavailable);
            }
            if long && st.pending.values().filter(|w| matches!(w, Waiter::Long)).count() >= MAX_PENDING_LONG {
                return Err(CallError::TooManyLong);
            }
            id = format!("d{}", NEXT_ID.fetch_add(1, Ordering::Relaxed));
            let line = encode_line(&json!({"id": id, "op": op, "args": args})).ok_or(CallError::TooLarge)?;
            // Registered before the write: the answer can arrive before write() returns.
            if long {
                st.pending.insert(id.clone(), Waiter::Long);
            } else {
                let (tx, r) = mpsc::channel();
                st.pending.insert(id.clone(), Waiter::Short(tx));
                rx = Some(r);
            }
            drop(st);
            if self.write(&line).is_err() {
                self.shared.lock().pending.remove(&id);
                let _ = self.shared.stream.shutdown(Shutdown::Both); // the reader sees EOF and fails the others
                return Err(CallError::Unavailable);
            }
        }
        Ok((id, rx))
    }

    fn write(&self, line: &[u8]) -> std::io::Result<()> {
        debug_assert!(line.len() <= MAX_LINE);
        let mut w = self.writer.lock().unwrap_or_else(|p| p.into_inner());
        w.write_all(line)?;
        w.flush()
    }

    /// Close the connection; the helper sees EOF. Pending waiters fail.
    pub fn close(&self) {
        let _ = self.shared.stream.shutdown(Shutdown::Both);
    }
}

impl Drop for Link {
    fn drop(&mut self) {
        self.close();
    }
}

fn read_loop(shared: &Shared, stream: UnixStream) {
    let mut reader = BufReader::with_capacity(16 * 1024, stream);
    let reason = loop {
        match read_frame(&mut reader, MAX_LINE) {
            Ok(Frame::Eof) => break "eof",
            Err(_) => break "read error",
            Ok(Frame::Oversize) => continue,
            Ok(Frame::Line(bytes)) => {
                // from_slice rejects non-UTF-8 strings and nesting beyond serde_json's recursion limit with an error: dropped.
                if let Ok(Value::Object(obj)) = serde_json::from_slice::<Value>(&bytes) {
                    shared.dispatch(obj);
                }
            }
        }
    };
    shared.fail_all(reason);
}

/// What the webview may see of a helper response: `{id, ok, result}` or `{id, ok:false, error:{code, message, detail?}}` with `detail`
/// restricted to scalar values of [`DETAIL_ALLOW`] keys and the message cut to 500 characters (`server.py` `public_response`).
pub fn public_response(resp: &Value) -> Value {
    let id = resp.get("id").cloned().unwrap_or(Value::Null);
    if resp.get("ok") == Some(&Value::Bool(true)) {
        return json!({"id": id, "ok": true, "result": resp.get("result").cloned().unwrap_or(Value::Null)});
    }
    let err = resp.get("error").and_then(Value::as_object);
    let code = err.and_then(|e| e.get("code")).and_then(Value::as_str).unwrap_or("INTERNAL");
    let message: String = match err.and_then(|e| e.get("message")) {
        Some(Value::String(s)) => s.chars().take(500).collect(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string().chars().take(500).collect(),
    };
    let mut out = json!({"code": code, "message": message});
    if let Some(det) = err.and_then(|e| e.get("detail")).and_then(Value::as_object) {
        let keep: Map<String, Value> =
            det.iter().filter(|(k, v)| DETAIL_ALLOW.contains(&k.as_str()) && !v.is_array() && !v.is_object()).map(|(k, v)| (k.clone(), v.clone())).collect();
        if !keep.is_empty() {
            out["detail"] = Value::Object(keep);
        }
    }
    json!({"id": id, "ok": false, "error": out})
}
