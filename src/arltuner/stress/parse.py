"""Incremental parsers of the stress children's output (pure; no IO). The output is a *user process's*: untrusted text, kept printable and bounded.

stress-ng (verifying methods): a ``stress-ng: fail:`` / ``error:`` line is an error; the closing ``failed: N`` counts the failed stressors. [UNVERIFIED on this
machine beyond the 0.1x format recorded in tests/stress/fakebin: confirm the strings on the reference machine.]
stressapptest: ``Status: PASS|FAIL``; ``Report Error`` / ``Hardware Error`` / ``Process Error`` / ``Page Error`` lines and ``miscompare`` mentions are errors;
``Found N hardware incidents``. SPEC section 9.3: a miscompare class only *suggests* a path; nothing here says "pass" or "proves".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["MAX_ERROR_LINES", "MAX_LINE_CHARS", "StressNgScanner", "StressappScanner", "Verdict", "clean_line"]

MAX_ERROR_LINES = 20
MAX_LINE_CHARS = 200
_STRESS_NG_LINE = re.compile(r"^stress-ng:\s+(\w+):\s+(?:\[\d+\]\s+)?(.*)$")
_FAILED_COUNT = re.compile(r"^failed:\s+(\d+)\b")
_APP_STATUS = re.compile(r"\bStatus:\s+(PASS|FAIL)\b")
_APP_ERROR = re.compile(r"\b(?:Hardware|Report|Process|Page)\s+Error\b|\bmiscompare", re.I)
_APP_INCIDENTS = re.compile(r"\bFound\s+(\d+)\s+hardware\s+incidents?\b", re.I)


def clean_line(text: str) -> str:
    """Printable ASCII only, bounded: a child cannot smuggle control characters or a megabyte line into a stored record."""
    return "".join(c if " " <= c <= "~" else "?" for c in text.strip()[:MAX_LINE_CHARS])


@dataclass
class Verdict:
    errors: int
    error_lines: list[str]
    ok: bool  # no error line and exit code 0 (never shown as "pass": D-019)


class _Scanner:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _keep(self, text: str) -> None:
        if len(self.lines) < MAX_ERROR_LINES:
            self.lines.append(clean_line(text))


class StressNgScanner(_Scanner):
    def __init__(self) -> None:
        super().__init__()
        self.fail_lines = 0
        self.failed: int | None = None
        self.success = False

    def feed(self, line: str) -> None:
        m = _STRESS_NG_LINE.match(line.strip())
        if not m:
            return
        level, body = m.group(1).lower(), m.group(2)
        if level in ("fail", "error"):
            self.fail_lines += 1
            self._keep(line)
            return
        count = _FAILED_COUNT.match(body)
        if count:
            self.failed = int(count.group(1))
        elif body.startswith("successful run completed"):
            self.success = True

    @property
    def errors(self) -> int:
        return self.failed if self.failed else self.fail_lines  # "failed: N" counts stressors; the fail lines are its details (and all there is when stress-ng died early)

    def finish(self, returncode: int | None) -> Verdict:
        return Verdict(self.errors, list(self.lines), self.errors == 0 and returncode == 0)


class StressappScanner(_Scanner):
    def __init__(self) -> None:
        super().__init__()
        self.error_count = 0
        self.incidents = 0
        self.status: str | None = None

    def feed(self, line: str) -> None:
        status = _APP_STATUS.search(line)
        if status:
            self.status = status.group(1)
            if self.status == "FAIL":
                self._keep(line)
            return
        inc = _APP_INCIDENTS.search(line)
        if inc:
            self.incidents = max(self.incidents, int(inc.group(1)))
        if _APP_ERROR.search(line):
            self.error_count += 1
            self._keep(line)

    @property
    def errors(self) -> int:
        n = max(self.error_count, self.incidents)
        return max(n, 1) if self.status == "FAIL" else n

    def finish(self, returncode: int | None) -> Verdict:
        return Verdict(self.errors, list(self.lines), self.errors == 0 and returncode == 0)
