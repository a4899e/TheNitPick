"""Tests for the reviewer-edit report in scripts/codex_review.py.

Run: python3 -m unittest discover -s tests
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from codex_review import Git, edit_report, ignored_paths  # noqa: E402

FILE = "sha256:" + "0" * 64 + " mode 644"


def none_ignored(paths):
    return set()


class EditReportTest(unittest.TestCase):
    def test_changes_to_the_reviewed_tree_are_flagged(self) -> None:
        before = {"a.py": FILE, "b.py": FILE}
        summary, _ = edit_report(before, {"a.py": FILE.replace("644", "755")}, "h1", "h2",
                                 none_ignored)
        text = "".join(summary)
        self.assertIn("REVIEWER CHANGED THE REVIEWED TREE", text)
        self.assertIn("HEAD moved: h1 -> h2", text)
        self.assertIn("changed: a.py", text)
        self.assertIn("deleted: b.py", text)

    def test_gitignored_output_is_grouped_and_listed_apart(self) -> None:
        after = {"a.py": FILE, **{f"build/gen/{i}.js": FILE for i in range(50)}, "t.py": FILE}
        summary, full = edit_report({"a.py": FILE}, after, "h", "h", lambda p: {"build"})
        text = "".join(summary)
        self.assertIn("reviewed tree unchanged", text)
        self.assertIn("created: t.py", text)
        self.assertIn("build/  50", text)
        self.assertNotIn("build/gen/0.js", text)
        self.assertEqual(len(full), 50)

    def test_new_gitignore_turns_grouping_off(self) -> None:
        # A .gitignore the reviewer writes could ignore itself and its neighbours.
        after = {"a.py": FILE, "src/.gitignore": FILE, "src/scratch.py": FILE}
        summary, full = edit_report({"a.py": FILE}, after, "h", "h",
                                    lambda p: {"src/.gitignore", "src/scratch.py"})
        text = "".join(summary)
        self.assertIn("created: src/.gitignore", text)
        self.assertIn("created: src/scratch.py", text)
        self.assertEqual(full, [])

    def test_new_unlistable_directory_is_not_a_tree_change(self) -> None:
        after = {"a.py": FILE, "build/tmp/locked": "unlistable directory (errno 13)"}
        summary, _ = edit_report({"a.py": FILE}, after, "h", "h", lambda p: {"build"})
        self.assertIn("reviewed tree unchanged", "".join(summary))

    def test_reviewed_directory_made_unlistable_is_a_tree_change(self) -> None:
        after = {"a.py": FILE, "src": "unlistable directory (errno 13)"}
        summary, _ = edit_report({"a.py": FILE, "src/x.py": FILE}, after, "h", "h", none_ignored)
        text = "".join(summary)
        self.assertIn("REVIEWER CHANGED THE REVIEWED TREE", text)
        self.assertIn("now unlistable: src/", text)


@unittest.skipIf(shutil.which("git") is None, "needs git")
class IgnoredPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / ".gitignore").write_text("build\n")
        self.git = Git(self.repo / "no-hooks")

    def test_pathspec_magic_is_taken_literally(self) -> None:
        for name in (":build", ":!x", "build"):
            (self.repo / name).write_text("x")
        self.assertEqual(ignored_paths(self.git, self.repo, [":build", ":!x", "build"]),
                         {"build"})

    @unittest.skipIf(os.name == "nt", "non-UTF-8 names are POSIX-only")
    def test_odd_names_round_trip(self) -> None:
        (self.repo / ".gitignore").write_text("*.log\n")
        odd = os.fsdecode(b"\xff.log")
        names = [odd, "a b.log", "keep.txt"]
        for name in names:
            (self.repo / name).write_text("x")
        self.assertEqual(ignored_paths(self.git, self.repo, names), {odd, "a b.log"})


@unittest.skipIf(os.name == "nt" or shutil.which("git") is None, "needs git and a POSIX shell")
class WrapperOutputTest(unittest.TestCase):
    """The whole wrapper, with a fake Codex that writes build output."""

    def test_output_ends_with_status_and_summary(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        (repo / ".gitignore").write_text("build/\n")
        (repo / "a.py").write_text("a\n")
        run("add", "-A")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        fake = tmp / "codex"
        fake.write_text(
            "#!/bin/sh\n"
            'while [ $# -gt 0 ]; do [ "$1" = -C ] && cd "$2"; shift; done\n'
            "cat >/dev/null\n"
            "mkdir -p build/gen\n"
            "for i in 1 2 3 4 5; do echo $i > build/gen/$i.js; done\n"
            "echo t > scratch_test.py\n"
            "printf 'No findings.\\ntokens used\\n1,234\\n'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        out = tmp / "out" / "c1.txt"
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPTS / "codex_review.py"), "--repo", str(repo),
             "--ref", "main", "--base", "main", "--out", str(out), "--codex", str(fake)],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = out.read_text()
        self.assertNotIn("build/gen/1.js", text)
        tail = text[text.index("No findings."):]
        self.assertIn("=== tokens=1234", tail)
        self.assertIn("=== codex_review.py: exit=0", tail)
        self.assertIn("created: scratch_test.py", tail)
        self.assertIn("build/  5", tail)
        listing = out.with_name("c1-gitignored.txt")
        self.assertEqual(len(listing.read_text().splitlines()), 5)
        self.assertIn(str(listing), tail)


if __name__ == "__main__":
    unittest.main()
