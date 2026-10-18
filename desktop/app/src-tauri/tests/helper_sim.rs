//! Integration: the link against the Python simulator helper (`tools/arltuner-helper.py --sim --socket`) on a temp socket.
//! Simulator only: every path (socket, audit log, lock, state dir, helper.toml) is a temp path; nothing touches /run/arltuner or hardware.
//! Needs `python3` and `--features dev-socket`.
#![cfg(feature = "dev-socket")]

#[path = "fixtures/common.rs"]
mod common;

use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use arltuner_desktop_lib::helper::connect::{SysProbe, Target};
use arltuner_desktop_lib::helper::HelperState;
use common::*;
use serde_json::json;

struct Sim {
    child: Child,
    sock: PathBuf,
    _dir: TempDir,
}

impl Drop for Sim {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn user_name() -> String {
    let out = Command::new("id").arg("-un").output().expect("id -un");
    String::from_utf8(out.stdout).unwrap().trim().to_string()
}

fn spawn_sim(admitted_user: &str) -> Sim {
    let dir = TempDir::new("sim");
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap();
    let toml = dir.0.join("helper.toml");
    std::fs::write(&toml, format!("[access]\nusers = [\"{admitted_user}\"]\n")).unwrap();
    std::fs::set_permissions(&toml, std::fs::Permissions::from_mode(0o600)).unwrap();
    let sock = dir.0.join("h.sock");
    let state = dir.0.join("state");
    std::fs::create_dir(&state).unwrap();
    let child = Command::new("python3")
        .arg(root.join("tools/arltuner-helper.py"))
        .args(["--sim", "--socket", "--socket-path"])
        .arg(&sock)
        .arg("--audit")
        .arg(dir.0.join("audit.jsonl"))
        .arg("--lock")
        .arg(dir.0.join("lock"))
        .arg("--state-dir")
        .arg(&state)
        .arg("--helper-toml")
        .arg(&toml)
        .current_dir(&dir.0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("python3 starts the simulator");
    let mut sim = Sim { child, sock, _dir: dir };
    let deadline = Instant::now() + Duration::from_secs(10);
    while !sim.sock.exists() {
        if let Some(status) = sim.child.try_wait().unwrap() {
            panic!("the simulator exited early: {status}");
        }
        assert!(Instant::now() < deadline, "the simulator did not create its socket in time");
        std::thread::sleep(Duration::from_millis(50));
    }
    sim
}

#[test]
fn preflight_then_telemetry_events_from_the_simulator() {
    let sim = spawn_sim(&user_name());
    let (sink, rx) = collect_sink();
    let st = HelperState::with_parts(Target::dev(&sim.sock), Box::new(SysProbe), sink, Duration::from_secs(20));

    let status = st.connect();
    assert_eq!(status["connected"], true, "{status}");
    assert_eq!(status["dev_socket"], true);
    assert_eq!(status["preflight"]["ok"], true, "{status}");

    let pre = st.call("preflight", &json!({}));
    assert_eq!(pre["ok"], true, "{pre}");
    assert!(pre["result"].is_object());

    // an op outside the allowlist never reaches the simulator
    let bad = st.call("mailbox.write", &json!({"word": 1}));
    assert_eq!(bad["error"]["code"], "UNKNOWN_OP");

    let started = st.call("telemetry.start", &json!({"interval_s": 0.2}));
    assert_eq!(started["ok"], true, "{started}");
    let first = next_event(&rx, "helper://telemetry");
    let second = next_event(&rx, "helper://telemetry");
    assert!(first["values"].is_object() && second["values"].is_object(), "{first} {second}");
    assert_ne!(first["t"], second["t"]);

    let stopped = st.call("telemetry.stop", &json!({}));
    assert_eq!(stopped["ok"], true, "{stopped}");
    assert_eq!(st.status()["connected"], true);
}

#[test]
fn a_user_the_simulator_does_not_admit_gets_the_add_user_fix() {
    let sim = spawn_sim("nobody-at-all-g204");
    let (sink, _rx) = collect_sink();
    let st = HelperState::with_parts(Target::dev(&sim.sock), Box::new(SysProbe), sink, Duration::from_secs(20));
    let status = st.connect();
    assert_eq!(status["connected"], false);
    assert_eq!(status["last_error"]["kind"], "not_allowed", "{status}");
    assert!(status["last_error"]["message"].as_str().unwrap().contains("sudo bash install.sh --add-user "));
}
