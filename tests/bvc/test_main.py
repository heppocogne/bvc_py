# main / __main__ / cli の入口(M0-3)。

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from bvc import __version__, cli
from bvc.main import main


class TestMain(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_help(self):
        code, out, _ = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage: bvc", out)
        for opt in ("-C", "--json", "-q"):
            self.assertIn(opt, out)

    def test_version(self):
        code, out, _ = self.run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    def test_no_command_is_usage_error(self):
        # 仕様書 2.2節: 引数の誤りは 2
        code, _, err = self.run_main([])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("usage: bvc", err)

    def test_unknown_option_is_usage_error(self):
        code, _, _ = self.run_main(["--no-such-option"])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_python_m_bvc(self):
        # I-13: python -m bvc で起動でき、終了コードが引き継がれる
        r = subprocess.run(
            [sys.executable, "-m", "bvc", "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"usage: bvc", r.stdout)
        r = subprocess.run(
            [sys.executable, "-m", "bvc"], stdin=subprocess.DEVNULL, capture_output=True
        )
        self.assertEqual(r.returncode, cli.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
