"""scratch: python3 .g226/pt.py [start_dir_or_module ...] -- run the Python tests only (no node/cargo hooks); log to .g226/last.log, print the summary."""
import io
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
import os
sys.stderr = open(os.devnull, "w")
buf = io.StringIO()
result = unittest.TextTestRunner(stream=buf, verbosity=1).run(suite)
(ROOT / ".g226" / ("last-" + args[0].replace("/", ".") + ".log")).write_text(buf.getvalue())
bad = [(str(t), "ERROR") for t, _ in result.errors] + [(str(t), "FAIL") for t, _ in result.failures]
by_mod = {}
for name, kind in bad:
    mod = name.split("(")[-1].rstrip(")").rsplit(".", 1)[0] if "(" in name else name[:60]
    by_mod[mod] = by_mod.get(mod, 0) + 1
print(f"ran {result.testsRun}, failures {len(result.failures)}, errors {len(result.errors)}, skipped {len(result.skipped)}")
for mod, n in sorted(by_mod.items()):
    print(f"  {n:4d} {mod}")
