//! Desktop notifications (PLAN §3.1), sent from Rust through `tauri-plugin-notification`, so the webview needs no notification permission.
//!
//! * A long op's final response (`helper://result`: finished, failed, or lost with the link) raises one notification, but only while the
//!   main window is not focused (hidden to the tray, minimised or behind another window) and the `notifications` setting is on.
//! * `notify(title, body)` is the same path for the UI (e.g. a measure run it tracks itself): same gates, text sanitised and clipped.
//! Nothing here talks to the helper or hardware.

use serde_json::{json, Value};
use tauri::{AppHandle, Listener, Manager};
use tauri_plugin_notification::NotificationExt;

use crate::settings::SettingsStore;

pub const TITLE_MAX: usize = 80;
pub const BODY_MAX: usize = 240;
const APP_TITLE: &str = "arltuner";

/// Control characters become spaces, runs of whitespace collapse, and the text is cut to `max` characters (with an ellipsis).
pub fn clean(s: &str, max: usize) -> String {
    let flat = s.chars().map(|c| if c.is_control() { ' ' } else { c }).collect::<String>();
    let flat = flat.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= max {
        return flat;
    }
    let mut cut: String = flat.chars().take(max.saturating_sub(1)).collect();
    cut.push('…');
    cut
}

/// The gate: the setting is on and nobody is looking at the window.
pub fn should_notify(enabled: bool, window_focused: bool) -> bool {
    enabled && !window_focused
}

/// `(title, body)` for a `helper://result` payload (`{id, ok, result|error}`); `None` when the payload is not a final response.
pub fn result_message(payload: &Value) -> Option<(String, String)> {
    let ok = payload.get("ok")?.as_bool()?;
    if ok {
        return Some((APP_TITLE.to_string(), "The operation finished.".to_string()));
    }
    let err = payload.get("error");
    let code = err.and_then(|e| e.get("code")).and_then(Value::as_str).unwrap_or("INTERNAL");
    let message = err.and_then(|e| e.get("message")).and_then(Value::as_str).unwrap_or("");
    let body = if message.is_empty() { code.to_string() } else { format!("{code}: {message}") };
    Some((format!("{APP_TITLE}: operation failed"), clean(&body, BODY_MAX)))
}

fn window_focused(app: &AppHandle) -> bool {
    app.get_webview_window("main").is_some_and(|w| w.is_focused().unwrap_or(false))
}

fn enabled(app: &AppHandle) -> bool {
    app.try_state::<SettingsStore>().is_some_and(|s| s.get().notifications)
}

/// Show one notification if the gates allow it. Returns whether it was handed to the desktop.
pub fn send(app: &AppHandle, title: &str, body: &str) -> bool {
    if !should_notify(enabled(app), window_focused(app)) {
        return false;
    }
    app.notification().builder().title(clean(title, TITLE_MAX)).body(clean(body, BODY_MAX)).show().is_ok()
}

/// Listen for long-op results (called once from `setup`, after the settings store is managed).
pub fn init(app: &AppHandle) {
    let handle = app.clone();
    app.listen("helper://result", move |ev| {
        let Ok(payload) = serde_json::from_str::<Value>(ev.payload()) else { return };
        if let Some((title, body)) = result_message(&payload) {
            send(&handle, &title, &body);
        }
    });
}

#[tauri::command]
pub async fn notify(app: AppHandle, title: String, body: String) -> Value {
    if title.trim().is_empty() {
        return json!({"ok": false, "error": {"code": "BAD_REQUEST", "message": "notify needs a title"}});
    }
    json!({"ok": true, "result": {"shown": send(&app, &title, &body)}})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gate_needs_setting_on_and_window_unfocused() {
        assert!(should_notify(true, false));
        assert!(!should_notify(true, true));
        assert!(!should_notify(false, false));
        assert!(!should_notify(false, true));
    }

    #[test]
    fn clean_strips_controls_collapses_space_and_clips() {
        assert_eq!(clean("a\nb\t\u{7}c   d", 80), "a b c d");
        assert_eq!(clean("  ", 80), "");
        let long = "x".repeat(300);
        let c = clean(&long, 10);
        assert_eq!(c.chars().count(), 10);
        assert!(c.ends_with('…'));
        assert_eq!(clean("héllo wörld", 5).chars().count(), 5); // clips on characters, never inside one
    }

    #[test]
    fn success_and_failure_messages() {
        let ok = result_message(&json!({"id": "d1", "ok": true, "result": {"anything": 1}})).unwrap();
        assert_eq!(ok, ("arltuner".to_string(), "The operation finished.".to_string()));
        let bad = result_message(&json!({"id": "d2", "ok": false, "error": {"code": "LINK_LOST", "message": "the helper connection was lost"}})).unwrap();
        assert_eq!(bad.0, "arltuner: operation failed");
        assert_eq!(bad.1, "LINK_LOST: the helper connection was lost");
        let bare = result_message(&json!({"ok": false})).unwrap();
        assert_eq!(bare.1, "INTERNAL");
        let long = result_message(&json!({"ok": false, "error": {"code": "X", "message": "y".repeat(1000)}})).unwrap();
        assert_eq!(long.1.chars().count(), BODY_MAX);
    }

    #[test]
    fn non_responses_are_ignored() {
        assert!(result_message(&json!({"id": "d1"})).is_none());
        assert!(result_message(&json!({"ok": "yes"})).is_none());
        assert!(result_message(&json!(null)).is_none());
    }
}
