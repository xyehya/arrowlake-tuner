//! `dev-socket` seam against a scripted Rust listener: the dev target, reconnect-on-demand, lost ops. Run with `--features dev-socket`.
#![cfg(feature = "dev-socket")]

#[path = "fixtures/common.rs"]
mod common;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::Arc;

use arltuner_desktop_lib::helper::connect::{NodeObs, Probe, SysProbe, Target};
use arltuner_desktop_lib::helper::HelperState;
use common::*;
use serde_json::{json, Value};

fn state(path: &Path) -> (HelperState, std::sync::mpsc::Receiver<(String, Value)>) {
    let (sink, rx) = collect_sink();
    (HelperState::with_parts(Target::dev(path), Box::new(SysProbe), sink, T), rx)
}

fn serve_one(l: &UnixListener, then: impl FnOnce(&mut BufReader<UnixStream>, &mut UnixStream) -> Vec<Value>) -> Vec<Value> {
    let (s, _) = l.accept().unwrap();
    s.set_read_timeout(Some(T)).unwrap();
    let mut w = s.try_clone().unwrap();
    let mut r = BufReader::new(s);
    let mut seen = vec![];
    let mut line = String::new();
    r.read_line(&mut line).unwrap();
    let pre: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(pre["op"], "preflight");
    seen.push(pre.clone());
    writeln!(w, "{}", json!({"id": pre["id"], "ok": true, "result": {"writes_allowed": false}})).unwrap();
    seen.extend(then(&mut r, &mut w));
    seen
}

fn read_req(r: &mut BufReader<UnixStream>) -> Value {
    let mut line = String::new();
    r.read_line(&mut line).unwrap();
    serde_json::from_str(&line).unwrap()
}

#[test]
fn dev_target_connects_reports_a_lost_op_and_reconnects_on_the_next_action() {
    let dir = TempDir::new("dev");
    let sock = dir.0.join("h.sock");
    let listener = UnixListener::bind(&sock).unwrap();
    let (st, rx) = state(&sock);
    assert_eq!(st.status()["connected"], false);

    let server = std::thread::spawn(move || {
        // connection 1: preflight, then a request arrives and the helper dies without answering
        let first = serve_one(&listener, |r, _w| vec![read_req(r)]);
        // connection 2: preflight, then one short op
        let second = serve_one(&listener, |r, w| {
            let req = read_req(r);
            writeln!(w, "{}", json!({"id": req["id"], "ok": true, "result": {"rows": []}})).unwrap();
            let mut rest = String::new();
            let _ = r.read_line(&mut rest); // stay up until the client leaves
            vec![req]
        });
        (first, second)
    });

    // the first user action connects
    let r = st.call("profiles.list", &json!({}));
    assert_eq!(r["ok"], false); // connection 1's helper never answers this one: it reads it and hangs up
    assert_eq!(r["error"]["code"], "LINK_LOST");
    next_event(&rx, "helper://closed");
    assert_eq!(st.status()["connected"], false);
    assert_eq!(st.status()["dev_socket"], true);

    // the next user action reconnects (once) and works; the lost op is not replayed
    let r = st.call("audit.tail", &json!({"n": 3}));
    assert_eq!(r["ok"], true, "{r}");
    assert_eq!(st.status()["connected"], true);
    assert_eq!(st.status()["preflight"]["result"]["writes_allowed"], false);

    drop(st);
    let (first, second) = server.join().unwrap();
    assert_eq!(first.iter().map(|v| v["op"].as_str().unwrap()).collect::<Vec<_>>(), ["preflight", "profiles.list"]);
    assert_eq!(second.iter().map(|v| v["op"].as_str().unwrap()).collect::<Vec<_>>(), ["preflight", "audit.tail"]);
}

#[test]
fn a_long_op_returns_its_id_and_finishes_as_an_event() {
    let dir = TempDir::new("dev-long");
    let sock = dir.0.join("h.sock");
    let listener = UnixListener::bind(&sock).unwrap();
    let (st, rx) = state(&sock);
    let server = std::thread::spawn(move || {
        serve_one(&listener, |r, w| {
            let req = read_req(r);
            writeln!(w, "{}", json!({"event": "progress", "op_id": req["id"], "step": 1, "of": 1})).unwrap();
            writeln!(w, "{}", json!({"id": req["id"], "ok": true, "result": {"units": 12}})).unwrap();
            // keep the connection open until the client has what it needs
            let mut rest = String::new();
            let _ = r.read_line(&mut rest);
            vec![req]
        })
    });
    let r = st.call("tune.read", &json!({}));
    assert_eq!(r["ok"], true);
    assert_eq!(r["pending"], true);
    let id = r["id"].as_str().unwrap().to_string();
    let res = next_event(&rx, "helper://result");
    assert_eq!(res, json!({"id": id, "ok": true, "result": {"units": 12}}));
    drop(st);
    let seen = server.join().unwrap();
    assert_eq!(seen[1]["op"], "tune.read");
}

#[test]
fn unknown_ops_are_refused_without_connecting() {
    let dir = TempDir::new("dev-unknown");
    let (st, _rx) = state(&dir.0.join("never.sock")); // does not exist
    for (op, args) in [("rm", json!({})), ("preflight", json!({"x": 1}))] {
        let r = st.call(op, &args);
        assert_eq!(r["ok"], false);
        assert!(["UNKNOWN_OP", "BAD_REQUEST"].contains(&r["error"]["code"].as_str().unwrap()));
        assert!(r["error"]["message"].as_str().unwrap().starts_with("refused locally"));
    }
    assert_eq!(st.status()["last_error"], Value::Null); // no connection was even attempted
}

#[test]
fn a_missing_socket_is_reported_with_the_fix_and_remembered() {
    let dir = TempDir::new("dev-missing");
    let (st, _rx) = state(&dir.0.join("never.sock"));
    let r = st.call("preflight", &json!({}));
    assert_eq!(r["error"]["code"], "NOT_CONNECTED");
    assert_eq!(r["error"]["detail"]["kind"], "missing");
    assert!(r["error"]["message"].as_str().unwrap().contains("does not exist. Run: sudo bash install.sh"));
    assert_eq!(st.status()["last_error"]["kind"], "missing");
}

#[test]
fn the_dev_target_still_refuses_a_non_socket_and_a_node_owned_by_someone_else() {
    let dir = TempDir::new("dev-untrusted");
    let file = dir.0.join("plain");
    std::fs::write(&file, b"x").unwrap();
    let (st, _rx) = state(&file);
    assert_eq!(st.connect()["last_error"]["kind"], "untrusted");

    // a socket the probe says is root's: the dev target wants the current uid
    struct RootOwned;
    impl Probe for RootOwned {
        fn lstat(&self, _: &Path) -> std::io::Result<NodeObs> {
            Ok(NodeObs { is_socket: true, uid: 0 })
        }
        fn peer_uid(&self, _: &UnixStream) -> std::io::Result<u32> {
            Ok(0)
        }
    }
    let (sink, _rx2) = collect_sink();
    let st = HelperState::with_parts(Target::dev(dir.0.join("whatever")), Box::new(RootOwned), sink, T);
    assert_eq!(st.connect()["last_error"]["kind"], "untrusted");
}

#[test]
fn the_environment_variable_selects_the_dev_target() {
    std::env::set_var("ARLTUNER_DESKTOP_SOCKET", "/tmp/arltuner-g204-dev.sock");
    let t = Target::from_environment();
    assert_eq!(t.path(), Path::new("/tmp/arltuner-g204-dev.sock"));
    assert!(t.is_dev());
    std::env::remove_var("ARLTUNER_DESKTOP_SOCKET");
    assert!(!Target::from_environment().is_dev());
}

#[test]
fn concurrent_actions_share_one_connection() {
    let dir = TempDir::new("dev-concurrent");
    let sock = dir.0.join("h.sock");
    let listener = UnixListener::bind(&sock).unwrap();
    let (st, _rx) = state(&sock);
    let st = Arc::new(st);
    let server = std::thread::spawn(move || {
        serve_one(&listener, |r, w| {
            let mut seen = vec![];
            for _ in 0..4 {
                let req = read_req(r);
                writeln!(w, "{}", json!({"id": req["id"], "ok": true, "result": {}})).unwrap();
                seen.push(req);
            }
            seen
        })
    });
    let threads: Vec<_> = (0..4)
        .map(|_| {
            let st = st.clone();
            std::thread::spawn(move || st.call("mem.read", &json!({}))["ok"] == true)
        })
        .collect();
    assert!(threads.into_iter().all(|t| t.join().unwrap()));
    let seen = server.join().unwrap();
    let ids: std::collections::HashSet<_> = seen.iter().map(|v| v["id"].as_str().unwrap().to_string()).collect();
    assert_eq!(ids.len(), 5); // preflight + 4 distinct ids on the one connection
}
