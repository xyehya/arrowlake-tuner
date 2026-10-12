"""The helper as a root service on a Unix socket (D-043, G-144): no polkit, no pkexec.

``arltuner-helper --device --socket`` listens on ``/run/arltuner/helper.sock`` (``SOCKET_PATH``) and serves the ordinary protocol (contract 8, JSON lines)
to **one client at a time**. Two ways to get the listening socket, one code path after that:

* **socket activation** (the shipped ``arltuner-helper.socket`` unit): systemd binds the socket (``root`` 0666) and hands it over as fd 3 with
  ``LISTEN_PID`` / ``LISTEN_FDS`` (the ``sd_listen_fds`` protocol, implemented here with the standard library only). The helper then exits after
  ``idle_exit_s`` (``helper.toml``, default 600) with no client and no operation; the next connect starts it again. The handed-over socket is checked
  (a listening filesystem socket owned by root) and refused otherwise;
* **self-made** (any other init, a manual start): the helper creates the socket itself. The run directory must be a trusted directory (root-owned, not
  group/world-writable); an existing node is removed only when it is a socket owned by us (a stale one - the instance lock proves no other helper runs);
  it is bound under ``umask 0177`` (so it is born 0600), then (``[access] group`` set: handed to that group, ``chown``) widened to 0666, and only then does it
  ``listen``: at no moment can anyone connect through a wider permission than the final one. It stays until the helper stops; it never idles out.

**The socket is world-connectable (D-043 amendment, 2026-10-12): the peer check below is THE gate**, so a newly allowed user needs no re-login for a group.

**Authorization is the peer, on every accept** (``SO_PEERCRED``, kernel-supplied). The peer's uid must be
admitted by ``/etc/arltuner/helper.toml`` ``[access]`` (:class:`~arltuner.helper.settings.Access`; re-read - trust-checked - at every accept, so a change takes
effect without a restart; unreadable / untrusted / invalid / missing = nobody). A refused peer, and a second client while one is connected, gets exactly one
framed ``REFUSED`` error line (``id`` null) and is closed; nothing of the helper's state is shown to a peer that is not admitted (the busy answer comes after
the access check). The peer uid then *is* the invoker: audit ``operator`` ``gui:uid<N>``, the stress child's user; root as the peer has no invoker for stress.

A client leaving (EOF / broken socket) ends its connection the way EOF always did: telemetry stops, an operation in flight completes its read-back or
restore at the next safe point, no new one starts, and the helper is ready for the next client.
A client that stops reading is treated the same way (``SO_SNDTIMEO`` on its connection: a timed-out send ends the connection). The next client is accepted only
after **every** thread of the previous connection has ended (``Helper.serve`` returns then); only an admitted client counts as activity for the idle exit.
"""
from __future__ import annotations

import os
import socket
import stat
import struct
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import Helper
from .protocol import REFUSED, encode, error_response
from .settings import DEFAULT_IDLE_EXIT_S, HelperToml
from .trust import TrustError, check_trusted_path

__all__ = ["CLIENT_BUSY", "ACCESS_DENIED", "CachedSettings", "Peer", "SOCKET_MODE", "SOCKET_PATH", "Service", "ServiceError", "adopt_socket", "create_socket", "listen_fds", "peer_credentials"]

SOCKET_PATH = Path("/run/arltuner/helper.sock")
SOCKET_MODE = 0o666  # connectable by everyone; SO_PEERCRED + [access] decide (D-043 amendment)
SD_LISTEN_FDS_START = 3
ACCESS_DENIED = "ACCESS_DENIED"  # detail.check of the refusal for a peer that is not admitted
CLIENT_BUSY = "CLIENT_BUSY"  # detail.check of the refusal for a second client
EXIT_USAGE = 64
EXIT_REFUSED_TO_START = 77
_PEERCRED = "3i"  # struct ucred: pid, uid, gid
_BACKLOG = 4
_POLL_S = 0.2
_REFUSAL_SEND_S = 1.0  # a refused peer that does not read must not hold the accept loop
SEND_TIMEOUT_S = 8.0  # a client that does not read for this long is a dead client (SO_SNDTIMEO on its connection): the connection ends like a disconnect
_SESSION_JOIN_S = 300.0  # how long a stopping service waits for a session's operation to finish its read-back / restore; the unit's TimeoutStopSec is >= this
_LOG_EVERY_S = 10.0  # refusal log lines: at most one per kind and uid in this window, with the count of the suppressed ones


class ServiceError(Exception):
    """The socket cannot be made safely; the helper refuses to start (exit 77). The message names what is wrong."""


@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    gid: int


def peer_credentials(conn: socket.socket) -> Peer:
    """The kernel's record of who connected (``SO_PEERCRED``): not something the peer can claim."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(_PEERCRED))
    pid, uid, gid = struct.unpack(_PEERCRED, raw)
    return Peer(pid, uid, gid)


# =============================================================================================================================================
# the listening socket
# =============================================================================================================================================
def _something_listens(path: str) -> bool:
    """True when a connect to the node succeeds (a live listener, e.g. systemd's socket unit). ``ECONNREFUSED`` / ``ENOENT`` = stale. Any other error cannot
    prove the node stale, so it counts as in use (nothing is removed on a guess)."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(2.0)
        probe.connect(path)
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True
    finally:
        probe.close()

def listen_fds(environ: Mapping[str, str], pid: int) -> int | None:
    """``sd_listen_fds`` without libsystemd: the fd number (3) when systemd handed exactly one socket to *this* process, ``None`` when nothing was handed over
    (no variables, or ``LISTEN_PID`` names another process - an inherited environment). ``ServiceError`` for a half-set or malformed handover."""
    raw_pid, raw_n = environ.get("LISTEN_PID"), environ.get("LISTEN_FDS")
    if raw_pid is None and raw_n is None:
        return None
    if raw_pid is None or raw_n is None or not raw_pid.isascii() or not raw_pid.isdigit() or not raw_n.isascii() or not raw_n.isdigit():
        raise ServiceError("LISTEN_PID / LISTEN_FDS are set but malformed: refusing to guess which descriptor is the socket")
    if int(raw_pid) != pid:
        return None
    if int(raw_n) != 1:
        raise ServiceError(f"systemd handed over {raw_n} descriptors; the helper expects exactly one socket")
    return SD_LISTEN_FDS_START


def adopt_socket(fd: int = SD_LISTEN_FDS_START, *, euid: int = 0, lstat_fn: Callable[[str], Any] = os.lstat) -> socket.socket:
    """The descriptor systemd handed over, checked: a listening ``AF_UNIX`` stream socket on a filesystem path whose node is a socket of ``euid`` (its mode
    is not checked: the peer check is the gate)."""
    try:
        sock = socket.socket(fileno=fd)
    except OSError as exc:
        raise ServiceError(f"descriptor {fd} is not a socket: {exc.strerror or exc}") from None
    try:
        if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
            raise ServiceError("the handed-over socket is not an AF_UNIX stream socket")
        if not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
            raise ServiceError("the handed-over socket is not listening")
        name = sock.getsockname()
        if not isinstance(name, str) or not name or name.startswith("\0"):
            raise ServiceError("the handed-over socket has no filesystem path (an abstract or unnamed socket has no permissions to check)")
        try:
            st = lstat_fn(name)
        except OSError as exc:
            raise ServiceError(f"cannot stat the socket {name}: {exc.strerror or exc}") from None
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != euid:
            raise ServiceError(f"{name} is not a socket owned by uid {euid}")
        os.set_inheritable(sock.fileno(), False)
    except BaseException:
        sock.detach()  # leave the descriptor alone: the process is exiting and nothing else owns it
        raise
    return sock


def create_socket(
    path: Path | str, *, group: str | None, euid: int = 0, lstat_fn: Callable[[str], Any] = os.lstat, umask_fn: Callable[[int], int] = os.umask,
    chown_fn: Callable[..., object] = os.chown, chmod_fn: Callable[[str, int], object] = os.chmod, unlink_fn: Callable[[str], object] = os.unlink,
    mkdir_fn: Callable[[str, int], object] = os.mkdir, getgrnam: Callable[[str], Any] | None = None, trust_parents: bool = True,
    probe_fn: Callable[[str], bool] | None = None,
) -> socket.socket:
    """Make the listening socket safely (module docstring). ``ServiceError`` for anything that is not as it should be; a socket is never left wider than
    0600 until it is complete (then 0666: the peer check is the gate) and never bound at all if its directory or an existing node is suspect. ``group`` is
    optional (``[access] group``); when given it must exist and the node is handed to it. ``trust_parents=False`` (non-root test / simulator runs)
    checks only the directory itself (owned by ``euid``, not group/world-writable), not its ancestors."""
    path = Path(path)
    directory = str(path.parent)
    gid = None
    if group:
        if getgrnam is None:
            import grp

            getgrnam = grp.getgrnam
        try:
            gid = getgrnam(group).gr_gid
        except (KeyError, OSError):
            raise ServiceError(f"the group {group!r} of [access] does not exist") from None
    try:
        lstat_fn(directory)
    except FileNotFoundError:
        try:
            mkdir_fn(directory, 0o755)
        except OSError as exc:
            raise ServiceError(f"cannot create {directory}: {exc.strerror or exc}") from None
    except OSError as exc:
        raise ServiceError(f"cannot stat {directory}: {exc.strerror or exc}") from None
    try:
        if trust_parents:
            check_trusted_path(directory, want="dir", lstat_fn=lstat_fn, root_uid=euid)
        else:
            dst = lstat_fn(directory)
            if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != euid or dst.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise TrustError(directory, "not a directory of this user that others cannot write")
    except (TrustError, OSError) as exc:
        raise ServiceError(f"the run directory is not trusted: {exc}") from None
    try:
        st = lstat_fn(str(path))
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise ServiceError(f"cannot stat {path}: {exc.strerror or exc}") from None
    if st is not None:
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != euid:
            raise ServiceError(f"{path} exists and is not a socket owned by uid {euid}; not removed")
        if (probe_fn or _something_listens)(str(path)):  # the instance lock proves no other helper process, but systemd's socket unit may hold the node
            raise ServiceError(f"{path} is in use: something is listening on it (the systemd socket unit arltuner-helper.socket?); not removed. "
                               "Use the socket unit (sudo systemctl start arltuner-helper.socket) or stop it first")
        try:
            unlink_fn(str(path))  # stale: nobody listens
        except OSError as exc:
            raise ServiceError(f"cannot remove the stale socket {path}: {exc.strerror or exc}") from None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = umask_fn(0o177)
    bound = False
    try:
        try:
            sock.bind(str(path))
            bound = True
        finally:
            umask_fn(old)
        if gid is not None:
            chown_fn(str(path), -1, gid, follow_symlinks=False)
        chmod_fn(str(path), SOCKET_MODE)
        sock.listen(_BACKLOG)
    except BaseException as exc:
        sock.close()
        if bound:
            try:
                unlink_fn(str(path))
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise ServiceError(f"cannot create the socket {path}: {exc.strerror or exc}") from None
        raise
    return sock


# =============================================================================================================================================
# the service
# =============================================================================================================================================
class CachedSettings:
    """``helper.toml`` through the trust check, re-read only when the file changes (device, inode, size, mtime, ctime): a flood of connects does not make the
    root helper re-parse and re-check it every time. A failure is cached under the same key (it is still a refusal). A change of an *ancestor directory's*
    permissions without any change to the file is not noticed until the file changes or the helper restarts (idle exit)."""

    def __init__(self, loader: Callable[[], HelperToml], path: Path | str, lstat_fn: Callable[[str], Any] = os.lstat) -> None:
        self._loader, self._path, self._lstat = loader, str(path), lstat_fn
        self._key: tuple[int, ...] | None = None
        self._value: HelperToml | None = None
        self._error: Exception | None = None

    def __call__(self) -> HelperToml:
        try:
            st = self._lstat(self._path)
            key: tuple[int, ...] | None = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        except (OSError, AttributeError):
            key = None  # absent / unstatable: the loader decides, nothing is cached
        if key is not None and key == self._key:
            if self._error is not None:
                raise self._error
            assert self._value is not None
            return self._value
        try:
            value, error = self._loader(), None
        except Exception as exc:
            value, error = None, exc
        self._key, self._value, self._error = key, value, error
        if error is not None:
            raise error
        assert value is not None
        return value


class _LogLimiter:
    """At most one log line per key in ``every_s``; the next one that gets through says how many were left out."""

    def __init__(self, every_s: float, monotonic: Callable[[], float]) -> None:
        self._every, self._mono = every_s, monotonic
        self._last: dict[Any, float] = {}
        self._skipped: dict[Any, int] = {}

    def __call__(self, key: Any, text: str) -> str | None:
        now = self._mono()
        last = self._last.get(key)
        if last is not None and now - last < self._every:
            self._skipped[key] = self._skipped.get(key, 0) + 1
            return None
        self._last[key] = now
        if len(self._last) > 256:  # bounded: forget the oldest keys
            for old in sorted(self._last, key=self._last.get)[:128]:  # type: ignore[arg-type]
                self._last.pop(old, None)
                self._skipped.pop(old, None)
        n = self._skipped.pop(key, 0)
        return f"{text} (+{n} similar refusals in the last {self._every:.0f} s not logged)" if n else text


class _ConnWriter:
    """Write side of a connection for the protocol ``Writer``: whole-buffer writes (``sendall``)."""

    def __init__(self, conn: socket.socket) -> None:
        self._c = conn

    def write(self, data: bytes) -> int:
        self._c.sendall(data)
        return len(data)

    def flush(self) -> None:
        pass


class Service:
    """Accept loop around a :class:`Helper`. ``activated``: the socket came from systemd (idle exit applies, the socket node is not ours to remove)."""

    def __init__(
        self, helper: Helper, sock_factory: Callable[[], socket.socket], *, path: Path | str = SOCKET_PATH, settings_loader: Callable[[], HelperToml], activated: bool,
        peercred: Callable[[socket.socket], Peer] = peer_credentials, lookups: Mapping[str, Any] | None = None, log: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic, poll_s: float = _POLL_S, send_timeout_s: float = SEND_TIMEOUT_S, sndbuf: int | None = None,
    ) -> None:
        self.helper = helper
        self.sock_factory = sock_factory  # called after the instance lock is held: a socket node found then is stale by construction
        self.sock: socket.socket | None = None
        self.path = Path(path)
        self.settings_loader = settings_loader
        self.activated = activated
        self.peercred = peercred
        self.lookups = dict(lookups or {})
        self.log = log or helper.cfg.log
        self.monotonic = monotonic
        self.poll_s = poll_s
        self.send_timeout_s = send_timeout_s
        self.sndbuf = sndbuf  # tests: a small send buffer makes a stalled reader show up quickly
        self._limited = _LogLimiter(_LOG_EVERY_S, monotonic)
        self.idle_exit_s = DEFAULT_IDLE_EXIT_S
        self.session: threading.Thread | None = None
        self._conn: socket.socket | None = None
        self.helper.socket_mode = True

    # ---------------------------------------------------------------- admission
    def _refuse(self, conn: socket.socket, check: str, message: str) -> None:
        """One framed error line, then close. Never blocks the loop for long, never raises."""
        try:
            conn.settimeout(_REFUSAL_SEND_S)
            conn.sendall(encode(error_response(None, REFUSED, message, {"check": check})))
        except (OSError, ValueError):
            pass
        finally:
            self._close(conn)

    @staticmethod
    def _shutdown_conn(conn: socket.socket) -> None:
        """Both directions: the client sees the end, the blocked reader returns, a blocked send fails."""
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @staticmethod
    def _close(conn: socket.socket) -> None:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _log_limited(self, key: Any, text: str) -> None:
        line = self._limited(key, text)
        if line is not None:
            self.log(line)

    def _admitted(self, peer: Peer) -> bool:
        try:
            settings = self.settings_loader()
        except Exception as exc:  # unreadable / untrusted / invalid helper.toml: nobody
            self._log_limited(("toml",), f"access: helper.toml refused ({type(exc).__name__}: {exc}); no client is admitted")
            return False
        self.idle_exit_s = settings.idle_exit_s
        if settings.access.empty:
            self._log_limited(("empty",), "access: [access] in helper.toml is missing or empty; no client is admitted")
            return False
        try:
            return bool(settings.access.allows(peer.uid, **self.lookups))
        except Exception as exc:
            self._log_limited(("check",), f"access: check failed ({type(exc).__name__}: {exc}); refused")
            return False

    def _accept_one(self, conn: socket.socket) -> bool:
        """Admit (start the session) or refuse the connection. True only for an admitted client: refused connects do not count as activity (idle exit)."""
        try:
            peer = self.peercred(conn)
        except (OSError, struct.error) as exc:
            self._log_limited(("cred",), f"refused a connection: cannot read the peer credentials ({exc})")
            self._refuse(conn, ACCESS_DENIED, "the peer's credentials could not be read; refused")
            return False
        if not self._admitted(peer):
            self._log_limited(("denied", peer.uid), f"refused uid {peer.uid} (pid {peer.pid}): not allowed by helper.toml [access]")
            self._refuse(conn, ACCESS_DENIED, f"uid {peer.uid} is not allowed to use the helper (not in [access] of /etc/arltuner/helper.toml)")
            return False
        if self.session is not None and self.session.is_alive():
            self._log_limited(("busy", peer.uid), f"refused uid {peer.uid} (pid {peer.pid}): another client is connected")
            self._refuse(conn, CLIENT_BUSY, "another client is connected")
            return False
        conn.settimeout(None)
        try:
            # a client that stops reading must not freeze the helper (a send blocks under the writer lock): after this long without room the connection is dead
            whole, frac = divmod(self.send_timeout_s, 1.0)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("@ll", int(whole), int(frac * 1_000_000)))
            if self.sndbuf is not None:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.sndbuf)
        except OSError as exc:
            self.log(f"cannot set the send timeout on the client socket ({exc}); refused")
            self._refuse(conn, ACCESS_DENIED, "the connection could not be set up; refused")
            return False
        self._conn = conn
        self.session = threading.Thread(target=self._session, args=(conn, peer), name="helper-client", daemon=True)
        self.session.start()
        return True

    def _session(self, conn: socket.socket, peer: Peer) -> None:
        self.log(f"client connected: uid {peer.uid} (pid {peer.pid})")
        rfile = conn.makefile("rb")
        try:
            self.helper.serve(rfile, _ConnWriter(conn), peer_uid=peer.uid, abort=lambda: self._shutdown_conn(conn))  # type: ignore[arg-type]
        except Exception:
            self.log("client session failed:\n" + traceback.format_exc())
        finally:
            try:
                rfile.close()
            except (OSError, ValueError):
                pass
            self._close(conn)
            self.log(f"client disconnected: uid {peer.uid}")

    # ---------------------------------------------------------------- the loop
    def _idle_expired(self, last_active: float) -> bool:
        if not self.activated or self.idle_exit_s <= 0:
            return False
        if self.session is not None and self.session.is_alive():
            return False
        return not self.helper.busy() and self.monotonic() - last_active >= self.idle_exit_s

    def run(self) -> int:
        """Start the helper, then accept until a signal or the idle exit. Exit code 0 on an orderly stop."""
        code = self.helper.start()
        if code is not None:
            return code
        try:
            try:
                self.sock = self.sock_factory()
            except ServiceError as exc:
                self.log(f"refused to start: {exc}")
                return EXIT_REFUSED_TO_START
            try:
                self.idle_exit_s = self.settings_loader().idle_exit_s
            except Exception:
                self.idle_exit_s = DEFAULT_IDLE_EXIT_S
            self.sock.settimeout(self.poll_s)
            last_active = self.monotonic()
            while not self.helper.signalled:
                if self.session is not None and not self.session.is_alive():
                    self.session, self._conn = None, None
                    last_active = self.monotonic()
                try:
                    conn, _addr = self.sock.accept()
                except TimeoutError:
                    if self._idle_expired(last_active):
                        self.log(f"idle for {self.idle_exit_s} s: exiting (socket activation starts the helper again)")
                        break
                    continue
                except OSError as exc:
                    self.log(f"accept failed: {exc}")
                    return 1
                if self._accept_one(conn):  # only an admitted client counts as activity: a stream of refused connects must not keep an idle helper alive
                    last_active = self.monotonic()
            return 0
        finally:
            self._shutdown_session()
            self._cleanup()
            self.helper.stop()

    def _shutdown_session(self) -> None:
        session, conn = self.session, self._conn
        if session is None:
            return
        self.helper.request_shutdown("service stopping")
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)  # unblocks the reader and any send to a client that stopped reading
            except OSError:
                pass
        session.join(_SESSION_JOIN_S)

    def _cleanup(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.sock is not None and not self.activated:  # a socket we made: remove our node (systemd removes its own: RemoveOnStop)
            try:
                st = os.lstat(self.path)
                if stat.S_ISSOCK(st.st_mode):
                    os.unlink(self.path)
            except OSError:
                pass
