//! Link behaviour over a socketpair with a scripted fake helper: allowlist, framing, matching, events, loss. No hardware.

#[path = "fixtures/common.rs"]
mod common;

use std::io::Read;
use std::time::Duration;

use arltuner_desktop_lib::helper::frame::MAX_LINE;
use arltuner_desktop_lib::helper::link::{public_response, CallError, Link, Reply, MAX_PENDING_LONG};
use common::*;
use serde_json::{json, Value};

fn start() -> (Link, FakeHelper, std::sync::mpsc::Receiver<(String, Value)>) {
    let (client, helper) = pair();
    let (sink, rx) = collect_sink();
    (Link::start(client, sink).unwrap(), helper, rx)
}

fn done(r: Result<Reply, CallError>) -> Value {
    match r {
        Ok(Reply::Done(v)) => v,
        other => panic!("expected a short answer, got {other:?}"),
    }
}

/// Plan on `link` the way the app does: a successful `tune.plan` answer carrying `plan_id`.
fn make_plan(link: &Link, helper: &mut FakeHelper, plan_id: &str) {
    std::thread::scope(|s| {
        let t = s.spawn(|| {
            let req = helper.request();
            assert_eq!(req["op"], "tune.plan");
            helper.send(&json!({"id": req["id"], "ok": true, "result": {"plan_id": plan_id}}));
        });
        assert_eq!(done(link.call("tune.plan", &json!({"intent": {"cores": []}}), T))["ok"], true);
        t.join().unwrap();
    });
}

#[test]
fn unknown_op_is_refused_locally_and_nothing_is_written() {
    let (link, mut helper, _rx) = start();
    for (op, args) in [
        ("rm", json!({})),
        ("mem.write", json!({})),
        ("power.set", json!({"pl1_w": 400})),
        ("", json!({})),
        ("PREFLIGHT", json!({})),
        ("preflight\n", json!({})),
    ] {
        match link.call(op, &args, T) {
            Err(CallError::Refused(r)) => assert_eq!(r.code, "UNKNOWN_OP", "{op}"),
            other => panic!("{op:?}: {other:?}"),
        }
    }
    // wrong argument shapes of allowed ops
    for (op, args) in [
        ("preflight", json!({"x": 1})),
        ("preflight", json!([])),
        ("preflight", json!(null)),
        ("audit.tail", json!({"n": 201})),
        ("audit.tail", json!({"n": 1.5})),
        ("audit.tail", json!({"n": true})),
        ("telemetry.start", json!({"interval_s": 0.1})),
        ("telemetry.start", json!({"interval_s": "1"})),
        ("tune.apply", json!({"plan_id": "x", "typed": "CONFIRM ABCDEF"})),
        ("tune.apply", json!({"plan_id": "a".repeat(64), "typed": "CONFIRM ABC1EF"})),
        ("tune.plan", json!({"intent": []})),
        ("tune.plan", json!({"intent": {"pad": "x".repeat(17_000)}})),
        ("profiles.save", json!({"name": "UPPER", "intent": {}})),
        ("curves.get", json!({"id": "nope"})),
        ("curves.update", json!({"id": "20261018T101500Z-a1b2"})),
        ("vf.measure", json!({"units": ["P1", "P1"]})),
        ("vf.measure", json!({"units": ["X1"]})),
        ("stress.start", json!({"kind": "cpu"})),
        ("stress.start", json!({"kind": "cpu", "duration_s": 29})),
        ("stress.start", json!({"kind": "gpu", "duration_s": 60})),
        ("stress.start", json!({"kind": "cpu", "duration_s": 60, "extra": 1})),
        ("cancel", json!({"op_id": "a b"})),
        ("tune.read", json!({"cpus": [1024]})),
        ("tune.read", json!({"cpus": []})),
    ] {
        match link.call(op, &args, T) {
            Err(CallError::Refused(r)) => assert_eq!(r.code, "BAD_REQUEST", "{op} {args}"),
            other => panic!("{op} {args}: {other:?}"),
        }
    }
    // The helper side saw nothing at all.
    helper.w.set_read_timeout(Some(Duration::from_millis(300))).unwrap();
    let mut buf = [0u8; 16];
    let got = helper.r.get_mut().read(&mut buf);
    assert!(matches!(got, Err(ref e) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut)), "{got:?}");
}

#[test]
fn allowed_shapes_pass_and_go_out_as_compact_lines_with_unique_ids() {
    let (link, mut helper, _rx) = start();
    let t = std::thread::spawn(move || {
        let a = helper.answer_ok();
        let b = helper.answer_ok();
        (a, b)
    });
    assert_eq!(done(link.call("preflight", &json!({}), T))["ok"], true);
    done(link.call("audit.tail", &json!({"n": 5}), T));
    let (a, b) = t.join().unwrap();
    assert_eq!(a["op"], "preflight");
    assert_eq!(b["args"], json!({"n": 5}));
    assert_ne!(a["id"], b["id"]);
    assert!(a["id"].as_str().unwrap().len() <= 64);
    assert_eq!(a.as_object().unwrap().len(), 3); // exactly id, op, args
}

#[test]
fn responses_are_matched_by_id_even_out_of_order() {
    let (link, mut helper, _rx) = start();
    let link = std::sync::Arc::new(link);
    let l1 = link.clone();
    let l2 = link.clone();
    let t1 = std::thread::spawn(move || done(l1.call("audit.tail", &json!({"n": 1}), T)));
    let first = helper.request();
    let t2 = std::thread::spawn(move || done(l2.call("audit.tail", &json!({"n": 2}), T)));
    let second = helper.request();
    // answer the second request first
    helper.send(&json!({"id": second["id"], "ok": true, "result": {"which": 2}}));
    helper.send(&json!({"id": first["id"], "ok": true, "result": {"which": 1}}));
    assert_eq!(t1.join().unwrap()["result"]["which"], 1);
    assert_eq!(t2.join().unwrap()["result"]["which"], 2);
}

#[test]
fn oversize_inbound_line_is_dropped_and_the_link_keeps_working() {
    let (link, mut helper, rx) = start();
    let t = std::thread::spawn(move || {
        let req = helper.request();
        let mut junk = vec![b'x'; MAX_LINE + 5000];
        junk.push(b'\n');
        helper.send_raw(&junk);
        // an exact-limit event (64 KiB with the newline) is still delivered
        let head = br#"{"event":"progress","pad":""#;
        let tail = b"\"}\n";
        let pad = MAX_LINE - head.len() - tail.len();
        let mut at_limit = head.to_vec();
        at_limit.extend(std::iter::repeat(b'p').take(pad));
        at_limit.extend_from_slice(tail);
        assert_eq!(at_limit.len(), MAX_LINE);
        helper.send_raw(&at_limit);
        helper.send(&json!({"id": req["id"], "ok": true, "result": {"after": "junk"}}));
        helper
    });
    let r = done(link.call("preflight", &json!({}), T));
    assert_eq!(r["result"]["after"], "junk");
    let ev = next_event(&rx, "helper://progress");
    assert_eq!(ev["pad"].as_str().unwrap().len(), MAX_LINE - 30);
    drop(t.join().unwrap());
}

#[test]
fn one_byte_over_the_limit_is_dropped() {
    let (link, mut helper, rx) = start();
    let head = br#"{"event":"progress","pad":""#;
    let tail = b"\"}\n";
    let mut over = head.to_vec();
    over.extend(std::iter::repeat(b'p').take(MAX_LINE + 1 - head.len() - tail.len()));
    over.extend_from_slice(tail);
    assert_eq!(over.len(), MAX_LINE + 1);
    helper.send_raw(&over);
    helper.send(&json!({"event": "state", "state": "idle"}));
    assert_eq!(next_event(&rx, "helper://state")["state"], "idle");
    while let Ok((name, _)) = rx.try_recv() {
        assert_ne!(name, "helper://progress");
    }
    assert!(link.alive());
}

#[test]
fn malformed_lines_never_kill_the_reader() {
    let (link, mut helper, rx) = start();
    helper.send_raw(b"this is not json\n");
    helper.send_raw(b"[1,2,3]\n");
    helper.send_raw(b"42\n");
    helper.send_raw(b"\n");
    helper.send_raw(b"\xff\xfe\xfd{\n"); // not UTF-8
    helper.send_raw(&[vec![b'['; 60_000], b"\n".to_vec()].concat()); // nesting far beyond the recursion limit
    helper.send_raw(br#"{"id":7,"ok":true}"#); // non-string id, no newline yet
    helper.send_raw(b"\n");
    helper.send_raw(br#"{"event":5}"#);
    helper.send_raw(b"\n");
    helper.send_raw(br#"{"event":"Has Upper"}"#);
    helper.send_raw(b"\n");
    helper.send_raw(br#"{"id":"never-sent","ok":true,"result":{}}"#);
    helper.send_raw(b"\n");
    helper.send(&json!({"event": "heartbeat", "state": "idle", "ops_used": 3}));
    let hb = next_event(&rx, "helper://heartbeat");
    assert_eq!(hb["ops_used"], 3);
    assert!(link.alive());
    assert_eq!(link.latest()["heartbeat"]["ops_used"], 3);
}

#[test]
fn events_are_published_as_helper_scheme_names_with_dots_mapped() {
    let (_link, mut helper, rx) = start();
    for ev in ["heartbeat", "state", "progress", "telemetry", "vf.point"] {
        helper.send(&json!({"event": ev, "op_id": null, "n": 1}));
    }
    // order is preserved
    let names: Vec<String> = (0..5).map(|_| rx.recv_timeout(T).unwrap().0).collect();
    assert_eq!(names, ["helper://heartbeat", "helper://state", "helper://progress", "helper://telemetry", "helper://vf-point"]);
}

#[test]
fn a_helper_line_cannot_impersonate_the_links_result_or_closed_events() {
    let (_link, mut helper, rx) = start();
    helper.send(&json!({"event": "result", "id": "d1", "ok": true}));
    helper.send(&json!({"event": "closed"}));
    helper.send(&json!({"event": "state", "state": "idle"}));
    let (name, _) = rx.recv_timeout(T).unwrap();
    assert_eq!(name, "helper://state");
}

#[test]
fn a_long_ops_result_is_delivered_as_an_event() {
    let (link, mut helper, rx) = start();
    let reply = link.call("tune.read", &json!({}), T).expect("sent");
    let Reply::Started(id) = reply else { panic!("long op must return its id") };
    let req = helper.request();
    assert_eq!(req["id"], id.as_str());
    assert_eq!(req["op"], "tune.read");
    assert_eq!(link.pending_long(), 1);
    helper.send(&json!({"event": "progress", "op_id": id, "step": 1, "of": 2}));
    helper.send(&json!({"id": id, "ok": true, "result": {"units": 12}}));
    let res = next_event(&rx, "helper://result");
    assert_eq!(res, json!({"id": id, "ok": true, "result": {"units": 12}}));
    assert_eq!(link.pending_long(), 0);
}

#[test]
fn error_results_reach_the_webview_without_unlisted_detail() {
    let (link, mut helper, rx) = start();
    make_plan(&link, &mut helper, &"a".repeat(64));
    let Ok(Reply::Started(id)) = link.call("tune.apply", &json!({"plan_id": "a".repeat(64), "typed": "CONFIRM ABCDEF"}), T) else { panic!() };
    helper.request();
    let raw = fixture("error_with_detail.json").replace("REPLACE", &id);
    helper.send_raw(raw.as_bytes());
    let res = next_event(&rx, "helper://result");
    assert_eq!(
        res,
        json!({"id": id, "ok": false, "error": {"code": "REFUSED", "message": "policy window exceeded",
                                                 "detail": {"check": "POLICY_RANGE", "state": "idle", "curve_id": "20261018T101500Z-a1b2"}}})
    );
}

#[test]
fn short_errors_are_filtered_too() {
    let (link, mut helper, _rx) = start();
    let t = std::thread::spawn(move || {
        let req = helper.request();
        let raw = fixture("error_with_detail.json").replace("REPLACE", req["id"].as_str().unwrap());
        helper.send_raw(raw.as_bytes());
        helper
    });
    let r = public_response(&done(link.call("profiles.list", &json!({}), T)));
    assert_eq!(r["ok"], false);
    assert_eq!(r["error"]["detail"], json!({"check": "POLICY_RANGE", "state": "idle", "curve_id": "20261018T101500Z-a1b2"}));
    assert!(r["error"].get("secret_path").is_none() && r["error"]["detail"].get("partial").is_none());
    drop(t.join().unwrap());
}

#[test]
fn a_long_op_in_flight_when_the_link_dies_is_reported_lost_not_replayed() {
    let (link, mut helper, rx) = start();
    let Ok(Reply::Started(id)) = link.call("vf.measure", &json!({}), T) else { panic!() };
    helper.request();
    drop(helper); // helper vanishes
    let res = next_event(&rx, "helper://result");
    assert_eq!(res["id"], id.as_str());
    assert_eq!(res["ok"], false);
    assert_eq!(res["error"]["code"], "LINK_LOST");
    next_event(&rx, "helper://closed");
    assert!(link.wait_finished(T));
    assert!(!link.alive());
    // later calls fail at once and write nothing
    assert!(matches!(link.call("preflight", &json!({}), T), Err(CallError::Unavailable)));
}

#[test]
fn a_short_op_in_flight_when_the_link_dies_fails() {
    let (link, mut helper, _rx) = start();
    let t = std::thread::spawn(move || {
        helper.request();
        drop(helper);
    });
    assert!(matches!(link.call("mem.read", &json!({}), T), Err(CallError::Unavailable)));
    t.join().unwrap();
}

#[test]
fn a_silent_helper_times_out_and_a_late_answer_is_dropped() {
    let (link, mut helper, rx) = start();
    let err = link.call("mem.read", &json!({}), Duration::from_millis(200));
    assert!(matches!(err, Err(CallError::Timeout)), "{err:?}");
    let req = helper.request();
    helper.send(&json!({"id": req["id"], "ok": true, "result": {}})); // late
    let t = std::thread::spawn(move || {
        helper.answer_ok();
        helper
    });
    assert_eq!(done(link.call("stress.list", &json!({}), T))["ok"], true);
    drop(t.join().unwrap());
    assert!(rx.try_iter().all(|(n, _)| n != "helper://result"));
}

#[test]
fn at_most_four_long_ops_run_at_once() {
    let (link, mut helper, _rx) = start();
    for _ in 0..MAX_PENDING_LONG {
        assert!(matches!(link.call("tune.read", &json!({}), T), Ok(Reply::Started(_))));
        helper.request();
    }
    assert!(matches!(link.call("tune.read", &json!({}), T), Err(CallError::TooManyLong)));
    // short ops still go
    let t = std::thread::spawn(move || {
        helper.answer_ok();
        helper
    });
    assert_eq!(done(link.call("preflight", &json!({}), T))["ok"], true);
    drop(t.join().unwrap());
}

#[test]
fn the_framed_refusal_line_is_kept_for_the_connect_error() {
    let (link, mut helper, _rx) = start();
    helper.send_raw(fixture("refused_client_busy.jsonl").as_bytes());
    drop(helper);
    assert!(link.wait_finished(T));
    assert_eq!(link.refusal().unwrap()["detail"]["check"], "CLIENT_BUSY");
}

#[test]
fn tune_apply_needs_a_plan_made_on_this_connection_and_spends_it() {
    let (link, mut helper, _rx) = start();
    let apply = |plan: &str| json!({"plan_id": plan, "typed": "CONFIRM ABCDEF"});
    let (known, other) = ("a".repeat(64), "b".repeat(64));

    // nothing planned yet: refused locally, nothing written
    match link.call("tune.apply", &apply(&known), T) {
        Err(CallError::Refused(r)) => assert_eq!(r.message, "refused locally: plan not made by this app"),
        other => panic!("{other:?}"),
    }
    make_plan(&link, &mut helper, &known);
    // a different (well-formed) plan id is still refused
    assert!(matches!(link.call("tune.apply", &apply(&other), T), Err(CallError::Refused(_))));
    // the planned one goes out ...
    assert!(matches!(link.call("tune.apply", &apply(&known), T), Ok(Reply::Started(_))));
    let req = helper.request();
    assert_eq!(req["op"], "tune.apply", "the refused applies wrote nothing");
    // ... and is spent
    assert!(matches!(link.call("tune.apply", &apply(&known), T), Err(CallError::Refused(_))));
}

#[test]
fn a_failed_plan_is_not_remembered_and_only_the_last_eight_plans_are() {
    let (link, mut helper, _rx) = start();
    std::thread::scope(|s| {
        let t = s.spawn(|| {
            let req = helper.request();
            helper.send(&json!({"id": req["id"], "ok": false, "error": {"code": "REFUSED", "message": "no"}}));
        });
        assert_eq!(done(link.call("tune.plan", &json!({"intent": {}}), T))["ok"], false);
        t.join().unwrap();
    });
    let plans: Vec<String> = (0..9).map(|i| format!("{i:x}").repeat(64)).collect();
    for p in &plans {
        make_plan(&link, &mut helper, p);
    }
    let apply = |plan: &str| json!({"plan_id": plan, "typed": "CONFIRM ABCDEF"});
    assert!(matches!(link.call("tune.apply", &apply(&plans[0]), T), Err(CallError::Refused(_))), "oldest of nine was dropped");
    assert!(matches!(link.call("tune.apply", &apply(&plans[8]), T), Ok(Reply::Started(_))));
}

#[test]
fn request_ids_are_not_reused_across_links() {
    let mut seen = std::collections::HashSet::new();
    for _ in 0..3 {
        let (link, mut helper, _rx) = start();
        for _ in 0..2 {
            let t = std::thread::scope(|s| {
                let t = s.spawn(|| helper.answer_ok());
                done(link.call("preflight", &json!({}), T));
                t.join().unwrap()
            });
            assert!(seen.insert(t["id"].as_str().unwrap().to_string()), "id repeated");
        }
    }
}
