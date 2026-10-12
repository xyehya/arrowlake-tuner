"""``arltuner-helper``: the launcher around :class:`~arltuner.helper.core.Helper`. Real I/O is built here and only here.

**argv is the trust boundary.** The helper is started by pkexec (SPEC 4.1), and everything after the program name on that command line is chosen by
the unprivileged caller. So when the effective uid is 0 the helper accepts *no* argv at all (a lone ``--device`` is tolerated, nothing else):
``--sim`` is refused outright and ``--audit`` / ``--lock`` / ``--policy`` / ``--allow-taint-bit`` are refused by name. As root every path and every
safety setting comes from installed constants (below) or from a root-owned, trust-checked file:

* policy ``/etc/arltuner/policy.toml``, audit log ``/var/lib/arltuner/audit.jsonl``, accepted state ``/etc/arltuner/accepted/``;
* lock: ``/run/arltuner/mailbox.lock`` (root-owned 0755 directory made by tmpfiles.d) via the CLI's ``default_lock_path()`` and its D-030 rule: the
  rule sees the installed, trusted ``/etc/arltuner/policy.toml``; a present-but-untrusted one is a named REFUSED (check ``TRUST_PATH``, exit 77);
* the expected first-write kernel-taint bits: ``/etc/arltuner/helper.toml``, key ``allow_taint_bits = [34]`` (a list of ints 0..63; the file may be
  absent = none; any other key is refused). Root-owned, not group/world-writable, parents root-owned (SPEC 4.4), checked on every ``vf.dump``.

Packaging (G-106): the polkit action pins ``org.freedesktop.policykit.exec.path`` to ``/usr/lib/arltuner/arltuner-helper``, started as
``pkexec /usr/lib/arltuner/arltuner-helper --device``. A non-root run (tests, ``--sim``) keeps the argv options; ``--sim`` never applies the D-030 rule
(simulator runs keep the per-user lock / audit defaults). Exit codes: 0 stopped cleanly - 64 usage - 75 another helper runs -
77 refused to start.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from arltuner.cli import InstalledStateRefused, default_audit_path, default_lock_path
from arltuner.platform.preflight import preflight
from arltuner.topology import Topology, arl_s_285k, synthetic

from .config import HelperConfig
from .core import Helper
from .protocol import REFUSED, Writer, error_response
from .trust import TrustError, check_code_tree, check_trusted_path, open_checked

__all__ = [
    "INSTALLED_ACCEPTED_DIR", "INSTALLED_AUDIT", "INSTALLED_HELPER_TOML", "INSTALLED_POLICY", "PortOwningSampler", "build_config", "device_imc", "load_taint_bits",
    "main", "measure_wiring", "process_main", "root_argv_refusal", "sim_topology",
]

EXIT_USAGE = 64
EXIT_REFUSED_TO_START = 77

INSTALLED_POLICY = Path("/etc/arltuner/policy.toml")
INSTALLED_AUDIT = Path("/var/lib/arltuner/audit.jsonl")
INSTALLED_HELPER_TOML = Path("/etc/arltuner/helper.toml")
INSTALLED_ACCEPTED_DIR = Path("/etc/arltuner/accepted")

_ROOT_FORBIDDEN_NAMED = ("--sim", "--audit", "--lock", "--policy", "--allow-taint-bit", "--state-dir")


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _Usage(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        raise _Usage(message or "")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="arltuner-helper", description="arltuner root helper (JSON lines on stdin/stdout). Non-root runs only; as root there is no argv.", add_help=True, allow_abbrev=False)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sim", action="store_true", help="simulator: no hardware")
    mode.add_argument("--device", action="store_true", help="/dev/cpu/N/msr (hardware; needs root, a trusted code tree, --policy and --audit)")
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
    """As root, argv must be empty (or exactly ``--device``). Returns ``(check, message)`` for the first thing that is not."""
    for token in argv:
        if token == "--device":
            continue
        name = token.split("=", 1)[0]
        if name == "--sim":
            return "ROOT_SIM", "--sim is refused when running as root: the simulator is not a way to run the helper with privileges"
        if name in _ROOT_FORBIDDEN_NAMED:
            return "ROOT_ARGV", f"{name} is refused when running as root: paths and safety settings come from installed, root-owned files only (SPEC 4.4)"
        return "ROOT_ARGV", f"argument {token!r} is refused when running as root: the helper takes no arguments (polkit exec.path, no argv)"
    return None


def load_taint_bits(path: Path | str = INSTALLED_HELPER_TOML, *, lstat_fn: Callable[[str], Any] = os.lstat, root_uid: int = 0) -> frozenset[int]:
    """The expected first-write taint bits from the root-owned ``helper.toml`` (documented in the module docstring). Absent file = none.
    ``TrustError`` if the path is not trusted, ``ValueError`` if the content is not exactly ``allow_taint_bits = [int, ...]`` (0..63)."""
    check_trusted_path(path, want="file", must_exist=False, lstat_fn=lstat_fn, root_uid=root_uid)
    try:
        fd = open_checked(path, write=False, create=False, euid=root_uid, what="helper.toml")
    except FileNotFoundError:
        return frozenset()
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(65536)
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: not valid TOML: {exc}") from None
    extra = sorted(set(doc) - {"allow_taint_bits"})
    if extra:
        raise ValueError(f"{path}: unknown key(s) {extra}")
    bits = doc.get("allow_taint_bits", [])
    if not isinstance(bits, list) or not all(isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 63 for b in bits):
        raise ValueError(f"{path}: allow_taint_bits must be a list of integers 0..63")
    return frozenset(bits)


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
            preflight_fn=lambda: preflight(Path("/"), topology=topo),
            sampler_factory=lambda interval: Sampler(
                None, topo, hwmon_root=nowhere / "hwmon", powercap_root=nowhere / "powercap", cpu_root=nowhere / "cpu", interval_s=interval
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
            return PortOwningSampler(port, Sampler(TelemetryMsr(port), imc=device_imc(interval, lstat_fn), interval_s=interval))
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
        preflight_fn=lambda: preflight(Path("/")),
        sampler_factory=sampler_factory,
        simulator=False,
        enforce_trust=True,
        health_gate_factory=gate_factory,
        operator=f"gui:uid{os.environ.get('PKEXEC_UID', 'unknown')}",
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
        return "--device needs root (start it through pkexec)"
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
            args = argparse.Namespace(sim=False, device=True, policy=None, audit=None, lock=None, allow_taint_bit=[], state_dir=None)
        else:
            args = _parser().parse_args(argv)
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
        return helper.run(inp, out)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


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
