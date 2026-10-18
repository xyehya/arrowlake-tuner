//! Helper link (PLAN §3.1): connect to the root helper's socket, prove it is the real one, and carry the allowlisted ops and the events
//! between it and the webview. Commands for JS: `helper_status`, `helper_connect`, `helper_call`.
//!
//! The app is an unprivileged client. The only trusted listener is root at `/run/arltuner/helper.sock`; the `dev-socket` feature (tests and
//! the simulator helper) trusts the current uid at `ARLTUNER_DESKTOP_SOCKET` instead and cannot be built into a release profile:
//! `build.rs` panics on it and the `compile_error!` below fires when debug assertions are off.

#[cfg(all(feature = "dev-socket", not(debug_assertions)))]
compile_error!("feature `dev-socket` must not be enabled in a release build (the AppImage is an unprivileged client of the root helper)");

pub mod connect;
pub mod frame;
pub mod link;
pub mod ops;

use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};

use connect::{ConnectError, Probe, SysProbe, Target};
use link::{public_response, EventSink, Link, Reply};

/// A short op that gets no answer in this time is reported `TIMEOUT` (server.py `call_timeout_s`).
pub const CALL_TIMEOUT: Duration = Duration::from_secs(30);

/// The webview-facing state: at most one link; reconnects on demand.
pub struct HelperState {
    target: Target,
    probe: Box<dyn Probe>,
    sink: Arc<dyn EventSink>,
    handshake_timeout: Duration,
    link: Mutex<Option<Arc<Link>>>,
    connecting: Mutex<()>,
    last_error: Mutex<Option<ConnectError>>,
    preflight: Mutex<Option<Value>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

fn error_json(code: &str, message: &str, detail: Option<Value>) -> Value {
    let mut err = json!({"code": code, "message": message});
    if let Some(d) = detail {
        err["detail"] = d;
    }
    json!({"ok": false, "error": err})
}

impl HelperState {
    /// The app's state: the target from [`Target::from_environment`], real `lstat` / `SO_PEERCRED`.
    pub fn new(sink: Arc<dyn EventSink>) -> HelperState {
        HelperState::with_parts(Target::from_environment(), Box::new(SysProbe), sink, connect::HANDSHAKE)
    }

    pub fn with_parts(target: Target, probe: Box<dyn Probe>, sink: Arc<dyn EventSink>, handshake_timeout: Duration) -> HelperState {
        HelperState {
            target,
            probe,
            sink,
            handshake_timeout,
            link: Mutex::new(None),
            connecting: Mutex::new(()),
            last_error: Mutex::new(None),
            preflight: Mutex::new(None),
        }
    }

    fn live_link(&self) -> Option<Arc<Link>> {
        lock(&self.link).as_ref().filter(|l| l.alive()).cloned()
    }

    /// The live link, or one fresh connection attempt (one per user action).
    fn ensure_link(&self) -> Result<Arc<Link>, ConnectError> {
        if let Some(l) = self.live_link() {
            return Ok(l);
        }
        let _one_at_a_time = lock(&self.connecting);
        if let Some(l) = self.live_link() {
            return Ok(l);
        }
        match connect::connect(&self.target, self.probe.as_ref(), Arc::clone(&self.sink), &connect::whoami(), self.handshake_timeout) {
            Ok((link, pre)) => {
                let link = Arc::new(link);
                *lock(&self.link) = Some(Arc::clone(&link));
                *lock(&self.last_error) = None;
                *lock(&self.preflight) = Some(public_response(&pre));
                Ok(link)
            }
            Err(e) => {
                *lock(&self.link) = None;
                *lock(&self.last_error) = Some(e.clone());
                Err(e)
            }
        }
    }

    /// `{connected, path, dev_socket, pending_long, last_error, preflight, latest}`.
    pub fn status(&self) -> Value {
        let link = self.live_link();
        json!({
            "connected": link.is_some(),
            "path": self.target.path().display().to_string(),
            "dev_socket": self.target.is_dev(),
            "pending_long": link.as_ref().map_or(0, |l| l.pending_long()),
            "latest": link.as_ref().map_or_else(Default::default, |l| Value::Object(l.latest())),
            "preflight": lock(&self.preflight).clone(),
            "last_error": lock(&self.last_error).as_ref().map(ConnectError::to_json),
        })
    }

    /// Connect now (if not already) and return the status.
    pub fn connect(&self) -> Value {
        let _ = self.ensure_link();
        self.status()
    }

    /// `{ok, result}` / `{ok:false, error}` for a short op; `{ok:true, id, pending:true}` for a long op, whose final response comes as
    /// the `helper://result` event. Refused locally (nothing connected, nothing written) when the op or its arguments are not allowlisted.
    pub fn call(&self, op: &str, args: &Value) -> Value {
        if let Err(r) = ops::check(op, args) {
            return error_json(r.code, &r.message, None);
        }
        let link = match self.ensure_link() {
            Ok(l) => l,
            Err(e) => return error_json("NOT_CONNECTED", &e.message, Some(json!({"kind": e.kind.as_str()}))),
        };
        match link.call(op, args, CALL_TIMEOUT) {
            Ok(Reply::Done(resp)) => public_response(&resp),
            Ok(Reply::Started(id)) => json!({"id": id, "ok": true, "pending": true}),
            Err(e) => {
                let (code, message) = e.code_message();
                error_json(&code, &message, None)
            }
        }
    }
}

struct TauriSink(AppHandle);

impl EventSink for TauriSink {
    fn emit(&self, name: &str, payload: Value) {
        let _ = self.0.emit(name, payload);
    }
}

/// Register the state with the app (called once from `setup`).
pub fn init(app: &AppHandle) {
    app.manage(HelperState::new(Arc::new(TauriSink(app.clone()))));
}

async fn blocking<F: FnOnce(&HelperState) -> Value + Send + 'static>(app: AppHandle, f: F) -> Value {
    // Connecting and waiting for answers block: never on the main thread.
    let joined = tauri::async_runtime::spawn_blocking(move || f(&app.state::<HelperState>())).await;
    joined.unwrap_or_else(|_| error_json("INTERNAL", "the helper link task failed", None))
}

#[tauri::command]
pub async fn helper_status(app: AppHandle) -> Value {
    blocking(app, |s| s.status()).await
}

#[tauri::command]
pub async fn helper_connect(app: AppHandle) -> Value {
    blocking(app, |s| s.connect()).await
}

#[tauri::command]
pub async fn helper_call(app: AppHandle, op: String, args: Option<Value>) -> Value {
    let args = match args {
        None | Some(Value::Null) => Value::Object(Default::default()),
        Some(v) => v,
    };
    blocking(app, move |s| s.call(&op, &args)).await
}
