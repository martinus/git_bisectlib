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

    def test_time_predicate(self):
        # passed sees Result.seconds -> "min of N runs below T" via min_passes=1
        d = make_repo()
        ok = ("import bisectlib as b\n"
              "b.test('sleep 0.05', attempts=3, min_passes=1, "
              "passed=lambda r: r.seconds < 0.5)\n")
        code, _, _ = run_recipe(d, ok)
        self.assertEqual(code, 0)  # 0.05s < 0.5s -> good
        # an impossible threshold -> bad
        d2 = make_repo()
        bad = ("import bisectlib as b\n"
               "b.test('sleep 0.2', attempts=3, min_passes=1, "
               "passed=lambda r: r.seconds < 0.01)\n")
        code2, _, _ = run_recipe(d2, bad)
        self.assertEqual(code2, 1)  # never fast enough -> bad

    def test_default_passed_is_exit_code(self):
        # default predicate ignores timing and only checks the exit code
        d = make_repo()
        code, _, _ = run_recipe(d, "import bisectlib as b\nb.test('sleep 0.2')\n")
        self.assertEqual(code, 0)  # slow but exit 0 -> good

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

    def test_fixup_patch_reverts(self):
        d = make_repo()  # has tracked file code.txt = "original\n"
        # build a patch that turns original -> patched, then revert the tree
        Path(d, "code.txt").write_text("patched\n")
        patch = sh(d, "git", "diff").stdout
        sh(d, "git", "checkout", "--", "code.txt")
        Path(d, "fix.patch").write_text(patch)
        body = ("import bisectlib as b\n"
                "with b.fixup('fix.patch'):\n"
                "    b.test('grep -q patched code.txt')\n")  # applied inside -> good
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        self.assertEqual(Path(d, "code.txt").read_text(), "original\n")  # reverted
        self.assertEqual(
            sh(d, "git", "status", "--porcelain", "--untracked-files=no").stdout.strip(),
            "")

    def test_fixup_cherrypick_reverts(self):
        d = make_repo()  # commit c1: code.txt = "original\n"
        Path(d, "code.txt").write_text("original\nFIXED\n")
        sh(d, "git", "commit", "-qam", "the fix")
        fix = sh(d, "git", "rev-parse", "HEAD").stdout.strip()
        c1 = sh(d, "git", "rev-parse", "HEAD~1").stdout.strip()
        sh(d, "git", "checkout", "-q", c1)  # simulate a bisect checkout at the old commit
        body = ("import bisectlib as b\n"
                f"with b.fixup(cherry_pick={fix!r}):\n"
                "    b.test('grep -q FIXED code.txt')\n")  # applied inside -> good
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        self.assertNotIn("FIXED", Path(d, "code.txt").read_text())  # reverted
        self.assertEqual(
            sh(d, "git", "status", "--porcelain", "--untracked-files=no").stdout.strip(),
            "")
        self.assertFalse((Path(d) / ".git" / "CHERRY_PICK_HEAD").exists())

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

    def test_cwd(self):
        d = make_repo()
        os.makedirs(os.path.join(d, "sub"), exist_ok=True)
        # relative cwd resolves against the repo root
        body = ("import bisectlib as b\n"
                "r = b.check('basename $(pwd)', cwd='sub')\n"
                "assert r.out.strip() == 'sub', r.out\n"
                "b.test('test -d sub', cwd='sub')\n")  # runs inside sub, checks nested? no
        # simpler: create a marker only reachable from sub via relative path
        Path(d, "sub", "here").write_text("x")
        body = ("import bisectlib as b\n"
                "assert b.check('basename $(pwd)', cwd='sub').out.strip() == 'sub'\n"
                # $PWD is kept in sync with the real cwd (not stale)
                "assert b.check('basename $PWD', cwd='sub').out.strip() == 'sub'\n"
                "b.run('test -f here', cwd='sub')\n"   # relative to sub -> found
                "b.test('test -f here', cwd='sub')\n")
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        # global default via configure(cwd=...)
        body2 = ("import bisectlib as b\nb.configure(cwd='sub')\n"
                 "b.test('test -f here')\n")  # no per-call cwd -> uses configured sub
        code2, _, _ = run_recipe(d, body2)
        self.assertEqual(code2, 0)

    def test_verdict_primitives(self):
        d = make_repo()
        # decide from Python after measuring with check()
        good = ("import bisectlib as b\n"
                "n = int(b.check('echo 3').out)\n"
                "if n > 5: b.bad('too big')\n")   # 3 <= 5 -> fall through -> good
        code, _, _ = run_recipe(d, good)
        self.assertEqual(code, 0)
        bad = ("import bisectlib as b\n"
               "n = int(b.check('echo 9').out)\n"
               "if n > 5: b.bad('too big')\n")    # 9 > 5 -> bad
        code2, _, _ = run_recipe(d, bad)
        self.assertEqual(code2, 1)
        # skip() and explicit good()/abort()
        self.assertEqual(run_recipe(d, "import bisectlib as b\nb.skip()\n")[0], 125)
        self.assertEqual(run_recipe(d, "import bisectlib as b\nb.good()\nb.bad()\n")[0], 0)
        self.assertEqual(run_recipe(d, "import bisectlib as b\nb.abort()\n")[0], 128)

    def test_streams_command_output(self):
        d = make_repo()
        body = ("import bisectlib as b\n"
                "b.run('echo UNIQ_BUILD_MARKER')\n"
                "b.test('echo UNIQ_TEST_MARKER')\n")
        code, stderr, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        # command output is shown live (to stderr), not swallowed
        self.assertIn("UNIQ_BUILD_MARKER", stderr)
        self.assertIn("UNIQ_TEST_MARKER", stderr)
        # and the old "bisectlog status:" announcement is gone
        self.assertNotIn("bisectlog status", stderr)

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
