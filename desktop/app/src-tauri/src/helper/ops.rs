//! Op allowlist with argument-shape checks (PLAN §3.1, defence in depth: the helper re-validates everything).
//!
//! Mirrors `OPS` and its validators in `src/arltuner/web/server.py:57-171` (same op names, same shapes, same bounds). Python's
//! `type(x) is int` becomes "a JSON integer" (`Number` without fraction; booleans and floats are not integers). Nothing outside this table
//! ever reaches the socket: [`crate::helper::link::Link`] calls [`check`] on every request it writes.
//! `power.*` mirrors `src/arltuner/helper/ops_power.py` and `runtime.*` mirrors `ops_runtime.py` (PLAN §6). Core parking is held (D-048):
//! a `parking` key is refused here as well as by the helper.

use serde_json::{Map, Value};

type Args = Map<String, Value>;

/// A local refusal: `code` is a helper protocol code (`UNKNOWN_OP`, `BAD_REQUEST`), `message` says what was wrong.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub code: &'static str,
    pub message: String,
}

/// What the allowlist knows about an accepted op.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OpInfo {
    /// Long ops answer at the end (events meanwhile): the link returns `{id}` at once and delivers the result as `helper://result`.
    pub long: bool,
}

/// Largest `intent` (compact JSON, bytes) the allowlist forwards (server.py `_tune_plan`: 16 KiB).
pub const MAX_INTENT_BYTES: usize = 16 * 1024;
const MAX_UNITS: usize = 256;
/// CPU numbers a `runtime.plan` cap may name are 0..1023 (ops_runtime.py `_CPU_MAX`).
const CPU_MAX: usize = 1024;

/// Every allowed op name, in the order of `server.py` `OPS`.
pub const ALLOWED_OPS: &[&str] = &[
    "preflight", "audit.tail", "telemetry.start", "telemetry.stop", "cancel", "vf.dump", "tune.read", "tune.plan", "tune.apply",
    "profiles.list", "profiles.save", "profiles.accept", "profiles.fingerprint", "profiles.get", "tune.last", "vf.measure", "curves.list",
    "curves.get", "curves.update", "curves.delete", "mem.read", "stress.start", "stress.stop", "stress.list", "power.read", "power.plan",
    "power.apply", "runtime.read", "runtime.plan", "runtime.apply",
];

/// A family of plans: `*.apply` is bound to plan ids this connection got from the same kind's `*.plan`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlanKind {
    Tune,
    Power,
    Runtime,
}

impl PlanKind {
    pub const COUNT: usize = 3;

    pub fn index(self) -> usize {
        self as usize
    }
}

/// `Some((kind, is_apply))` for a `*.plan` (`false`) or `*.apply` (`true`) op.
pub fn plan_role(op: &str) -> Option<(PlanKind, bool)> {
    match op {
        "tune.plan" => Some((PlanKind::Tune, false)),
        "tune.apply" => Some((PlanKind::Tune, true)),
        "power.plan" => Some((PlanKind::Power, false)),
        "power.apply" => Some((PlanKind::Power, true)),
        "runtime.plan" => Some((PlanKind::Runtime, false)),
        "runtime.apply" => Some((PlanKind::Runtime, true)),
        _ => None,
    }
}

/// Judge a request locally: the op must be allowlisted and `args` (a JSON object) must have the op's shape.
pub fn check(op: &str, args: &Value) -> Result<OpInfo, Refusal> {
    let Some(a) = args.as_object() else {
        return Err(refuse("BAD_REQUEST", "args must be a JSON object"));
    };
    let (ok, long): (bool, bool) = match op {
        "preflight" | "telemetry.stop" | "profiles.list" | "profiles.fingerprint" | "tune.last" | "curves.list" | "mem.read" | "stress.stop"
        | "stress.list" | "runtime.read" => (a.is_empty(), false),
        "power.read" => (a.is_empty(), true), // guarded MSR reads: a long op in the helper
        "audit.tail" => (audit_tail(a), false),
        "telemetry.start" => (telemetry_start(a), false),
        "cancel" => (cancel(a), false),
        "vf.dump" | "tune.read" => (vf_dump(a), true),
        "tune.plan" => (tune_plan(a), false),
        "tune.apply" | "power.apply" | "runtime.apply" => (apply(a), true),
        "power.plan" => (power_plan(a), false),
        "runtime.plan" => (runtime_plan(a), false),
        "profiles.save" => (profiles_save(a), false),
        "profiles.accept" => (profiles_accept(a), false),
        "profiles.get" => (profiles_get(a), false),
        "vf.measure" => (vf_measure(a), true),
        "curves.get" => (curves_get(a), false),
        "curves.update" => (curves_update(a), false),
        "curves.delete" => (curves_delete(a), false),
        "stress.start" => (stress_start(a), true),
        _ => return Err(refuse("UNKNOWN_OP", &format!("op {:?} is not allowed", clip(op)))),
    };
    if ok {
        Ok(OpInfo { long })
    } else {
        Err(refuse("BAD_REQUEST", &format!("arguments of {op} have the wrong shape")))
    }
}

fn refuse(code: &'static str, message: &str) -> Refusal {
    Refusal { code, message: format!("refused locally: {message}") }
}

fn clip(s: &str) -> String {
    s.chars().take(40).collect()
}

// ---- small matchers (the regexes of server.py, by hand: no regex crate) ------------------------------------------------------------
fn all_chars(s: &str, min: usize, max: usize, ok: impl Fn(char) -> bool) -> bool {
    let n = s.chars().count();
    (min..=max).contains(&n) && s.chars().all(ok)
}

fn req_id(s: &str) -> bool {
    all_chars(s, 1, 64, |c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
}

fn hex64(s: &str) -> bool {
    all_chars(s, 64, 64, |c| matches!(c, '0'..='9' | 'a'..='f'))
}

fn typed(s: &str) -> bool {
    s.strip_prefix("CONFIRM ").is_some_and(|rest| all_chars(rest, 6, 6, |c| matches!(c, 'A'..='Z' | '2'..='9')))
}

fn profile_name(s: &str) -> bool {
    all_chars(s, 1, 64, |c| matches!(c, 'a'..='z' | '0'..='9' | '.' | '_' | '-'))
}

fn curve_id_str(s: &str) -> bool {
    // 8 digits, 'T', 6 digits, 'Z', '-', 4 of [a-z0-9]
    let b = s.as_bytes();
    b.len() == 21
        && b[..8].iter().all(u8::is_ascii_digit)
        && b[8] == b'T'
        && b[9..15].iter().all(u8::is_ascii_digit)
        && b[15] == b'Z'
        && b[16] == b'-'
        && b[17..].iter().all(|c| c.is_ascii_digit() || c.is_ascii_lowercase())
}

fn unit_str(s: &str) -> bool {
    let digits = s.strip_prefix("EC").or_else(|| s.strip_prefix('P'));
    digits.is_some_and(|d| (1..=3).contains(&d.len()) && d.bytes().all(|c| c.is_ascii_digit()))
}

// ---- value checks -------------------------------------------------------------------------------------------------------------------
fn int_in(v: &Value, lo: i64, hi: i64) -> bool {
    v.as_i64().is_some_and(|x| (lo..=hi).contains(&x))
}

fn str_is(v: &Value, f: fn(&str) -> bool) -> bool {
    v.as_str().is_some_and(f)
}

fn keys_within(a: &Args, allowed: &[&str]) -> bool {
    a.keys().all(|k| allowed.contains(&k.as_str()))
}

fn keys_exactly(a: &Args, wanted: &[&str]) -> bool {
    a.len() == wanted.len() && wanted.iter().all(|k| a.contains_key(*k))
}

fn intent_ok(v: &Value) -> bool {
    v.is_object() && serde_json::to_string(v).is_ok_and(|s| s.len() <= MAX_INTENT_BYTES)
}

fn unit_list(v: &Value) -> bool {
    let Some(list) = v.as_array() else { return false };
    if list.is_empty() || list.len() > MAX_UNITS {
        return false;
    }
    let mut seen: Vec<&str> = Vec::with_capacity(list.len());
    for item in list {
        match item.as_str() {
            Some(s) if unit_str(s) && !seen.contains(&s) => seen.push(s),
            _ => return false,
        }
    }
    true
}

fn name_ok(v: &Value) -> bool {
    // server.py `_name`: 1..64 characters once stripped, printable. Python's isprintable() is approximated: no control character and no
    // whitespace other than the plain space (the helper is the authority).
    let Some(s) = v.as_str() else { return false };
    (1..=64).contains(&s.trim().chars().count()) && s.chars().all(|c| !c.is_control() && (c == ' ' || !c.is_whitespace()))
}

// ---- per-op argument validators (server.py names in comments) -----------------------------------------------------------------------
fn audit_tail(a: &Args) -> bool {
    keys_within(a, &["n"]) && a.get("n").map_or(true, |n| int_in(n, 1, 200))
}

fn vf_dump(a: &Args) -> bool {
    if !keys_within(a, &["cpus", "all_e"]) || a.get("all_e").is_some_and(|v| !v.is_boolean()) {
        return false;
    }
    match a.get("cpus") {
        None | Some(Value::Null) => true,
        Some(Value::Array(c)) => (1..=64).contains(&c.len()) && c.iter().all(|x| int_in(x, 0, 1023)),
        Some(_) => false,
    }
}

fn telemetry_start(a: &Args) -> bool {
    keys_within(a, &["interval_s"])
        && a.get("interval_s").map_or(true, |v| v.is_number() && v.as_f64().is_some_and(|f| (0.2..=60.0).contains(&f)))
}

fn cancel(a: &Args) -> bool {
    keys_within(a, &["op_id"]) && a.get("op_id").map_or(true, |v| str_is(v, req_id))
}

fn apply(a: &Args) -> bool {
    keys_exactly(a, &["plan_id", "typed"]) && str_is(&a["plan_id"], hex64) && str_is(&a["typed"], typed)
}

fn tune_plan(a: &Args) -> bool {
    keys_exactly(a, &["intent"]) && intent_ok(&a["intent"])
}

fn profiles_save(a: &Args) -> bool {
    keys_exactly(a, &["name", "intent"]) && str_is(&a["name"], profile_name) && intent_ok(&a["intent"])
}

fn profiles_accept(a: &Args) -> bool {
    (keys_exactly(a, &["name"]) && str_is(&a["name"], profile_name)) || (keys_exactly(a, &["from"]) && a["from"].as_str() == Some("read"))
}

fn profiles_get(a: &Args) -> bool {
    keys_exactly(a, &["name"]) && str_is(&a["name"], profile_name)
}

fn vf_measure(a: &Args) -> bool {
    keys_within(a, &["mode", "units", "name", "baseline"])
        && a.get("mode").map_or(true, |m| matches!(m.as_str(), Some("quick" | "dense")))
        && a.get("baseline").map_or(true, Value::is_boolean)
        && a.get("units").map_or(true, unit_list)
        && a.get("name").map_or(true, name_ok)
}

fn stress_start(a: &Args) -> bool {
    if !keys_within(a, &["kind", "method", "units", "ratio_cap", "duration_s", "memory_mb"])
        || !matches!(a.get("kind").and_then(Value::as_str), Some("cpu" | "memory"))
    {
        return false;
    }
    a.get("duration_s").is_some_and(|d| int_in(d, 30, 3600))
        && a.get("method").map_or(true, |m| matches!(m.as_str(), Some("integer" | "vector-math" | "cache")))
        && a.get("units").map_or(true, unit_list)
        && a.get("ratio_cap").map_or(true, |c| c.is_null() || int_in(c, 8, 60))
        && a.get("memory_mb").map_or(true, |m| int_in(m, 256, 1 << 20))
}

/// ops_power.py `_validate_power_plan`: exactly one of `power` ({pl1_w?, pl2_w?, tau_s?}: numbers, at least one set) or `revert: true`.
fn power_plan(a: &Args) -> bool {
    if !keys_within(a, &["power", "revert"]) || a.contains_key("power") == a.contains_key("revert") {
        return false;
    }
    if let Some(r) = a.get("revert") {
        return r == &Value::Bool(true);
    }
    let Some(p) = a["power"].as_object() else { return false };
    keys_within(p, &["pl1_w", "pl2_w", "tau_s"]) && p.values().all(|v| v.is_null() || v.is_number()) && p.values().any(Value::is_number)
}

fn ratio(v: &Value) -> bool {
    int_in(v, 1, 255)
}

fn cpu_key(k: &str) -> bool {
    all_chars(k, 1, 4, |c| c.is_ascii_digit()) && k.parse::<usize>().is_ok_and(|n| n < CPU_MAX)
}

/// ops_runtime.py `_validate_runtime_plan`: `{revert: true}` alone, or `caps` ({"cpu": ratio}) and/or `uncore` ({min_ratio?, max_ratio?})
/// with at least one value set. Parking is not supported (D-048): any other key is refused.
fn runtime_plan(a: &Args) -> bool {
    if !keys_within(a, &["caps", "uncore", "revert"]) {
        return false;
    }
    if let Some(r) = a.get("revert") {
        return a.len() == 1 && r == &Value::Bool(true);
    }
    let caps = match a.get("caps") {
        None | Some(Value::Null) => false,
        Some(Value::Object(c)) if !c.is_empty() && c.len() <= CPU_MAX && c.iter().all(|(k, v)| cpu_key(k) && ratio(v)) => true,
        Some(_) => return false,
    };
    let uncore = match a.get("uncore") {
        None | Some(Value::Null) => false,
        Some(Value::Object(u)) if keys_within(u, &["min_ratio", "max_ratio"]) && u.values().all(|v| v.is_null() || ratio(v)) => {
            u.values().any(|v| !v.is_null())
        }
        Some(_) => return false,
    };
    caps || uncore
}

fn curves_get(a: &Args) -> bool {
    keys_within(a, &["id", "units"]) && a.get("id").is_some_and(|v| str_is(v, curve_id_str)) && a.get("units").map_or(true, unit_list)
}

fn curves_update(a: &Args) -> bool {
    keys_within(a, &["id", "name", "baseline"])
        && a.get("id").is_some_and(|v| str_is(v, curve_id_str))
        && (a.contains_key("name") || a.contains_key("baseline"))
        && a.get("name").map_or(true, name_ok)
        && a.get("baseline").map_or(true, Value::is_boolean)
}

fn curves_delete(a: &Args) -> bool {
    keys_exactly(a, &["id"]) && str_is(&a["id"], curve_id_str)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn every_listed_op_is_known_to_check() {
        for op in ALLOWED_OPS {
            // an op with args that are surely wrong is BAD_REQUEST (known op), never UNKNOWN_OP; {} may be fine.
            let e = check(op, &json!({"zzz": 1}));
            assert!(e.as_ref().err().map_or(true, |r| r.code == "BAD_REQUEST"), "{op}: {e:?}");
        }
        assert_eq!(check("rm -rf", &json!({})).unwrap_err().code, "UNKNOWN_OP");
        assert_eq!(ALLOWED_OPS.len(), 30);
    }

    fn ok(op: &str, args: Value) -> bool {
        check(op, &args).is_ok()
    }

    #[test]
    fn power_shapes() {
        assert!(ok("power.read", json!({})) && check("power.read", &json!({})).unwrap().long);
        assert!(!ok("power.read", json!({"x": 1})));
        assert!(ok("power.plan", json!({"power": {"pl1_w": 250, "pl2_w": 295.5, "tau_s": 28}})));
        assert!(ok("power.plan", json!({"power": {"pl1_w": 200, "tau_s": null}})));
        assert!(ok("power.plan", json!({"revert": true})));
        assert!(!check("power.plan", &json!({"revert": true})).unwrap().long, "a plan is answered in line so the link can remember its id");
        for bad in [
            json!({}),
            json!({"revert": false}),
            json!({"revert": 1}),
            json!({"revert": true, "power": {"pl1_w": 200}}),
            json!({"power": {}}),
            json!({"power": {"pl1_w": null}}),
            json!({"power": {"pl1_w": "200"}}),
            json!({"power": {"pl1_w": true}}),
            json!({"power": {"pl3_w": 200}}),
            json!({"power": [200]}),
            json!({"power": {"pl1_w": 200}, "extra": 1}),
            json!({"intent": {"power": {"pl1_w": 200}}}),
        ] {
            assert!(!ok("power.plan", bad.clone()), "{bad}");
        }
    }

    #[test]
    fn runtime_shapes() {
        assert!(ok("runtime.read", json!({})) && !ok("runtime.read", json!({"cpus": [0]})));
        assert!(ok("runtime.plan", json!({"caps": {"0": 40, "23": 255}})));
        assert!(ok("runtime.plan", json!({"uncore": {"min_ratio": 8, "max_ratio": 40}})));
        assert!(ok("runtime.plan", json!({"caps": {"1": 30}, "uncore": {"max_ratio": null}})));
        assert!(ok("runtime.plan", json!({"caps": null, "uncore": {"max_ratio": 36}})));
        assert!(ok("runtime.plan", json!({"revert": true})));
        for bad in [
            json!({}),
            json!({"parking": {"3": false}}),
            json!({"caps": {"1": 30}, "parking": {"3": false}}),
            json!({"parking": null, "caps": {"1": 30}}),
            json!({"revert": true, "caps": {"1": 30}}),
            json!({"revert": false}),
            json!({"caps": {}}),
            json!({"caps": null}),
            json!({"caps": [30]}),
            json!({"caps": {"x": 30}}),
            json!({"caps": {"-1": 30}}),
            json!({"caps": {"+1": 30}}),
            json!({"caps": {"1024": 30}}),
            json!({"caps": {"12345": 30}}),
            json!({"caps": {"": 30}}),
            json!({"caps": {"1": 0}}),
            json!({"caps": {"1": 256}}),
            json!({"caps": {"1": 30.5}}),
            json!({"caps": {"1": "30"}}),
            json!({"caps": {"1": null}}),
            json!({"uncore": {}}),
            json!({"uncore": {"min_ratio": null}}),
            json!({"uncore": {"lo": 8}}),
            json!({"uncore": {"min_ratio": 0}}),
            json!({"uncore": 8}),
            json!({"uncore": {"min_khz": 800_000}}),
        ] {
            assert!(!ok("runtime.plan", bad.clone()), "{bad}");
        }
    }

    #[test]
    fn apply_shapes_and_plan_roles() {
        let good = json!({"plan_id": "a".repeat(64), "typed": "CONFIRM ABCDEF"});
        for op in ["tune.apply", "power.apply", "runtime.apply"] {
            assert!(check(op, &good).unwrap().long, "{op}");
            assert!(!ok(op, json!({"plan_id": "a".repeat(64)})), "{op}");
            assert!(!ok(op, json!({"plan_id": "x", "typed": "CONFIRM ABCDEF"})), "{op}");
            let mut extra = good.clone();
            extra["power"] = json!({"pl1_w": 400});
            assert!(!ok(op, extra), "{op}: unknown key");
        }
        assert_eq!(plan_role("power.plan"), Some((PlanKind::Power, false)));
        assert_eq!(plan_role("runtime.apply"), Some((PlanKind::Runtime, true)));
        assert_eq!(plan_role("power.read"), None);
        let idx: Vec<usize> = [PlanKind::Tune, PlanKind::Power, PlanKind::Runtime].iter().map(|k| k.index()).collect();
        assert_eq!(idx, [0, 1, 2]);
        assert_eq!(PlanKind::COUNT, 3);
    }

    #[test]
    fn matchers() {
        assert!(curve_id_str("20261018T101500Z-a1b2"));
        assert!(!curve_id_str("20261018T101500Z-A1b2"));
        assert!(!curve_id_str("20261018T101500Z-a1b2x"));
        assert!(unit_str("P0") && unit_str("EC3") && unit_str("P123"));
        assert!(!unit_str("P") && !unit_str("P1234") && !unit_str("E3") && !unit_str("EC") && !unit_str("p1") && !unit_str("P+1"));
        assert!(typed("CONFIRM AB2345") && !typed("CONFIRM AB1345") && !typed("CONFIRM AB234") && !typed("confirm AB2345"));
    }
}
