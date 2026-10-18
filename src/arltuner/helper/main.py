"""``arltuner-helper``: the launcher around :class:`~arltuner.helper.core.Helper`. Real I/O is built here and only here.

**argv is the trust boundary.** The root helper is started by systemd (the shipped socket + service units) or by root by hand, but its argv is still treated
as input (D-029, D-043): when the effective uid is 0 the only argv accepted is *nothing*, ``--device``, or ``--device --socket`` (the units use the last):
``--sim`` is refused outright and ``--audit`` / ``--lock`` / ``--policy`` / ``--allow-taint-bit`` / ``--socket-path`` / ``--helper-toml`` are refused by name. As
root every path and every safety setting comes from installed constants (below) or from a root-owned, trust-checked file:

* policy ``/etc/arltuner/policy.toml``, audit log ``/var/lib/arltuner/audit.jsonl``, accepted state ``/etc/arltuner/accepted/``;
* lock: ``/run/arltuner/mailbox.lock`` (root-owned 0755 directory made by tmpfiles.d) via the CLI's ``default_lock_path()`` and its D-030 rule: the
  rule sees the installed, trusted ``/etc/arltuner/policy.toml``; a present-but-untrusted one is a named REFUSED (check ``TRUST_PATH``, exit 77);
* the expected first-write kernel-taint bits: ``/etc/arltuner/helper.toml``, key ``allow_taint_bits = [34]`` (a list of ints 0..63; the file may be
  absent = none; the other keys are ``idle_exit_s`` and ``[access]``, see :mod:`arltuner.helper.settings`; any other key is refused). Root-owned, not
  group/world-writable, parents root-owned (SPEC 4.4), checked on every ``vf.dump`` and at every socket accept.

Packaging (G-106, D-043): ``/usr/lib/arltuner/arltuner-helper --device --socket`` is the ``ExecStart`` of ``arltuner-helper.service`` (socket-activated by
``arltuner-helper.socket``); without ``--socket`` the protocol runs on stdin/stdout (the dev path). A non-root run (tests, ``--sim``) keeps the argv options
(``--socket`` then also takes ``--socket-path`` and ``--helper-toml``); ``--sim`` never applies the D-030 rule (simulator runs keep the per-user lock /
audit defaults). Exit codes: 0 stopped cleanly - 64 usage - 75 another helper runs - 77 refused to start.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from arltuner.telemetry.throttle import throttle_log_path
from typing import Any, BinaryIO, TextIO

from arltuner.cli import InstalledStateRefused, default_audit_path, default_lock_path
from arltuner.platform.preflight import preflight
from arltuner.topology import Topology, arl_s_285k, synthetic

from .config import HelperConfig
from .core import Helper
from .protocol import REFUSED, Writer, error_response
from .settings import load_helper_toml
from .trust import TrustError, check_code_tree, check_trusted_path

__all__ = [
    "INSTALLED_ACCEPTED_DIR", "INSTALLED_AUDIT", "INSTALLED_HELPER_TOML", "INSTALLED_POLICY", "PortOwningSampler", "build_config", "device_imc", "load_taint_bits",
    "main", "measure_wiring", "process_main", "root_argv_refusal", "run_socket", "sim_topology",
]

EXIT_USAGE = 64
EXIT_REFUSED_TO_START = 77

INSTALLED_POLICY = Path("/etc/arltuner/policy.toml")
INSTALLED_AUDIT = Path("/var/lib/arltuner/audit.jsonl")
INSTALLED_HELPER_TOML = Path("/etc/arltuner/helper.toml")
INSTALLED_ACCEPTED_DIR = Path("/etc/arltuner/accepted")

_ROOT_FORBIDDEN_NAMED = ("--sim", "--audit", "--lock", "--policy", "--allow-taint-bit", "--state-dir", "--socket-path", "--helper-toml")
ROOT_ARGVS = ((), ("--device",), ("--device", "--socket"))


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _Usage(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        raise _Usage(message or "")



def _accepted_label(directory: Path | None) -> str | None:
    """G-229: the throttle log's ``tuning_id`` = the accepted profile's name (``accepted:<name>``), read from ``CURRENT`` only when an event opens; None if absent."""
    if directory is None:
        return None
    try:
        text = (directory / "CURRENT").read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("profile = profiles/") and line.endswith(".toml"):
            return "accepted:" + line[len("profile = profiles/"):-len(".toml")][:64]
    return None

def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="arltuner-helper", description="arltuner root helper (JSON lines on stdin/stdout, or on a Unix socket with --socket). Non-root runs only; as root there is no argv but --device [--socket].", add_help=True, allow_abbrev=False)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sim", action="store_true", help="simulator: no hardware")
    mode.add_argument("--device", action="store_true", help="/dev/cpu/N/msr (hardware; needs root, a trusted code tree, --policy and --audit)")
    p.add_argument("--socket", action="store_true", help="serve one client at a time on a Unix socket (/run/arltuner/helper.sock when installed; systemd socket activation is used when it hands one over) instead of stdin/stdout")
    p.add_argument("--socket-path", metavar="FILE", help="--socket, non-root only: where to listen (default /run/arltuner/helper.sock)")
    p.add_argument("--helper-toml", metavar="FILE", help="--socket, non-root only: the settings file with [access] (owned by the invoking user; default /etc/arltuner/helper.toml)")
    p.add_argument("--policy", metavar="FILE", help="reviewed policy (--sim defaults to the built-in conservative one)")
    p.add_argument("--audit", metavar="FILE", help="audit log (--sim defaults to $XDG_STATE_HOME/arltuner/audit.jsonl (default ~/.local/state/arltuner/))")
    p.add_argument("--lock", metavar="FILE", help="machine-wide mailbox lock (non-root default: /run/arltuner/mailbox.lock when installed (D-030), else /run/lock/arltuner-mailbox.lock; --sim: always the latter)")
    p.add_argument("--allow-taint-bit", metavar="N", type=int, action="append", default=[], help="kernel taint bit the first write is expected to set (health gate)")
    p.add_argument("--sim-shape", metavar="P+E", help="--sim only: simulate another part, e.g. 6+8 (6 P-cores + 8 E-cores in clusters of four); default: the simulated reference CPU (8+16)")
    p.add_argument("--state-dir", metavar="DIR", help="--sim only: where the simulated curve store and measurement journal live (default: a fresh private temp directory; never /var/lib)")
    return p


def sim_topology(args: argparse.Namespace) -> Topology:
    """The topology of a ``--sim`` helper: the simulated reference CPU, or ``--sim-shape P+E`` (P-cores, E-cores; E in clusters of four; the first P-core and, with
    at least four, the fourth are favored). A fixture: nothing here is a statement about any machine."""
    shape = getattr(args, "sim_shape", None)
    if not shape:
        return arl_s_285k()
    m = re.fullmatch(r"([1-9][0-9]?)\+([1-9][0-9]?)", shape)
    if m is None or int(m.group(2)) % 4:
        raise _Usage(f"--sim-shape {shape!r}: expected P+E with E a multiple of 4, e.g. 6+8")
    p, e = int(m.group(1)), int(m.group(2))
    return synthetic(p, e, favored_p=(0, 3) if p > 3 else (0,))


def root_argv_refusal(argv: Sequence[str]) -> tuple[str, str] | None:
    """As root, argv must be empty, exactly ``--device``, or exactly ``--device --socket`` (D-029, D-043). Returns ``(check, message)`` for the first thing that is not."""
    for token in argv:
        if token in ("--device", "--socket"):
            continue
        name = token.split("=", 1)[0]
        if name == "--sim":
            return "ROOT_SIM", "--sim is refused when running as root: the simulator is not a way to run the helper with privileges"
        if name in _ROOT_FORBIDDEN_NAMED:
            return "ROOT_ARGV", f"{name} is refused when running as root: paths and safety settings come from installed, root-owned files only (SPEC 4.4)"
        return "ROOT_ARGV", f"argument {token!r} is refused when running as root: the helper takes only --device or --device --socket"
    if tuple(argv) not in ROOT_ARGVS:
        return "ROOT_ARGV", f"arguments {list(argv)!r} are refused when running as root: the helper takes only --device or --device --socket, once each, in that order"
    return None


def load_taint_bits(path: Path | str = INSTALLED_HELPER_TOML, *, lstat_fn: Callable[[str], Any] = os.lstat, root_uid: int = 0) -> frozenset[int]:
    """The expected first-write taint bits from the root-owned ``helper.toml`` (documented in the module docstring). Absent file = none.
    ``TrustError`` if the path is not trusted, ``ValueError`` if the content is not valid (:func:`~arltuner.helper.settings.parse_helper_toml`)."""
    return load_helper_toml(path, lstat_fn=lstat_fn, root_uid=root_uid).taint_bits


def device_imc(interval: float, lstat_fn: Callable[[str], Any] = os.lstat, *, popen: Callable[..., Any] | None = None) -> Any:
    """The DRAM-bandwidth source of the device sampler: one ``perf stat`` (``/usr/bin/perf``, fixed env, see :mod:`arltuner.telemetry.imc`) started and stopped
    with the sampler. Before each spawn perf and its parents must be root-owned, not group/world-writable and not a symlink (SPEC 4.4); otherwise the DRAM
    readings are unavailable with the reason and telemetry continues."""
    from arltuner.telemetry import imc

    def trust_check(path: str) -> None:
        try:
            check_trusted_path(path, want="file", lstat_fn=lstat_fn)
        except TrustError as exc:
            raise imc.PerfRefused(f"{exc} (perf is not trusted; DRAM bandwidth off)") from None

    kwargs: dict[str, Any] = {} if popen is None else {"popen": popen}
    return imc.ImcStat(max(100, round(interval * 1000)), perf=imc.PERF_PATH, trust_check=trust_check, **kwargs)


class PortOwningSampler:
    """A telemetry sampler that owns the MSR port it reads through and closes it when it stops *or fails to start* (no descriptor outlives a
    start/stop cycle)."""

    def __init__(self, port: Any, sampler: Any) -> None:
        self._port, self._sampler = port, sampler

    def _close(self) -> None:
        close = getattr(self._port, "close", None)
        if callable(close):
            close()

    def start(self) -> None:
        try:
            self._sampler.start()
        except BaseException:
            self._close()
            raise

    def stop(self) -> None:
        try:
            self._sampler.stop()
        finally:
            self._close()

    def snapshot(self) -> Any:
        return self._sampler.snapshot()


def build_config(args: argparse.Namespace, *, log: Callable[[str], None], root: bool = False, lstat_fn: Callable[[str], Any] = os.lstat) -> HelperConfig:
    """``root=True``: every path and safety setting is an installed constant, nothing is read from ``args``."""
    from arltuner.telemetry import Sampler

    if root and args.sim:
        raise _Usage("--sim is refused when running as root")
    # D-030: installed (trusted /etc/arltuner/policy.toml) => /run/arltuner/mailbox.lock (root-owned dir; /run/lock is 1777). A simulator never applies it.
    lock = Path(args.lock) if (args.lock and not root) else default_lock_path(lstat_fn=lstat_fn, consider_install=not args.sim)
    if args.sim:
        nowhere = Path("/nonexistent-arltuner-sim")

        topo = sim_topology(args)

        def sim_port():  # type: ignore[no-untyped-def]
            from arltuner.msr import SimConfig, SimMsrPort

            return SimMsrPort(SimConfig.measured(per_point=True, reject_zero_fields=True, topology=topo))

        return HelperConfig(
            lock_path=lock,
            audit_path=Path(args.audit) if args.audit else default_audit_path(os.environ, consider_install=False),
            policy_path=Path(args.policy) if args.policy else None,
            port_factory=sim_port,
            topology_fn=lambda: topo,
            preflight_fn=lambda: preflight(Path("/"), topology=topo, helper_service=False),
            sampler_factory=lambda interval: Sampler(
                None, topo, hwmon_root=nowhere / "hwmon", powercap_root=nowhere / "powercap", cpu_root=nowhere / "cpu", interval_s=interval,
                throttle_log=throttle_log_path(Path(args.audit)) if args.audit else None,
            ),
            simulator=True,
            enforce_trust=False,
            log=log,
        )
    from arltuner.health import make_health_gate
    from arltuner.msr import DevCpuMsrPort
    from arltuner.telemetry import TelemetryMsr
    from arltuner.topology import detect

    def sampler_factory(interval: float) -> PortOwningSampler:
        port = DevCpuMsrPort(writable=False)
        try:
            return PortOwningSampler(port, Sampler(TelemetryMsr(port), imc=device_imc(interval, lstat_fn), interval_s=interval,
                                                   throttle_log=throttle_log_path(audit), tuning_id_fn=lambda: _accepted_label(INSTALLED_ACCEPTED_DIR if root else None)))
        except BaseException:
            port.close()
            raise

    if root:
        audit, policy = INSTALLED_AUDIT, INSTALLED_POLICY
        gate_factory = lambda: make_health_gate(Path("/"), allowed_new_taint_bits=load_taint_bits(INSTALLED_HELPER_TOML))  # noqa: E731
    else:
        if not args.policy or not args.audit:
            raise _Usage("--device needs an explicit --policy and --audit (root-owned, not group/world-writable)")
        audit, policy = Path(args.audit), Path(args.policy)
        bits = frozenset(args.allow_taint_bit)
        gate_factory = lambda: make_health_gate(Path("/"), allowed_new_taint_bits=bits)  # noqa: E731
    return HelperConfig(
        lock_path=lock,
        audit_path=audit,
        policy_path=policy,
        cpuinfo_path=Path("/proc/cpuinfo"),
        accepted_dir=INSTALLED_ACCEPTED_DIR if root else None,  # G-106/G-107: EXPECTED-words.txt, trust-checked by the op that reads it
        port_factory=lambda: DevCpuMsrPort(writable=True),
        topology_fn=detect,
        preflight_fn=lambda: preflight(Path("/"), helper_service=bool(getattr(args, "socket", False))),
        sampler_factory=sampler_factory,
        simulator=False,
        enforce_trust=True,
        health_gate_factory=gate_factory,
        # socket mode: the connected peer's uid names the operator per connection (Helper.serve); the PKEXEC_UID of the environment is the stdio dev path only
        operator="helper" if getattr(args, "socket", False) else f"gui:uid{os.environ.get('PKEXEC_UID', 'unknown')}",
        log=log,
    )


def measure_wiring(args: argparse.Namespace, *, lstat_fn: Callable[[str], Any] = os.lstat) -> Any:
    """G-111: cpufreq / MSR-read / store / journal for ``vf.measure``, ``curves.*`` and ``mem.read``. ``--sim``: the model and a scratch ``--state-dir`` (or a fresh
    private temp directory); hardware: the installed root-owned paths only."""
    from . import ops_measure

    if args.sim:
        state_dir = getattr(args, "state_dir", None)
        return ops_measure.sim_wiring(
        Path(state_dir) if state_dir else Path(tempfile.gettempdir()) / f"arltuner-sim-{os.getpid()}-{secrets.token_hex(4)}", topology=sim_topology(args)
    )  # created lazily, 0700
    return ops_measure.device_wiring(lstat_fn=lstat_fn)


def _startup_refusal(args: argparse.Namespace, euid: int) -> str | None:
    """Hardware mode only: root, and the whole installed package tree trusted (never a git checkout or ~/.local)."""
    if args.sim:
        return None
    if euid != 0:
        return "--device needs root (the arltuner-helper systemd units start it)"
    try:
        check_code_tree(Path(os.path.abspath(__file__)).parent.parent)  # the arltuner package: every dir and file below it
    except TrustError as exc:
        return f"the helper's code is not in a root-owned, non-writable tree (SPEC 4.4): {exc}"
    return None


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: TextIO | None = None,
    euid_fn: Callable[[], int] = getattr(os, "geteuid", lambda: 0),
    lstat_fn: Callable[[str], Any] = os.lstat,
) -> int:
    err = sys.stderr if stderr is None else stderr
    out = sys.stdout.buffer if stdout is None else stdout
    inp = sys.stdin.buffer if stdin is None else stdin
    argv = list(sys.argv[1:] if argv is None else argv)
    euid = euid_fn()

    def log(text: str) -> None:
        err.write(f"arltuner-helper: {text}\n")
        err.flush()

    def refuse(check: str, message: str) -> int:
        Writer(out).send(error_response(None, REFUSED, message, {"check": check}))
        log(f"refused to start: {message}")
        return EXIT_REFUSED_TO_START

    try:
        if euid == 0:
            bad = root_argv_refusal(argv)
            if bad is not None:
                return refuse(*bad)
            args = argparse.Namespace(sim=False, device=True, policy=None, audit=None, lock=None, allow_taint_bit=[], state_dir=None, socket="--socket" in argv,
                                      socket_path=None, helper_toml=None)
        else:
            args = _parser().parse_args(argv)
            if (args.socket_path or args.helper_toml) and not args.socket:
                raise _Usage("--socket-path and --helper-toml need --socket")
            if args.device and (not args.policy or not args.audit):
                raise _Usage("--device needs an explicit --policy and --audit (root-owned, not group/world-writable)")
        problem = _startup_refusal(args, euid)
        if problem is not None:
            return refuse("STARTUP", problem)
        cfg = build_config(args, log=log, root=euid == 0, lstat_fn=lstat_fn)
    except InstalledStateRefused as exc:
        return refuse("TRUST_PATH", str(exc).strip())
    except _Usage as exc:
        if str(exc):
            err.write(f"arltuner-helper: {exc}\n")
        return EXIT_USAGE if str(exc) else 0
    helper = Helper(cfg)
    helper.measure_wiring = measure_wiring(args, lstat_fn=lstat_fn)
    scratch = helper.measure_wiring.journal_path.parent if args.sim and not getattr(args, "state_dir", None) else None  # our own mkdtemp: removed on exit
    helper.install_signal_handlers()
    try:
        if getattr(args, "socket", False):
            return run_socket(helper, args, euid, log=log, lstat_fn=lstat_fn)
        return helper.run(inp, out)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def run_socket(
    helper: Helper, args: argparse.Namespace, euid: int, *, log: Callable[[str], None], lstat_fn: Callable[[str], Any] = os.lstat,
    environ: dict[str, str] | None = None, getpid: Callable[[], int] = os.getpid,
) -> int:
    """``--socket``: the helper as a service (D-043). Root: the installed socket path and ``/etc/arltuner/helper.toml``, nothing from argv. Non-root (tests,
    ``--sim``): ``--socket-path`` / ``--helper-toml``, whose files must belong to the invoking user the way root's belong to root."""
    from .service import SOCKET_PATH, CachedSettings, Service, ServiceError, adopt_socket, create_socket, listen_fds

    env = os.environ if environ is None else environ
    if euid == 0:
        sock_path, toml_path = SOCKET_PATH, INSTALLED_HELPER_TOML
    else:
        sock_path = Path(args.socket_path) if args.socket_path else SOCKET_PATH
        toml_path = Path(args.helper_toml) if args.helper_toml else INSTALLED_HELPER_TOML
    try:
        fd = listen_fds(env, getpid())
    except ServiceError as exc:
        log(f"refused to start: {exc}")
        return EXIT_REFUSED_TO_START
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):  # nothing we start may think it was handed a socket
        env.pop(name, None)

    def load_settings():  # type: ignore[no-untyped-def]
        return load_helper_toml(toml_path, lstat_fn=lstat_fn, root_uid=euid, check_parents=euid == 0)

    settings = CachedSettings(load_settings, toml_path, lstat_fn)  # re-read through the trust check only when the file changes

    def make_socket():  # type: ignore[no-untyped-def]
        if fd is not None:
            return adopt_socket(fd, euid=euid, lstat_fn=lstat_fn)
        try:
            group = settings().access.group  # optional: the node is handed to this group; the peer check is the gate either way
        except (TrustError, ValueError, OSError) as exc:
            log(f"helper.toml unreadable ({exc}); the socket is made without a group and nobody is admitted until it is fixed")
            group = None
        return create_socket(sock_path, group=group, euid=euid, lstat_fn=lstat_fn, trust_parents=euid == 0)

    return Service(helper, make_socket, path=sock_path, settings_loader=settings, activated=fd is not None, log=log).run()


def process_main() -> None:
    """Entry point of the real process: ``main()`` then a hard exit. The stdin reader thread may still sit in a blocking read (a signal ended the
    run, not EOF) and Python aborts at interpreter shutdown over a daemon thread holding the stdin buffer lock; everything is flushed first."""
    code = main()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)
