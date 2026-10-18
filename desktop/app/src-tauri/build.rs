fn main() {
    // PLAN §3.1: the dev-socket seam must never reach a shipped (release/AppImage) build.
    let dev_socket = std::env::var_os("CARGO_FEATURE_DEV_SOCKET").is_some();
    let release = std::env::var("PROFILE").map(|p| p == "release").unwrap_or(false);
    if dev_socket && release {
        panic!("feature `dev-socket` must not be enabled in a release build (the AppImage is unprivileged-client only)");
    }

    // App commands are listed here so capabilities can grant them one by one.
    let app = tauri_build::AppManifest::new().commands(&["app_version"]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(app))
        .expect("tauri build script failed");
}
