//! Tray icon (PLAN §3.1): a coloured dot that follows the helper's state, and a menu of Show / Quit. No hardware action is reachable from here.
//!
//! The state comes from the link's events only: `helper://state` and `helper://heartbeat` carry the helper's state machine value
//! (`idle planning applying capped stressing measuring`), `helper://closed` means the link is gone. The app starts out Disconnected and
//! leaves it on the first state/heartbeat from a live link.

use std::sync::Mutex;

use serde_json::Value;
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Listener, Manager, Wry};

pub const TRAY_ID: &str = "main";
const ICON_PX: u32 = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayState {
    Idle,
    Applying,
    Stressing,
    Disconnected,
}

impl TrayState {
    /// A helper `state` value. Planning / applying / measuring are the helper doing a tuning operation (Applying); capped / stressing are
    /// the stress run and its cpufreq caps (Stressing). A value this build does not know is shown as busy (Applying), never as idle.
    pub fn from_helper(state: &str) -> TrayState {
        match state {
            "idle" => TrayState::Idle,
            "stressing" | "capped" => TrayState::Stressing,
            _ => TrayState::Applying,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            TrayState::Idle => "Helper idle",
            TrayState::Applying => "Helper applying",
            TrayState::Stressing => "Helper stress running",
            TrayState::Disconnected => "Helper not connected",
        }
    }

    /// The semantic accents of the UI (ok / warn / danger / faint).
    pub fn rgb(self) -> [u8; 3] {
        match self {
            TrayState::Idle => [0x4f, 0xc5, 0x8c],
            TrayState::Applying => [0xf2, 0xb2, 0x4c],
            TrayState::Stressing => [0xef, 0x6b, 0x5f],
            TrayState::Disconnected => [0x6c, 0x78, 0x89],
        }
    }
}

/// The state after a link event (`name` is the full event name), or `current` when the event says nothing about it.
pub fn next_state(current: TrayState, name: &str, payload: &Value) -> TrayState {
    match name {
        "helper://closed" => TrayState::Disconnected,
        "helper://state" | "helper://heartbeat" => payload.get("state").and_then(Value::as_str).map_or(current, TrayState::from_helper),
        _ => current,
    }
}

/// A filled, anti-aliased dot with a dark rim on a transparent square: `(rgba, width, height)`.
pub fn icon_rgba(state: TrayState) -> (Vec<u8>, u32, u32) {
    let [r, g, b] = state.rgb();
    let c = (ICON_PX as f32 - 1.0) / 2.0;
    let outer = c - 1.0;
    let rim = 2.0;
    let mut px = Vec::with_capacity((ICON_PX * ICON_PX * 4) as usize);
    for y in 0..ICON_PX {
        for x in 0..ICON_PX {
            let d = ((x as f32 - c).powi(2) + (y as f32 - c).powi(2)).sqrt();
            let alpha = (outer + 0.5 - d).clamp(0.0, 1.0);
            let k = if d > outer - rim { 0.55 } else { 1.0 }; // darker rim keeps the dot readable on light and dark panels
            px.extend_from_slice(&[(r as f32 * k) as u8, (g as f32 * k) as u8, (b as f32 * k) as u8, (alpha * 255.0) as u8]);
        }
    }
    (px, ICON_PX, ICON_PX)
}

struct TrayControl {
    status_item: MenuItem<Wry>,
    state: Mutex<TrayState>,
}

fn apply(app: &AppHandle, ctl: &TrayControl, next: TrayState) {
    {
        let mut cur = ctl.state.lock().unwrap_or_else(|p| p.into_inner());
        if *cur == next {
            return;
        }
        *cur = next;
    }
    let _ = ctl.status_item.set_text(next.label());
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let (rgba, w, h) = icon_rgba(next);
        let _ = tray.set_icon(Some(Image::new_owned(rgba, w, h)));
        let _ = tray.set_tooltip(Some(format!("arltuner: {}", next.label())));
    }
}

/// Build the tray (starts Disconnected) and follow the link's events. Called once from `setup`.
pub fn build(app: &AppHandle) -> tauri::Result<()> {
    let status_item = MenuItem::with_id(app, "status", TrayState::Disconnected.label(), false, None::<&str>)?;
    let show = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&status_item, &sep, &show, &quit])?;
    let (rgba, w, h) = icon_rgba(TrayState::Disconnected);
    TrayIconBuilder::with_id(TRAY_ID)
        .icon(Image::new_owned(rgba, w, h))
        .tooltip(format!("arltuner: {}", TrayState::Disconnected.label()))
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => crate::show_main(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    app.manage(TrayControl { status_item, state: Mutex::new(TrayState::Disconnected) });

    for name in ["helper://state", "helper://heartbeat", "helper://closed"] {
        let handle = app.clone();
        app.listen(name, move |ev| {
            let payload = serde_json::from_str::<Value>(ev.payload()).unwrap_or(Value::Null);
            let Some(ctl) = handle.try_state::<TrayControl>() else { return };
            let current = *ctl.state.lock().unwrap_or_else(|p| p.into_inner());
            apply(&handle, &ctl, next_state(current, name, &payload));
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn helper_states_map_to_the_four_tray_states() {
        for (s, want) in [
            ("idle", TrayState::Idle),
            ("planning", TrayState::Applying),
            ("applying", TrayState::Applying),
            ("measuring", TrayState::Applying),
            ("capped", TrayState::Stressing),
            ("stressing", TrayState::Stressing),
            ("something-new", TrayState::Applying),
            ("", TrayState::Applying),
        ] {
            assert_eq!(TrayState::from_helper(s), want, "{s}");
        }
    }

    #[test]
    fn events_drive_the_state() {
        let mut s = TrayState::Disconnected;
        s = next_state(s, "helper://heartbeat", &json!({"state": "idle", "ops_used": 0}));
        assert_eq!(s, TrayState::Idle);
        s = next_state(s, "helper://state", &json!({"event": "state", "state": "applying", "op_id": "x"}));
        assert_eq!(s, TrayState::Applying);
        s = next_state(s, "helper://state", &json!({"state": "stressing"}));
        assert_eq!(s, TrayState::Stressing);
        // events that do not say anything about the state leave it alone
        assert_eq!(next_state(s, "helper://progress", &json!({"state": "idle"})), TrayState::Stressing);
        assert_eq!(next_state(s, "helper://state", &json!({"state": 5})), TrayState::Stressing);
        assert_eq!(next_state(s, "helper://heartbeat", &Value::Null), TrayState::Stressing);
        s = next_state(s, "helper://closed", &json!({"reason": "eof"}));
        assert_eq!(s, TrayState::Disconnected);
    }

    #[test]
    fn colours_and_labels_are_distinct() {
        let all = [TrayState::Idle, TrayState::Applying, TrayState::Stressing, TrayState::Disconnected];
        for (i, a) in all.iter().enumerate() {
            for b in &all[i + 1..] {
                assert_ne!(a.rgb(), b.rgb());
                assert_ne!(a.label(), b.label());
            }
        }
    }

    #[test]
    fn icon_is_a_coloured_dot_on_transparency() {
        for st in [TrayState::Idle, TrayState::Applying, TrayState::Stressing, TrayState::Disconnected] {
            let (px, w, h) = icon_rgba(st);
            assert_eq!((w, h), (ICON_PX, ICON_PX));
            assert_eq!(px.len(), (w * h * 4) as usize);
            let at = |x: u32, y: u32| &px[((y * w + x) * 4) as usize..][..4];
            assert_eq!(at(0, 0)[3], 0, "corner is transparent");
            let mid = at(w / 2, h / 2);
            assert_eq!(mid[3], 255, "centre is opaque");
            assert_eq!(&mid[..3], &st.rgb(), "centre is the state colour");
        }
        assert_ne!(icon_rgba(TrayState::Idle).0, icon_rgba(TrayState::Stressing).0);
    }
}
