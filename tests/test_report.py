"""Tests for bisectlib's report reconstruction + Markdown rendering
(bisectlib._report) against a real, freshly-built git repo + bisect session."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bisectlib import _report  # noqa: E402


def run(cwd, *args, env=None, check=True):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=e)
    if check and p.returncode != 0:
        raise AssertionError(f"{args} failed: {p.stderr}\n{p.stdout}")
    return p


def make_repo(n=16, bug_at=11):
    """Build a linear repo of n commits; `bug_at` introduces the regression."""
    d = tempfile.mkdtemp(prefix="bisect-report-test-")
    run(d, "git", "init", "-q")
    run(d, "git", "config", "user.email", "t@t.t")
    run(d, "git", "config", "user.name", "Tester")
    shas = []
    for i in range(1, n + 1):
        content = "BUG\n" if i >= bug_at else "ok\n"
        Path(d, "code.txt").write_text(content)
        Path(d, f"f{i}.txt").write_text(str(i))
        run(d, "git", "add", "-A")
        # space commits a day apart so durations are meaningful
        date = f"2026-01-{i:02d}T12:00:00"
        run(d, "git", "commit", "-q", "-m", f"commit {i}",
            env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        shas.append(run(d, "git", "rev-parse", "HEAD").stdout.strip())
    return d, shas


class TestReport(unittest.TestCase):
    def test_full_run_finds_first_bad(self):
        d, shas = make_repo(n=16, bug_at=11)
        bad, good = shas[-1], shas[0]
        bug_sha = shas[10]  # commit 11, 0-indexed

        # a `git bisect run` test script: exit 1 (bad) if BUG present, else 0 (good)
        script = Path(d, "t.sh")
        script.write_text("#!/bin/sh\ngrep -q BUG code.txt && exit 1\nexit 0\n")
        script.chmod(0o755)

        run(d, "git", "bisect", "start", bad, good)
        run(d, "git", "bisect", "run", "./t.sh")

        rep = _report.build_report(d)
        self.assertIsNotNone(rep)
        self.assertEqual(rep.first_bad, bug_sha)
        self.assertEqual(rep.orig_bad, bad)
        self.assertIn(good, rep.orig_goods)
        # markdown renders without error and mentions the culprit
        md = _report.render_markdown(rep)
        self.assertIn("First bad commit", md)
        self.assertIn(bug_sha[:9], md)
        # the full culprit commit is shown git-bisect style: metadata + diffstat
        self.assertIn("Author:", md)
        self.assertIn(" changed, ", md)  # git show --stat summary line
        run(d, "git", "bisect", "reset")

    def test_midbisect_rows_and_bounds(self):
        d, shas = make_repo(n=16, bug_at=11)
        bad, good = shas[-1], shas[0]
        run(d, "git", "bisect", "start", bad, good)

        # one manual step: mark whatever git checked out
        head1 = run(d, "git", "rev-parse", "HEAD").stdout.strip()
        # decide truthfully: bad if it has the bug
        has_bug = "BUG" in Path(d, "code.txt").read_text()
        run(d, "git", "bisect", "bad" if has_bug else "good")

        rep = _report.build_report(d)
        self.assertIsNotNone(rep)
        self.assertGreaterEqual(len(rep.rows), 1)
        # first row's probe is the first commit git checked out
        first = rep.rows[0]
        self.assertEqual(first.probe, head1)
        self.assertEqual(first.status, "bad" if has_bug else "good")
        # there should be an in-flight todo row for the new HEAD
        self.assertTrue(rep.in_progress)
        self.assertEqual(rep.rows[-1].status, "todo")
        # range metrics populated
        self.assertGreater(first.n_commits, 0)
        self.assertGreater(first.span_seconds, 0)
        run(d, "git", "bisect", "reset")

    def test_date_delta_tolerates_z_suffix(self):
        # newer git emits UTC as `…T12:00:00Z` for %cI; Python 3.10's
        # fromisoformat rejects the Z suffix, which silently zeroed spans.
        secs = _report._date_delta_seconds(
            "2026-01-01T12:00:00Z", "2026-01-16T12:00:00Z")
        self.assertEqual(secs, 15 * 86400)
        self.assertEqual(_report.fmt_date("2026-01-16T12:00:00Z"), "2026-01-16 12:00")

    def test_range_count_excludes_all_goods(self):
        # In a merge DAG, git's candidate range excludes ancestors of EVERY good,
        # not just the latest. With a good anchor on a side branch that diverges
        # from the mainline probe, counting only `latest_good..bad` overcounts.
        d = tempfile.mkdtemp(prefix="bisect-report-dag-")
        run(d, "git", "init", "-q")
        run(d, "git", "config", "user.email", "t@t.t")
        run(d, "git", "config", "user.name", "T")

        def mk(name):
            Path(d, f"f_{name}").write_text(name)
            run(d, "git", "add", "-A")
            run(d, "git", "commit", "-qm", name)

        mk("root")
        main = run(d, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        run(d, "git", "branch", "side")
        for i in range(1, 11):
            mk(f"m{i}")
        run(d, "git", "checkout", "-q", "side")
        for i in range(1, 4):
            mk(f"s{i}")
        s3 = run(d, "git", "rev-parse", "HEAD").stdout.strip()  # good on side branch
        run(d, "git", "checkout", "-q", main)
        run(d, "git", "merge", "-q", "--no-ff", "-m", "merge", s3)
        for i in range(11, 21):
            mk(f"m{i}")
        bad = run(d, "git", "rev-parse", "HEAD").stdout.strip()
        firstmid = run(d, "git", "rev-list", "--bisect", bad, "--not", s3).stdout.strip()

        run(d, "git", "bisect", "start", bad, s3)
        run(d, "git", "bisect", "good", firstmid)  # firstmid diverges from s3
        rep = _report.build_report(d)
        todo = [r for r in rep.rows if r.status == "todo"][0]
        both = int(run(d, "git", "rev-list", "--count", bad, "--not", firstmid, s3).stdout)
        single = int(run(d, "git", "rev-list", "--count", f"{firstmid}..{bad}").stdout)
        self.assertEqual(todo.n_commits, both)     # excludes BOTH goods
        self.assertLess(both, single)              # the divergent good really matters
        run(d, "git", "bisect", "reset")

    def test_good_bound_advances_without_ancestry(self):
        # Regression: in a merge DAG (or shallow clone) the newly-good commit need
        # not be a topological descendant of the prior good, so the old ancestry
        # gate froze the good bound and the range. Trust git for evaluation goods.
        d, shas = make_repo(n=16, bug_at=11)
        script = Path(d, "t.sh")
        script.write_text("#!/bin/sh\ngrep -q BUG code.txt && exit 1\nexit 0\n")
        script.chmod(0o755)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        run(d, "git", "bisect", "run", "./t.sh")
        orig = _report.is_ancestor
        _report.is_ancestor = lambda *a, **k: False  # ancestry can't confirm
        try:
            rep = _report.build_report(d)
        finally:
            _report.is_ancestor = orig
        goods = [r.good for r in rep.rows]
        self.assertGreater(len(set(goods)), 1, "good bound never advanced")
        # ranges must shrink monotonically as bounds tighten
        counts = [r.n_commits for r in rep.rows]
        self.assertEqual(counts, sorted(counts, reverse=True))
        run(d, "git", "bisect", "reset")

    def test_in_flight_row_falls_back_to_sidecar(self):
        import json
        d, shas = make_repo(n=16, bug_at=11)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        head = run(d, "git", "rev-parse", "HEAD").stdout.strip()
        logs = tempfile.mkdtemp(prefix="bl-logs-")
        sc = Path(logs, head)
        sc.mkdir()
        (sc / "eval.json").write_text(json.dumps(
            {"sha": head, "outcome": "good", "exit_code": 0, "pending": True,
             "steps": [{"verb": "run", "cmd": "configure", "code": 0}]}))
        orig = _report.is_ancestor
        _report.is_ancestor = lambda *a, **k: False  # ancestry can't confirm range
        try:
            with_sc = _report.build_report(d, logs_dir=logs)
            without_sc = _report.build_report(d)  # no sidecar → no invented row
        finally:
            _report.is_ancestor = orig
        self.assertIn(head, [r.probe for r in with_sc.rows])
        self.assertNotIn(head, [r.probe for r in without_sc.rows])
        run(d, "git", "bisect", "reset")

    def test_cells_show_date_and_author_not_subject(self):
        d, shas = make_repo(n=8, bug_at=5)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        has_bug = "BUG" in Path(d, "code.txt").read_text()
        run(d, "git", "bisect", "bad" if has_bug else "good")
        rep = _report.build_report(d)
        table = _report.render_markdown(rep).split("## Details")[0]
        self.assertIn("Tester", table)                       # author shown in cells
        self.assertRegex(table, r"`[0-9a-f]{9}` 2026-01-\d\d")  # sha + commit date
        self.assertNotIn("commit 8", table)                  # subject NOT in cells
        run(d, "git", "bisect", "reset")

    def test_step_links_to_log_file(self):
        import json
        d, shas = make_repo(n=8, bug_at=5)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        head = run(d, "git", "rev-parse", "HEAD").stdout.strip()
        logs = tempfile.mkdtemp(prefix="bl-logs-")
        sc = Path(logs, head)
        sc.mkdir()
        (sc / "eval.json").write_text(json.dumps(
            {"sha": head, "outcome": "good", "exit_code": 0, "pending": False,
             "steps": [{"verb": "run", "cmd": "make", "code": 0, "duration_s": 1.0,
                        "log": "01-run-make.log"}]}))
        rep = _report.build_report(d, logs_dir=logs)
        md = _report.render_markdown(rep, details=True)
        self.assertIn(f"[run]({head}/01-run-make.log)", md)   # step cell is a link
        run(d, "git", "bisect", "reset")

    def test_running_step_shown_before_command_finishes(self):
        import json
        d, shas = make_repo(n=8, bug_at=5)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        head = run(d, "git", "rev-parse", "HEAD").stdout.strip()
        logs = tempfile.mkdtemp(prefix="bl-logs-")
        sc = Path(logs, head)
        sc.mkdir()
        # a provisional step written by _begin_step: no exit code yet == running
        (sc / "eval.json").write_text(json.dumps(
            {"sha": head, "outcome": "good", "exit_code": 0, "pending": True,
             "steps": [{"verb": "run", "cmd": "make -j", "code": None,
                        "duration_s": None, "log": "01-run-make.log",
                        "running": True}]}))
        rep = _report.build_report(d, logs_dir=logs)
        md = _report.render_markdown(rep, details=True)
        # top-level table names the in-flight command; details link its live log
        self.assertIn("running `make -j`", md)
        self.assertIn(f"[run]({head}/01-run-make.log)", md)
        self.assertIn("⏳", md)
        run(d, "git", "bisect", "reset")

    def test_in_progress_row_uses_finalized_sidecar_verdict(self):
        import json
        d, shas = make_repo(n=8, bug_at=5)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        head = run(d, "git", "rev-parse", "HEAD").stdout.strip()
        logs = tempfile.mkdtemp(prefix="bl-logs-")
        sc = Path(logs, head)
        sc.mkdir()

        def write(pending, outcome):
            (sc / "eval.json").write_text(json.dumps(
                {"sha": head, "outcome": outcome, "exit_code": 0,
                 "pending": pending, "steps": []}))

        # a finalized sidecar surfaces the real verdict on the in-flight row
        write(pending=False, outcome="bad")
        rep = _report.build_report(d, logs_dir=logs)
        row = next(r for r in rep.rows if r.probe == head)
        self.assertEqual(row.status, "bad")
        # while still pending, it stays `todo`
        write(pending=True, outcome="good")
        rep = _report.build_report(d, logs_dir=logs)
        row = next(r for r in rep.rows if r.probe == head)
        self.assertEqual(row.status, "todo")
        run(d, "git", "bisect", "reset")

    def test_first_eval_survives_failed_ancestry_check(self):
        # Regression: on a shallow/grafted clone `merge-base --is-ancestor` can
        # fail, which used to leave the range never "ready" so the first real
        # evaluation was swallowed as an anchor (no row, no detail).
        d, shas = make_repo(n=20, bug_at=18)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        m1 = run(d, "git", "rev-parse", "HEAD").stdout.strip()
        has_bug = "BUG" in Path(d, "code.txt").read_text()
        run(d, "git", "bisect", "bad" if has_bug else "good")
        orig = _report.is_ancestor
        _report.is_ancestor = lambda *a, **k: False  # emulate the failing check
        try:
            rep = _report.build_report(d)
        finally:
            _report.is_ancestor = orig
        self.assertIn(m1, [r.probe for r in rep.rows])
        run(d, "git", "bisect", "reset")

    def test_render_format(self):
        d, shas = make_repo(n=8, bug_at=5)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        has_bug = "BUG" in Path(d, "code.txt").read_text()
        run(d, "git", "bisect", "bad" if has_bug else "good")
        rep = _report.build_report(d)
        rep.rows[0].sidecar = _report.Sidecar(
            fixups=[{"kind": "replace", "path": "f",
                     "old": "OLD_VALUE", "new": "NEW_VALUE"}], steps=[])
        md = _report.render_markdown(rep, details=True)
        self.assertIn("| good | bad | probe | range | status |", md)
        self.assertRegex(md, r"🟢|🔴")                     # status icons
        self.assertNotIn("✅", md)
        self.assertNotIn("→ ", md.split("## Details")[0])  # no dates/arrows in range
        self.assertIn("in `f`: `OLD_VALUE` → `NEW_VALUE`", md)
        run(d, "git", "bisect", "reset")

    def test_no_bisect_returns_none(self):
        d, _ = make_repo(n=4, bug_at=3)
        self.assertIsNone(_report.build_report(d))

    def test_render_from_saved_log(self):
        d, shas = make_repo(n=8, bug_at=5)
        run(d, "git", "bisect", "start", shas[-1], shas[0])
        run(d, "git", "bisect", "good" if "BUG" not in Path(d, "code.txt").read_text()
            else "bad")
        log = _report.bisect_log(d)
        rep = _report.build_report(d, log_text=log)
        self.assertIsNotNone(rep)
        run(d, "git", "bisect", "reset")

    def test_fmt_duration(self):
        self.assertEqual(_report.fmt_duration(0), "0m")
        self.assertEqual(_report.fmt_duration(90), "1m")
        self.assertEqual(_report.fmt_duration(3700), "1h 1m")
        self.assertEqual(_report.fmt_duration(90000), "1d 1h 0m")

    def test_short_seconds(self):
        # decimals for short runs, dropping a trailing .0
        self.assertEqual(_report.short_seconds(0.5), "0.5s")
        self.assertEqual(_report.short_seconds(0.503), "0.5s")
        self.assertEqual(_report.short_seconds(9.34), "9.3s")
        self.assertEqual(_report.short_seconds(12.4), "12.4s")
        self.assertEqual(_report.short_seconds(45), "45s")     # 45.0 -> "45s"
        # minute and up: m/s then h/m
        self.assertEqual(_report.short_seconds(60), "1m00s")
        self.assertEqual(_report.short_seconds(83), "1m23s")
        self.assertEqual(_report.short_seconds(7325), "2h02m")

    def test_hammer_step_summary(self):
        # the status-table cell for a hammer verdict shows total runs, threads
        # used, and total runtime.
        sc = _report.Sidecar(
            outcome="good", pending=False,
            steps=[_report.Step(
                verb="hammer", cmd="./flaky", code=0, duration_s=60.0,
                extra={"executed": 12043, "parallel": 32, "elapsed_s": 60.0,
                       "durations_s": [0.002]})])
        summary = _report._step_summary(sc)
        self.assertIn("12043 runs", summary)
        self.assertIn("32× parallel", summary)
        self.assertIn("1m00s", summary)

    def test_report_memoisation_is_correct(self):
        # build_report caches immutable graph facts across renders. Two things must
        # hold: rendering the same finished bisect twice is byte-identical (the
        # cache-hit path returns the same answer), and a *different* repo in the
        # same process is not contaminated by the first repo's cached values
        # (caches are keyed by repo).
        d1, shas1 = make_repo(n=12, bug_at=7)
        script = Path(d1, "t.sh")
        script.write_text("#!/bin/sh\ngrep -q BUG code.txt && exit 1\nexit 0\n")
        script.chmod(0o755)
        run(d1, "git", "bisect", "start", shas1[-1], shas1[0])
        run(d1, "git", "bisect", "run", "./t.sh")
        md_a = _report.render_markdown(_report.build_report(d1))
        md_b = _report.render_markdown(_report.build_report(d1))  # 2nd render: all cache hits
        self.assertEqual(md_a, md_b)
        self.assertIn("First bad commit", md_a)
        run(d1, "git", "bisect", "reset")

        d2, shas2 = make_repo(n=8, bug_at=5)  # unrelated repo, same process
        run(d2, "git", "bisect", "start", shas2[-1], shas2[0])
        rep2 = _report.build_report(d2)
        self.assertEqual(rep2.orig_bad, shas2[-1])   # its own anchors, not d1's
        self.assertIn(shas2[0], rep2.orig_goods)
        run(d2, "git", "bisect", "reset")


if __name__ == "__main__":
    unittest.main(verbosity=2)
