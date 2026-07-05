#!/usr/bin/env python3
"""Reconstruct a `git bisect` session and render it as the Markdown status page.

Internal to bisectlib: the engine calls :func:`build_report` + :func:`render_markdown`
after every step to (re)write ``.bisect/status.md`` — the live, watchable report.

Read-only and stateless: the entire report derives from only
  (1) `git bisect log`, and
  (2) per-commit information (git metadata + each commit's optional `eval.json`
      sidecar of recorded facts written by the engine).

No reflog, no /proc, no PID, no heuristic inference. If a fact wasn't logged or
recorded, it isn't shown.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATUS_ICON = {"good": "🟢", "bad": "🔴", "skip": "⏭️", "todo": "🕒", "abort": "🛑"}


# --------------------------------------------------------------------------- git
class GitError(RuntimeError):
    pass


def git(repo: str, *args: str, check: bool = True) -> str:
    """Run a git command in ``repo`` and return stripped stdout."""
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def git_ok(repo: str, *args: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True
        ).returncode
        == 0
    )


def is_ancestor(repo: str, a: str, b: str) -> bool:
    """True if commit ``a`` is an ancestor of ``b`` (or a == b)."""
    if a == b:
        return True
    return git_ok(repo, "merge-base", "--is-ancestor", a, b)


# ------------------------------------------------------------------- bisect terms
def bisect_terms(repo: str) -> tuple[str, str]:
    """Return (bad_term, good_term), honouring custom terms; default bad/good."""
    bad = git(repo, "bisect", "terms", "--term-bad", check=False) or "bad"
    good = git(repo, "bisect", "terms", "--term-good", check=False) or "good"
    return bad.strip() or "bad", good.strip() or "good"


def bisect_log(repo: str) -> str:
    """Raw `git bisect log` text, or '' if no bisect is in progress."""
    return git(repo, "bisect", "log", check=False)


# --------------------------------------------------------------------- data model
@dataclass
class Step:
    verb: str
    cmd: str
    code: Optional[int] = None
    outcome: Optional[str] = None
    duration_s: Optional[float] = None
    log: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class Sidecar:
    outcome: Optional[str] = None
    exit_code: Optional[int] = None
    duration_s: Optional[float] = None
    steps: list[Step] = field(default_factory=list)
    fixups: list[dict] = field(default_factory=list)
    pending: bool = True  # True until the recipe locked in a verdict (see engine)


@dataclass
class Row:
    bad: str          # input-range bad bound (sha) before this evaluation
    good: str         # input-range good bound (sha) before this evaluation
    midpoint: str     # the commit evaluated this step
    status: str       # good | bad | skip | todo
    n_commits: int = 0          # candidate commits still in range at this step
    span_seconds: int = 0       # wall span between good and bad commit dates
    good_date: str = ""
    bad_date: str = ""
    sidecar: Optional[Sidecar] = None
    goods: list = field(default_factory=list)  # ALL goods bounding the range here


@dataclass
class Report:
    repo: str
    bad_term: str
    good_term: str
    orig_bad: Optional[str]
    orig_goods: list[str]
    rows: list[Row]
    current_bad: Optional[str]
    current_good: Optional[str]
    first_bad: Optional[str]
    in_progress: bool
    head: Optional[str]
    subjects: dict[str, str] = field(default_factory=dict)
    dates: dict[str, str] = field(default_factory=dict)
    authors: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def short(self, sha: Optional[str]) -> str:
        return sha[:9] if sha else "—"

    def subject(self, sha: Optional[str]) -> str:
        return self.subjects.get(sha, "") if sha else ""

    def author(self, sha: Optional[str]) -> str:
        return self.authors.get(sha, "") if sha else ""

    def commit_meta(self, sha: Optional[str]) -> str:
        """`YYYY-MM-DD HH:MM, Author` for a commit cell — date then author, no subject."""
        if not sha:
            return ""
        iso = self.dates.get(sha, "")
        parts = [p for p in (fmt_date(iso) if iso else "", self.author(sha)) if p]
        return ", ".join(parts)


# --------------------------------------------------------------------- log parsing
def parse_log(log_text: str) -> list[tuple[str, list[str]]]:
    """Parse `git bisect log` into ordered ('verb', [revs/args]) operations."""
    ops: list[tuple[str, list[str]]] = []
    for line in log_text.splitlines():
        line = line.strip()
        if not line.startswith("git bisect "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        rest = parts[2:]  # drop "git", "bisect"
        if not rest:
            continue
        verb, args = rest[0], rest[1:]
        ops.append((verb, args))
    return ops


# ---------------------------------------------------------------- reconstruction
def build_report(
    repo: str,
    log_text: Optional[str] = None,
    logs_dir: Optional[str] = None,
) -> Optional[Report]:
    """Reconstruct the full bisect report from `git bisect log` + commit info."""
    repo = git(repo, "rev-parse", "--show-toplevel")
    if log_text is None:
        log_text = bisect_log(repo)
    if not log_text.strip():
        return None  # no bisect in progress / nothing to render

    bad_term, good_term = bisect_terms(repo)
    ops = parse_log(log_text)

    def resolve(rev: str) -> Optional[str]:
        out = git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}", check=False)
        return out or None

    orig_bad: Optional[str] = None
    orig_goods: list[str] = []
    current_bad: Optional[str] = None
    current_good: Optional[str] = None
    active_goods: list[str] = []   # every good marked so far (defines the range)
    rows: list[Row] = []
    seen_midpoints: set[str] = set()

    def ready() -> bool:
        return current_bad is not None and current_good is not None

    def _add_good(sha: str) -> None:
        if sha not in active_goods:
            active_goods.append(sha)

    def _anchor_good(sha: str) -> None:
        # An *anchor* good establishes the good bound. Establish it directly the
        # first time (do NOT gate on ancestry — a shallow clone or grafted history
        # can make `merge-base --is-ancestor` fail, which would otherwise leave the
        # range never "ready" and cause the first real evaluation to be swallowed
        # as another anchor). Additional anchor goods tighten via set_good.
        nonlocal current_good
        if current_good is None:
            current_good = sha
        else:
            set_good(sha)
        _add_good(sha)

    def set_good(sha: str) -> None:
        nonlocal current_good
        # Tighten the good bound to the newest good that is an ancestor of bad.
        if current_bad and not is_ancestor(repo, sha, current_bad):
            return
        if current_good is None or is_ancestor(repo, current_good, sha):
            current_good = sha

    def add_row(midpoint: str, status: str) -> None:
        rows.append(
            Row(
                bad=current_bad,
                good=current_good,
                midpoint=midpoint,
                status=status,
                goods=list(active_goods),
            )
        )
        seen_midpoints.add(midpoint)

    for verb, args in ops:
        revs = [a for a in args if not a.startswith("-")]
        if verb == "start":
            # `git bisect start [<bad> [<good>...]]`: first positional = bad, rest = good
            shas = [s for s in (resolve(r) for r in revs) if s]
            if shas:
                if current_bad is None:
                    current_bad = orig_bad = shas[0]
                for g in shas[1:]:
                    orig_goods.append(g)
                    _anchor_good(g)
            continue

        term = verb
        sha = resolve(revs[0]) if revs else None
        if sha is None:
            continue

        if term == bad_term:
            if not ready():  # anchor
                current_bad = orig_bad = current_bad or sha
            else:            # evaluation
                add_row(sha, "bad")
                current_bad = sha
        elif term == good_term:
            if not ready():  # anchor
                orig_goods.append(sha)
                _anchor_good(sha)
            else:            # evaluation
                add_row(sha, "good")
                # Trust git: an evaluation `good` is the new good bound (git only
                # marks commits it picked inside the range), exactly mirroring how
                # a `bad` sets current_bad. Do NOT gate this on ancestry — in a
                # DAG the newly-good commit need not be a descendant of the prior
                # good, and on shallow clones the check can't be verified, either
                # of which would otherwise freeze the good bound and the range.
                current_good = sha
                _add_good(sha)   # excluded from the range for subsequent rows
        elif term == "skip":
            if ready():
                add_row(sha, "skip")
            # a skip before ready is unusual; ignore for bounds

    head = git(repo, "rev-parse", "HEAD", check=False) or None

    # Determine the first-bad answer / progress. The candidate set is commits
    # reachable from bad but from NONE of the goods (git excludes ancestors of
    # every good, not just the latest — crucial in a DAG where goods diverge).
    def _range_count(bad: str, goods: list) -> int:
        goods = [g for g in goods if g]
        if not goods:
            return 0
        return int(git(repo, "rev-list", "--count", bad, "--not", *goods) or 0)

    first_bad: Optional[str] = None
    n_remaining = None
    if ready():
        n_remaining = _range_count(current_bad, active_goods)
        if n_remaining <= 1:
            first_bad = current_bad

    # In-flight row: HEAD is the midpoint git currently has checked out, awaiting a
    # verdict, and is not yet a logged marking.
    in_progress = False
    if (
        ready()
        and first_bad is None
        and head
        and head not in seen_midpoints
        and head != current_good
    ):
        within_range = (
            is_ancestor(repo, current_good, head)
            and is_ancestor(repo, head, current_bad)
        )
        # A per-commit sidecar written by the engine is proof HEAD is the commit
        # being evaluated right now — trust it when the ancestry checks can't
        # confirm the range (shallow/grafted clone, or an anchor good that isn't a
        # topological ancestor of the midpoint). This keeps status.md live (the
        # in-flight row and its steps refresh after every command) on such repos.
        has_sidecar = bool(logs_dir) and (Path(logs_dir) / head / "eval.json").is_file()
        if within_range or has_sidecar:
            add_row(head, "todo")
            in_progress = True

    # Gather subjects + dates for every sha we reference.
    shas = set()
    for r in rows:
        shas.update([r.bad, r.good, r.midpoint])
    shas.update([orig_bad, current_bad, current_good, *orig_goods])
    shas.discard(None)
    subjects, dates, authors = _commit_meta(repo, shas)

    # Fill range metrics + sidecars per row.
    for r in rows:
        if r.bad and r.goods:
            r.n_commits = _range_count(r.bad, r.goods)
        elif r.good and r.bad:  # fallback (shouldn't happen once ready)
            r.n_commits = int(git(repo, "rev-list", "--count", f"{r.good}..{r.bad}") or 0)
        if r.good and r.bad:
            r.good_date = dates.get(r.good, "")
            r.bad_date = dates.get(r.bad, "")
            r.span_seconds = _date_delta_seconds(r.good_date, r.bad_date)
        if logs_dir:
            r.sidecar = _load_sidecar(logs_dir, r.midpoint)

    # The in-flight commit (HEAD) is a `todo` row because git hasn't recorded its
    # mark yet — but if the recipe already finished and its sidecar carries a
    # locked-in verdict, show that instead so the saved status.md reflects the
    # completed evaluation rather than a perpetual `todo`.
    if in_progress and rows and rows[-1].status == "todo":
        sc = rows[-1].sidecar
        if sc and not sc.pending and sc.outcome in ("good", "bad", "skip", "abort"):
            rows[-1].status = sc.outcome

    note = ""
    if ready() and first_bad is None and not in_progress and n_remaining and n_remaining > 1:
        # No current checkout and range unresolved -> likely only skips remain.
        skips = [r for r in rows if r.status == "skip"]
        if skips:
            note = (
                "Bisect stalled: only skipped commits left to test in the current "
                "range — git cannot name a single first-bad commit."
            )
        else:
            note = "Bisect not finished (no commit currently checked out)."

    return Report(
        repo=repo,
        bad_term=bad_term,
        good_term=good_term,
        orig_bad=orig_bad,
        orig_goods=orig_goods,
        rows=rows,
        current_bad=current_bad,
        current_good=current_good,
        first_bad=first_bad,
        in_progress=in_progress,
        head=head,
        subjects=subjects,
        dates=dates,
        authors=authors,
        note=note,
    )


# ------------------------------------------------------------- commit metadata
def _commit_meta(repo: str, shas) -> tuple[dict, dict, dict]:
    """Subject, ISO date and author for each sha, in a single `git show` apiece.

    Replaces three separate per-sha calls (subject/date/author) with one — a
    third of the git spawns per render, and the report re-renders after every
    step. `%s` is always a single line, so splitting the output is unambiguous.
    """
    subjects: dict[str, str] = {}
    dates: dict[str, str] = {}
    authors: dict[str, str] = {}
    for sha in shas:
        if not sha:
            continue
        out = git(repo, "show", "-s", "--format=%s%n%cI%n%an", sha, check=False)
        parts = out.split("\n", 2)
        subjects[sha] = parts[0] if len(parts) > 0 else ""
        dates[sha] = parts[1] if len(parts) > 1 else ""
        authors[sha] = parts[2] if len(parts) > 2 else ""
    return subjects, dates, authors


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``.

    Recent git emits UTC as ``…T12:00:00Z`` for ``%cI``; Python 3.10's
    ``datetime.fromisoformat`` rejects the ``Z`` suffix (3.11+ accepts it), so
    normalise it to an explicit ``+00:00`` offset first.
    """
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _date_delta_seconds(a: str, b: str) -> int:
    try:
        return int(abs((_parse_iso(b) - _parse_iso(a)).total_seconds()))
    except (ValueError, TypeError):
        return 0


def fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def short_seconds(seconds) -> str:
    """Compact runtime: '0.5s', '12.4s', '45s', '1m03s', '2h05m'.

    Used for hammer's total wall time. Sub-minute runs keep one decimal (a short
    soak reads '0.5s', not a rounded '1s'), dropping a trailing '.0'; from a
    minute up it switches to m/s then h/m where the fraction stops mattering.
    """
    try:
        sec = float(seconds)
    except (ValueError, TypeError):
        return "?"
    if sec < 60:
        return f"{sec:.1f}".rstrip("0").rstrip(".") + "s"
    s = int(round(sec))
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def fmt_date(iso: str) -> str:
    try:
        return _parse_iso(iso).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso or "—"


# ------------------------------------------------------------------- sidecars
def _load_sidecar(logs_dir: str, sha: str) -> Optional[Sidecar]:
    path = Path(logs_dir) / sha / "eval.json"
    if not path.is_file():
        # also try short-sha dirs
        candidates = list(Path(logs_dir).glob(f"{sha[:12]}*/eval.json"))
        if not candidates:
            return None
        path = candidates[0]
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    steps = [
        Step(
            verb=s.get("verb", ""),
            cmd=s.get("cmd", ""),
            code=s.get("code"),
            outcome=s.get("outcome"),
            duration_s=s.get("duration_s"),
            log=s.get("log"),
            extra={
                k: v
                for k, v in s.items()
                if k not in {"verb", "cmd", "code", "outcome", "duration_s", "log"}
            },
        )
        for s in data.get("steps", [])
    ]
    return Sidecar(
        outcome=data.get("outcome"),
        exit_code=data.get("exit_code"),
        duration_s=data.get("duration_s"),
        steps=steps,
        fixups=data.get("fixups", []),
        pending=data.get("pending", True),
    )


def _step_summary(sc: Optional[Sidecar]) -> str:
    """Inline recorded detail for a row's status cell, e.g. '2/5 · 1.8s'.

    While a command is executing, its step carries no exit code yet; show what is
    running right now so the top-level table names the in-flight command at a
    glance (the Details section links its live log)."""
    if not sc:
        return ""
    for s in sc.steps:
        if s.code is None:
            cmd = s.cmd if len(s.cmd) <= 60 else s.cmd[:59] + "…"
            return f"⏳ running `{cmd}`"
    bits = []
    for s in sc.steps:
        if s.verb == "hammer":
            # total runs, parallel threads used, and total runtime
            seg = [f"{s.extra.get('executed', 0)} runs"]
            par = s.extra.get("parallel")
            if par:
                seg.append(f"{par}× parallel")
            el = s.extra.get("elapsed_s")
            if el is not None:
                seg.append(short_seconds(el))
            bits.append(" · ".join(seg))
            d = s.extra.get("durations_s")
            if d:
                bits.append(f"min {min(d):.3g}s")
        elif s.verb == "test":
            executed = s.extra.get("executed")
            if executed and executed > 1:
                bits.append(f"{s.extra.get('passes', 0)}/{executed}")
                d = s.extra.get("durations_s")
                if d:
                    bits.append(f"min {min(d):.3g}s")
    if not bits and sc.duration_s is not None:
        bits.append(f"{sc.duration_s:.3g}s")
    return " · ".join(bits)


# ------------------------------------------------------------------ resume line
def resume_command(rep: Report) -> Optional[str]:
    if rep.current_bad and rep.current_good:
        return (
            f"git bisect start {rep.short(rep.current_bad)} "
            f"{rep.short(rep.current_good)}"
        )
    return None


# -------------------------------------------------------------------- Markdown
def render_markdown(rep: Report, details: bool = False, color: bool = True) -> str:
    def icon(status: str) -> str:
        return (STATUS_ICON.get(status, "") + " " if color else "") + status

    def cell(sha: str) -> str:
        meta = rep.commit_meta(sha).replace("|", "\\|")
        return f"`{rep.short(sha)}` {meta}" if meta else f"`{rep.short(sha)}`"

    lines: list[str] = []
    title = "Bisect report"
    lines.append(f"# {title}")
    lines.append("")
    ob, og = rep.orig_bad, (rep.orig_goods[0] if rep.orig_goods else None)
    lines.append(
        f"**original range:** {rep.good_term} `{rep.short(og)}` · "
        f"{rep.bad_term} `{rep.short(ob)}`"
    )
    resume = resume_command(rep)
    if resume and not rep.first_bad:
        lines.append(f"**resume:** `{resume}`")
    if rep.first_bad:
        lines.append("")
        lines.append(
            f"## 🎯 First bad commit: `{rep.short(rep.first_bad)}` "
            f"— {rep.subject(rep.first_bad)}"
        )
        # Show the full commit (header, message, diffstat) the way `git bisect`
        # reports it when it lands on the first bad commit. One `git show --stat`
        # yields the commit metadata, message, and per-file stat without the diff.
        full = git(rep.repo, "show", "--stat", "--format=medium",
                   rep.first_bad, check=False)
        if full:
            lines.append("")
            lines.append("```")
            lines.append(full)
            lines.append("```")
    if rep.note:
        lines.append("")
        lines.append(f"> ⚠️ {rep.note}")
    lines.append("")

    lines.append("| good | bad | midpoint | range | status |")
    lines.append("|------|-----|----------|-------|--------|")
    for r in rep.rows:
        rng = f"{fmt_duration(r.span_seconds)} · {r.n_commits} commits"
        status = icon(r.status)
        extra = _step_summary(r.sidecar)
        if extra:
            status += f" · {extra}"
        lines.append(
            f"| {cell(r.good)} | {cell(r.bad)} | {cell(r.midpoint)} | {rng} | {status} |"
        )
    lines.append("")

    if details:
        detail_rows = [r for r in rep.rows if r.sidecar]
        if detail_rows:
            lines.append("## Details")
            lines.append("")
            for r in detail_rows:
                lines.append(
                    f"### `{rep.short(r.midpoint)}` — {rep.subject(r.midpoint)} "
                    f"({icon(r.status)})"
                )
                if r.sidecar.fixups:
                    fx = ", ".join(
                        f"{f.get('kind')}: `{f.get('detail', f.get('path',''))}`"
                        for f in r.sidecar.fixups
                    )
                    lines.append(f"- fixups: {fx}")
                for s in r.sidecar.steps:
                    if s.verb != "hammer":
                        continue
                    el = s.extra.get("elapsed_s")
                    rt = short_seconds(el) if el is not None else "?"
                    lines.append(
                        f"- hammer: **{s.extra.get('executed', 0)} runs** · "
                        f"**{s.extra.get('parallel', '?')}× parallel** · "
                        f"**{rt}** total · "
                        f"{s.extra.get('passes', 0)} passed, "
                        f"{s.extra.get('failures', 0)} failed"
                    )
                lines.append("")
                lines.append("| step | cmd | exit | time |")
                lines.append("|------|-----|------|------|")
                for s in r.sidecar.steps:
                    # a step with no exit code yet is running right now
                    running = s.code is None
                    dur = ("running…" if running else
                           (f"{s.duration_s:.3g}s" if s.duration_s is not None else ""))
                    code = "⏳" if running else str(s.code)
                    # link the step to its captured log file (relative to status.md,
                    # which sits alongside the per-commit <sha>/ log dirs); the log
                    # streams live, so the link is watchable while the step runs
                    step = f"[{s.verb}]({r.midpoint}/{s.log})" if s.log else s.verb
                    lines.append(
                        f"| {step} | `{s.cmd}` | {code} | {dur} |"
                    )
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"
