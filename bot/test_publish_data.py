"""publish_data.sh must not fail when decisions.jsonl is absent."""
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "bot", "publish_data.sh")


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


class PublishData(unittest.TestCase):
    def test_missing_decisions_file_still_publishes_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare = os.path.join(tmp, "origin.git")
            repo = os.path.join(tmp, "repo")
            subprocess.run(["git", "init", "--bare", bare], check=True, capture_output=True)
            subprocess.run(["git", "init", repo], check=True, capture_output=True)
            _git(repo, "config", "user.email", "test@example.com")
            _git(repo, "config", "user.name", "test")
            _git(repo, "remote", "add", "origin", bare)
            readme = os.path.join(repo, "README.md")
            with open(readme, "w") as handle:
                handle.write("code\n")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "init")
            _git(repo, "branch", "-M", "main")
            _git(repo, "push", "-u", "origin", "main")

            src = os.path.join(repo, "bot", "runtime")
            os.makedirs(src)
            with open(os.path.join(src, "funding_snapshots.csv"), "w") as handle:
                handle.write("hour,coin\n2026-11-21T01,BTC\n")
            with open(os.path.join(src, "state.json"), "w") as handle:
                handle.write("{}\n")

            ran = subprocess.run(["bash", SCRIPT, src], cwd=repo, capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("published", ran.stdout)
            listed = subprocess.run(
                ["git", "-C", repo, "ls-tree", "-r", "--name-only", "HEAD"],
                check=True, capture_output=True, text=True,
            )
            names = set(listed.stdout.split())
            self.assertIn("funding_snapshots.csv", names)
            self.assertIn("state.json", names)
            self.assertNotIn("decisions.jsonl", names)

            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            quiet = subprocess.run(["bash", SCRIPT, empty], cwd=repo, capture_output=True, text=True)
            self.assertEqual(quiet.returncode, 0, quiet.stderr)
            self.assertIn("nothing to publish", quiet.stdout)


if __name__ == "__main__":
    unittest.main()
