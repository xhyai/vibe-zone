"""Keep public source typography readable in terminals and copyable examples."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PublicTextTests(unittest.TestCase):
    def test_tracked_text(self):
        if not (ROOT / ".git").exists():
            self.skipTest("Source checkout required for tracked-file lint")
        names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        problems = []
        for name in filter(None, names):
            if name.startswith("docs/codex") and name.endswith(".log"):
                continue
            try:
                text = (ROOT / name).read_text()
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            if any(chr(code) in text for code in (0x2014, 0x2013, 0x201c, 0x201d, 0x201e, 0x2018, 0x2019, 0x2026, 0x2192, 0x21d2, 0x2190)):
                problems.append(name)
        self.assertEqual(problems, [], "Unsupported punctuation in tracked files")
