//! Persisted UI settings (PLAN §3.1, §4.4): `~/.config/arltuner-desktop/settings.json`, mode 0600, written atomically (temp file in the same
//! directory, fsync, rename), schema-checked on the way in and on the way out. Commands for JS: `settings_get`, `settings_set(patch)`.
//!
//! Schema (every key optional in a patch; unknown keys are refused by `settings_set` and ignored when loading an older/newer file):
//! `refresh_s` number 0.25..=60 · `strip_series` array (≤ 16, unique, `[a-z0-9_]{1,32}`; keys of the UI's SERIES) · `window.density`
//! `"comfortable"|"compact"` · `notifications` bool. A file that does not parse or does not pass the schema is not used (defaults + a
//! `warning`) and is left alone until the next successful `settings_set` replaces it.
//! Nothing here touches the helper or hardware: these are the app's own preferences.

use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard};

use serde::Serialize;
use serde_json::{json, Map, Value};
use tauri::{AppHandle, Manager};

pub const DIR_NAME: &str = "arltuner-desktop";
pub const FILE_NAME: &str = "settings.json";
/// A settings file larger than this is not read (it is a handful of short keys).
pub const MAX_FILE: u64 = 64 * 1024;
pub const REFRESH_MIN_S: f64 = 0.25;
pub const REFRESH_MAX_S: f64 = 60.0;
pub const MAX_SERIES: usize = 16;
pub const DENSITIES: [&str; 2] = ["comfortable", "compact"];
/// The UI's SERIES keys (`ui/store.js`), the default monitoring strip.
pub const DEFAULT_SERIES: [&str; 6] = ["pkg_power", "clock", "temp", "vcore", "core_power", "dram"];

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Window {
    pub density: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Settings {
    pub refresh_s: f64,
    pub strip_series: Vec<String>,
    pub window: Window,
    pub notifications: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            refresh_s: 1.0,
            strip_series: DEFAULT_SERIES.iter().map(|s| s.to_string()).collect(),
            window: Window { density: "comfortable".to_string() },
            notifications: true,
        }
    }
}

fn series_key_ok(k: &str) -> bool {
    (1..=32).contains(&k.len()) && k.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

impl Settings {
    /// `base` with `patch` applied. `strict` refuses unknown keys (a `settings_set` patch); loading ignores them.
    pub fn merged(base: &Settings, patch: &Value, strict: bool) -> Result<Settings, String> {
        let obj = patch.as_object().ok_or("settings must be a JSON object")?;
        let mut out = base.clone();
        for (key, val) in obj {
            match key.as_str() {
                "refresh_s" => {
                    let n = val.as_f64().filter(|n| n.is_finite()).ok_or("refresh_s must be a number")?;
                    if !(REFRESH_MIN_S..=REFRESH_MAX_S).contains(&n) {
                        return Err(format!("refresh_s must be between {REFRESH_MIN_S} and {REFRESH_MAX_S} seconds"));
                    }
                    out.refresh_s = n;
                }
                "strip_series" => {
                    let arr = val.as_array().ok_or("strip_series must be an array of series keys")?;
                    if arr.len() > MAX_SERIES {
                        return Err(format!("strip_series has more than {MAX_SERIES} entries"));
                    }
                    let mut keys: Vec<String> = Vec::with_capacity(arr.len());
                    for v in arr {
                        let k = v.as_str().filter(|k| series_key_ok(k)).ok_or("strip_series entries must be series keys ([a-z0-9_], 1-32 characters)")?;
                        if keys.iter().any(|x| x == k) {
                            return Err(format!("strip_series lists {k} twice"));
                        }
                        keys.push(k.to_string());
                    }
                    out.strip_series = keys;
                }
                "window" => {
                    let w = val.as_object().ok_or("window must be an object")?;
                    for (wk, wv) in w {
                        match wk.as_str() {
                            "density" => {
                                let d = wv.as_str().filter(|d| DENSITIES.contains(d)).ok_or("window.density must be \"comfortable\" or \"compact\"")?;
                                out.window.density = d.to_string();
                            }
                            other if strict => return Err(format!("unknown setting window.{other}")),
                            _ => {}
                        }
                    }
                }
                "notifications" => out.notifications = val.as_bool().ok_or("notifications must be true or false")?,
                other if strict => return Err(format!("unknown setting {other}")),
                _ => {}
            }
        }
        Ok(out)
    }

    pub fn to_json(&self) -> Value {
        serde_json::to_value(self).unwrap_or(Value::Null)
    }
}

/// `$XDG_CONFIG_HOME/arltuner-desktop/settings.json`, else `~/.config/arltuner-desktop/settings.json` (relative XDG values are ignored per spec).
pub fn default_path() -> Option<PathBuf> {
    let abs = |k: &str| std::env::var_os(k).map(PathBuf::from).filter(|p| p.is_absolute());
    let base = abs("XDG_CONFIG_HOME").or_else(|| abs("HOME").map(|h| h.join(".config")))?;
    Some(base.join(DIR_NAME).join(FILE_NAME))
}

fn read_limited(path: &Path) -> Result<Option<String>, String> {
    let f = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("cannot read {}: {e}", path.display())),
    };
    let mut buf = String::new();
    f.take(MAX_FILE + 1).read_to_string(&mut buf).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    if buf.len() as u64 > MAX_FILE {
        return Err(format!("{} is larger than {MAX_FILE} bytes", path.display()));
    }
    Ok(Some(buf))
}

/// Defaults plus a warning when the file is unusable; `(settings, None)` for a good or absent file.
pub fn load_file(path: &Path) -> (Settings, Option<String>) {
    let text = match read_limited(path) {
        Ok(None) => return (Settings::default(), None),
        Ok(Some(t)) => t,
        Err(w) => return (Settings::default(), Some(w)),
    };
    let parsed = serde_json::from_str::<Value>(&text).map_err(|e| format!("not valid JSON ({e})"));
    match parsed.and_then(|v| Settings::merged(&Settings::default(), &v, false)) {
        Ok(s) => (s, None),
        Err(e) => (Settings::default(), Some(format!("{} ignored, using defaults: {e}", path.display()))),
    }
}

static TMP_SEQ: AtomicU64 = AtomicU64::new(0);

/// Temp file (0600, exclusive) in the target's directory (0700 when created), fsync, rename over the target, fsync the directory.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let dir = path.parent().ok_or_else(|| std::io::Error::other("settings path has no directory"))?;
    DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
    let tmp = dir.join(format!(".{FILE_NAME}.{}.{}.tmp", std::process::id(), TMP_SEQ.fetch_add(1, Ordering::Relaxed)));
    let result = (|| {
        let mut f = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        fs::rename(&tmp, path)?;
        File::open(dir).and_then(|d| d.sync_all())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

pub struct SettingsStore {
    path: Option<PathBuf>,
    inner: Mutex<(Settings, Option<String>)>,
}

impl SettingsStore {
    pub fn load(path: Option<PathBuf>) -> SettingsStore {
        let loaded = match &path {
            Some(p) => load_file(p),
            None => (Settings::default(), Some("no config directory (HOME is not set); settings will not be saved".to_string())),
        };
        SettingsStore { path, inner: Mutex::new(loaded) }
    }

    fn lock(&self) -> MutexGuard<'_, (Settings, Option<String>)> {
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    pub fn get(&self) -> Settings {
        self.lock().0.clone()
    }

    /// `{ok, result: settings, warning?}`.
    pub fn get_response(&self) -> Value {
        let g = self.lock();
        let mut out = json!({"ok": true, "result": g.0.to_json()});
        if let Some(w) = &g.1 {
            out["warning"] = json!(w);
        }
        out
    }

    /// Validate, persist, then adopt. A failed write leaves both the file and the in-memory settings as they were.
    pub fn set(&self, patch: &Value) -> Value {
        let mut g = self.lock();
        let next = match Settings::merged(&g.0, patch, true) {
            Ok(s) => s,
            Err(m) => return error("BAD_REQUEST", &m),
        };
        let Some(path) = &self.path else { return error("IO", "no config directory (HOME is not set); settings cannot be saved") };
        let mut text = serde_json::to_string_pretty(&next.to_json()).unwrap_or_default();
        text.push('\n');
        if let Err(e) = write_atomic(path, text.as_bytes()) {
            return error("IO", &format!("cannot write {}: {e}", path.display()));
        }
        *g = (next, None);
        json!({"ok": true, "result": g.0.to_json()})
    }
}

fn error(code: &str, message: &str) -> Value {
    let mut err = Map::new();
    err.insert("code".into(), json!(code));
    err.insert("message".into(), json!(message));
    json!({"ok": false, "error": err})
}

/// Register the store with the app (called once from `setup`).
pub fn init(app: &AppHandle) {
    app.manage(SettingsStore::load(default_path()));
}

#[tauri::command]
pub async fn settings_get(app: AppHandle) -> Value {
    app.state::<SettingsStore>().get_response()
}

#[tauri::command]
pub async fn settings_set(app: AppHandle, patch: Value) -> Value {
    app.state::<SettingsStore>().set(&patch)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn scratch(tag: &str) -> PathBuf {
        let n = TMP_SEQ.fetch_add(1, Ordering::Relaxed);
        let d = std::env::temp_dir().join(format!("arltuner-g219-{tag}-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        d
    }

    fn mode(p: &Path) -> u32 {
        fs::metadata(p).unwrap().permissions().mode() & 0o777
    }

    #[test]
    fn defaults_are_valid_and_match_the_ui_series() {
        let d = Settings::default();
        assert_eq!(Settings::merged(&d, &d.to_json(), true).unwrap(), d);
        let store_js = include_str!("../../ui/store.js");
        for k in DEFAULT_SERIES {
            assert!(store_js.contains(&format!("key: \"{k}\"")), "{k} is not a UI series");
        }
    }

    #[test]
    fn patch_merges_and_validates() {
        let d = Settings::default();
        let s = Settings::merged(&d, &json!({"refresh_s": 0.5, "notifications": false, "window": {"density": "compact"}}), true).unwrap();
        assert_eq!((s.refresh_s, s.notifications, s.window.density.as_str()), (0.5, false, "compact"));
        assert_eq!(s.strip_series, d.strip_series);
        for bad in [
            json!({"refresh_s": 0.1}),
            json!({"refresh_s": 61}),
            json!({"refresh_s": "1"}),
            json!({"refresh_s": null}),
            json!({"strip_series": "clock"}),
            json!({"strip_series": ["Clock"]}),
            json!({"strip_series": ["clock", "clock"]}),
            json!({"strip_series": [1]}),
            json!({"notifications": 1}),
            json!({"window": "wide"}),
            json!({"window": {"density": "huge"}}),
            json!({"window": {"width": 5}}),
            json!({"colour": "red"}),
            json!([1]),
        ] {
            assert!(Settings::merged(&d, &bad, true).is_err(), "{bad} must be refused");
        }
        let many: Vec<String> = (0..17).map(|i| format!("s{i}")).collect();
        assert!(Settings::merged(&d, &json!({"strip_series": many}), true).is_err());
        assert!(Settings::merged(&d, &json!({"strip_series": []}), true).is_ok()); // an empty strip is a choice
    }

    #[test]
    fn loading_ignores_unknown_keys_but_not_bad_values() {
        let s = Settings::merged(&Settings::default(), &json!({"future_key": 1, "window": {"zoom": 2}, "refresh_s": 2}), false).unwrap();
        assert_eq!(s.refresh_s, 2.0);
        assert!(Settings::merged(&Settings::default(), &json!({"refresh_s": 999}), false).is_err());
    }

    #[test]
    fn set_writes_0600_atomically_and_get_reads_it_back() {
        let dir = scratch("set");
        let path = dir.join(DIR_NAME).join(FILE_NAME);
        let store = SettingsStore::load(Some(path.clone()));
        assert_eq!(store.get(), Settings::default());
        assert!(!path.exists(), "loading must not create the file");

        let r = store.set(&json!({"refresh_s": 2, "strip_series": ["temp", "vcore"]}));
        assert_eq!(r["ok"], true, "{r}");
        assert_eq!(r["result"]["refresh_s"], 2.0);
        assert_eq!(mode(&path), 0o600);
        assert_eq!(mode(path.parent().unwrap()), 0o700);
        let names: Vec<_> = fs::read_dir(path.parent().unwrap()).unwrap().map(|e| e.unwrap().file_name()).collect();
        assert_eq!(names, [std::ffi::OsString::from(FILE_NAME)], "no temp file left behind");

        let again = SettingsStore::load(Some(path.clone()));
        assert_eq!(again.get(), store.get());
        assert_eq!(again.get().strip_series, ["temp", "vcore"]);
        assert!(again.get_response().get("warning").is_none());

        // a second set replaces the file in place and keeps the mode
        assert_eq!(store.set(&json!({"notifications": false}))["ok"], true);
        assert_eq!(mode(&path), 0o600);
        assert!(!SettingsStore::load(Some(path)).get().notifications);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn refused_patch_changes_nothing() {
        let dir = scratch("refuse");
        let path = dir.join(FILE_NAME);
        let store = SettingsStore::load(Some(path.clone()));
        let r = store.set(&json!({"refresh_s": 5, "bogus": true}));
        assert_eq!(r["ok"], false);
        assert_eq!(r["error"]["code"], "BAD_REQUEST");
        assert_eq!(store.get(), Settings::default());
        assert!(!path.exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn bad_file_gives_defaults_and_a_warning_and_is_kept_until_replaced() {
        let dir = scratch("bad");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join(FILE_NAME);
        for text in ["{not json", "{\"refresh_s\": 1000}", "[]"] {
            fs::write(&path, text).unwrap();
            let store = SettingsStore::load(Some(path.clone()));
            assert_eq!(store.get(), Settings::default());
            assert!(store.get_response()["warning"].as_str().unwrap().contains("using defaults"), "{text}");
            assert_eq!(fs::read_to_string(&path).unwrap(), text, "an unusable file is not overwritten by loading");
        }
        let store = SettingsStore::load(Some(path.clone()));
        assert_eq!(store.set(&json!({"refresh_s": 5}))["ok"], true);
        assert!(store.get_response().get("warning").is_none());
        assert_eq!(SettingsStore::load(Some(path)).get().refresh_s, 5.0);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn oversize_file_is_not_read() {
        let dir = scratch("big");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join(FILE_NAME);
        fs::write(&path, vec![b' '; (MAX_FILE + 1) as usize]).unwrap();
        let (s, w) = load_file(&path);
        assert_eq!(s, Settings::default());
        assert!(w.unwrap().contains("larger than"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn failed_write_leaves_memory_untouched() {
        let dir = scratch("ro");
        fs::create_dir_all(&dir).unwrap();
        let blocker = dir.join("file");
        fs::write(&blocker, "x").unwrap();
        // the "directory" for the settings is a regular file: create_dir fails
        let store = SettingsStore::load(Some(blocker.join(FILE_NAME)));
        let r = store.set(&json!({"refresh_s": 5}));
        assert_eq!((r["ok"].clone(), r["error"]["code"].clone()), (json!(false), json!("IO")));
        assert_eq!(store.get(), Settings::default());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn no_home_means_defaults_and_unsaved() {
        let store = SettingsStore::load(None);
        assert!(store.get_response()["warning"].is_string());
        assert_eq!(store.set(&json!({"refresh_s": 5}))["error"]["code"], "IO");
    }

    #[test]
    fn default_path_honours_xdg_then_home() {
        // Pure on the environment only for the shape: the file is under <dir>/arltuner-desktop/settings.json.
        if let Some(p) = default_path() {
            assert!(p.ends_with(Path::new(DIR_NAME).join(FILE_NAME)));
            assert!(p.is_absolute());
        }
    }
}
