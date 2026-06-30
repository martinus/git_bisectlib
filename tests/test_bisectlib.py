"""Tests for the bisectlib recipe engine: exit-code contract, flaky logic,
clean-tree guarantee, and the eval.json sidecar."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sh(cwd, *args, env=None, check=True):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=e)
    if check and p.returncode != 0:
        raise AssertionError(f"{args} failed: {p.stderr}\n{p.stdout}")
    return p


def make_repo():
    d = tempfile.mkdtemp(prefix="bisectlib-eng-")
    sh(d, "git", "init", "-q")
    sh(d, "git", "config", "user.email", "t@t.t")
    sh(d, "git", "config", "user.name", "T")
    Path(d, "code.txt").write_text("original\n")
    sh(d, "git", "add", "-A")
    sh(d, "git", "commit", "-q", "-m", "c1")
    return d


def run_recipe(repo, body, cache=None):
    """Write `body` as a recipe and run it; return (exit_code, stderr, cache_dir)."""
    cache = cache or tempfile.mkdtemp(prefix="bl-cache-")
    recipe = Path(repo, "recipe.py")
    recipe.write_text("import sys\nsys.path.insert(0, %r)\n" % str(ROOT) + body)
    env = {"PYTHONPATH": str(ROOT), "XDG_CACHE_HOME": cache, "NO_COLOR": "1"}
    p = subprocess.run([sys.executable, "recipe.py"], cwd=repo,
                       capture_output=True, text=True, env={**os.environ, **env})
    return p.returncode, p.stderr, cache


def make_linear(n=16, bug_at=10):
    d = tempfile.mkdtemp(prefix="bisectlib-lin-")
    sh(d, "git", "init", "-q")
    sh(d, "git", "config", "user.email", "t@t.t")
    sh(d, "git", "config", "user.name", "T")
    shas = []
    for i in range(1, n + 1):
        Path(d, "code.txt").write_text("BUG\n" if i >= bug_at else "ok\n")
        Path(d, f"f{i}").write_text(str(i))
        sh(d, "git", "add", "-A")
        date = f"2026-04-{i:02d}T12:00:00"
        sh(d, "git", "commit", "-q", "-m", f"commit {i}",
           env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        shas.append(sh(d, "git", "rev-parse", "HEAD").stdout.strip())
    return d, shas


def run_script(repo, name, body, env=None):
    Path(repo, name).write_text(body)
    e = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1"}
    if env:
        e.update(env)
    return subprocess.run([sys.executable, name], cwd=repo,
                          capture_output=True, text=True, env=e)


class TestFindAnchorsAndDriver(unittest.TestCase):
    def test_find_anchors(self):
        d, shas = make_linear(n=16, bug_at=10)
        body = (
            "import sys; sys.path.insert(0, %r)\n" % str(ROOT)
            + "import bisectlib as b\nfrom pathlib import Path\n"
            "good, bad = b.find_anchors(bad='HEAD',\n"
            "    probe=lambda: 'BUG' not in Path(b._toplevel(), 'code.txt').read_text())\n"
            "print(good, bad)\n"
        )
        p = run_script(d, "fa.py", body)
        self.assertEqual(p.returncode, 0, p.stderr)
        good, bad = p.stdout.split()
        self.assertEqual(bad, shas[-1])
        self.assertNotEqual(good, bad)
        # good must be bug-free, bad must have the bug, good must precede bad
        self.assertNotIn("BUG", sh(d, "git", "show", f"{good}:code.txt").stdout)
        self.assertIn("BUG", sh(d, "git", "show", f"{bad}:code.txt").stdout)
        self.assertEqual(
            sh(d, "git", "merge-base", "--is-ancestor", good, bad, check=False).returncode,
            0)
        # HEAD restored to the original branch
        self.assertEqual(
            sh(d, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(), "master")

    def test_driver_runs_bisect(self):
        d, shas = make_linear(n=16, bug_at=10)
        good, bad, bug = shas[0], shas[-1], shas[9]
        recipe = (
            "import sys; sys.path.insert(0, %r)\n" % str(ROOT)
            + "import bisectlib as b\nb.test('! grep -q BUG code.txt')\n"
        )
        Path(d, "recipe.py").write_text(recipe)
        driver = (
            "import sys; sys.path.insert(0, %r)\n" % str(ROOT)
            + "import bisectlib as b\n"
            f"r = b.bisect({good!r}, {bad!r}, 'recipe.py', reset=True)\n"
            "print('FIRSTBAD', r.first_bad if r else None)\n"
        )
        cache = tempfile.mkdtemp(prefix="bl-cache-")
        p = run_script(d, "drive.py", driver, env={"XDG_CACHE_HOME": cache})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"FIRSTBAD {bug}", p.stdout)


class TestEngine(unittest.TestCase):
    def test_end_of_script_is_good(self):
        d = make_repo()
        code, _, _ = run_recipe(d, "import bisectlib as b\nb.run('true')\n")
        self.assertEqual(code, 0)

    def test_run_failure_aborts_by_default(self):
        d = make_repo()
        code, _, _ = run_recipe(d, "import bisectlib as b\nb.run('false')\n")
        self.assertEqual(code, 128)  # ABORT

    def test_run_failure_skip_on_error(self):
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\nb.run('false', skip_on_error=True)\n")
        self.assertEqual(code, 125)  # SKIP

    def test_test_pass_is_good_fail_is_bad(self):
        d = make_repo()
        code, _, _ = run_recipe(d, "import bisectlib as b\nb.test('true')\n")
        self.assertEqual(code, 0)
        code, _, _ = run_recipe(d, "import bisectlib as b\nb.test('false')\n")
        self.assertEqual(code, 1)  # BAD

    def test_multiple_tests_and_together(self):
        # several test() calls: all pass -> GOOD; a passing test continues
        d = make_repo()
        code, _, cache = run_recipe(
            d, "import bisectlib as b\n"
               "b.test('true')\nb.test('echo hi | grep -q hi')\nb.test('true')\n")
        self.assertEqual(code, 0)
        # all three ran and were recorded
        ev = json.loads(next(Path(cache, "bisectlib").glob("*/*/eval.json")).read_text())
        self.assertEqual([s["verb"] for s in ev["steps"]], ["test", "test", "test"])

        # a later failing test makes the whole thing BAD
        d2 = make_repo()
        code2, _, _ = run_recipe(
            d2, "import bisectlib as b\nb.test('true')\nb.test('false')\n")
        self.assertEqual(code2, 1)

        # the first failing test exits immediately (second never runs)
        d3 = make_repo()
        code3, _, cache3 = run_recipe(
            d3, "import bisectlib as b\nb.test('false')\nb.test('true')\n")
        self.assertEqual(code3, 1)
        ev3 = json.loads(next(Path(cache3, "bisectlib").glob("*/*/eval.json")).read_text())
        self.assertEqual(len(ev3["steps"]), 1)  # second test did not run

    def test_flaky_min_passes(self):
        d = make_repo()
        # command passes its first 2 invocations, then fails
        cmd = r"c=$(cat n 2>/dev/null || echo 0); c=$((c+1)); echo $c>n; [ $c -le 2 ]"
        body = ("import bisectlib as b\n"
                f"b.test({cmd!r}, attempts=5, min_passes=2)\n")
        code, _, cache = run_recipe(d, body)
        self.assertEqual(code, 0)  # 2 passes meets min_passes=2 -> good
        # early stop: should have stopped at 2 attempts (verdict locked)
        ev = json.loads(next(Path(cache, "bisectlib").glob("*/*/eval.json")).read_text())
        self.assertEqual(ev["steps"][0]["executed"], 2)

        # min_passes=3 cannot be met (only 2 ever pass) -> bad
        d2 = make_repo()
        body2 = ("import bisectlib as b\n"
                 f"b.test({cmd!r}, attempts=5, min_passes=3)\n")
        code2, _, _ = run_recipe(d2, body2)
        self.assertEqual(code2, 1)  # bad

    def test_bad_when_pass_inverts(self):
        d = make_repo()
        # command always succeeds; bad_when='pass' means success == bad
        code, _, _ = run_recipe(
            d, "import bisectlib as b\nb.test('true', bad_when='pass')\n")
        self.assertEqual(code, 1)  # BAD

    def test_replace_reverts_tree(self):
        d = make_repo()
        body = ("import bisectlib as b\n"
                "b.replace('code.txt', 'original', 'patched')\n"
                "b.test('grep -q patched code.txt')\n")  # passes -> good
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        # no tracked modifications must remain (untracked recipe.py is irrelevant
        # to git bisect's checkout); the edited file must be restored
        tracked = sh(d, "git", "status", "--porcelain",
                     "--untracked-files=no").stdout.strip()
        self.assertEqual(tracked, "")
        self.assertEqual(Path(d, "code.txt").read_text(), "original\n")

    def test_replace_missing_skips(self):
        d = make_repo()
        body = ("import bisectlib as b\n"
                "b.replace('code.txt', 'NOPE', 'x')\nb.test('true')\n")
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 125)  # SKIP (pattern not found)

    def test_uncaught_exception_aborts(self):
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\nraise RuntimeError('boom')\n")
        self.assertEqual(code, 128)  # ABORT, never 'bad'

    def test_check_does_not_exit(self):
        d = make_repo()
        body = ("import bisectlib as b\n"
                "r = b.check('echo hello')\n"
                "assert r.ok and 'hello' in r.out\n"
                "b.test('false')\n")  # we still reach test -> bad
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 1)

    def test_sidecar_written(self):
        d = make_repo()
        body = ("import bisectlib as b\n"
                "b.run('true')\nb.test('true')\n")
        code, _, cache = run_recipe(d, body)
        self.assertEqual(code, 0)
        evals = list(Path(cache, "bisectlib").glob("*/*/eval.json"))
        self.assertTrue(evals, "expected an eval.json sidecar")
        data = json.loads(evals[0].read_text())
        self.assertEqual(data["outcome"], "good")
        self.assertEqual(len(data["steps"]), 2)
        self.assertEqual(data["steps"][0]["verb"], "run")
        self.assertEqual(data["steps"][1]["verb"], "test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
