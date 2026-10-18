"""scratch (deleted before commit): python3 tools/g226_pt.py [dir|module ...] -- Python tests only; per-test digest in .g226/digest.txt."""
import io
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
args = sys.argv[1:] or ["tests"]
suite = unittest.TestSuite()
for a in args:
    if (ROOT / a).is_dir():
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / a), top_level_dir=str(ROOT)))
    else:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(a))
sys.stderr = open(os.devnull, "w")
buf = io.StringIO()
result = unittest.TextTestRunner(stream=buf, verbosity=1).run(suite)
lines = []
by_mod: dict[str, int] = {}
for t, tb in result.errors + result.failures:
    name = str(t)
    mod = name.split("(")[-1].rstrip(")")
    mod = ".".join(mod.split(".")[:-1]) if "." in mod else name[:60]
    by_mod[mod] = by_mod.get(mod, 0) + 1
    last = [l for l in tb.strip().splitlines() if l.strip()][-1][:300]
    lines.append(f"{name.split(' ')[0]} [{mod}] :: {last}")
(ROOT / ".g226" / "digest.txt").write_text("\n".join(lines))
print(f"ran {result.testsRun}, failures {len(result.failures)}, errors {len(result.errors)}, skipped {len(result.skipped)}")
for mod, n in sorted(by_mod.items()):
    print(f"  {n:4d} {mod}")
