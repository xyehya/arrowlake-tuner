//! Connecting to the root helper (PLAN §3.1; mirrors `src/arltuner/web/helperlink.py` `connect_helper`, lines 305-362).
//!
//! Order: `lstat` the node (a socket owned by the trusted uid) -> `connect` -> `SO_PEERCRED` of the listener (the trusted uid again) ->
//! `preflight` as the handshake. The trusted uid is **0**, a constant of [`Target::installed`]. The only other target is the
//! `dev-socket` feature's [`Target::dev`], which trusts the current uid and exists only in non-release builds (see `mod.rs`).
//! What the checks *observe* (`lstat`, peer credentials) goes through [`Probe`] so tests can feed in non-root nodes and peers; what they
//! *require* is never a parameter.
//! Refusal texts are the web GUI's, word for word, so the operator sees the same fix commands everywhere.

use std::fmt;
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;

use super::link::{CallError, EventSink, Link, Reply};

/// `arltuner.helper.service.SOCKET_PATH`.
pub const HELPER_SOCKET: &str = "/run/arltuner/helper.sock";
/// Socket activation starts the helper on the first connect; never unbounded (helperlink.py `HANDSHAKE_S`).
pub const HANDSHAKE: Duration = Duration::from_secs(20);
/// How long the reader may take to deliver a framed refusal before the EOF.
const REFUSAL_WAIT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Missing,
    Untrusted,
    Denied,
    NotAllowed,
    Busy,
    Unavailable,
    Closed,
    Timeout,
    /// The desktop app itself runs as root (it must never).
    Root,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Missing => "missing",
            Kind::Untrusted => "untrusted",
            Kind::Denied => "denied",
            Kind::NotAllowed => "not_allowed",
            Kind::Busy => "busy",
            Kind::Unavailable => "unavailable",
            Kind::Closed => "closed",
            Kind::Timeout => "timeout",
            Kind::Root => "root",
        }
    }
}

/// The helper socket cannot be used; `message` says what to do about it.
#[derive(Debug, Clone)]
pub struct ConnectError {
    pub kind: Kind,
    pub message: String,
}

impl ConnectError {
    fn new(kind: Kind, message: String) -> Self {
        ConnectError { kind, message }
    }

    pub fn to_json(&self) -> Value {
        serde_json::json!({"kind": self.kind.as_str(), "message": self.message})
    }
}

impl fmt::Display for ConnectError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for ConnectError {}

/// Which node to connect to and which uid must own it and listen on it.
#[derive(Debug, Clone)]
pub struct Target {
    path: PathBuf,
    owner_uid: u32,
}

impl Target {
    /// The installed helper: `/run/arltuner/helper.sock`, root-owned, root listening. The only target of a release build.
    pub fn installed() -> Target {
        Target { path: PathBuf::from(HELPER_SOCKET), owner_uid: 0 }
    }

    /// Dev/test seam: a socket at `path` owned by, and listened on by, **the current uid**.
    #[cfg(feature = "dev-socket")]
    pub fn dev(path: impl Into<PathBuf>) -> Target {
        Target { path: path.into(), owner_uid: crate::effective_uid() }
    }

    /// `ARLTUNER_DESKTOP_SOCKET` -> [`Target::dev`], else [`Target::installed`]. Without the `dev-socket` feature the variable is never read.
    pub fn from_environment() -> Target {
        #[cfg(feature = "dev-socket")]
        if let Some(p) = std::env::var_os("ARLTUNER_DESKTOP_SOCKET").filter(|p| !p.is_empty()) {
            return Target::dev(p);
        }
        Target::installed()
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn is_dev(&self) -> bool {
        self.owner_uid != 0
    }
}

/// What `lstat` says about the node.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NodeObs {
    pub is_socket: bool,
    pub uid: u32,
}

/// The two observations the trust checks rest on. Tests substitute fakes; the app uses [`SysProbe`].
pub trait Probe: Send + Sync {
    /// `lstat` (no symlink following).
    fn lstat(&self, path: &Path) -> io::Result<NodeObs>;
    /// `SO_PEERCRED` of a connected stream: the uid of the process that listens on the node.
    fn peer_uid(&self, stream: &UnixStream) -> io::Result<u32>;
}

pub struct SysProbe;

impl Probe for SysProbe {
    fn lstat(&self, path: &Path) -> io::Result<NodeObs> {
        let md = std::fs::symlink_metadata(path)?;
        Ok(NodeObs { is_socket: md.file_type().is_socket(), uid: md.uid() })
    }

    fn peer_uid(&self, stream: &UnixStream) -> io::Result<u32> {
        let mut cred = libc::ucred { pid: 0, uid: 0, gid: 0 };
        let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        // SAFETY: the fd is open for the duration of the borrow of `stream`; `cred` and `len` are live locals, `len` is the exact size
        // of `cred`, so the kernel writes at most size_of::<ucred>() bytes into it and updates `len`.
        let rc = unsafe {
            libc::getsockopt(
                stream.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_PEERCRED,
                (&mut cred as *mut libc::ucred).cast::<libc::c_void>(),
                &mut len,
            )
        };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }
        if len as usize != std::mem::size_of::<libc::ucred>() {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "short SO_PEERCRED answer"));
        }
        Ok(cred.uid)
    }
}

/// The OS error text without Rust's ` (os error N)` suffix (python's `exc.strerror`).
fn strerror(e: &io::Error) -> String {
    let s = e.to_string();
    match s.find(" (os error") {
        Some(i) => s[..i].to_string(),
        None => s,
    }
}

/// Name for the `--add-user` hint: `$USER`, `$LOGNAME`, else `YOURNAME`.
pub fn whoami() -> String {
    ["USER", "LOGNAME"]
        .iter()
        .find_map(|k| std::env::var(k).ok().filter(|v| !v.is_empty() && v.len() <= 64 && v.bytes().all(|b| b.is_ascii_graphic())))
        .unwrap_or_else(|| "YOURNAME".to_string())
}

/// Step 1: the node must exist and be a socket owned by the trusted uid.
pub fn check_node(target: &Target, probe: &dyn Probe) -> Result<(), ConnectError> {
    let path = target.path.display();
    match probe.lstat(&target.path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Err(ConnectError::new(
            Kind::Missing,
            format!("the helper is not installed or not started: {path} does not exist. Run: sudo bash install.sh (it enables arltuner-helper.socket)"),
        )),
        Err(e) => Err(ConnectError::new(Kind::Missing, format!("cannot look at {path}: {}", strerror(&e)))),
        Ok(obs) if !obs.is_socket || obs.uid != target.owner_uid => {
            Err(ConnectError::new(Kind::Untrusted, format!("{path} is not a socket owned by root; not connecting to it")))
        }
        Ok(_) => Ok(()),
    }
}

/// Step 2: connect, with the web GUI's wording for each failure.
fn open(target: &Target) -> Result<UnixStream, ConnectError> {
    let path = target.path.display();
    UnixStream::connect(&target.path).map_err(|e| match e.kind() {
        io::ErrorKind::PermissionDenied => ConnectError::new(
            Kind::Denied,
            format!(
                "permission denied on the helper socket {path}: its file mode is wrong (it should be root-owned, mode 0666; the helper's allowlist \
                 decides who may use it). Reinstall to repair it: sudo bash install.sh"
            ),
        ),
        io::ErrorKind::ConnectionRefused => ConnectError::new(
            Kind::Unavailable,
            format!("nothing is listening on {path}: start it with: sudo systemctl enable --now arltuner-helper.socket"),
        ),
        _ => ConnectError::new(Kind::Unavailable, format!("cannot connect to {path}: {}", strerror(&e))),
    })
}

/// Step 3: the process listening on the node must be the trusted uid too (never hand requests to a look-alike).
pub fn check_peer(target: &Target, probe: &dyn Probe, stream: &UnixStream) -> Result<(), ConnectError> {
    let path = target.path.display();
    match probe.peer_uid(stream) {
        Ok(uid) if uid == target.owner_uid => Ok(()),
        Ok(uid) => Err(ConnectError::new(Kind::Untrusted, format!("the process listening on {path} is uid {uid}, not root; not talking to it"))),
        Err(e) => Err(ConnectError::new(Kind::Untrusted, format!("cannot verify who listens on {path} ({}); not talking to it", strerror(&e)))),
    }
}

/// Step 4: `preflight` is the handshake. Returns the link and the helper's raw preflight response (which may itself be an error
/// response: the helper answered, so the link works; the caller shows the report). EOF or a framed refusal becomes a [`ConnectError`].
pub fn handshake(stream: UnixStream, sink: Arc<dyn EventSink>, who: &str, timeout: Duration) -> Result<(Link, Value), ConnectError> {
    let link = Link::start(stream, sink)
        .map_err(|e| ConnectError::new(Kind::Unavailable, format!("cannot start the helper link: {}", strerror(&e))))?;
    match link.call("preflight", &Value::Object(Default::default()), timeout) {
        Ok(Reply::Done(resp)) => Ok((link, resp)),
        Ok(Reply::Started(_)) | Err(CallError::Unavailable | CallError::Refused(_) | CallError::TooLarge | CallError::TooManyLong) => {
            link.wait_finished(REFUSAL_WAIT); // the framed refusal arrives before the EOF; do not miss it when the write failed first
            let refusal = link.refusal();
            link.close();
            Err(match refusal {
                Some(r) => {
                    let check = r.get("detail").and_then(|d| d.get("check")).and_then(Value::as_str);
                    match check {
                        Some("CLIENT_BUSY") => ConnectError::new(
                            Kind::Busy,
                            "another client is connected to the helper (another arltuner GUI?). Close it, then start this one again.".to_string(),
                        ),
                        Some("ACCESS_DENIED") => ConnectError::new(
                            Kind::NotAllowed,
                            format!("the helper refused this user: not in the allowlist (/etc/arltuner/helper.toml). Run: sudo bash install.sh --add-user {who}"),
                        ),
                        _ => {
                            let msg = r.get("message").and_then(Value::as_str).unwrap_or("no reason given");
                            ConnectError::new(Kind::NotAllowed, format!("the helper refused the connection: {msg}"))
                        }
                    }
                }
                None => ConnectError::new(
                    Kind::Closed,
                    "the helper closed the connection right away (it may have refused to start). See: journalctl -u arltuner-helper -n 30".to_string(),
                ),
            })
        }
        Err(CallError::Timeout) => {
            link.close();
            Err(ConnectError::new(
                Kind::Timeout,
                format!("the helper did not answer within {:.0} s. See: journalctl -u arltuner-helper -n 30", timeout.as_secs_f64()),
            ))
        }
    }
}

/// All four steps. The desktop app refuses to talk to the helper when it runs as root itself.
pub fn connect(target: &Target, probe: &dyn Probe, sink: Arc<dyn EventSink>, who: &str, timeout: Duration) -> Result<(Link, Value), ConnectError> {
    if crate::effective_uid() == 0 {
        return Err(ConnectError::new(Kind::Root, "arltuner-desktop must not run as root; start it as your normal user".to_string()));
    }
    check_node(target, probe)?;
    let stream = open(target)?;
    check_peer(target, probe, &stream)?;
    handshake(stream, sink, who, timeout)
}
