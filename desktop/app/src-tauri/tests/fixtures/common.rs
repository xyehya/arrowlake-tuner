//! Shared test support, included with `#[path = "fixtures/common.rs"] mod common;` by the helper_*.rs integration tests.
#![allow(dead_code)]

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use arltuner_desktop_lib::helper::link::EventSink;
use serde_json::Value;

pub const T: Duration = Duration::from_secs(5);

/// Collects every event the link emits.
pub struct CollectSink {
    tx: Mutex<Sender<(String, Value)>>,
}

pub fn collect_sink() -> (Arc<CollectSink>, Receiver<(String, Value)>) {
    let (tx, rx) = channel();
    (Arc::new(CollectSink { tx: Mutex::new(tx) }), rx)
}

impl EventSink for CollectSink {
    fn emit(&self, name: &str, payload: Value) {
        let _ = self.tx.lock().unwrap().send((name.to_string(), payload));
    }
}

/// Next event called `name` within `T`; other events are skipped.
pub fn next_event(rx: &Receiver<(String, Value)>, name: &str) -> Value {
    let deadline = Instant::now() + T;
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        match rx.recv_timeout(left) {
            Ok((n, v)) if n == name => return v,
            Ok(_) => {}
            Err(_) => panic!("no {name} event within {T:?}"),
        }
    }
}

/// The client end for the link under test and the helper-side end for the test to play.
pub fn pair() -> (UnixStream, FakeHelper) {
    let (mine, theirs) = UnixStream::pair().unwrap();
    theirs.set_read_timeout(Some(T)).unwrap();
    (mine, FakeHelper { r: BufReader::new(theirs.try_clone().unwrap()), w: theirs })
}

pub struct FakeHelper {
    pub r: BufReader<UnixStream>,
    pub w: UnixStream,
}

impl FakeHelper {
    /// Read one request line as JSON.
    pub fn request(&mut self) -> Value {
        let mut line = String::new();
        self.r.read_line(&mut line).expect("request line");
        serde_json::from_str(&line).expect("request is JSON")
    }

    pub fn send(&mut self, v: &Value) {
        self.send_raw(format!("{v}\n").as_bytes());
    }

    pub fn send_raw(&mut self, b: &[u8]) {
        self.w.write_all(b).unwrap();
    }

    /// Answer the next request with `{"id": <its id>, "ok": true, "result": {}}` and return the request.
    pub fn answer_ok(&mut self) -> Value {
        let req = self.request();
        self.send(&serde_json::json!({"id": req["id"], "ok": true, "result": {}}));
        req
    }
}

static N: AtomicU32 = AtomicU32::new(0);

/// A private temp directory removed on drop.
pub struct TempDir(pub PathBuf);

impl TempDir {
    pub fn new(tag: &str) -> TempDir {
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().subsec_nanos();
        let p = std::env::temp_dir().join(format!("arltuner-g204-{tag}-{}-{nanos}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)));
        std::fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

pub fn fixture(name: &str) -> String {
    std::fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures").join(name)).unwrap()
}
