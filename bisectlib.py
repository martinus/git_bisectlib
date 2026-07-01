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
    good()/bad()/skip()/abort()               decide the commit directly from Python
                                              (e.g. after measuring with check())
    replace(path, old, new, ...)         sed-like edit, auto-reverted (clean tree)
    fixup(patch=/cherry_pick=, when=)    apply a patch/cherry-pick, auto-reverted
"""
from __future__ import annotations

import atexit
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

__version__ = "0.1.0"

__all__ = [
    "run", "test", "check",                 # the verbs
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
    status_md: Optional[str] = None     # default: <cache>/bisectlib/<id>.md
    logs: Optional[str] = None          # default: <cache>/bisectlib/<id>/
    clean: str = "reset"                # "reset" | "clean"
    color: Optional[bool] = None        # None=auto
    cwd: Optional[str] = None           # default working dir for commands (repo root)


_cfg = _Config()
_steps: list[dict] = []
_reverts: list[Callable[[], None]] = []
_final: dict = {"outcome": "good", "code": GOOD}
_finalized = False


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
    # Keep PWD in sync with the real cwd (subprocess chdir's but doesn't update
    # PWD; child scripts that read $PWD instead of getcwd() would see a stale dir).
    env = {**os.environ, "PWD": os.path.abspath(workdir)}
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
def _bisect_id() -> str:
    import hashlib
    try:
        top = _toplevel()
    except RuntimeError:
        top = os.getcwd()
    anchors = ""
    log = _git("bisect", "log", check=False)
    for line in log.splitlines():
        if line.startswith("git bisect ") and (" bad " in line or " good " in line
                                                or " start " in line):
            anchors += line + "\n"
    h = hashlib.sha1((top + "\n" + anchors).encode()).hexdigest()[:12]
    return h


def _cache_base() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(cache) / "bisectlib"


def _logs_dir() -> Path:
    if _cfg.logs:
        return Path(_cfg.logs)
    return _cache_base() / _bisect_id()


def _status_md_path() -> Path:
    if _cfg.status_md:
        return Path(_cfg.status_md)
    return _cache_base() / f"{_bisect_id()}.md"


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
    res = _exec(cmd, timeout, _commit_log_dir() / f"{len(_steps)+1:02d}-run.log", cwd)
    timed_out = res.code == -1
    ok = res.code == 0
    _record_step("run", cmd, res, ok)
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
    for i in range(warmup + attempts):
        res = _exec(cmd, timeout,
                    _commit_log_dir() / f"{len(_steps)+1:02d}-test-{i+1}.log", cwd)
        last = res
        if res.code == -1:  # timeout
            _record_step("test", cmd, res, False,
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
    _record_step("test", cmd, last, good, extra=extra,
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
    res = _exec(cmd, timeout, _commit_log_dir() / f"{len(_steps)+1:02d}-check.log", cwd)
    _record_step("check", cmd, res, res.ok)
    _echo_result("check", cmd, res.ok, res.seconds, "ok" if res.ok else "fail")
    return res


def _record_step(verb, cmd, res: Optional[Result], ok, extra=None, outcome=None):
    step = {"verb": verb, "cmd": cmd,
            "code": (res.code if res else None),
            "duration_s": round(res.seconds, 4) if res else None,
            "log": None}
    if outcome:
        step["outcome"] = outcome
    if extra:
        step.update(extra)
    _steps.append(step)


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
    _final.setdefault("fixups", []).append(
        {"kind": "replace", "path": path,
         "detail": f"{_short(old)}→{_short(new)}"})
    sys.stderr.write(f"  edit {path}: {n} replacement(s)\n")


def _short(s) -> str:
    s = s.pattern if isinstance(s, re.Pattern) else str(s)
    return (s[:20] + "…") if len(s) > 21 else s


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
        data = {"sha": sha(), "outcome": _final["outcome"],
                "exit_code": _final["code"], "steps": _steps}
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
    _write_sidecar()
    _refresh_status_md()


def _excepthook(exc_type, exc, tb):
    """An uncaught error in a recipe is a harness bug -> ABORT, never 'bad'."""
    import traceback
    traceback.print_exception(exc_type, exc, tb)
    _final["outcome"], _final["code"] = "abort", ABORT
    os._exit(ABORT)


sys.excepthook = _excepthook
