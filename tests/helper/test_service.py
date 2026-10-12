"""G-144 / D-043: the socket service. Real Unix sockets in a temp directory, the simulator, a real ``SO_PEERCRED`` (our own uid) or an injected one for the
cases a non-root test run cannot produce (other uids, root). No hardware, no /run, no systemd, no root."""
from __future__ import annotations

import grp
import json
import os
import pwd
import socket
import stat
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any

from arltuner.helper import Helper, OpSpec
from arltuner.helper.service import (
    ACCESS_DENIED, CLIENT_BUSY, CachedSettings, Peer, Service, ServiceError, adopt_socket, create_socket, listen_fds, peer_credentials,
)
from arltuner.helper.settings import Access, HelperToml, load_helper_toml, parse_helper_toml

from ._support import BlockingSim, HelperCase

POSIX = os.name == "posix"
T = 10.0


def my_name() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        raise unittest.SkipTest("the test user has no passwd entry") from None


def my_group() -> str:
    try:
        return grp.getgrgid(os.getgid()).gr_name
    except KeyError:
        raise unittest.SkipTest("the test group has no name") from None


class Client:
    """A line client of the socket, the way the GUI is."""

    def __init__(self, path: Path | str) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(T)
        self.sock.connect(str(path))
        self.file = self.sock.makefile("rb")

    def send(self, msg: dict[str, Any]) -> None:
        self.sock.sendall(json.dumps(msg).encode() + b"\n")

    def read(self) -> dict[str, Any] | None:
        line = self.file.readline()
        return json.loads(line) if line else None

    def read_until(self, pred) -> dict[str, Any]:
        while True:
            msg = self.read()
            if msg is None:
                raise AssertionError("connection closed")
            if pred(msg):
                return msg

    def call(self, rid: str, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.send({"id": rid, "op": op, **({} if args is None else {"args": args})})
        return self.read_until(lambda m: m.get("id") == rid)

    def at_eof(self) -> bool:
        return self.read() is None

    def close(self) -> None:
        for f in (self.file, self.sock):
            try:
                f.close()
            except OSError:
                pass


class ServiceCase(HelperCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_dir = self.tmp / "run"
        self.path = self.run_dir / "helper.sock"
        self.clients: list[Client] = []
        self.services: list[tuple[Service, threading.Thread, list[int]]] = []
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        for c in self.clients:
            c.close()
        release = getattr(self.sim, "release", None)
        if release is not None:
            release.set()
        for svc, thread, _ in self.services:
            svc.helper._signal = 15  # what SIGTERM does
            thread.join(T)

    def toml(self, **kw: Any) -> HelperToml:
        return HelperToml(access=kw.pop("access", Access((my_name(),))), **kw)

    def serve(self, settings=None, *, activated: bool = False, group: str | None = "mine", cfg=None, peercred=None, lookups=None, poll_s: float = 0.02,
              extra_ops=None, **service_kw: Any) -> Service:
        if extra_ops:
            cfg = cfg or self.config(extra_ops=extra_ops)
        helper = Helper(cfg or self.config())
        loader = settings if callable(settings) else (lambda s=settings or self.toml(): s)
        euid, grp_name = os.geteuid(), (my_group() if group == "mine" else group)
        kw: dict[str, Any] = {} if peercred is None else {"peercred": peercred}
        svc = Service(
            helper, lambda: create_socket(self.path, group=grp_name, euid=euid, trust_parents=False), path=self.path, settings_loader=loader, activated=activated,
            lookups=lookups, log=self.logs.append, poll_s=poll_s, **kw, **service_kw,
        )
        codes: list[int] = []
        thread = threading.Thread(target=lambda: codes.append(svc.run()), daemon=True)
        thread.start()
        self.services.append((svc, thread, codes))
        deadline = time.monotonic() + T
        while time.monotonic() < deadline:
            try:
                probe = os.lstat(self.path)
                if stat.S_ISSOCK(probe.st_mode) and stat.S_IMODE(probe.st_mode) == 0o666:
                    break
            except OSError:
                pass
            time.sleep(0.01)
        else:
            raise AssertionError(f"the service did not come up: {self.logs}")
        svc.codes = codes  # type: ignore[attr-defined]
        svc.thread = thread  # type: ignore[attr-defined]
        return svc

    def connect(self) -> Client:
        c = Client(self.path)
        self.clients.append(c)
        return c

    def wait(self, pred, what: str = "condition", timeout: float = T) -> None:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}; log: {self.logs}")
            time.sleep(0.01)

    def refusal(self, c: Client) -> dict[str, Any]:
        msg = c.read()
        assert msg is not None
        self.assertIsNone(msg["id"])
        self.assertFalse(msg["ok"])
        self.assertEqual(msg["error"]["code"], "REFUSED")
        self.assertTrue(c.at_eof())  # exactly one line, then closed
        return msg["error"]


# =============================================================================================================================================
@unittest.skipUnless(POSIX, "Unix sockets, SO_PEERCRED")
class AdmissionTests(ServiceCase):
    def test_an_allowed_user_gets_the_ordinary_protocol(self) -> None:
        self.serve()
        c = self.connect()
        self.assertTrue(c.call("1", "preflight")["ok"])
        self.assertTrue(c.call("2", "audit.tail")["ok"])

    def test_the_peer_credentials_are_the_kernels(self) -> None:
        a, b = socket.socketpair()
        with a, b:
            self.assertEqual(peer_credentials(a), Peer(os.getpid(), os.getuid(), os.getgid()))

    def test_a_peer_that_is_not_listed_gets_one_framed_refusal_and_is_closed(self) -> None:
        self.serve(self.toml(access=Access(("a-user-that-is-not-us",))))
        err = self.refusal(self.connect())
        self.assertEqual(err["detail"], {"check": ACCESS_DENIED})
        self.assertIn(str(os.getuid()), err["message"])
        self.assertTrue(any("refused uid" in line for line in self.logs))

    def test_fail_closed_missing_empty_or_broken_access(self) -> None:
        def broken():
            raise ValueError("helper.toml: not valid TOML")

        for label, settings in (("empty access", HelperToml()), ("no users no group", HelperToml(access=Access())), ("loader raises", broken)):
            with self.subTest(label):
                self.tearDown_services()
                self.serve(settings)
                self.refusal(self.connect())

    def tearDown_services(self) -> None:
        for svc, thread, _ in self.services:
            svc.helper._signal = 15
            thread.join(T)
        self.services.clear()
        self.logs.clear()

    def test_admission_is_decided_again_at_every_accept(self) -> None:
        state = {"toml": self.toml()}
        self.serve(lambda: state["toml"])
        first = self.connect()
        self.assertTrue(first.call("1", "preflight")["ok"])
        first.close()
        self.wait(lambda: self.services[0][0].session is None or not self.services[0][0].session.is_alive(), "session end")
        state["toml"] = HelperToml(access=Access(("nobody-at-all",)))  # the operator removed the user: no restart needed
        self.refusal(self.connect())

    def test_group_membership_admits_and_root_is_never_admitted_through_the_group(self) -> None:
        pw = {"alice": types.SimpleNamespace(pw_uid=4242, pw_gid=4242, pw_name="alice"), "root": types.SimpleNamespace(pw_uid=0, pw_gid=0, pw_name="root")}
        lookups = dict(
            getpwnam=lambda n: pw[n], getpwuid=lambda u: next(v for v in pw.values() if v.pw_uid == u),
            getgrnam=lambda n: types.SimpleNamespace(gr_gid=777),
            getgrouplist=lambda name, gid: [gid, 777] if name in ("alice", "root") else [gid],
        )
        for uid, listed, expect in ((4242, (), True), (0, (), False), (0, ("root",), True), (9999, (), False)):
            with self.subTest(uid=uid, listed=listed):
                self.tearDown_services()
                self.serve(self.toml(access=Access(listed, "arltuner")), peercred=lambda conn, uid=uid: Peer(1, uid, 1), lookups=lookups)
                c = self.connect()
                if expect:
                    self.assertTrue(c.call("1", "preflight")["ok"])
                else:
                    self.refusal(c)

    def test_a_peer_credentials_failure_refuses(self) -> None:
        def boom(conn):
            raise OSError("no cred")

        self.serve(peercred=boom)
        self.refusal(self.connect())


@unittest.skipUnless(POSIX, "Unix sockets")
class AccessUnitTests(unittest.TestCase):
    PW = {"alice": types.SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="alice"), "bob": types.SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_name="bob"),
          "root": types.SimpleNamespace(pw_uid=0, pw_gid=0, pw_name="root")}

    def allows(self, access: Access, uid: Any, members=("alice",)) -> bool:
        def getpwnam(n):
            return self.PW[n]

        def getpwuid(u):
            for v in self.PW.values():
                if v.pw_uid == u:
                    return v
            raise KeyError(u)

        return access.allows(uid, getpwnam=getpwnam, getpwuid=getpwuid, getgrnam=lambda n: types.SimpleNamespace(gr_gid=50) if n == "arltuner" else (_ for _ in ()).throw(KeyError(n)),
                             getgrouplist=lambda name, gid: [gid, 50] if name in members else [gid])

    def test_users_by_name(self) -> None:
        self.assertTrue(self.allows(Access(("alice",)), 1000))
        self.assertFalse(self.allows(Access(("alice",)), 1001))
        self.assertTrue(self.allows(Access(("ghost", "bob")), 1001))  # an unknown name is just no match

    def test_group(self) -> None:
        self.assertTrue(self.allows(Access((), "arltuner"), 1000))
        self.assertFalse(self.allows(Access((), "arltuner"), 1001))
        self.assertFalse(self.allows(Access((), "nogroup"), 1000))  # unknown group

    def test_root_only_by_name(self) -> None:
        self.assertFalse(self.allows(Access((), "arltuner"), 0, members=("root",)))
        self.assertFalse(self.allows(Access(("alice",)), 0))
        self.assertTrue(self.allows(Access(("root",)), 0))

    def test_garbage_uids_and_empty_access(self) -> None:
        for uid in (-1, True, None, "1000", 1.0):
            self.assertFalse(self.allows(Access(("alice",), "arltuner"), uid))
        self.assertFalse(self.allows(Access(), 1000))
        self.assertTrue(Access().empty)

    def test_names_are_resolved_at_each_check(self) -> None:
        a = Access(("carol",))
        self.assertFalse(a.allows(1002, getpwnam=lambda n: (_ for _ in ()).throw(KeyError(n))))
        self.assertTrue(a.allows(1002, getpwnam=lambda n: types.SimpleNamespace(pw_uid=1002)))


class HelperTomlTests(unittest.TestCase):
    def test_access_and_idle(self) -> None:
        t = parse_helper_toml('allow_taint_bits = [2]\nidle_exit_s = 30\n[access]\nusers = ["a", "b"]\ngroup = "arltuner"\n')
        self.assertEqual((t.taint_bits, t.idle_exit_s, t.access), (frozenset({2}), 30, Access(("a", "b"), "arltuner")))

    def test_defaults_are_nobody(self) -> None:
        t = parse_helper_toml("allow_taint_bits = [2]\n")
        self.assertTrue(t.access.empty)
        self.assertEqual(t.idle_exit_s, 600)

    def test_bad_shapes_are_refused(self) -> None:
        for text in ('[access]\nusers = "a"\n', "[access]\nusers = [1]\n", '[access]\nuser = ["a"]\n', "[access]\ngroup = 3\n", 'access = "x"\n', "idle_exit_s = -1\n",
                     "idle_exit_s = 86401\n", "idle_exit_s = true\n", '[access]\nusers = [""]\n', "other = 1\n", '[access]\ngroup = ""\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_helper_toml(text)

    def test_the_trust_checked_read_needs_an_owned_private_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "helper.toml"
            self.assertTrue(load_helper_toml(p, check_parents=False).access.empty)  # absent: nobody
            p.write_text('[access]\nusers = ["x"]\n')
            os.chmod(p, 0o644)
            self.assertEqual(load_helper_toml(p, root_uid=os.geteuid(), check_parents=False).access.users, ("x",))
            os.chmod(p, 0o666)
            from arltuner.helper.trust import TrustError

            with self.assertRaises(TrustError):
                load_helper_toml(p, root_uid=os.geteuid(), check_parents=False)


@unittest.skipUnless(POSIX, "Unix sockets")
class OneClientTests(ServiceCase):
    def test_a_second_client_gets_one_busy_line_and_the_first_is_unharmed(self) -> None:
        self.serve()
        first = self.connect()
        self.assertTrue(first.call("1", "preflight")["ok"])
        err = self.refusal(self.connect())
        self.assertEqual(err["detail"], {"check": CLIENT_BUSY})
        self.assertEqual(err["message"], "another client is connected")
        self.assertTrue(first.call("2", "preflight")["ok"])

    def test_an_unlisted_peer_is_denied_not_told_that_the_helper_is_busy(self) -> None:
        pw = types.SimpleNamespace(pw_uid=os.getuid())

        def settings() -> HelperToml:
            return HelperToml(access=Access(("me",)))

        # the second connect is "somebody else": fake the allow decision per connection through the uid the kernel reports
        peers = iter([Peer(1, os.getuid(), 1), Peer(2, os.getuid() + 1, 1)])
        self.serve(settings, peercred=lambda c: next(peers), lookups=dict(getpwnam=lambda n: pw))
        first = self.connect()
        self.assertTrue(first.call("1", "preflight")["ok"])
        err = self.refusal(self.connect())
        self.assertEqual(err["detail"], {"check": ACCESS_DENIED})

    def test_after_the_first_leaves_the_next_is_served_and_ids_start_fresh(self) -> None:
        self.serve()
        first = self.connect()
        self.assertTrue(first.call("same", "preflight")["ok"])
        first.close()
        svc = self.services[0][0]
        self.wait(lambda: svc.session is None or not svc.session.is_alive(), "the first session to end")
        second = self.connect()
        self.assertTrue(second.call("same", "preflight")["ok"])  # no "request id already used"

    def test_the_peer_uid_is_the_operator_for_exactly_its_connection(self) -> None:
        who = OpSpec("stub.who", lambda h, ctx, a: {"operator": h.cfg.operator, "peer": h.peer_uid, "socket": h.socket_mode}, lambda a: a)
        self.serve(extra_ops={"stub.who": who})
        c = self.connect()
        self.assertEqual(c.call("w", "stub.who")["result"], {"operator": f"gui:uid{os.getuid()}", "peer": os.getuid(), "socket": True})
        c.close()
        svc = self.services[0][0]
        self.wait(lambda: svc.session is None or not svc.session.is_alive(), "session end")
        self.assertIsNone(svc.helper.peer_uid)
        self.assertEqual(svc.helper.cfg.operator, "helper")


@unittest.skipUnless(POSIX, "Unix sockets")
class DisconnectTests(ServiceCase):
    def test_disconnect_during_an_operation_lets_it_finish_and_gives_no_new_client_a_stranger_s_answer(self) -> None:
        self.sim = sim = BlockingSim(self.sim.config)
        sim.park_at = 2
        svc = self.serve()
        c1 = self.connect()
        c1.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        self.assertTrue(sim.parked.wait(T))
        c1.close()  # the client is gone while a mailbox transaction is in flight
        self.wait(lambda: any("closing the connection: client disconnected" in line for line in self.logs), "the disconnect to be noticed")
        self.assertFalse([line for line in self.logs if "EOF on stdin" in line or "shutting down" in line])  # socket-mode wording (the process is not stopping)
        err = self.refusal(self.connect())  # the op is still finishing: still one client's worth of helper
        self.assertEqual(err["detail"], {"check": CLIENT_BUSY})
        self.assertIsNotNone(svc.helper.machine.running)
        sim.release.set()
        self.wait(lambda: svc.session is None or not svc.session.is_alive(), "the session to end after the op")
        self.assertIsNone(svc.helper.machine.running)  # restored to idle, the op answered once into the void
        self.assertFalse(svc.helper.busy())
        c2 = self.connect()
        self.assertTrue(c2.call("p", "preflight")["ok"])
        self.assertTrue(c2.call("d", "vf.dump", {"cpus": "0"})["ok"])  # a fresh client can run its own op under the same id

    def test_disconnect_stops_telemetry(self) -> None:
        svc = self.serve()
        c = self.connect()
        self.assertTrue(c.call("t", "telemetry.start", {"interval_s": 0.2})["ok"])
        self.assertIsNotNone(svc.helper._tele)
        c.close()
        self.wait(lambda: svc.helper._tele is None, "telemetry to stop")

    def test_a_signal_ends_the_service_cleanly_and_removes_the_socket(self) -> None:
        svc = self.serve()
        c = self.connect()
        self.assertTrue(c.call("1", "preflight")["ok"])
        svc.helper._signal = 15
        svc.thread.join(T)  # type: ignore[attr-defined]
        self.assertFalse(svc.thread.is_alive())  # type: ignore[attr-defined]
        self.assertEqual(svc.codes, [0])  # type: ignore[attr-defined]
        self.assertFalse(self.path.exists())
        self.assertTrue(c.at_eof())


@unittest.skipUnless(POSIX, "Unix sockets")
class IdleExitTests(ServiceCase):
    def test_activated_service_exits_after_idle_but_not_with_a_client(self) -> None:
        svc = self.serve(self.toml(idle_exit_s=1), activated=True)
        c = self.connect()
        self.assertTrue(c.call("1", "preflight")["ok"])
        time.sleep(1.4)
        self.assertTrue(svc.thread.is_alive())  # type: ignore[attr-defined]  # a connected client keeps it
        c.close()
        svc.thread.join(4)  # type: ignore[attr-defined]
        self.assertFalse(svc.thread.is_alive())  # type: ignore[attr-defined]
        self.assertEqual(svc.codes, [0])  # type: ignore[attr-defined]
        self.assertTrue(self.path.exists())  # not ours (systemd's): left alone

    def test_a_manually_started_service_never_idles_out(self) -> None:
        svc = self.serve(self.toml(idle_exit_s=1), activated=False)
        time.sleep(1.6)
        self.assertTrue(svc.thread.is_alive())  # type: ignore[attr-defined]

    def test_zero_means_never(self) -> None:
        svc = self.serve(self.toml(idle_exit_s=0), activated=True)
        time.sleep(1.2)
        self.assertTrue(svc.thread.is_alive())  # type: ignore[attr-defined]


@unittest.skipUnless(POSIX, "Unix sockets")
class SocketCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.path = self.tmp / "run" / "helper.sock"
        self.euid = os.geteuid()

    def make(self, **kw: Any) -> socket.socket:
        kw.setdefault("group", my_group())
        kw.setdefault("euid", self.euid)
        kw.setdefault("trust_parents", False)
        s = create_socket(self.path, **kw)
        self.addCleanup(s.close)
        return s

    def test_born_0600_and_never_listening_before_it_is_final(self) -> None:
        seen: dict[str, Any] = {}
        real_chown, real_chmod = os.chown, os.chmod

        def chown(p, uid, gid, **kw):
            seen["mode_at_chown"] = stat.S_IMODE(os.lstat(p).st_mode)
            real_chown(p, uid, gid, **kw)

        def chmod(p, mode):
            seen["mode_at_chmod"] = stat.S_IMODE(os.lstat(p).st_mode)
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(p)
                seen["connect_before_listen"] = "connected"
            except ConnectionRefusedError:
                seen["connect_before_listen"] = "refused"
            finally:
                probe.close()
            real_chmod(p, mode)

        s = self.make(chown_fn=chown, chmod_fn=chmod)
        self.assertEqual(seen, {"mode_at_chown": 0o600, "mode_at_chmod": 0o600, "connect_before_listen": "refused"})
        st = os.lstat(self.path)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_gid, stat.S_ISSOCK(st.st_mode)), (0o666, grp.getgrnam(my_group()).gr_gid, True))
        self.assertTrue(s.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))

    def test_the_process_umask_is_restored(self) -> None:
        old = os.umask(0o022)
        try:
            self.make()
            self.assertEqual(os.umask(0o022), 0o022)
        finally:
            os.umask(old)

    def test_a_failure_halfway_leaves_no_node(self) -> None:
        def chown(*a, **k):
            raise PermissionError(1, "Operation not permitted")

        with self.assertRaises(ServiceError):
            self.make(chown_fn=chown)
        self.assertFalse(self.path.exists())
        self.assertFalse(any(p.suffix == ".sock" for p in (self.tmp / "run").iterdir()))

    def test_a_stale_socket_is_replaced_but_nothing_else_is_removed(self) -> None:
        (self.tmp / "run").mkdir(mode=0o755)
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(self.path))
        stale.close()
        self.make()  # the old node (a socket of ours) went away, a new listening one is there
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.path).st_mode))

    def test_a_regular_file_or_a_foreign_socket_is_never_removed(self) -> None:
        (self.tmp / "run").mkdir(mode=0o755)
        self.path.write_text("precious")
        with self.assertRaises(ServiceError):
            self.make()
        self.assertEqual(self.path.read_text(), "precious")
        self.path.unlink()
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(self.path))
        stale.close()
        with self.assertRaises(ServiceError):
            self.make(euid=self.euid + 1)  # "owned by somebody else"
        self.assertTrue(os.path.lexists(self.path))

    def test_an_untrusted_run_directory_is_refused(self) -> None:
        d = self.tmp / "run"
        d.mkdir()
        os.chmod(d, 0o777)
        with self.assertRaises(ServiceError) as ctx:
            self.make()
        self.assertIn("not trusted", str(ctx.exception))
        self.assertFalse(os.path.lexists(self.path))

    def test_the_full_parent_chain_is_checked_for_root(self) -> None:
        # trust_parents=True with a stat that says /tmp-like parents are not root-owned
        with self.assertRaises(ServiceError):
            self.make(trust_parents=True, euid=0, lstat_fn=lambda p: types.SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o755))
        self.assertFalse(os.path.lexists(self.path))

    def test_an_unknown_group_means_no_socket(self) -> None:
        with self.assertRaises(ServiceError):
            self.make(group="no-such-group-xyz")
        self.assertFalse(os.path.lexists(self.path))

    def test_the_group_is_optional_the_socket_is_still_0666_and_never_chowned(self) -> None:
        chowned: list[Any] = []
        for group in (None, ""):
            with self.subTest(group=group):
                self.path.unlink(missing_ok=True)
                s = self.make(group=group, chown_fn=lambda *a, **k: chowned.append(a))
                self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o666)
                self.assertTrue(s.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
        self.assertEqual(chowned, [])


@unittest.skipUnless(POSIX, "Unix sockets")
class SocketActivationTests(unittest.TestCase):
    def test_listen_fds_protocol(self) -> None:
        self.assertIsNone(listen_fds({}, 5))
        self.assertEqual(listen_fds({"LISTEN_PID": "5", "LISTEN_FDS": "1"}, 5), 3)
        self.assertIsNone(listen_fds({"LISTEN_PID": "6", "LISTEN_FDS": "1"}, 5))  # an inherited environment, not for us
        for env in ({"LISTEN_PID": "5"}, {"LISTEN_FDS": "1"}, {"LISTEN_PID": "x", "LISTEN_FDS": "1"}, {"LISTEN_PID": "5", "LISTEN_FDS": "2"},
                    {"LISTEN_PID": "5", "LISTEN_FDS": "0"}, {"LISTEN_PID": "5", "LISTEN_FDS": "-1"}, {"LISTEN_PID": "5", "LISTEN_FDS": ""}):
            with self.subTest(env=env), self.assertRaises(ServiceError):
                listen_fds(env, 5)

    def make_listener(self, mode: int, *, listen: bool = True) -> tuple[socket.socket, Path]:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "s.sock"
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        s.bind(str(path))
        os.chmod(path, mode)
        if listen:
            s.listen(2)
        return s, path

    def test_a_handed_over_socket_is_adopted_whatever_its_mode(self) -> None:
        for mode in (0o666, 0o660, 0o600):
            s, _ = self.make_listener(mode)
            got = adopt_socket(os.dup(s.fileno()), euid=os.geteuid())  # the peer check is the gate, not the node's mode (D-043 amendment)
            self.addCleanup(got.close)

    def test_a_handed_over_socket_is_adopted(self) -> None:
        s, _ = self.make_listener(0o666)
        fd = os.dup(s.fileno())
        got = adopt_socket(fd, euid=os.geteuid())
        self.addCleanup(got.close)
        self.assertFalse(os.get_inheritable(got.fileno()))

    def test_unsafe_handovers_are_refused(self) -> None:
        for label, mode, listen, euid_add in (("not listening", 0o666, False, 0), ("wrong owner", 0o666, True, 1)):
            with self.subTest(label):
                s, _ = self.make_listener(mode, listen=listen)
                fd = os.dup(s.fileno())
                try:
                    with self.assertRaises(ServiceError):
                        adopt_socket(fd, euid=os.geteuid() + euid_add)
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_not_a_socket_and_not_a_unix_stream_socket(self) -> None:
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        with self.assertRaises(ServiceError):
            adopt_socket(r)
        u = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(u.close)
        u.bind(("127.0.0.1", 0))
        u.listen(1)
        with self.assertRaises(ServiceError):
            adopt_socket(os.dup(u.fileno()))

    def test_an_abstract_socket_is_refused(self) -> None:
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        s.bind("\0arltuner-test-" + str(os.getpid()))
        s.listen(1)
        with self.assertRaises(ServiceError):
            adopt_socket(os.dup(s.fileno()))


# =============================================================================================================================================
# security review of G-144 (Opus): stalls, cross-connection leaks, idle, log floods, stale-socket removal
# =============================================================================================================================================
@unittest.skipUnless(POSIX, "Unix sockets")
class StalledClientTests(ServiceCase):
    """MED 1: a client that stops reading must not freeze the helper (the send happens under the writer lock)."""

    def flood(self, c: Client, n: int = 600) -> threading.Thread:
        def go() -> None:
            c.sock.settimeout(1.0)
            try:
                for i in range(n):
                    c.sock.sendall(json.dumps({"id": f"p{i}", "op": "preflight"}).encode() + b"\n")
            except OSError:
                pass  # the helper closed the connection: expected

        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t

    def test_a_stalled_reader_is_dropped_after_the_send_timeout_and_the_next_client_is_served(self) -> None:
        svc = self.serve(send_timeout_s=0.5, sndbuf=4096)
        a = self.connect()
        t0 = time.monotonic()
        self.wait(lambda: any("client connected" in line for line in self.logs), "the session to start")
        self.flood(a)  # requests in, answers never read
        self.wait(lambda: svc.session is None or not svc.session.is_alive(), "the stalled session to be dropped", timeout=15)
        self.assertLess(time.monotonic() - t0, 12)
        self.assertTrue(any("stalled" in line for line in self.logs), self.logs)
        b = self.connect()
        self.assertTrue(b.call("1", "preflight")["ok"])  # the service never froze

    def test_an_operation_in_flight_when_the_client_stalls_completes_and_no_new_one_starts(self) -> None:
        self.sim = sim = BlockingSim(self.sim.config)
        sim.park_at = 2
        svc = self.serve(send_timeout_s=0.5, sndbuf=4096)
        a = self.connect()
        a.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        self.assertTrue(sim.parked.wait(T))  # a mailbox transaction is in flight
        self.flood(a)
        self.wait(lambda: any("stalled" in line for line in self.logs), "the stall to be noticed", timeout=15)
        self.assertIsNotNone(svc.helper.machine.running)  # still finishing: the session is not over, the op was not abandoned
        self.assertTrue(svc.session.is_alive())
        sim.release.set()
        self.wait(lambda: svc.session is None or not svc.session.is_alive(), "the session to end after the op", timeout=15)
        self.assertIsNone(svc.helper.machine.running)
        self.assertFalse(svc.helper.busy())
        b = self.connect()
        self.assertTrue(b.call("1", "preflight")["ok"])


@unittest.skipUnless(POSIX, "Unix sockets")
class CrossConnectionTests(ServiceCase):
    """MED 2: nothing of one connection (a late answer, its peer uid, its threads) may reach or influence the next."""

    PEERS = {"a": 1111, "b": 2222}

    def two_users(self):
        pw = {"a": types.SimpleNamespace(pw_uid=1111), "b": types.SimpleNamespace(pw_uid=2222)}
        uids = iter([1111, 2222, 2222, 2222, 2222, 2222])
        return dict(settings=HelperToml(access=Access(("a", "b"))), peercred=lambda conn: Peer(1, next(uids), 1), lookups=dict(getpwnam=lambda n: pw[n]))

    def test_a_late_read_only_answer_never_reaches_the_next_client_and_the_next_client_waits(self) -> None:
        seen: list[Any] = []

        def slow(h, ctx, a):
            time.sleep(1.5)
            seen.append(("peer_at_end", h.peer_uid, h.cfg.operator))
            ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": 1, "of": 1, "ops_used": 0, "ops_budget": 1})  # a late event as well
            return {"secret_of": "client A"}

        spec = OpSpec("stub.slow", slow, lambda a: a, slow=True)
        before = set(threading.enumerate())
        svc = self.serve(extra_ops={"stub.slow": spec}, **self.two_users())
        a = self.connect()
        a.send({"id": "A-req", "op": "stub.slow"})
        time.sleep(0.3)
        a.close()  # A is gone while its read-only op is still running
        b = self.connect()
        err = self.refusal(b)  # B is not served while any thread of A's connection lives
        self.assertEqual(err["detail"], {"check": CLIENT_BUSY})
        self.wait(lambda: svc.session is None or not svc.session.is_alive(), "A's connection to end")
        self.assertEqual(seen, [("peer_at_end", 1111, "gui:uid1111")])  # the op saw A's peer, never B's
        leftovers = [t for t in set(threading.enumerate()) - before if t.name.startswith(("helper-stdin", "helper-heartbeat", "helper-op", "helper-telemetry"))]
        self.assertEqual(leftovers, [])  # every thread of the connection ended before the service took the next client
        b = self.connect()
        mine = []
        deadline = time.monotonic() + 3
        b.send({"id": "B-req", "op": "audit.tail"})
        while time.monotonic() < deadline:
            m = b.read()
            mine.append(m)
            if m and m.get("id") == "B-req":
                break
        self.assertFalse([m for m in mine if m and (m.get("id") == "A-req" or "secret_of" in json.dumps(m))], mine)
        self.assertEqual(svc.helper.peer_uid, 2222)
        self.assertEqual(svc.helper.cfg.operator, "gui:uid2222")

    def test_a_dead_connection_drops_its_emits(self) -> None:
        from arltuner.helper.core import Connection
        from arltuner.helper.protocol import Writer
        import io

        out = io.BytesIO()
        conn = Connection(Writer(out), 5, "gui:uid5")
        self.assertTrue(conn.emit({"event": "heartbeat"}))
        conn.dead = True
        self.assertFalse(conn.emit({"event": "heartbeat"}))
        self.assertEqual(out.getvalue().count(b"\n"), 1)
        self.assertNotEqual(conn.generation, Connection(Writer(out), 5, "x").generation)

    def test_the_old_reader_cannot_cancel_the_next_connections_operation(self) -> None:
        self.sim = sim = BlockingSim(self.sim.config)
        sim.park_at = 2
        svc = self.serve()
        a = self.connect()
        a.close()  # A: EOF at once
        self.wait(lambda: any("client disconnected: uid" in line for line in self.logs), "A's connection to end")
        self.wait(lambda: svc.session is None or not svc.session.is_alive(), "A's session thread to end")
        b = self.connect()
        b.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        self.assertTrue(sim.parked.wait(T))
        time.sleep(0.3)
        cancel = svc.helper._cancel
        self.assertIsNotNone(cancel)
        self.assertFalse(cancel.is_set())  # nothing from A's connection touched B's op
        sim.release.set()
        self.assertTrue(b.read_until(lambda m: m.get("id") == "d")["ok"])


@unittest.skipUnless(POSIX, "Unix sockets")
class IdleAndFloodTests(ServiceCase):
    def test_refused_connects_do_not_keep_an_idle_helper_alive(self) -> None:
        svc = self.serve(self.toml(access=Access(("a-user-that-is-not-us",)), idle_exit_s=1), activated=True)
        end = time.monotonic() + 3.5
        while svc.thread.is_alive() and time.monotonic() < end:  # type: ignore[attr-defined]
            try:
                c = self.connect()
                c.read()
                c.close()
            except OSError:
                break
            time.sleep(0.1)
        self.assertFalse(svc.thread.is_alive())  # type: ignore[attr-defined]
        self.assertEqual(svc.codes, [0])  # type: ignore[attr-defined]

    def test_refusal_log_lines_are_rate_limited(self) -> None:
        self.serve(self.toml(access=Access(("a-user-that-is-not-us",))))
        for _ in range(40):
            c = self.connect()
            c.read()
            c.close()
        lines = [line for line in self.logs if line.startswith("refused uid")]
        self.assertEqual(len(lines), 1, lines)

    def test_the_limiter_reports_what_it_left_out(self) -> None:
        from arltuner.helper.service import _LogLimiter

        now = [0.0]
        lim = _LogLimiter(10.0, lambda: now[0])
        self.assertEqual(lim("k", "x"), "x")
        self.assertIsNone(lim("k", "x"))
        self.assertIsNone(lim("k", "x"))
        self.assertEqual(lim("other", "y"), "y")
        now[0] = 11.0
        self.assertIn("+2 similar", lim("k", "x"))
        now[0] = 30.0
        self.assertEqual(lim("k", "x"), "x")

    def test_helper_toml_is_re_read_only_when_it_changes(self) -> None:
        calls = []
        d = self.tmp / "t"
        d.mkdir()
        f = d / "helper.toml"
        f.write_text('[access]\nusers = ["a"]\n')
        cached = CachedSettings(lambda: calls.append(1) or load_helper_toml(f, root_uid=os.geteuid(), check_parents=False), f)
        for _ in range(20):
            self.assertEqual(cached().access.users, ("a",))
        self.assertEqual(len(calls), 1)
        f.write_text('[access]\nusers = ["a", "b"]\n')
        self.assertEqual(cached().access.users, ("a", "b"))
        self.assertEqual(len(calls), 2)
        os.chmod(f, 0o666)  # a mode change (ctime) is a change: the trust check runs again and now refuses
        with self.assertRaises(Exception):
            cached()
        self.assertEqual(len(calls), 3)
        for _ in range(5):
            with self.assertRaises(Exception):
                cached()
        self.assertEqual(len(calls), 3)  # the refusal is cached under the same key
        f.unlink()
        cached()  # absent: defaults, not cached
        cached()
        self.assertEqual(len(calls), 5)


@unittest.skipUnless(POSIX, "Unix sockets")
class LiveSocketIsNeverRemovedTests(unittest.TestCase):
    def test_a_manual_start_refuses_when_something_listens_on_the_node(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "run"
            run.mkdir(mode=0o755)
            node = run / "helper.sock"
            live = socket.socket(socket.AF_UNIX)
            live.bind(str(node))
            live.listen(1)  # e.g. systemd's arltuner-helper.socket
            try:
                with self.assertRaises(ServiceError) as ctx:
                    create_socket(node, group=None, euid=os.geteuid(), trust_parents=False)
                self.assertIn("in use", str(ctx.exception))
                self.assertIn("arltuner-helper.socket", str(ctx.exception))
                self.assertTrue(stat.S_ISSOCK(os.lstat(node).st_mode))  # not removed
                probe = socket.socket(socket.AF_UNIX)
                probe.connect(str(node))  # and still the same live listener
                probe.close()
            finally:
                live.close()
            create_socket(node, group=None, euid=os.geteuid(), trust_parents=False).close()  # now stale: replaced

    def test_an_error_other_than_refused_counts_as_in_use(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "run"
            run.mkdir(mode=0o755)
            node = run / "helper.sock"
            stale = socket.socket(socket.AF_UNIX)
            stale.bind(str(node))
            stale.close()
            with self.assertRaises(ServiceError):
                create_socket(node, group=None, euid=os.geteuid(), trust_parents=False, probe_fn=lambda p: True)
            self.assertTrue(os.path.lexists(node))


if __name__ == "__main__":
    unittest.main()
