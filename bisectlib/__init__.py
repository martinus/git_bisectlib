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
    check(cmd) -> Result                      runs once, NEVER exits (introspection)
    is_first_run() -> bool                    guard one-time setup: if is_first_run(): ...
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
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

__version__ = "0.7.0"

__all__ = [
    "run", "test", "check",                 # the verbs
    "is_first_run",                         # guard one-time setup
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
    status_md: Optional[str] = None     # default: <cache>/bisectlib/<session>/status.md
    logs: Optional[str] = None          # default: <cache>/bisectlib/<session>/
    clean: str = "reset"                # "reset" | "clean"
    color: Optional[bool] = None        # None=auto
    cwd: Optional[str] = None           # default working dir for commands (repo root)


_cfg = _Config()
_steps: list[dict] = []
_reverts: list[Callable[[], None]] = []
_final: dict = {"outcome": "good", "code": GOOD}
_finalized = False
_first_run_pending = False


def configure(status_md=None, logs=None, clean=None, color=None, cwd=None) -> None:
    if status_md is not None:
        _cfg.status_md = status_md
    if logs is not None:
        _cfg.logs = logs
    if clean is not None:
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
        lo, hi = spec.split("..", 1)
    else:
        lo = spec
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


_C = {"run": "\033[36m", "test": "\033[35m", "check": "\033[90m",
      "good": "\033[32m", "bad": "\033[31m", "skip": "\033[33m",
      "abort": "\033[91m", "dim": "\033[2m", "reset": "\033[0m"}


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
          cwd: Optional[str] = None) -> Result:
    """Run a shell command, streaming its output live while also capturing it.

    Combined stdout+stderr is echoed to this process's stderr as it arrives (so
    you watch the build/test run — git bisect run forwards it to your terminal)
    and simultaneously collected into the returned Result and the log file.
    The process group is killed on timeout.
    """
    start = time.monotonic()
    workdir = _workdir(cwd)
    env = _clean_env(workdir)
    proc = subprocess.Popen(
        cmd, shell=True, cwd=workdir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, start_new_session=True,
    )
    captured: list[str] = []

    def _pump() -> None:
        for line in proc.stdout:            # line-buffered; live as it arrives
            captured.append(line)
            sys.stderr.write(line)
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

    out = "".join(captured)
    code = -1 if timed_out else proc.returncode   # -1 == timeout sentinel
    seconds = time.monotonic() - start
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(out)
        except OSError:
            pass
    return Result(code=code, out=out, seconds=seconds)


# --------------------------------------------------------------- log directory
def _bisect_anchors() -> tuple[str, Optional[str], Optional[str]]:
    """(repo_top, first_bad, first_good) — the ORIGINAL anchors of this session.

    These are fixed for the whole bisect. `git bisect log` grows with every
    evaluation, so keying on anything else would change the identity each commit
    (breaking the first-run marker and scattering logs across directories).
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


def _cache_base() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(cache) / "bisectlib"


_resolved_logs_dir: Optional[Path] = None


_ID_LEN = 8  # short-id length used in the session dir name (and glob key)


def _session_dirname() -> str:
    """A human-readable, stable directory name for this bisect session.

    ``<YYYY-MM-DD>_<HH-MM>__<good>-<bad>__<id>`` — a date/time to tell sessions
    apart and spot the latest at a glance, the short ``good-bad`` range so you
    can see what was bisected, and a short id as a collision-proof suffix (the id
    is what we glob on to keep the name fixed for the whole session). Double
    underscores separate the three groups; kept lean with 7-char shas,
    minute-precision time, and an 8-char id, e.g.
    ``2026-07-02_08-30__ac65905-720acb6__49fd6cd5``.
    """
    _, bad0, good0 = _bisect_anchors()
    bid = _bisect_id()[:_ID_LEN]
    stamp = time.strftime("%Y-%m-%d_%H-%M")
    if good0 and bad0:
        return f"{stamp}__{good0[:7]}-{bad0[:7]}__{bid}"
    return f"{stamp}__{bid}"


def _logs_dir() -> Path:
    global _resolved_logs_dir
    if _cfg.logs:
        return Path(_cfg.logs)
    if _resolved_logs_dir is not None:
        return _resolved_logs_dir
    base = _cache_base()
    bid = _bisect_id()[:_ID_LEN]
    # Reuse an existing session dir for this id so the name (and its timestamp)
    # stays fixed across every commit evaluated; only the first process creates it.
    existing = sorted(base.glob(f"*__{bid}"))
    _resolved_logs_dir = existing[0] if existing else base / _session_dirname()
    return _resolved_logs_dir


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
        on_timeout: str = "abort", cwd: Optional[str] = None,
        name: Optional[str] = None) -> Result:
    """Infrastructure step (configure/build/setup).

    Success -> continue. Failure -> ABORT by default (the harness is presumed
    broken; bisect state is preserved so you can fix the recipe and resume).
    Set skip_on_error=True to SKIP this commit instead.

    ``cwd`` sets the working directory (relative to the repo root; absolute paths
    honoured); defaults to the repo root or ``configure(cwd=…)``.
    """
    _echo_start("run", cmd)
    log_name = f"{len(_steps)+1:02d}-run-{_slug(cmd)}.log"
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
         bad_when: str = "fail", timeout: Optional[float] = None,
         on_timeout: str = "skip", cwd: Optional[str] = None,
         name: Optional[str] = None) -> Optional[Result]:
    """A verdict step. Good -> continue; bad -> exit 1 (BAD).

    Like ``run``, a *passing* test continues to the next line, so you can have
    several ``test`` calls and they combine with logical AND — any one failing is
    BAD; reaching the end of the recipe is GOOD. Returns the last Result on good.

    ``attempts`` is the *maximum* number of tries and ``min_passes`` how many must
    pass (default: all). Evaluation **stops as soon as the verdict is known** — at
    the moment ``min_passes`` is reached (good), or once the remaining attempts can
    no longer reach it (bad) — so ``attempts`` is an upper bound, not a fixed count.

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
    """
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
    log_name = f"{idx:02d}-test-{slug}.log"
    for i in range(warmup + attempts):
        res = _exec(cmd, timeout,
                    _commit_log_dir() / f"{idx:02d}-test-{slug}-{i+1}.log", cwd)
        last = res
        if res.code == -1:  # timeout
            _record_step("test", cmd, res, False, log=log_name,
                         extra={"attempts": attempts, "passes": passes,
                                "timeout": True})
            _echo_result("test", cmd, False, res.seconds, on_timeout)
            _decide({"skip": SKIP, "bad": BAD, "abort": ABORT}.get(on_timeout, SKIP))
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


def check(cmd: str, *, timeout: Optional[float] = None,
          cwd: Optional[str] = None) -> Result:
    """Run once and return the Result. NEVER exits the process."""
    _echo_start("check", cmd)
    log_name = f"{len(_steps)+1:02d}-check-{_slug(cmd)}.log"
    res = _exec(cmd, timeout, _commit_log_dir() / log_name, cwd)
    _record_step("check", cmd, res, res.ok, log=log_name)
    _echo_result("check", cmd, res.ok, res.seconds, "ok" if res.ok else "fail")
    return res


def is_first_run() -> bool:
    """True on the first commit evaluated in this bisect session, False after.

    Guard one-time, commit-independent setup (fetch a dependency, create a
    symlink) that you'd otherwise repeat on every commit::

        if is_first_run():
            run("./gradlew :nativesdk:fetchAgent", cwd="test")
            run("ln -fs $(pwd)/.../liboneagentsdk.so .../liboneagentsdk.so")

    The "already ran" marker (keyed by the bisect id) is committed only once this
    evaluation finishes with a real verdict — **not on abort** — so if the setup
    fails and aborts, the next run re-runs it. The setup's artifacts must survive
    `git checkout` between commits (be untracked / outside the work tree, e.g.
    build outputs or symlinks in ignored dirs).
    """
    global _first_run_pending
    if _first_run_marker().exists():
        return False
    _first_run_pending = True
    return True


def _first_run_marker() -> Path:
    return _logs_dir() / "first-run-done"


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
    _steps.append(step)
    _flush_status()  # keep status.md current after every step, so it's watchable live


# -------------------------------------------------------------------- replace
def replace(path: str, old: Union[str, "re.Pattern"], new: str, *,
            count: int = 0, when=None, if_missing: str = "skip") -> None:
    """sed-like in-file edit, auto-reverted before the process exits.

    `old` is a literal substring (str) or a regex (re.Pattern); type decides.
    `if_missing`: "skip" (default), "abort", or "ignore" when `old` isn't found.
    """
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
        subprocess.run(["git", "clean", "-fdxq"], cwd=top, capture_output=True)


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


def _refresh_status_md() -> None:
    try:
        import bisectlog
        rep = bisectlog.build_report(_toplevel(), logs_dir=str(_logs_dir()))
        if rep is None:
            return
        path = _status_md_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bisectlog.render_markdown(rep, details=True))
    except Exception:
        pass  # rendering is best-effort, never breaks the recipe


def _flush_status() -> None:
    """Persist the in-progress sidecar and re-render status.md.

    Called after every step so the current commit's `eval.json` and the rendered
    `status.md` reflect progress live (the commit shows as the in-flight `todo`
    row with its steps so far) — a file you can `bisectlog --watch` or tail while
    a long build/test runs, instead of only seeing it once the verdict lands.
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
    # commit the first-run marker only if this evaluation produced a real verdict
    # (not an abort) — a setup that aborted should re-run next time
    if _first_run_pending and _final.get("code") != ABORT:
        try:
            m = _first_run_marker()
            m.parent.mkdir(parents=True, exist_ok=True)
            m.write_text("done")
        except OSError:
            pass
    _write_sidecar()
    _refresh_status_md()


def _excepthook(exc_type, exc, tb):
    """An uncaught error in a recipe is a harness bug -> ABORT, never 'bad'."""
    import traceback
    traceback.print_exception(exc_type, exc, tb)
    _final["outcome"], _final["code"] = "abort", ABORT
    os._exit(ABORT)


sys.excepthook = _excepthook
