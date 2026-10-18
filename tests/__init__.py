import os as _os
import unittest as _unittest


def load_tests(loader, standard_tests, pattern):  # G226-SCRATCH: removed before commit
    root = _os.path.join(_os.path.dirname(__file__), "..")
    target = _os.path.join(root, ".g226", "targets.txt")
    if not _os.path.exists(target):
        return standard_tests
    names = [l.strip() for l in open(target) if l.strip() and not l.startswith("#")]
    suite = _unittest.TestSuite()
    for n in names:
        if "/" in n:
            suite.addTests(loader.discover(_os.path.join(root, n), top_level_dir=_os.path.abspath(root)))
        else:
            suite.addTests(loader.loadTestsFromName(n))
    return suite
