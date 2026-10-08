import unittest

import arltuner


class Smoke(unittest.TestCase):
    def test_package_imports(self):
        self.assertEqual(arltuner.__version__, "0.0.1")


if __name__ == "__main__":
    unittest.main()
