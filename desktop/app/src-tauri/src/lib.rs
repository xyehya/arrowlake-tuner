//! arltuner-desktop: unprivileged Tauri 2 client (PLAN §3). This scaffold has the window, tray,
//! single-instance, window-state and notification plumbing; the helper link arrives in G-204.

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager};

pub mod helper;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// True when the process was asked only for its version (`--version` / `-V`).
pub fn wants_version<I: IntoIterator<Item = S>, S: AsRef<str>>(args: I) -> bool {
    args.into_iter().skip(1).any(|a| matches!(a.as_ref(), "--version" | "-V"))
}

pub fn version_line() -> String {
    format!("arltuner-desktop {VERSION}")
}

/// Effective uid of this process (the app must never run as root; the check lands with the link).
pub fn effective_uid() -> u32 {
    // SAFETY: geteuid has no preconditions and cannot fail.
    unsafe { libc::geteuid() }
}

#[tauri::command]
fn app_version() -> String {
    VERSION.to_string()
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("arltuner")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main(app),
            "quit" => app.exit(0),
            _ => {}
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

pub fn run() {
    // Before any GTK/WebKit init: a root webview is a privilege-escalation surface, and the helper link refuses root anyway.
    if effective_uid() == 0 {
        eprintln!("arltuner-desktop must not run as root; start it as your normal user (the helper holds the privileges)");
        std::process::exit(1);
    }
    tauri::Builder::default()
        // Must be registered first: a second launch focuses the existing window and exits.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            app_version,
            helper::helper_status,
            helper::helper_connect,
            helper::helper_call
        ])
        .setup(|app| {
            helper::init(app.handle());
            build_tray(app.handle())?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running arltuner-desktop");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_flag_detected() {
        assert!(wants_version(["arltuner-desktop", "--version"]));
        assert!(wants_version(["arltuner-desktop", "-V"]));
        assert!(!wants_version(["arltuner-desktop"]));
        assert!(!wants_version(["--version"])); // argv[0] is never a flag
    }

    #[test]
    fn version_line_matches_manifest() {
        assert_eq!(version_line(), format!("arltuner-desktop {}", env!("CARGO_PKG_VERSION")));
        assert_eq!(app_version(), VERSION);
    }

    #[test]
    fn config_matches_task_contract() {
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).expect("tauri.conf.json parses");
        assert_eq!(conf["identifier"], "io.github.xyehya.arltuner");
        assert_eq!(conf["build"]["frontendDist"], "../ui");
        assert_eq!(conf["build"]["beforeDevCommand"], "");
        assert_eq!(conf["build"]["beforeBuildCommand"], "");
        assert_eq!(conf["app"]["withGlobalTauri"], true);
        assert_eq!(
            conf["app"]["security"]["csp"],
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src ipc: http://ipc.localhost"
        );
        assert_eq!(conf["version"], VERSION);
    }

    #[test]
    fn capability_grants_exactly_the_expected_permissions() {
        let cap: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/default.json")).expect("capability parses");
        let perms: Vec<&str> = cap["permissions"].as_array().unwrap().iter().map(|p| p.as_str().unwrap()).collect();
        assert_eq!(
            perms,
            [
                "core:window:default",
                "core:event:allow-listen",
                "core:event:allow-unlisten",
                "allow-app-version",
                "allow-helper-status",
                "allow-helper-connect",
                "allow-helper-call"
            ]
        );
    }

    #[test]
    fn not_root_under_test() {
        assert_ne!(effective_uid(), 0, "tests must not run as root");
    }
}
