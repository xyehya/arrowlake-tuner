"""``arltuner-helper``: the launcher around :class:`~arltuner.helper.core.Helper`. Real I/O is built here and only here.

``--sim`` is the simulator (no hardware, no trust checks: temp paths cannot be root-owned). ``--device`` is the hardware: it refuses to start
unless it runs as root from a trusted code tree (SPEC 4.4), needs an explicit ``--policy`` and ``--audit`` (both trust-checked on use), and gates
every mailbox op on its own fresh preflight (D-027 c). It is started by pkexec (SPEC 4.1) and never by a developer shell as a casual command;
the orchestrator runs it on hardware only with the operator's go-ahead (AGENTS.md). Exit codes: 0 stopped cleanly - 64 usage - 75 another
helper runs - 77 refused to start.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, TextIO

from arltuner.cli import default_audit_path, default_lock_path
from arltuner.platform.preflight import preflight
from arltuner.topology import arl_s_285k

from .config import HelperConfig
from .core import Helper
from .protocol import REFUSED, Writer, error_response
from .trust import TrustError, check_code_tree

__all__ = ["build_config", "main", "process_main"]

EXIT_USAGE = 64
EXIT_REFUSED_TO_START = 77


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _Usage(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        raise _Usage(message or "")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="arltuner-helper", description=__doc__.split("\n\n")[0], add_help=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sim", action="store_true", help="simulator: no hardware")
    mode.add_argument("--device", action="store_true", help="/dev/cpu/N/msr (hardware; needs root, a trusted code tree, --policy and --audit)")
    p.add_argument("--policy", metavar="FILE", help="reviewed policy (required for --device; --sim defaults to the built-in conservative one)")
    p.add_argument("--audit", metavar="FILE", help="audit log (required for --device; --sim defaults to the CLI's ~/.local/state/linuxmon/audit.jsonl)")
    p.add_argument("--lock", metavar="FILE", help="machine-wide mailbox lock (default: the CLI's /run/lock/linuxmon-mailbox.lock)")
    p.add_argument("--allow-taint-bit", metavar="N", type=int, action="append", default=[], help="kernel taint bit the first write is expected to set (health gate)")
    return p


def build_config(args: argparse.Namespace, *, log: Callable[[str], None]) -> HelperConfig:
    from arltuner.telemetry import Sampler

    lock = Path(args.lock) if args.lock else default_lock_path()
    if args.sim:
        nowhere = Path("/nonexistent-arltuner-sim")

        def sim_port():  # type: ignore[no-untyped-def]
            from arltuner.msr import SimConfig, SimMsrPort

            return SimMsrPort(SimConfig.measured(per_point=True, reject_zero_fields=True))

        return HelperConfig(
            lock_path=lock,
            audit_path=Path(args.audit) if args.audit else default_audit_path(os.environ),
            policy_path=Path(args.policy) if args.policy else None,
            port_factory=sim_port,
            topology_fn=arl_s_285k,
            preflight_fn=lambda: preflight(Path("/")),
            sampler_factory=lambda interval: Sampler(None, hwmon_root=nowhere / "hwmon", powercap_root=nowhere / "powercap", interval_s=interval),
            simulator=True,
            enforce_trust=False,
            log=log,
        )
    from arltuner.health import make_health_gate
    from arltuner.msr import DevCpuMsrPort
    from arltuner.telemetry import TelemetryMsr
    from arltuner.topology import detect

    bits = frozenset(args.allow_taint_bit)
    return HelperConfig(
        lock_path=lock,
        audit_path=Path(args.audit),
        policy_path=Path(args.policy),
        port_factory=lambda: DevCpuMsrPort(writable=True),
        topology_fn=detect,
        preflight_fn=lambda: preflight(Path("/")),
        sampler_factory=lambda interval: Sampler(TelemetryMsr(DevCpuMsrPort(writable=False)), interval_s=interval),
        simulator=False,
        enforce_trust=True,
        health_gate_factory=lambda: make_health_gate(Path("/"), allowed_new_taint_bits=bits),
        operator=f"gui:uid{os.environ.get('PKEXEC_UID', 'unknown')}",
        log=log,
    )


def _startup_refusal(args: argparse.Namespace) -> str | None:
    """Hardware mode only: root, and code from a trusted tree (never a git checkout or ~/.local)."""
    if args.sim:
        return None
    if os.geteuid() != 0:
        return "--device needs root (start it through pkexec)"
    try:
        check_code_tree(Path(__file__).resolve().parent)
    except TrustError as exc:
        return f"the helper's code is not in a root-owned, non-writable tree (SPEC 4.4): {exc}"
    return None


def main(argv: Sequence[str] | None = None, *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None, stderr: TextIO | None = None) -> int:
    err = sys.stderr if stderr is None else stderr
    out = sys.stdout.buffer if stdout is None else stdout
    inp = sys.stdin.buffer if stdin is None else stdin

    def log(text: str) -> None:
        err.write(f"arltuner-helper: {text}\n")
        err.flush()

    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        if args.device and (not args.policy or not args.audit):
            raise _Usage("--device needs an explicit --policy and --audit (root-owned, not group/world-writable)")
        refusal = _startup_refusal(args)
        if refusal is not None:
            Writer(out).send(error_response(None, REFUSED, refusal, {"check": "STARTUP"}))
            log(f"refused to start: {refusal}")
            return EXIT_REFUSED_TO_START
        cfg = build_config(args, log=log)
    except _Usage as exc:
        if str(exc):
            err.write(f"arltuner-helper: {exc}\n")
        return EXIT_USAGE if str(exc) else 0
    helper = Helper(cfg)
    helper.install_signal_handlers()
    return helper.run(inp, out)


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
