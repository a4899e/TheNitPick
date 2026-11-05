"""Tests for scripts/metrics.py, run as a command against a throwaway database.

Run: python3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

METRICS = Path(__file__).resolve().parent.parent / "scripts" / "metrics.py"
SEL = ["--repo", "R", "--branch", "b"]


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "THENITPICK_DB": str(Path(self.tmp.name) / "m.db")}
        self.findings_file = Path(self.tmp.name) / "f.json"
        self.findings_file.write_text(json.dumps([{"severity": "P2", "title": "t"}]))

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(METRICS), *args], env=self.env,
                              capture_output=True, text=True, check=False)

    def ok(self, *args: str) -> dict:
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def refused(self, *args: str) -> str:
        result = self.cli(*args)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def start_cycle(self, reviewer: str = "codex") -> dict:
        return self.ok("cycle-start", *SEL, "--reviewer", reviewer)

    # -- naming the run by repo and branch ------------------------------------

    def test_repo_and_branch_name_the_open_run(self) -> None:
        run = self.ok("run-start", *SEL)
        cycle = self.start_cycle()
        self.assertEqual((cycle["run"], cycle["repo"], cycle["branch"]), (run["run"], "R", "b"))
        ended = self.ok("cycle-end", *SEL, "--tokens", "5")
        self.assertEqual((ended["cycle"], ended["tokens"]), (cycle["cycle"], 5))

    def test_second_open_run_for_a_branch_is_refused(self) -> None:
        self.ok("run-start", *SEL)
        self.assertIn("still open", self.refused("run-start", *SEL))

    def test_concurrent_run_starts_open_one_run(self) -> None:
        # Probabilistic: before the fix, 30 racing starts opened up to 4 runs.
        self.ok("report")  # create the database first, so only the insert races
        with ThreadPoolExecutor(max_workers=30) as pool:
            results = list(pool.map(lambda _: self.cli("run-start", *SEL), range(30)))
        self.assertEqual(sum(r.returncode == 0 for r in results), 1,
                         [r.stderr for r in results])

    def test_id_from_another_run_is_refused(self) -> None:
        self.ok("run-start", *SEL)
        self.ok("run-start", "--repo", "Other", "--branch", "b")
        other = self.ok("cycle-start", "--repo", "Other", "--branch", "b", "--reviewer", "codex")
        self.assertIn("belongs to run",
                      self.refused("cycle-end", *SEL, "--cycle", str(other["cycle"])))

    def test_branch_without_repo_is_refused(self) -> None:
        self.ok("run-start", *SEL)
        self.start_cycle()
        self.ok("cycle-end", *SEL)
        finding = self.ok("findings", *SEL, "--file", str(self.findings_file))["findings"][0]
        cycle = self.ok("ledger", *SEL)["findings"][0]["cycle"]
        self.assertIn("together", self.refused("finding-update", "--branch", "b",
                                               "--id", str(finding), "--verified", "1"))
        self.assertIn("together", self.refused("findings", "--branch", "b", "--cycle", str(cycle),
                                               "--file", str(self.findings_file)))

    # -- findings go to the right cycle ---------------------------------------

    def test_findings_refuse_an_open_latest_cycle(self) -> None:
        self.ok("run-start", *SEL)
        self.start_cycle("code-review")
        self.ok("cycle-end", *SEL)
        self.start_cycle("codex")  # the self-review findings were not recorded yet
        self.assertIn("still open", self.refused("findings", *SEL,
                                                 "--file", str(self.findings_file)))

    def test_findings_check_the_reviewer(self) -> None:
        self.ok("run-start", *SEL)
        self.start_cycle("codex")
        self.ok("cycle-end", *SEL)
        self.assertIn("reviewer", self.refused("findings", *SEL, "--reviewer", "code-review",
                                               "--file", str(self.findings_file)))
        self.ok("findings", *SEL, "--reviewer", "codex", "--file", str(self.findings_file))

    # -- tokens ----------------------------------------------------------------

    def test_tokens_are_not_overwritten_by_accident(self) -> None:
        self.ok("run-start", *SEL)
        cycle = str(self.start_cycle()["cycle"])
        self.ok("cycle-end", *SEL, "--tokens", "1234")
        self.assertIn("already has", self.refused("cycle-end", "--cycle", cycle, "--tokens", "99"))

    def test_tokens_can_be_corrected_explicitly(self) -> None:
        self.ok("run-start", *SEL)
        cycle = str(self.start_cycle()["cycle"])
        self.ok("cycle-end", *SEL, "--tokens", "1234")
        self.refused("cycle-end", "--cycle", cycle, "--tokens", "99", "--replace-tokens")
        fixed = self.ok("cycle-end", *SEL, "--cycle", cycle, "--tokens", "99", "--replace-tokens")
        self.assertEqual(fixed["tokens"], 99)

    def test_tokens_can_be_corrected_after_the_run_ends(self) -> None:
        self.ok("run-start", *SEL)
        cycle = str(self.start_cycle()["cycle"])
        self.ok("cycle-end", *SEL, "--tokens", "1234")
        self.ok("run-end", *SEL, "--outcome", "clean")
        fixed = self.ok("cycle-end", *SEL, "--cycle", cycle, "--tokens", "99", "--replace-tokens")
        self.assertEqual(fixed["tokens"], 99)
        self.ok("run-start", "--repo", "Other", "--branch", "b")
        self.assertIn("belongs to run", self.refused(
            "cycle-end", "--repo", "Other", "--branch", "b", "--cycle", cycle,
            "--tokens", "5", "--replace-tokens"))

    # -- ending a run -----------------------------------------------------------

    def test_pr_url_can_be_added_after_a_cap(self) -> None:
        self.ok("run-start", *SEL)
        self.ok("run-end", *SEL, "--outcome", "cap")
        ended = self.ok("run-end", *SEL, "--outcome", "cap", "--pr-url", "https://x/pr/1")
        self.assertEqual(ended["pr_url"], "https://x/pr/1")
        self.assertIn("ended", self.refused("run-end", *SEL, "--outcome", "clean"))


if __name__ == "__main__":
    unittest.main()
