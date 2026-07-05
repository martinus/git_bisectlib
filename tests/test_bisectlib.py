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
        ev = json.loads(next(Path(d, ".bisect").glob("*/eval.json")).read_text())
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
        ev3 = json.loads(next(Path(d3, ".bisect").glob("*/eval.json")).read_text())
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
        ev = json.loads(next(Path(d, ".bisect").glob("*/eval.json")).read_text())
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

    def test_unrunnable_test_aborts_not_bad(self):
        # A test that can't be launched (127 command-not-found / 126 not-exec) is
        # a broken recipe, NOT a "bug present" verdict. Marking it bad would
        # silently mis-bisect, so it must ABORT (128) instead.
        d = make_repo()
        # 127: no such command
        code, _, _ = run_recipe(
            d, "import bisectlib as b\nb.test('./nonexistent-binary')\n")
        self.assertEqual(code, 128)  # ABORT, not BAD (1)

        # 126: exists but not executable
        Path(d, "notexec.sh").write_text("#!/bin/sh\necho hi\n")  # no +x bit
        code2, _, _ = run_recipe(
            d, "import bisectlib as b\nb.test('./notexec.sh')\n")
        self.assertEqual(code2, 128)  # ABORT

    def test_unrunnable_test_aborts_even_with_custom_passed(self):
        # The strongest case: a benchmark predicate `r.seconds < T` ignores the
        # exit code, so a test that never launched (near-0s) would otherwise be
        # scored GOOD — a false negative. The unrunnable guard must fire first.
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\n"
               "b.test('./nonexistent-binary', passed=lambda r: r.seconds < 100)\n")
        self.assertEqual(code, 128)  # ABORT, not GOOD (0)

    def test_real_test_failure_still_bad(self):
        # Guard against over-reach: an ordinary non-zero exit (the test ran and
        # reported failure) must still be BAD, and a crash/signal stays BAD too.
        d = make_repo()
        self.assertEqual(
            run_recipe(d, "import bisectlib as b\nb.test('exit 1')\n")[0], 1)   # BAD
        self.assertEqual(
            run_recipe(d, "import bisectlib as b\nb.test('exit 3')\n")[0], 1)   # BAD
        # a segfault-style signal (bash reports 139) may BE the regression -> BAD
        self.assertEqual(
            run_recipe(d, "import bisectlib as b\nb.test('kill -SEGV $$')\n")[0], 1)

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

    def test_git_env_stripped(self):
        # git exports GIT_DIR/GIT_PREFIX/... under `git bisect run`; they must not
        # leak into recipe commands (or `git` inside them resolves the wrong repo).
        d = make_repo()
        body = ("import bisectlib as b\n"
                "assert b.check('echo D=$GIT_DIR W=$GIT_WORK_TREE P=$GIT_PREFIX')"
                ".out.strip() == 'D= W= P=', 'git env leaked'\n")
        Path(d, "recipe.py").write_text(
            "import sys; sys.path.insert(0, %r)\n" % str(ROOT) + body)
        # inject the vars into the recipe's own environment, as `git bisect run`
        # does (pointing at the REAL repo, so bisectlib's own git calls still work)
        env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1",
               "GIT_DIR": os.path.join(d, ".git"), "GIT_WORK_TREE": d,
               "GIT_PREFIX": "sub/"}
        p = subprocess.run([sys.executable, "recipe.py"], cwd=d,
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)

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
        # status.md is written silently — no per-step status announcement on stderr
        self.assertNotIn("status:", stderr)

    def test_log_streams_live_and_records_running_step(self):
        # the log file must be written *as output arrives* (watchable), and the
        # sidecar must carry a provisional 'running' step (code None) while the
        # command is still executing — both drive the live status.md.
        import time
        d = make_repo()
        cache = tempfile.mkdtemp(prefix="bl-live-")
        recipe = Path(d, "recipe.py")
        recipe.write_text(
            "import sys; sys.path.insert(0, %r)\n" % str(ROOT)
            + "import bisectlib as b\n"
            "b.run('echo LIVE_MARKER; sleep 3')\n")
        env = {**os.environ, "PYTHONPATH": str(ROOT), "XDG_CACHE_HOME": cache,
               "NO_COLOR": "1"}
        # `with` closes the stdout/stderr pipes (and waits) on exit — no leaked fds
        with subprocess.Popen([sys.executable, "recipe.py"], cwd=d,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env) as proc:
            deadline = time.time() + 2.5  # well before the 3s sleep ends
            live_log = running_step = False
            while time.time() < deadline and proc.poll() is None:
                logs = list(Path(d, ".bisect").glob("*/01-run-*.log"))
                if logs and "LIVE_MARKER" in logs[0].read_text():
                    live_log = True
                evs = list(Path(d, ".bisect").glob("*/eval.json"))
                if evs:
                    steps = json.loads(evs[0].read_text()).get("steps", [])
                    if any(s.get("code") is None for s in steps):
                        running_step = True
                if live_log and running_step:
                    break
                time.sleep(0.05)
            self.assertTrue(live_log, "log file did not stream output live")
            self.assertTrue(running_step, "no provisional running step recorded")
            self.assertIsNone(proc.poll(), "command exited; not a live check")
        # once finished, the running placeholder is replaced by the real result
        ev = json.loads(next(Path(d, ".bisect").glob("*/eval.json")).read_text())
        self.assertEqual(len(ev["steps"]), 1)
        self.assertEqual(ev["steps"][0]["code"], 0)
        self.assertNotIn("running", ev["steps"][0])

    def test_once_runs_once_under_real_bisect(self):
        # a real `git bisect run` over several commits: the setup guarded by
        # once() must execute exactly once across all evaluations, even
        # though `git bisect log` grows each step (regression guard for the id).
        d = tempfile.mkdtemp(prefix="bl-first-")
        sh(d, "git", "init", "-q")
        sh(d, "git", "config", "user.email", "t@t.t")
        sh(d, "git", "config", "user.name", "T")
        shas = []
        for i in range(1, 13):
            Path(d, "code.txt").write_text("BUG\n" if i >= 8 else "ok\n")
            Path(d, f"f{i}").write_text(str(i))
            sh(d, "git", "add", "-A")
            sh(d, "git", "commit", "-q", "-m", f"c{i}")
            shas.append(sh(d, "git", "rev-parse", "HEAD").stdout.strip())
        counter = Path(d, "counter")  # untracked -> survives checkouts, accumulates
        Path(d, "recipe.py").write_text(
            "import sys; sys.path.insert(0, %r)\n" % str(ROOT)
            + "import bisectlib as b\n"
            "if b.once():\n"
            f"    b.run('echo x >> {counter}')\n"
            "b.test('! grep -q BUG code.txt')\n")
        cache = tempfile.mkdtemp(prefix="bl-cache-")
        env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1",
               "XDG_CACHE_HOME": cache}
        sh(d, "git", "bisect", "start", shas[-1], shas[0], env=env)
        p = subprocess.run(["git", "bisect", "run", sys.executable, "recipe.py"],
                           cwd=d, capture_output=True, text=True, env=env)
        self.assertIn("first bad commit", (p.stdout + p.stderr).lower(), p.stdout)
        self.assertTrue(counter.exists(), "setup never ran")
        self.assertEqual(len(counter.read_text().split()), 1)  # ran exactly once
        sh(d, "git", "bisect", "reset", env=env)

    def test_once_reruns_after_abort(self):
        d = make_repo()
        cache = tempfile.mkdtemp(prefix="bl-first2-")
        # once() setup aborts -> marker NOT committed
        abort_body = ("import bisectlib as b\n"
                      "if b.once():\n"
                      "    b.run('echo x >> counter')\n"   # runs, then...
                      "    b.run('false')\n")             # aborts
        code, _, _ = run_recipe(d, abort_body, cache=cache)
        self.assertEqual(code, 128)  # ABORT
        # ... so on the next run once() is still True and setup re-runs
        ok_body = ("import bisectlib as b\n"
                   "if b.once():\n"
                   "    b.run('echo x >> counter')\n"
                   "b.test('true')\n")
        code2, _, _ = run_recipe(d, ok_body, cache=cache)
        self.assertEqual(code2, 0)
        self.assertEqual(len(Path(d, "counter").read_text().split()), 2)  # ran again

    def test_once_keys_are_independent(self):
        # keys get independent markers: a key committed in an earlier evaluation
        # stays done, while a *new* key introduced later still fires exactly once.
        # (With the old single global marker, the second key would never run.)
        d = make_repo()
        cache = tempfile.mkdtemp(prefix="bl-once3-")
        # run 1: only "a" present -> runs and commits its marker
        body1 = ("import bisectlib as b\n"
                 "if b.once('a'):\n"
                 "    b.run('echo x >> a_cnt')\n"
                 "b.test('true')\n")
        code1, _, _ = run_recipe(d, body1, cache=cache)
        self.assertEqual(code1, 0)
        # run 2: "a" is already done (skips), "b" is new -> "b" runs, "a" does not
        body2 = ("import bisectlib as b\n"
                 "if b.once('a'):\n"
                 "    b.run('echo x >> a_cnt')\n"
                 "if b.once('b'):\n"
                 "    b.run('echo x >> b_cnt')\n"
                 "b.test('true')\n")
        code2, _, _ = run_recipe(d, body2, cache=cache)
        self.assertEqual(code2, 0)
        self.assertEqual(len(Path(d, "a_cnt").read_text().split()), 1)  # never re-ran
        self.assertEqual(len(Path(d, "b_cnt").read_text().split()), 1)  # fired once

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
        evals = list(Path(d, ".bisect").glob("*/eval.json"))
        self.assertTrue(evals, "expected an eval.json sidecar")
        data = json.loads(evals[0].read_text())
        self.assertEqual(data["outcome"], "good")
        self.assertEqual(len(data["steps"]), 2)
        self.assertEqual(data["steps"][0]["verb"], "run")
        self.assertEqual(data["steps"][1]["verb"], "test")
        # once finalized, the sidecar carries the locked-in verdict (not pending),
        # so the renderer can show the real status instead of a perpetual `todo`
        self.assertEqual(data["pending"], False)
        # each step's recorded `log` names a file that actually exists next to the
        # sidecar, so the status.md step links resolve
        commit_dir = evals[0].parent
        for step in data["steps"]:
            self.assertTrue(step["log"], "step is missing a log filename")
            self.assertTrue((commit_dir / step["log"]).is_file(),
                            f"log file {step['log']} does not exist")

    def test_multi_attempt_test_log_exists(self):
        # a flaky `test` writes one file per attempt; the recorded log must point
        # at a real one (not the bare base name)
        d = make_repo()
        body = ("import bisectlib as b\n"
                "b.test('true', attempts=3, min_passes=2)\n")
        code, _, cache = run_recipe(d, body)
        self.assertEqual(code, 0)
        ev = next(Path(d, ".bisect").glob("*/eval.json"))
        step = json.loads(ev.read_text())["steps"][0]
        self.assertRegex(step["log"], r"-test-.*-\d+\.log$")
        self.assertTrue((ev.parent / step["log"]).is_file())

    def test_status_md_shows_first_bad_at_end(self):
        # End-to-end regression: `git bisect run` records a commit's mark only
        # AFTER the recipe exits, and stops after the final commit — so nothing
        # re-renders status.md with the resolved answer. The finalize render must
        # inject its own verdict, or status.md never names the first-bad commit.
        d = tempfile.mkdtemp(prefix="bl-firstbad-")
        sh(d, "git", "init", "-q")
        sh(d, "git", "config", "user.email", "t@t.t")
        sh(d, "git", "config", "user.name", "T")
        shas = []
        for i in range(1, 9):
            Path(d, "code.txt").write_text("BUG\n" if i >= 5 else "ok\n")
            Path(d, f"f{i}").write_text(str(i))
            sh(d, "git", "add", "-A")
            sh(d, "git", "commit", "-q", "-m", f"commit {i}")
            shas.append(sh(d, "git", "rev-parse", "HEAD").stdout.strip())
        bug = shas[4]  # commit 5: first to carry the BUG
        Path(d, "recipe.py").write_text(
            "import sys\nsys.path.insert(0, %r)\n" % str(ROOT) +
            "import bisectlib as b\n"
            "b.test('! grep -q BUG code.txt')\n")
        cache = tempfile.mkdtemp(prefix="bl-firstbad-cache-")
        env = {**os.environ, "XDG_CACHE_HOME": cache, "NO_COLOR": "1",
               "PYTHONPATH": str(ROOT)}
        sh(d, "git", "bisect", "start", shas[-1], shas[0], env=env)
        sh(d, "git", "bisect", "run", sys.executable, "recipe.py", env=env)
        status = Path(d, ".bisect", "status.md")
        self.assertTrue(status.is_file(), "status.md was not written")
        text = status.read_text()
        self.assertIn("First bad commit", text)
        self.assertIn(bug[:9], text)
        sh(d, "git", "bisect", "reset", env=env)

    def test_uncaught_exception_reverts_tree(self):
        # SPEC §2.2: the tree must be clean even on exception. A recipe that
        # edits a file then crashes must still ABORT *and* leave no tracked
        # modification behind — otherwise the edit lingers, and re-running the
        # fixed recipe finds `replace`'s pattern already gone (silent mis-bisect).
        d = make_repo()
        body = ("import bisectlib as b\n"
                "b.replace('code.txt', 'original', 'patched')\n"
                "raise RuntimeError('boom')\n")
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 128)  # ABORT, never bad
        self.assertEqual(Path(d, "code.txt").read_text(), "original\n")  # reverted
        self.assertEqual(
            sh(d, "git", "status", "--porcelain", "--untracked-files=no").stdout.strip(),
            "")

    def test_invalid_options_abort(self):
        # A mistyped string option must fail loudly (ABORT), not silently default.
        # bad_when is the dangerous one: a silent fallback would invert the whole
        # bisect's direction.
        d = make_repo()
        cases = [
            "b.test('true', bad_when='Pass')",       # typo'd enum
            "b.test('true', on_timeout='abrot')",    # typo'd enum
            "b.test('true', attempts=3, min_passes=5)",  # unreachable -> would be silent bad
            "b.test('true', attempts=0)",            # nonsensical count
            "b.run('true', on_timeout='nope')",
            "b.replace('code.txt', 'original', 'x', if_missing='meh')",
            "b.configure(clean='wipe')",
            "b.in_range('v1.0...v2.0')",             # three dots
            "b.in_range('onlyonerev')",              # missing high bound
            "b.hammer('true', parallel=0)",          # parallel must be >= 1
            "b.hammer('true', for_seconds=0)",       # budget must be > 0
            "b.hammer('true', bad_when='Nope')",     # typo'd enum
            "b.hammer('true', on_timeout='xyz')",    # typo'd enum
        ]
        for expr in cases:
            code, _, _ = run_recipe(d, f"import bisectlib as b\n{expr}\n")
            self.assertEqual(code, 128, f"{expr!r} should ABORT (ValueError)")

    def test_valid_min_passes_boundary_still_works(self):
        # guard the validator isn't over-eager: min_passes == attempts is valid
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\nb.test('true', attempts=3, min_passes=3)\n")
        self.assertEqual(code, 0)

    def test_custom_logs_dir_wiped_on_new_bisect(self):
        # configure(logs=…) must get the same new-bisect hygiene as .bisect/: a
        # stale once() marker from a *different* bisect must not leak in, while an
        # unrelated user file in that directory is left untouched.
        import hashlib
        d = make_repo()
        logs = tempfile.mkdtemp(prefix="bl-customlogs-")
        # a stale marker + an unrelated user file pre-populate the dir; the `id`
        # file names a *different* prior bisect so this run counts as new.
        stale_marker = f"once-setup-{hashlib.sha1(b'setup').hexdigest()[:8]}"
        Path(logs, "id").write_text("different-old-bisect-id")
        Path(logs, stale_marker).write_text("done")  # exact name once('setup') looks for
        Path(logs, "keep_me.txt").write_text("user data")
        body = ("import bisectlib as b\n"
                f"b.configure(logs={logs!r})\n"
                "assert b.once('setup') is True, 'stale marker leaked'\n"
                "b.test('true')\n")
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        self.assertTrue(Path(logs, "keep_me.txt").is_file(),
                        "unrelated user file must not be deleted")

    # -------------------------------------------------------- hammer (flaky-hunt)
    def _hammer_step(self, d):
        """The first recorded step from the most recent eval.json sidecar."""
        ev = json.loads(next(Path(d, ".bisect").glob("*/eval.json")).read_text())
        return ev["steps"][0]

    def test_hammer_good_loops_for_the_budget(self):
        # an always-passing command: GOOD, and it must actually loop many times
        # within the budget (not run once).
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\nb.hammer('true', for_seconds=0.3, parallel=4)\n")
        self.assertEqual(code, 0)
        step = self._hammer_step(d)
        self.assertEqual(step["verb"], "hammer")
        self.assertEqual(step["failures"], 0)
        self.assertEqual(step["parallel"], 4)
        self.assertGreater(step["executed"], 1)  # hammered, not a single run

    def test_hammer_defaults_all_cores_and_records_runtime(self):
        # parallel defaults to every core; the step records the run count, the
        # thread count actually used, and the total wall-clock runtime.
        import os
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\nb.hammer('true', for_seconds=0.2)\n")
        self.assertEqual(code, 0)
        step = self._hammer_step(d)
        self.assertEqual(step["parallel"], os.cpu_count() or 1)  # all cores
        self.assertGreater(step["executed"], 0)                  # total runs
        self.assertGreater(step["elapsed_s"], 0)                 # total runtime

    def test_hammer_bad_on_any_failure(self):
        # any failing run within the budget => BAD (the flaky-hunt verdict)
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\nb.hammer('false', for_seconds=1, parallel=4)\n")
        self.assertEqual(code, 1)

    def test_hammer_catches_a_rare_flake(self):
        # The point of the feature: a command that passes, passes, then fails on
        # the 3rd invocation must be caught as BAD — proving hammer keeps going
        # past early passes rather than trusting the first run. parallel=1 makes
        # the counter deterministic.
        d = make_repo()
        cmd = r"n=$(( $(cat c 2>/dev/null||echo 0)+1 )); echo $n>c; [ $n -ne 3 ]"
        body = ("import bisectlib as b\n"
                f"b.hammer({cmd!r}, for_seconds=5, parallel=1)\n")
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 1)              # the 3rd run failed -> bad
        step = self._hammer_step(d)
        self.assertEqual(step["passes"], 2)    # first two passed
        self.assertEqual(step["failures"], 1)
        # the failing run's output is captured in the single hammer log
        log = next(Path(d, ".bisect").glob(f"*/{step['log']}"))
        self.assertIn("FAIL", log.read_text())

    def test_hammer_is_actually_concurrent(self):
        # Each run sleeps 0.2s; a 0.5s sequential budget could finish ~2, but with
        # parallel=4 the first batch of four all complete well inside the budget.
        # executed>=4 can only happen if the four ran concurrently.
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\n"
               "b.hammer('sleep 0.2', for_seconds=0.5, parallel=4)\n")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(self._hammer_step(d)["executed"], 4)

    def test_hammer_benchmark_predicate(self):
        # passed= still defines a "failing" run: fail if any run is slower than the
        # ceiling. sleep 0.05 is always slower than 0.01 -> BAD.
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\n"
               "b.hammer('sleep 0.05', for_seconds=0.4, parallel=2, "
               "passed=lambda r: r.seconds < 0.01)\n")
        self.assertEqual(code, 1)

    def test_hammer_unrunnable_aborts(self):
        # a command that can't be launched is a broken recipe -> ABORT, not bad
        d = make_repo()
        code, _, _ = run_recipe(
            d, "import bisectlib as b\n"
               "b.hammer('./nonexistent-binary', for_seconds=2, parallel=4)\n")
        self.assertEqual(code, 128)

    # ----------------------------------------------------- git helper predicates
    def test_git_helpers(self):
        # sha()/subject()/touches()/is_clean() against a real HEAD, in-process so
        # a clean tree is observable (no untracked recipe.py in the way).
        sys.path.insert(0, str(ROOT))
        import bisectlib as b
        d = make_repo()  # single commit "c1" adding code.txt; tree is clean
        head = sh(d, "git", "rev-parse", "HEAD").stdout.strip()
        cwd0 = os.getcwd()
        os.chdir(d)
        try:
            self.assertEqual(b.sha(), head)
            self.assertEqual(b.subject(), "c1")
            self.assertTrue(b.touches("code.txt"))   # the commit added it
            self.assertFalse(b.touches("nope.txt"))
            self.assertTrue(b.is_clean())            # freshly committed, nothing dirty
            Path(d, "code.txt").write_text("changed")
            self.assertFalse(b.is_clean())           # a tracked modification shows
        finally:
            os.chdir(cwd0)

    def test_in_range_predicate(self):
        # in_range(lo..hi) is True iff HEAD is within [lo, hi]; also the (lo, hi)
        # form and the `sha in range` membership check.
        sys.path.insert(0, str(ROOT))
        import bisectlib as b
        d = tempfile.mkdtemp(prefix="bl-range-")
        sh(d, "git", "init", "-q")
        sh(d, "git", "config", "user.email", "t@t.t")
        sh(d, "git", "config", "user.name", "T")
        shas = []
        for i in range(1, 6):
            Path(d, f"f{i}").write_text(str(i))
            sh(d, "git", "add", "-A")
            sh(d, "git", "commit", "-q", "-m", f"c{i}")
            shas.append(sh(d, "git", "rev-parse", "HEAD").stdout.strip())
        c1, c3, c4, c5 = shas[0], shas[2], shas[3], shas[4]
        sh(d, "git", "checkout", "-q", c3)  # detached at the middle commit
        cwd0 = os.getcwd()
        os.chdir(d)
        try:
            self.assertTrue(bool(b.in_range(f"{c1}..{c5}")))    # c3 in c1..c5
            self.assertFalse(bool(b.in_range(f"{c4}..{c5}")))   # c3 below c4
            self.assertTrue(bool(b.in_range(c1, c5)))           # two-arg form
            self.assertIn(c3, b.in_range(f"{c1}..{c5}"))        # membership
            self.assertNotIn(c1, b.in_range(f"{c4}..{c5}"))
        finally:
            os.chdir(cwd0)

    # ---------------------------------------------------------- feature coverage
    def test_replace_accepts_a_compiled_regex(self):
        # `old` as a re.Pattern edits by regex (vs literal str); still auto-reverts
        d = make_repo()  # code.txt = "original\n"
        body = ("import bisectlib as b, re\n"
                "b.replace('code.txt', re.compile(r'orig\\w+'), 'PATCHED')\n"
                "b.test('grep -q PATCHED code.txt')\n")
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        self.assertEqual(Path(d, "code.txt").read_text(), "original\n")  # reverted

    def test_warmup_runs_are_excluded_from_the_verdict(self):
        # the first `warmup` runs are throwaway. A command that fails twice then
        # passes is GOOD with warmup=2 (the two failures are ignored), but BAD
        # without warmup (the very first failure decides).
        cmd = r"n=$(( $(cat c 2>/dev/null||echo 0)+1 )); echo $n>c; [ $n -ge 3 ]"
        d = make_repo()
        code, _, _ = run_recipe(
            d, f"import bisectlib as b\nb.test({cmd!r}, warmup=2, attempts=1)\n")
        self.assertEqual(code, 0)   # runs 1&2 (fail) are warmup; run 3 passes -> good
        d2 = make_repo()
        code2, _, _ = run_recipe(
            d2, f"import bisectlib as b\nb.test({cmd!r}, attempts=1)\n")
        self.assertEqual(code2, 1)  # no warmup: first run fails -> bad

    def test_timeout_triggers_the_on_timeout_outcome(self):
        # a command that exceeds `timeout` is killed and mapped via on_timeout.
        d = make_repo()
        self.assertEqual(  # test default on_timeout='skip'
            run_recipe(d, "import bisectlib as b\nb.test('sleep 5', timeout=0.3)\n")[0],
            125)
        self.assertEqual(  # test on_timeout='bad' (a hang IS the regression)
            run_recipe(d, "import bisectlib as b\n"
                          "b.test('sleep 5', timeout=0.3, on_timeout='bad')\n")[0],
            1)
        self.assertEqual(  # run on_timeout='skip'
            run_recipe(d, "import bisectlib as b\n"
                          "b.run('sleep 5', timeout=0.3, on_timeout='skip')\n")[0],
            125)

    def test_fixup_when_false_runs_unpatched(self):
        # a `when` predicate that is false leaves the block running unpatched.
        d = make_repo()  # code.txt = "original\n"
        Path(d, "code.txt").write_text("patched\n")
        patch = sh(d, "git", "diff").stdout
        sh(d, "git", "checkout", "--", "code.txt")
        Path(d, "fix.patch").write_text(patch)
        body = ("import bisectlib as b\n"
                "with b.fixup('fix.patch', when=False):\n"
                "    b.test('grep -q original code.txt')\n")  # unpatched -> passes
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        self.assertEqual(Path(d, "code.txt").read_text(), "original\n")

    def test_configure_clean_removes_untracked_but_keeps_bisect(self):
        # clean="clean" adds `git clean -fdx` to the revert, wiping untracked
        # build junk between commits — but never the .bisect/ report dir.
        d = make_repo()
        body = ("import bisectlib as b\n"
                "b.configure(clean='clean')\n"
                "b.replace('code.txt', 'original', 'x')\n"   # arms the tree revert
                "open('junk.txt', 'w').write('untracked build artifact')\n"
                "b.test('true')\n")
        code, _, _ = run_recipe(d, body)
        self.assertEqual(code, 0)
        self.assertFalse(Path(d, "junk.txt").exists(), "untracked junk not cleaned")
        self.assertTrue(Path(d, ".bisect").exists(), ".bisect/ must survive clean")


if __name__ == "__main__":
    unittest.main(verbosity=2)
