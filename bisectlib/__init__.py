#!/usr/bin/env python3
"""bisectlib - write tiny `git bisect run` recipes in Python.

A recipe is a one-shot script. `git bisect run python recipe.py` spawns a fresh
process per commit, so there is exactly one session per process: no ctx object,
no decorator, no return value. Falling off the end of the script == GOOD.

    from bisectlib import run, test

    run("cmake -B build")                 # infra: fail -> ABORT (exit 128)
    run("cmake --build build -j")         # infra: fail -> ABORT
    test("ctest -R foo", attempts=5, min_passes=2)  # flaky verdict: 2 of up to 5
    # reached the end -> GOOD (exit 0)

Exit-code contract (what `git bisect run` reads):
    0           good   (bug absent)
    1           bad    (bug present)
    125         skip   (commit untestable)
    128         abort  (harness broken; bisect state preserved -> fix & resume)

Verbs:
    run(cmd, skip_on_error=False, ...)        infra; ABORTS on error by default
    test(cmd, attempts=1, min_passes=None,…)  a verdict; pass->continue, fail->BAD.
                                              Use several; they AND together.
    hammer(cmd, for_seconds=60, ...)          hunt a rare flake: run til one fails
                                              (default: all cores for a minute)
    check(cmd) -> Result                      runs once, NEVER exits (introspection)
    once(key="setup") -> bool                 guard one-time setup: if once(): ...
    good()/bad()/skip()/abort()               decide the commit directly from Python
                                              (e.g. after measuring with check())
    replace(path, old, new, ...)         sed-like edit, auto-reverted (clean tree)
    fixup(patch=/cherry_pick=, when=)    apply a patch/cherry-pick, auto-reverted
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional, Union

# Small closed sets of string options, typed so editors autocomplete the choices
# and type-checkers reject a typo *before* the recipe runs (the runtime `_one_of`
# check still guards callers without a type-checker). Chosen over an Enum to keep
# recipes terse — `on_timeout="bad"`, no import, no `OnTimeout.BAD` ceremony.
_BadWhen = Literal["fail", "pass"]
_OnTimeout = Literal["abort", "skip", "bad"]

__version__ = "0.14.1"

__all__ = [
    "run", "test", "hammer", "check",       # the verbs
    "once",                                  # guard one-time setup (keyed)
    "good", "bad", "skip", "abort",         # verdict primitives
    "replace", "fixup",                     # tree edits (auto-reverted)
    "in_range", "touches", "sha", "subject", "is_clean",  # git helpers
    "configure", "Result",
    "GOOD", "BAD", "SKIP", "ABORT",
]

# exit codes / outcomes -------------------------------------------------------
GOOD, BAD, SKIP, ABORT = 0, 1, 125, 128
_OUTCOME_NAME = {GOOD: "good", BAD: "bad", SKIP: "skip", ABORT: "abort"}


# ----------------------------------------------------------------- configuration
@dataclass
class _Config:
    status_md: Optional[str] = None     # default: <repo>/.bisect/status.md
    logs: Optional[str] = None          # default: <repo>/.bisect/
    clean: str = "reset"                # "reset" | "clean"
    color: Optional[bool] = None        # None=auto
    cwd: Optional[str] = None           # default working dir for commands (repo root)


_cfg = _Config()
_steps: list[dict] = []
_reverts: list[Callable[[], None]] = []
_final: dict = {"outcome": "good", "code": GOOD}
_finalized = False
_once_pending: set[str] = set()


def configure(status_md=None, logs=None,
              clean: Optional[Literal["reset", "clean"]] = None,
              color=None, cwd=None) -> None:
    if status_md is not None:
        _cfg.status_md = status_md
    if logs is not None:
        _cfg.logs = logs
    if clean is not None:
        if clean not in ("reset", "clean"):
            raise ValueError(
                f"clean={clean!r} is not valid; expected 'reset' or 'clean'")
        _cfg.clean = clean
    if color is not None:
        _cfg.color = color
    if cwd is not None:
        _cfg.cwd = cwd


def _workdir(cwd: Optional[str]) -> str:
    """Resolve the working directory for a command.

    Precedence: per-call ``cwd`` > global ``configure(cwd=…)`` > repo root.
    A relative path is resolved against the repo root, so ``cwd="build"`` means
    ``<repo>/build`` regardless of where the recipe was launched from.
    """
    base = cwd if cwd is not None else _cfg.cwd
    if base is None:
        return _toplevel()
    if os.path.isabs(base):
        return base
    return os.path.join(_toplevel(), base)


# ------------------------------------------------------------------------- git
def _git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def _toplevel() -> str:
    return _git("rev-parse", "--show-toplevel")


def sha() -> str:
    """Full sha of the commit currently being evaluated (HEAD)."""
    return _git("rev-parse", "HEAD")


def subject() -> str:
    """Commit subject of HEAD."""
    return _git("show", "-s", "--format=%s", "HEAD")


def is_clean() -> bool:
    """True if the working tree has no uncommitted changes."""
    return _git("status", "--porcelain") == ""


class _Range:
    def __init__(self, lo: str, hi: str):
        self.lo, self.hi = lo, hi

    def __contains__(self, rev: str) -> bool:
        return _in_range(rev, self.lo, self.hi)

    def __bool__(self) -> bool:
        return _in_range("HEAD", self.lo, self.hi)


def _in_range(rev: str, lo: str, hi: str) -> bool:
    """True if rev is a descendant of lo (or == lo) and an ancestor of hi (or == hi)."""
    def anc(a, b):
        return a == b or subprocess.run(
            ["git", "merge-base", "--is-ancestor", a, b],
            capture_output=True,
        ).returncode == 0
    rev = _git("rev-parse", rev)
    return anc(lo, rev) and anc(rev, hi)


def in_range(spec: str, hi: Optional[str] = None):
    """Predicate: is HEAD within [lo, hi]? Accepts ('lo..hi') or (lo, hi)."""
    if hi is None and ".." in spec:
        if "..." in spec:
            raise ValueError(
                f"in_range({spec!r}): use two dots 'lo..hi', not three "
                f"(three-dot is git's symmetric-difference syntax, not a range)")
        lo, hi = spec.split("..", 1)
    else:
        lo = spec
    if not lo or not hi:
        raise ValueError(
            f"in_range({spec!r}"
            + (f", {hi!r}" if hi is not None else "")
            + "): need both a low and a high revision, e.g. in_range('v1.0..v2.0')")
    return _Range(lo, hi)


def touches(path: str) -> bool:
    """True if the HEAD commit modified `path`."""
    files = _git("show", "--name-only", "--format=", "HEAD").splitlines()
    return any(f == path or f.startswith(path.rstrip("/") + "/") for f in files)


# --------------------------------------------------------------------- console
def _use_color() -> bool:
    if _cfg.color is not None:
        return _cfg.color
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


_C = {"run": "\033[36m", "test": "\033[35m", "hammer": "\033[95m",
      "check": "\033[90m", "good": "\033[32m", "bad": "\033[31m",
      "skip": "\033[33m", "abort": "\033[91m", "dim": "\033[2m",
      "reset": "\033[0m"}


def _echo_start(verb: str, cmd: str) -> None:
    short = sha()[:9] if _in_git() else "?"
    if _use_color():
        sys.stderr.write(f"{_C.get(verb,'')}▶ [{short}] {verb:<5}{_C['reset']} {cmd}\n")
    else:
        sys.stderr.write(f"> [{short}] {verb:<5} {cmd}\n")
    sys.stderr.flush()


def _echo_result(verb: str, cmd: str, ok: bool, seconds: float, label: str) -> None:
    color = _C.get(label, "")
    mark = "✓" if ok else "✗"
    if _use_color():
        sys.stderr.write(f"{color}{mark} {label}{_C['reset']} "
                         f"{_C['dim']}({seconds:.1f}s){_C['reset']} {cmd}\n")
    else:
        sys.stderr.write(f"{mark} {label} ({seconds:.1f}s) {cmd}\n")
    sys.stderr.flush()


def _in_git() -> bool:
    return subprocess.run(["git", "rev-parse", "--git-dir"],
                          capture_output=True).returncode == 0


# ---------------------------------------------------------------------- Result
@dataclass
class Result:
    code: int
    out: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.code == 0


# git exports these per-invocation while running a `git bisect run` command; if
# they leak into the recipe's commands, any `git` those commands call resolves
# against git's bisect context instead of discovering from the directory — the
# classic "works when I run it, breaks under `git bisect run`" trap. Strip them so
# commands behave exactly as in a plain shell.
_GIT_ENV_STRIP = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX",
    "GIT_NAMESPACE", "GIT_COMMON_DIR", "GIT_INTERNAL_GETTEXT_TEST_HARNESS",
)


def _clean_env(workdir: str) -> dict:
    """Environment for spawned commands: git's per-invocation vars removed, and
    PWD kept in sync with the real cwd (subprocess chdir's but leaves PWD stale)."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_STRIP}
    env["PWD"] = os.path.abspath(workdir)
    return env


def _exec(cmd: str, timeout: Optional[float], log_path: Optional[Path],
          cwd: Optional[str] = None, stream: bool = True) -> Result:
    """Run a shell command, streaming its output live while also capturing it.

    Combined stdout+stderr is echoed to this process's stderr as it arrives (so
    you watch the build/test run — git bisect run forwards it to your terminal),
    appended line-by-line to the log file so it can be tailed/opened *while the
    command runs*, and collected into the returned Result. The process group is
    killed on timeout.

    ``stream=False`` suppresses the live stderr echo (but still captures + logs).
    Used when many runs execute concurrently, where interleaving dozens of output
    streams onto one terminal would be unreadable.
    """
    start = time.monotonic()
    workdir = _workdir(cwd)
    env = _clean_env(workdir)
    # Open the log up front and write to it as output arrives, so the linked log
    # in status.md exists and grows live (watchable), instead of only appearing
    # once the command finishes. Line-buffered so each line is flushed promptly.
    log_fh = None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "w", buffering=1)
        except OSError:
            log_fh = None
    proc = subprocess.Popen(
        cmd, shell=True, cwd=workdir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, start_new_session=True,
    )
    captured: list[str] = []

    def _pump() -> None:
        for line in proc.stdout:            # line-buffered; live as it arrives
            captured.append(line)
            if stream:
                sys.stderr.write(line)
            if log_fh is not None:
                try:
                    log_fh.write(line)
                except OSError:
                    pass
        if stream:
            sys.stderr.flush()

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
    pump.join()
    if log_fh is not None:
        try:
            log_fh.close()
        except OSError:
            pass

    out = "".join(captured)
    code = -1 if timed_out else proc.returncode   # -1 == timeout sentinel
    seconds = time.monotonic() - start
    return Result(code=code, out=out, seconds=seconds)


# --------------------------------------------------------------- log directory
def _bisect_anchors() -> tuple[str, Optional[str], Optional[str]]:
    """(repo_top, first_bad, first_good) — the ORIGINAL anchors of this session.

    These are fixed for the whole bisect. `git bisect log` grows with every
    evaluation, so keying on anything else would change the identity each commit
    (breaking the once() markers and scattering logs across directories).
    """
    try:
        top = _toplevel()
    except RuntimeError:
        top = os.getcwd()
    bad0 = good0 = None
    for line in _git("bisect", "log", check=False).splitlines():
        if not line.startswith("git bisect "):
            continue
        try:
            verb, *args = shlex.split(line)[2:]
        except ValueError:
            continue
        revs = [a for a in args if not a.startswith("-")]
        if verb == "start" and revs:
            bad0 = bad0 or revs[0]
            good0 = good0 or (revs[1] if len(revs) > 1 else None)
        elif verb == "bad" and revs:
            bad0 = bad0 or revs[0]
        elif verb == "good" and revs:
            good0 = good0 or revs[0]
    return top, bad0, good0


def _bisect_id() -> str:
    top, bad0, good0 = _bisect_anchors()
    anchors = f"{bad0 or ''}\n{good0 or ''}"
    return hashlib.sha1((top + "\n" + anchors).encode()).hexdigest()[:12]


# The report and per-commit logs live in a single `.bisect/` directory at the
# repo root — right beside the code being bisected, so `status.md` opens in the
# editor at a fixed, watchable path (``.bisect/status.md``) instead of a
# per-run subdir buried under ~/.cache. `.bisect/` is registered in the repo's
# local excludes (see `_register_exclude`), so it never shows up in `git status`
# or gets committed, and `git checkout` between commits leaves it untouched.
_DIRNAME = ".bisect"
_session_ready = False
_SHA_DIR_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _owned_entry(child: Path) -> bool:
    """True if `child` is something bisectlib itself created in the logs dir.

    Used to scope the new-bisect wipe to our own files (`status.md`, `once-*`
    markers, per-commit `<sha>/` dirs) so pointing ``configure(logs=…)`` at a
    shared or pre-populated directory never deletes the user's unrelated files.
    The `id` marker is preserved by the caller, not here.
    """
    name = child.name
    if name == "status.md" or name.startswith("once-"):
        return True
    if child.is_dir() and (_SHA_DIR_RE.match(name) or name == "unknown"):
        return True
    return False


def _bisect_root() -> Path:
    if _cfg.logs:
        return Path(_cfg.logs)
    try:
        top = _toplevel()
    except RuntimeError:
        top = os.getcwd()
    return Path(top) / _DIRNAME


def _register_exclude() -> None:
    """Add `.bisect/` to the repo's local excludes (`.git/info/exclude`).

    Local and untracked — it does not touch the project's own tracked
    `.gitignore`. This keeps our working-tree directory out of `git status`, so
    it never gets committed by accident and doesn't make the recipe's own
    `is_clean()` checks trip over an untracked dir.
    """
    if _cfg.logs:  # user pointed logs elsewhere; nothing to exclude
        return
    try:
        git_dir = _git("rev-parse", "--git-dir", check=False)
        if not git_dir:
            return
        info = Path(git_dir) / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        entry = f"/{_DIRNAME}/"
        existing = exclude.read_text() if exclude.exists() else ""
        if entry not in existing.split():
            sep = "" if not existing or existing.endswith("\n") else "\n"
            with open(exclude, "a") as fh:
                fh.write(f"{sep}{entry}\n")
    except OSError:
        pass


def _ensure_session() -> None:
    """Prepare `.bisect/` once per process; clear it when a new bisect starts.

    The directory is reused for every commit of one bisect (its `id` file holds
    the bisect id — repo + original good/bad anchors). When those anchors change
    a *different* bisect is under way, so we wipe the stale report, logs and
    `once()` markers left by the previous one; the same anchors (a resume, or the
    next commit of the same run) keep everything, so `once()` setup stays done.
    """
    global _session_ready
    if _session_ready:
        return
    _session_ready = True
    _register_exclude()
    root = _bisect_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        idfile = root / "id"
        cur = _bisect_id()
        prev = idfile.read_text().strip() if idfile.exists() else None
        if prev != cur:
            # A different bisect (repo + original good/bad anchors changed): drop
            # the previous run's report, logs and once() markers so they don't
            # leak in. Only our own entries are removed — safe even when
            # configure(logs=…) points at a shared/pre-populated directory.
            for child in root.iterdir():
                if child.name == "id" or not _owned_entry(child):
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
            idfile.write_text(cur)
    except OSError:
        pass


def _logs_dir() -> Path:
    _ensure_session()
    return _bisect_root()


def _status_md_path() -> Path:
    if _cfg.status_md:
        return Path(_cfg.status_md)
    return _logs_dir() / "status.md"


def _commit_log_dir() -> Path:
    return _logs_dir() / (sha() if _in_git() else "unknown")


# --------------------------------------------------------------------- verdict
def _decide(outcome_code: int, reason: str = "") -> "NoReturn":  # type: ignore[name-defined]
    """Record the verdict and exit the process with the bisect exit code."""
    _final["outcome"] = _OUTCOME_NAME[outcome_code]
    _final["code"] = outcome_code
    sys.exit(outcome_code)


def _verdict(code: int, msg: str) -> "NoReturn":  # type: ignore[name-defined]
    label = _OUTCOME_NAME[code]
    if _use_color():
        sys.stderr.write(f"{_C.get(label, '')}● {label}{_C['reset']} {msg}\n")
    else:
        sys.stderr.write(f"● {label} {msg}\n")
    sys.stderr.flush()
    _decide(code)


# Explicit verdict primitives — decide the current commit straight from Python
# (e.g. after measuring something with check()), no shell command needed:
#     size = int(check("stat -c%s build/app").out)
#     if size > 5 * 1024 * 1024:
#         bad("binary too big")
# Each exits the process immediately; reaching the end of the recipe is good.
def good(msg: str = "") -> "NoReturn":   # type: ignore[name-defined]
    """Declare this commit GOOD (exit 0) now — short-circuit the rest of the recipe."""
    _verdict(GOOD, msg)


def bad(msg: str = "") -> "NoReturn":    # type: ignore[name-defined]
    """Declare this commit BAD (exit 1) now — the bug is present."""
    _verdict(BAD, msg)


def skip(msg: str = "") -> "NoReturn":   # type: ignore[name-defined]
    """SKIP this commit (exit 125) — it can't be judged, route around it."""
    _verdict(SKIP, msg)


def abort(msg: str = "") -> "NoReturn":  # type: ignore[name-defined]
    """ABORT the bisect (exit 128) — harness broken; state kept, fix & resume."""
    _verdict(ABORT, msg)


# -------------------------------------------------------------------- run/test
def _one_of(name: str, value: str, allowed: tuple[str, ...]) -> None:
    """Fail fast on a mistyped string option instead of silently defaulting.

    A typo like ``bad_when="Pass"`` or ``on_timeout="abrot"`` used to fall
    through to a default — for ``bad_when`` that silently inverts the whole
    bisect. Since this library's whole point is never to be silently wrong, an
    unknown value raises (an uncaught error in a recipe ABORTS, never 'bad').
    """
    if value not in allowed:
        raise ValueError(
            f"{name}={value!r} is not valid; expected one of "
            + ", ".join(repr(a) for a in allowed))


def _slug(cmd: str, maxlen: int = 40) -> str:
    """A short, filesystem-safe label derived from a command, for log filenames.

    The program name is reduced to its basename (``./gradlew`` -> ``gradlew``,
    ``/usr/bin/make`` -> ``make``) and everything is lowercased with runs of
    non-alphanumerics collapsed to single dashes, e.g.
    ``./gradlew :nativesdk:fetchAgent`` -> ``gradlew-nativesdk-fetchagent``.
    """
    tokens = cmd.strip().split()
    if tokens:
        tokens[0] = os.path.basename(tokens[0])
    s = re.sub(r"[^a-z0-9]+", "-", " ".join(tokens).lower()).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip("-")
    return s or "cmd"


def run(cmd: str, *, skip_on_error: bool = False, timeout: Optional[float] = None,
        on_timeout: _OnTimeout = "abort", cwd: Optional[str] = None) -> Result:
    """Infrastructure step (configure/build/setup).

    Success -> continue. Failure -> ABORT by default (the harness is presumed
    broken; bisect state is preserved so you can fix the recipe and resume).
    Set skip_on_error=True to SKIP this commit instead.

    ``cwd`` sets the working directory (relative to the repo root; absolute paths
    honoured); defaults to the repo root or ``configure(cwd=…)``.
    """
    _one_of("on_timeout", on_timeout, ("abort", "skip", "bad"))
    _echo_start("run", cmd)
    log_name = f"{len(_steps)+1:02d}-run-{_slug(cmd)}.log"
    _begin_step("run", cmd, log_name)
    res = _exec(cmd, timeout, _commit_log_dir() / log_name, cwd)
    timed_out = res.code == -1
    ok = res.code == 0
    _record_step("run", cmd, res, ok, log=log_name)
    if timed_out:
        label = on_timeout
        _echo_result("run", cmd, False, res.seconds, label)
        _decide({"abort": ABORT, "skip": SKIP, "bad": BAD}.get(on_timeout, ABORT))
    if ok:
        _echo_result("run", cmd, True, res.seconds, "ok")
        return res
    # failure
    if skip_on_error:
        _echo_result("run", cmd, False, res.seconds, "skip")
        _decide(SKIP)
    else:
        _echo_result("run", cmd, False, res.seconds, "abort")
        _decide(ABORT)


def test(cmd: str, *, attempts: int = 1, min_passes: Optional[int] = None,
         passed: Optional[Callable[[Result], bool]] = None, warmup: int = 0,
         bad_when: _BadWhen = "fail", timeout: Optional[float] = None,
         on_timeout: _OnTimeout = "skip", cwd: Optional[str] = None) -> Optional[Result]:
    """A verdict step. Good -> continue; bad -> exit 1 (BAD).

    Like ``run``, a *passing* test continues to the next line, so you can have
    several ``test`` calls and they combine with logical AND — any one failing is
    BAD; reaching the end of the recipe is GOOD. Returns the last Result on good.

    ``attempts`` is the *maximum* number of tries and ``min_passes`` how many must
    pass (default: all). Evaluation **stops as soon as the verdict is known** — at
    the moment ``min_passes`` is reached (good), or once the remaining attempts can
    no longer reach it (bad) — so ``attempts`` is an upper bound, not a fixed count.
    (To hunt a *rare* flake — fail on the first bad run of many — use ``hammer``.)

    ``passed`` decides whether one attempt passed: a callable receiving the
    :class:`Result` (``.code``, ``.ok``, ``.out``, ``.seconds``) and returning
    bool. Default: ``lambda r: r.ok`` (exit code 0). Because it sees ``.seconds``,
    timing thresholds are just predicates combined with the quorum, e.g. the
    minimum of 5 runs below 6.7s::

        test("./bench", attempts=5, min_passes=1, passed=lambda r: r.seconds < 6.7)

    The quorum count expresses every aggregate: ``min(times) < T`` -> min_passes=1;
    ``max(times) < T`` (all) -> min_passes=attempts; ``median < T`` ->
    min_passes=attempts//2 + 1. Combine with success via ``r.ok and r.seconds < T``.

    ``warmup`` runs extra leading throwaway executions (excluded from the pass
    count). ``bad_when="pass"`` inverts the bug direction.

    A test that **cannot be run** — exit ``127`` (command not found) or ``126``
    (not executable) — is treated as a broken recipe and **ABORTS**, never BAD:
    a test that never executed is not evidence the bug is present, and marking it
    bad would silently mis-bisect. (A crash/signal, exit ``>=128``, stays BAD — it
    may be the regression itself.)
    """
    _one_of("bad_when", bad_when, ("fail", "pass"))
    _one_of("on_timeout", on_timeout, ("skip", "bad", "abort"))
    if attempts < 1:
        raise ValueError(f"attempts={attempts!r} must be >= 1")
    if warmup < 0:
        raise ValueError(f"warmup={warmup!r} must be >= 0")
    if min_passes is not None and not (1 <= min_passes <= attempts):
        raise ValueError(
            f"min_passes={min_passes!r} must be between 1 and attempts={attempts} "
            f"(min_passes>attempts is unreachable -> always bad)")
    _echo_start("test", cmd)
    if passed is None:
        passed = lambda r: r.ok  # noqa: E731
    if min_passes is None:
        min_passes = attempts

    durations: list[float] = []
    passes = 0
    executed = 0
    last: Optional[Result] = None
    idx, slug = len(_steps) + 1, _slug(cmd)
    log_name = f"{idx:02d}-test-{slug}-1.log"  # actual file of the last attempt run
    _begin_step("test", cmd, log_name)
    for i in range(warmup + attempts):
        log_name = f"{idx:02d}-test-{slug}-{i+1}.log"
        res = _exec(cmd, timeout, _commit_log_dir() / log_name, cwd)
        last = res
        if res.code == -1:  # timeout
            _record_step("test", cmd, res, False, log=log_name,
                         extra={"attempts": attempts, "passes": passes,
                                "timeout": True})
            _echo_result("test", cmd, False, res.seconds, on_timeout)
            _decide({"skip": SKIP, "bad": BAD, "abort": ABORT}.get(on_timeout, SKIP))
        if res.code in (126, 127):
            # The shell couldn't run the test at all — 127 (command not found) or
            # 126 (not executable): a broken recipe/build, not a "bug present"
            # verdict. Marking it BAD here would silently mis-bisect (the exact
            # sin this library exists to prevent), so ABORT instead — fix the
            # recipe and resume. Retrying is pointless (it's deterministic), so
            # bail on the first occurrence. A crash/signal (exit >=128) is left
            # as BAD on purpose: it may *be* the regression.
            _record_step("test", cmd, res, False, log=log_name,
                         extra={"attempts": attempts, "passes": passes,
                                "unrunnable": res.code})
            _echo_result("test", cmd, False, res.seconds, "abort")
            sys.stderr.write(
                f"   test command exited {res.code}: it could not be run "
                f"(missing binary / wrong path / not built?) — aborting so you "
                f"can fix the recipe, not marking this commit bad\n")
            _decide(ABORT)
        if i < warmup:
            continue
        executed += 1
        durations.append(res.seconds)
        ok = passed(res)
        if bad_when == "pass":
            ok = not ok
        if ok:
            passes += 1
        # stop as soon as the verdict is locked in
        if passes >= min_passes:
            break
        if (attempts - executed) < (min_passes - passes):
            break

    good = passes >= min_passes
    extra = {"attempts": attempts, "executed": executed, "passes": passes,
             "min_passes": min_passes, "durations_s": [round(d, 4) for d in durations]}
    _record_step("test", cmd, last, good, log=log_name, extra=extra,
                 outcome="good" if good else "bad")

    fastest = f" · min {min(durations):.3g}s" if durations else ""
    _echo_result("test", cmd, good, last.seconds if last else 0.0,
                 "good" if good else "bad")
    sys.stderr.write(f"   {passes}/{executed}{fastest}\n")
    if not good:
        _decide(BAD)
    return last  # good: continue to the next step (multiple tests AND together)


def _hammer_log(fh, n: int, kind: str, res: Result) -> None:
    """Append one line per completed run to the shared hammer log, plus the full
    output of any run that didn't pass (so a rare failure is captured even though
    the thousands of passing runs before it were not individually logged)."""
    label = {"pass": "pass", "fail": "FAIL", "timeout": "TIMEOUT",
             "unrunnable": "UNRUNNABLE"}.get(kind, kind)
    try:
        fh.write(f"run {n:>6}: {label:<10} exit={res.code} {res.seconds:.3f}s\n")
        if kind != "pass":
            fh.write("----- output of the failing run -----\n")
            fh.write(res.out if (not res.out or res.out.endswith("\n")) else res.out + "\n")
            fh.write("-------------------------------------\n")
    except OSError:
        pass


def hammer(cmd: str, *, for_seconds: float = 60.0, parallel: Optional[int] = None,
           passed: Optional[Callable[[Result], bool]] = None,
           bad_when: _BadWhen = "fail", timeout: Optional[float] = None,
           on_timeout: _OnTimeout = "skip", cwd: Optional[str] = None) -> Optional[Result]:
    """Hunt a rare flake: run ``cmd`` over and over until one fails.

    The mirror image of the flaky-*tolerant* ``test(attempts=…, min_passes=…)``.
    Where ``test`` runs a quorum and forgives a few failures, ``hammer`` pounds on
    the command to *expose* a failure that only shows up once in thousands of runs::

        hammer("./flaky")                            # one minute, all cores
        hammer("./flaky", for_seconds=120, parallel=8)

    It launches runs up to ``parallel`` at a time for ``for_seconds`` of wall-clock
    time; the defaults are **one minute** and **all CPU cores**
    (``os.cpu_count()``). The commit is **BAD the instant any run fails** (the
    search stops and the failing run's output is shown), and **GOOD only if the
    whole budget elapses with no failure**. Like ``test``, a good verdict continues
    to the next recipe line, so hammers/tests AND together.

    ``passed`` / ``bad_when`` define what "a failing run" is exactly as in ``test``
    (default: exit code 0 is a pass), so you can hammer a benchmark for a jitter
    spike too: ``passed=lambda r: r.seconds < T``. A run that **cannot be launched**
    (exit 126/127) ABORTS rather than counting as a failure, same as ``test``.
    """
    import concurrent.futures as cf

    _one_of("bad_when", bad_when, ("fail", "pass"))
    _one_of("on_timeout", on_timeout, ("skip", "bad", "abort"))
    if parallel is None:
        parallel = os.cpu_count() or 1     # default: use every core
    elif parallel < 1:
        raise ValueError(f"parallel={parallel!r} must be >= 1")
    if for_seconds <= 0:
        raise ValueError(f"for_seconds={for_seconds!r} must be > 0")
    _echo_start("hammer", cmd)
    if passed is None:
        passed = lambda r: r.ok  # noqa: E731

    idx, slug = len(_steps) + 1, _slug(cmd)
    log_name = f"{idx:02d}-hammer-{slug}.log"
    _begin_step("hammer", cmd, log_name)
    log_path = _commit_log_dir() / log_name
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "w", buffering=1)
    except OSError:
        log_fh = None
    # Resolve the working dir once: each run would otherwise re-shell
    # `git rev-parse --show-toplevel`, needless overhead when firing thousands of
    # runs. An absolute cwd passes through _workdir untouched.
    abs_cwd = _workdir(cwd)

    st = {"executed": 0, "passes": 0, "failures": 0, "completed": 0,
          "min_dur": None, "last": None, "failing": None,
          "abort_code": None, "timed_out": False, "stop": False}
    t0 = time.monotonic()
    deadline = t0 + for_seconds

    def record(res: Result) -> None:
        # Called only from this (main) thread as futures complete, so no locking:
        # the worker threads just run _exec and return a Result.
        st["last"] = res
        st["completed"] += 1
        n = st["completed"]
        if res.code == -1:                       # timed out
            st["timed_out"] = True
            st["stop"] = True
            kind = "timeout"
        elif res.code in (126, 127):             # unrunnable -> broken recipe
            st["abort_code"] = res.code
            st["stop"] = True
            kind = "unrunnable"
        else:
            ok = passed(res)
            if bad_when == "pass":
                ok = not ok
            st["executed"] += 1
            st["min_dur"] = (res.seconds if st["min_dur"] is None
                             else min(st["min_dur"], res.seconds))
            if ok:
                st["passes"] += 1
                kind = "pass"
            else:
                st["failures"] += 1
                if st["failing"] is None:
                    st["failing"] = res
                st["stop"] = True                # any failure ends the hunt -> BAD
                kind = "fail"
        if log_fh is not None:
            _hammer_log(log_fh, n, kind, res)

    with cf.ThreadPoolExecutor(max_workers=parallel) as ex:
        inflight: set = set()

        def top_up() -> None:
            while (len(inflight) < parallel and not st["stop"]
                   and time.monotonic() < deadline):
                inflight.add(ex.submit(_exec, cmd, timeout, None, abs_cwd, False))

        top_up()
        while inflight:
            done, inflight = cf.wait(inflight, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                record(fut.result())
            if not st["stop"]:
                top_up()
    if log_fh is not None:
        try:
            log_fh.close()
        except OSError:
            pass

    executed, last = st["executed"], st["last"]
    elapsed = time.monotonic() - t0
    # The recorded step's duration is the *total* hammer wall time (not the last
    # run's), so status.md's per-commit total and Details "time" reflect the whole
    # soak. Carry the run count, thread count and runtime for the report to show.
    agg = Result(code=(last.code if last else 0), out="", seconds=elapsed)
    extra = {"executed": executed, "passes": st["passes"],
             "failures": st["failures"], "parallel": parallel,
             "for_seconds": for_seconds, "elapsed_s": round(elapsed, 3)}
    if st["min_dur"] is not None:
        extra["durations_s"] = [round(st["min_dur"], 4)]  # report shows "min Xs"

    if st["abort_code"] is not None:             # a run could not be launched
        extra["unrunnable"] = st["abort_code"]
        _record_step("hammer", cmd, agg, False, log=log_name, extra=extra, outcome="bad")
        _echo_result("hammer", cmd, False, elapsed, "abort")
        sys.stderr.write(
            f"   command exited {st['abort_code']}: it could not be run "
            f"(missing binary / wrong path / not built?) — aborting so you can fix "
            f"the recipe, not marking this commit bad\n")
        _decide(ABORT)
    if st["timed_out"]:                          # a run exceeded timeout
        extra["timeout"] = True
        _record_step("hammer", cmd, agg, False, log=log_name, extra=extra)
        _echo_result("hammer", cmd, False, elapsed, on_timeout)
        _decide({"skip": SKIP, "bad": BAD, "abort": ABORT}.get(on_timeout, SKIP))

    good = st["failures"] == 0
    _record_step("hammer", cmd, agg, good, log=log_name, extra=extra,
                 outcome="good" if good else "bad")
    _echo_result("hammer", cmd, good, elapsed, "good" if good else "bad")
    fastest = f" · min {st['min_dur']:.3g}s" if st["min_dur"] is not None else ""
    sys.stderr.write(
        f"   {executed} runs, {parallel}× parallel in {elapsed:.1f}s "
        f"· {st['passes']} passed, {st['failures']} failed{fastest}\n")
    if not good:
        fr = st["failing"]
        if fr is not None and fr.out:            # show WHY the hunt found a bad run
            sys.stderr.write("   --- first failing run output (tail) ---\n")
            for line in fr.out.splitlines()[-20:]:
                sys.stderr.write(f"   {line}\n")
        _decide(BAD)
    return last


def check(cmd: str, *, timeout: Optional[float] = None,
          cwd: Optional[str] = None) -> Result:
    """Run once and return the Result. NEVER exits the process."""
    _echo_start("check", cmd)
    log_name = f"{len(_steps)+1:02d}-check-{_slug(cmd)}.log"
    _begin_step("check", cmd, log_name)
    res = _exec(cmd, timeout, _commit_log_dir() / log_name, cwd)
    _record_step("check", cmd, res, res.ok, log=log_name)
    _echo_result("check", cmd, res.ok, res.seconds, "ok" if res.ok else "fail")
    return res


def once(key: str = "setup") -> bool:
    """True the first time this `key` is seen in the bisect session, False after.

    Guard one-time, commit-independent setup (fetch a dependency, create a
    symlink) that you'd otherwise repeat on every commit::

        if once():                          # default key, for a single setup block
            run("./gradlew :nativesdk:fetchAgent", cwd="test")
            run("ln -fs $(pwd)/.../liboneagentsdk.so .../liboneagentsdk.so")

    Each `key` has its own independent marker, so distinct setups don't interfere::

        if once("fetch-agent"):
            run("./gradlew :nativesdk:fetchAgent", cwd="test")
        if once("symlink"):
            run("ln -fs $(pwd)/.../liboneagentsdk.so .../liboneagentsdk.so")

    A key's "already ran" marker (scoped to the bisect id) is committed only once
    an evaluation that armed it finishes with a real verdict — **not on abort**.
    Keys committed by an *earlier* evaluation stay done; every key armed in an
    evaluation that then aborts re-runs next time (the library can't tell which
    key's block completed before the abort, so keep each block idempotent). The
    setup's artifacts must survive `git checkout` between commits (be untracked /
    outside the work tree, e.g. build outputs or symlinks in ignored dirs).
    """
    if _once_marker(key).exists():
        return False
    _once_pending.add(key)
    return True


def _once_marker(key: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:40] or "key"
    h = hashlib.sha1(key.encode()).hexdigest()[:8]
    return _logs_dir() / f"once-{slug}-{h}"


def _begin_step(verb, cmd, log=None) -> None:
    """Register a provisional 'running' step and refresh status.md *before* the
    command executes, so the report immediately shows what is running right now
    with a link to its (live, growing) log. `_record_step` replaces it once the
    command finishes.
    """
    _steps.append({"verb": verb, "cmd": cmd, "code": None,
                   "duration_s": None, "log": log, "running": True})
    _flush_status()


def _record_step(verb, cmd, res: Optional[Result], ok, extra=None, outcome=None,
                 log=None):
    step = {"verb": verb, "cmd": cmd,
            "code": (res.code if res else None),
            "duration_s": round(res.seconds, 4) if res else None,
            "log": log}
    if outcome:
        step["outcome"] = outcome
    if extra:
        step.update(extra)
    # Replace the in-flight 'running' placeholder from _begin_step, if present,
    # rather than appending a duplicate row.
    if _steps and _steps[-1].get("running"):
        _steps[-1] = step
    else:
        _steps.append(step)
    _flush_status()  # keep status.md current after every step, so it's watchable live


# -------------------------------------------------------------------- replace
def replace(path: str, old: Union[str, "re.Pattern"], new: str, *,
            count: int = 0, when=None,
            if_missing: Literal["skip", "abort", "ignore"] = "skip") -> None:
    """sed-like in-file edit, auto-reverted before the process exits.

    `old` is a literal substring (str) or a regex (re.Pattern); type decides.
    `if_missing`: "skip" (default), "abort", or "ignore" when `old` isn't found.
    """
    _one_of("if_missing", if_missing, ("skip", "abort", "ignore"))
    if when is not None and not _truthy(when):
        return
    p = Path(_toplevel()) / path if not os.path.isabs(path) else Path(path)
    text = p.read_text()
    if isinstance(old, re.Pattern):
        new_text, n = old.subn(new, text, count=count or 0)
    else:
        n = text.count(old) if count == 0 else min(text.count(old), count)
        new_text = text.replace(old, new, count if count else -1)
    if n == 0:
        if if_missing == "ignore":
            return
        sys.stderr.write(f"replace: pattern not found in {path}\n")
        _decide(SKIP if if_missing == "skip" else ABORT)
    _register_revert_path(path)
    p.write_text(new_text)
    # Record the full replacement (not truncated) so the report can show it in
    # its entirety; the renderer wraps it in backticks.
    old_str = old.pattern if isinstance(old, re.Pattern) else str(old)
    _final.setdefault("fixups", []).append(
        {"kind": "replace", "path": path,
         "detail": f"{old_str} → {new}"})
    sys.stderr.write(f"  edit {path}: {n} replacement(s)\n")


# ---------------------------------------------------------------------- fixup
@contextmanager
def fixup(patch: Optional[str] = None, *, cherry_pick: Optional[str] = None,
          when=None):
    """Apply a patch or cherry-pick for the duration of the block; auto-revert.

    `when` (predicate) gates application; if false the block runs unpatched.
    """
    applied = False
    if when is None or _truthy(when):
        if patch:
            _git("apply", patch)
            _final.setdefault("fixups", []).append({"kind": "patch", "detail": patch})
            applied = True
        elif cherry_pick:
            _git("cherry-pick", "--no-commit", cherry_pick)
            _final.setdefault("fixups", []).append(
                {"kind": "cherry-pick", "detail": cherry_pick})
            applied = True
    try:
        yield
    finally:
        if applied:
            _revert_tree()


def _truthy(when) -> bool:
    return bool(when() if callable(when) else when)


# ------------------------------------------------------------- clean-tree revert
def _register_revert_path(path: str) -> None:
    _reverts.append(lambda: subprocess.run(
        ["git", "checkout", "--", path], cwd=_toplevel(), capture_output=True))


def _revert_tree() -> None:
    top = _toplevel()
    subprocess.run(["git", "reset", "-q", "--hard"], cwd=top, capture_output=True)
    if _cfg.clean == "clean":
        # keep our own `.bisect/` (status.md + logs) — a plain `git clean -fdx`
        # would wipe the report mid-bisect.
        subprocess.run(["git", "clean", "-fdxq", "-e", _DIRNAME],
                       cwd=top, capture_output=True)


# ------------------------------------------------------------------- finalize
def _write_sidecar() -> None:
    if not _in_git():
        return
    try:
        d = _commit_log_dir()
        d.mkdir(parents=True, exist_ok=True)
        # `pending` is True for the per-step live writes (verdict not yet decided)
        # and False once _finalize has locked in the outcome. The renderer uses it
        # to show the in-flight commit's real verdict in the saved status.md, rather
        # than leaving it stuck on `todo` (git only records the mark after we exit).
        data = {"sha": sha(), "outcome": _final["outcome"],
                "exit_code": _final["code"], "pending": not _finalized,
                "steps": _steps}
        if "fixups" in _final:
            data["fixups"] = _final["fixups"]
        total = sum(s.get("duration_s") or 0 for s in _steps)
        data["duration_s"] = round(total, 4)
        (d / "eval.json").write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _refresh_status_md(decided: bool = False) -> None:
    """Re-render status.md from the reconstructed report.

    `git bisect run` records the current commit's good/bad/skip mark only *after*
    this process exits, so `git bisect log` doesn't yet reflect our verdict while
    we render. That is fine for the live per-step writes (the commit shows as an
    in-flight row). But on the *final* commit git records the mark, names the
    first-bad commit, and stops — no further evaluation runs, so status.md would
    forever miss the answer. When `decided` is set (finalize, real verdict), feed
    build_report a log with our own mark appended so the finished report is
    complete and shows the first-bad commit.
    """
    try:
        from . import _report
        log_text = None
        outcome = _final.get("outcome")
        if decided and _in_git() and outcome in ("good", "bad", "skip"):
            log = _git("bisect", "log", check=False)
            if log:
                log_text = f"{log}\ngit bisect {outcome} {sha()}\n"
        rep = _report.build_report(
            _toplevel(), log_text=log_text, logs_dir=str(_logs_dir()))
        if rep is None:
            return
        path = _status_md_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_report.render_markdown(rep, details=True))
    except Exception:
        pass  # rendering is best-effort, never breaks the recipe


def _flush_status() -> None:
    """Persist the in-progress sidecar and re-render status.md.

    Called after every step so the current commit's `eval.json` and the rendered
    `status.md` reflect progress live (the commit shows as the in-flight `todo`
    row with its steps so far) — open `.bisect/status.md` in your editor and it
    refreshes as a long build/test runs, instead of only updating once the
    verdict lands.
    """
    _write_sidecar()
    _refresh_status_md()


@atexit.register
def _finalize() -> None:
    global _finalized
    if _finalized:
        return
    _finalized = True
    # leftover applied fixups (no `with` block) get reverted to keep the tree clean
    if "fixups" in _final or _reverts:
        _revert_tree()
        for r in _reverts:
            r()
    # commit each armed once() marker only if this evaluation produced a real
    # verdict (not an abort) — a setup that aborted should re-run next time
    if _once_pending and _final.get("code") != ABORT:
        for key in _once_pending:
            try:
                m = _once_marker(key)
                m.parent.mkdir(parents=True, exist_ok=True)
                m.write_text("done")
            except OSError:
                pass
    _write_sidecar()
    _refresh_status_md(decided=True)


def _excepthook(exc_type, exc, tb):
    """An uncaught error in a recipe is a harness bug -> ABORT, never 'bad'.

    Finalize by hand first: `os._exit` below skips the atexit-registered
    `_finalize`, so without this any `fixup`/`replace` edit would be left in the
    tree (violating the clean-tree guarantee — SPEC §2.2) and status.md would
    stay stuck on the in-flight step. `_finalize` reverts edits, records the
    abort, and does *not* commit any armed `once()` markers (code == ABORT).
    """
    import traceback
    traceback.print_exception(exc_type, exc, tb)
    _final["outcome"], _final["code"] = "abort", ABORT
    try:
        _finalize()
    except Exception:
        pass  # never let cleanup mask the original error or block the exit
    os._exit(ABORT)


sys.excepthook = _excepthook
