"""The CLI prints emoji (✅ ⚠️ ❌); a Windows console defaults to cp1252 and would crash with UnicodeEncodeError."""
import io
import sys
import unittest
from unittest import mock

from mme import cli


class Utf8ConsoleTest(unittest.TestCase):
    def test_reconfigures_a_cp1252_stdout_to_print_emoji(self):
        raw = io.BytesIO()
        fake = io.TextIOWrapper(raw, encoding="cp1252")
        with mock.patch.object(sys, "stdout", fake), mock.patch.object(sys, "stderr", fake):
            cli.utf8_console()
            print("✅ gate green")
            sys.stdout.flush()
        self.assertIn("✅".encode("utf-8"), raw.getvalue())
