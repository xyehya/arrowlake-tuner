// Prevents an extra console window on Windows release builds (no-op on Linux).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // `--version` must work headless: answer before any GTK/webview initialisation.
    if arltuner_desktop_lib::wants_version(std::env::args()) {
        println!("{}", arltuner_desktop_lib::version_line());
        return;
    }
    arltuner_desktop_lib::run();
}
