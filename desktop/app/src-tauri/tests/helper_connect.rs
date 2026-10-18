//! Root checks, refusal mapping and the handshake (PLAN §3.1). No hardware and no real helper: fake observations and socketpairs.
//! The installed target (`/run/arltuner/helper.sock`) is only ever used with a probe that fails before any connect.

#[path = "fixtures/common.rs"]
mod common;

use std::io;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

use arltuner_desktop_lib::helper::connect::{check_node, check_peer, handshake, Kind, NodeObs, Probe, Target, HELPER_SOCKET};
use common::*;

struct Fake {
    node: io::Result<NodeObs>,
    peer: io::Result<u32>,
}

fn clone_err<T: Copy>(r: &io::Result<T>) -> io::Result<T> {
    match r {
        Ok(v) => Ok(*v),
        Err(e) => Err(io::Error::from(e.kind())),
    }
}

impl Probe for Fake {
    fn lstat(&self, _: &Path) -> io::Result<NodeObs> {
        clone_err(&self.node)
    }
    fn peer_uid(&self, _: &UnixStream) -> io::Result<u32> {
        clone_err(&self.peer)
    }
}

fn fake(node: io::Result<NodeObs>, peer: io::Result<u32>) -> Fake {
    Fake { node, peer }
}

#[test]
fn missing_node_names_the_install_fix() {
    let e = check_node(&Target::installed(), &fake(Err(io::ErrorKind::NotFound.into()), Ok(0))).unwrap_err();
    assert_eq!(e.kind, Kind::Missing);
    assert_eq!(
        e.message,
        "the helper is not installed or not started: /run/arltuner/helper.sock does not exist. Run: sudo bash install.sh (it enables arltuner-helper.socket)"
    );
}

#[test]
fn unreadable_node_is_missing_with_the_os_reason() {
    let e = check_node(&Target::installed(), &fake(Err(io::ErrorKind::PermissionDenied.into()), Ok(0))).unwrap_err();
    assert_eq!(e.kind, Kind::Missing);
    assert_eq!(e.message, "cannot look at /run/arltuner/helper.sock: permission denied");
}

#[test]
fn a_node_that_is_not_a_socket_is_refused() {
    let e = check_node(&Target::installed(), &fake(Ok(NodeObs { is_socket: false, uid: 0 }), Ok(0))).unwrap_err();
    assert_eq!(e.kind, Kind::Untrusted);
    assert_eq!(e.message, "/run/arltuner/helper.sock is not a socket owned by root; not connecting to it");
}

#[test]
fn a_socket_not_owned_by_root_is_refused() {
    for uid in [1000, 1] {
        let e = check_node(&Target::installed(), &fake(Ok(NodeObs { is_socket: true, uid }), Ok(0))).unwrap_err();
        assert_eq!(e.kind, Kind::Untrusted);
    }
    assert!(check_node(&Target::installed(), &fake(Ok(NodeObs { is_socket: true, uid: 0 }), Ok(0))).is_ok());
}

#[test]
fn a_listener_that_is_not_root_is_refused() {
    let (client, _server) = UnixStream::pair().unwrap();
    let e = check_peer(&Target::installed(), &fake(Ok(NodeObs { is_socket: true, uid: 0 }), Ok(1000)), &client).unwrap_err();
    assert_eq!(e.kind, Kind::Untrusted);
    assert_eq!(e.message, "the process listening on /run/arltuner/helper.sock is uid 1000, not root; not talking to it");
    let e = check_peer(&Target::installed(), &fake(Ok(NodeObs { is_socket: true, uid: 0 }), Err(io::ErrorKind::PermissionDenied.into())), &client).unwrap_err();
    assert_eq!(e.kind, Kind::Untrusted);
    assert_eq!(e.message, "cannot verify who listens on /run/arltuner/helper.sock (permission denied); not talking to it");
    assert!(check_peer(&Target::installed(), &fake(Ok(NodeObs { is_socket: true, uid: 0 }), Ok(0)), &client).is_ok());
}

#[test]
fn the_real_peercred_of_a_socketpair_is_us_and_not_root() {
    use arltuner_desktop_lib::helper::connect::SysProbe;
    let (client, _server) = UnixStream::pair().unwrap();
    let uid = SysProbe.peer_uid(&client).unwrap();
    assert_eq!(uid, arltuner_desktop_lib::effective_uid());
    assert_ne!(uid, 0);
    // so the installed target (root only) rejects a same-user listener
    assert_eq!(check_peer(&Target::installed(), &SysProbe, &client).unwrap_err().kind, Kind::Untrusted);
}

#[test]
fn installed_target_is_the_fixed_root_socket() {
    let t = Target::installed();
    assert_eq!(t.path(), Path::new(HELPER_SOCKET));
    assert!(!t.is_dev());
}

#[cfg(not(feature = "dev-socket"))]
#[test]
fn without_the_dev_feature_the_environment_variable_is_ignored() {
    std::env::set_var("ARLTUNER_DESKTOP_SOCKET", "/tmp/not-the-helper.sock");
    let t = Target::from_environment();
    assert_eq!(t.path(), Path::new(HELPER_SOCKET));
    assert!(!t.is_dev());
}

// ---- handshake over a socketpair: the helper's framed refusals ----------------------------------------------------------------------
fn refused_by(fixture_name: &str, who: &str) -> arltuner_desktop_lib::helper::connect::ConnectError {
    let (client, mut helper) = pair();
    helper.send_raw(fixture(fixture_name).as_bytes());
    drop(helper); // the service closes after its one line
    let (sink, _rx) = collect_sink();
    handshake(client, sink, who, T).err().expect("refused")
}

#[test]
fn client_busy_is_mapped() {
    let e = refused_by("refused_client_busy.jsonl", "alice");
    assert_eq!(e.kind, Kind::Busy);
    assert_eq!(e.message, "another client is connected to the helper (another arltuner GUI?). Close it, then start this one again.");
}

#[test]
fn access_denied_is_mapped_with_the_add_user_fix() {
    let e = refused_by("refused_access_denied.jsonl", "alice");
    assert_eq!(e.kind, Kind::NotAllowed);
    assert_eq!(
        e.message,
        "the helper refused this user: not in the allowlist (/etc/arltuner/helper.toml). Run: sudo bash install.sh --add-user alice"
    );
}

#[test]
fn other_refusal_shows_the_helpers_reason() {
    let e = refused_by("refused_other.jsonl", "alice");
    assert_eq!(e.kind, Kind::NotAllowed);
    assert_eq!(e.message, "the helper refused the connection: helper is shutting down");
}

#[test]
fn eof_without_a_refusal_is_closed() {
    let (client, helper) = pair();
    drop(helper);
    let (sink, _rx) = collect_sink();
    let e = handshake(client, sink, "alice", T).err().unwrap();
    assert_eq!(e.kind, Kind::Closed);
    assert_eq!(e.message, "the helper closed the connection right away (it may have refused to start). See: journalctl -u arltuner-helper -n 30");
}

#[test]
fn silence_times_out() {
    let (client, _helper) = pair(); // kept open, never answers
    let (sink, _rx) = collect_sink();
    let e = handshake(client, sink, "alice", Duration::from_secs(1)).err().unwrap();
    assert_eq!(e.kind, Kind::Timeout);
    assert_eq!(e.message, "the helper did not answer within 1 s. See: journalctl -u arltuner-helper -n 30");
}

#[test]
fn a_preflight_answer_completes_the_handshake_even_when_it_reports_a_problem() {
    let (client, mut helper) = pair();
    let t = std::thread::spawn(move || {
        let req = helper.request();
        assert_eq!(req["op"], "preflight");
        assert_eq!(req["args"], serde_json::json!({}));
        helper.send(&serde_json::json!({"id": req["id"], "ok": false, "error": {"code": "PREFLIGHT_FAILED", "message": "mode is read-only"}}));
        helper // keep the socket open until joined
    });
    let (sink, _rx) = collect_sink();
    let (link, pre) = handshake(client, sink, "alice", T).expect("connected");
    assert!(link.alive());
    assert_eq!(pre["error"]["code"], "PREFLIGHT_FAILED");
    drop(t.join().unwrap());
}
