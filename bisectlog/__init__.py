#!/usr/bin/env python3
"""bisectlog - render a `git bisect` session as Markdown or HTML.

Read-only and stateless: the entire report derives from only
  (1) `git bisect log`, and
  (2) per-commit information (git metadata + each commit's optional `eval.json`
      sidecar of recorded facts written by the bisectlib engine).

No reflog, no /proc, no PID, no heuristic inference. If a fact wasn't logged or
recorded, it isn't shown.

Usage:
    bisectlog [--format md|html] [-o FILE] [--open] [--watch[=SECS]]
              [-C DIR] [--log FILE] [--logs DIR] [--details] [--no-color]

Also importable:
    from bisectlog import build_report, render_markdown, render_html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shlex
import subprocess
import sys
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__version__ = "0.11.0"

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
    subjects = _commit_subjects(repo, shas)
    dates = _commit_dates(repo, shas)
    authors = _commit_authors(repo, shas)

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
def _commit_subjects(repo: str, shas) -> dict[str, str]:
    out: dict[str, str] = {}
    for sha in shas:
        if not sha:
            continue
        s = git(repo, "show", "-s", "--format=%s", sha, check=False)
        out[sha] = s
    return out


def _commit_dates(repo: str, shas) -> dict[str, str]:
    out: dict[str, str] = {}
    for sha in shas:
        if not sha:
            continue
        out[sha] = git(repo, "show", "-s", "--format=%cI", sha, check=False)
    return out


def _commit_authors(repo: str, shas) -> dict[str, str]:
    out: dict[str, str] = {}
    for sha in shas:
        if not sha:
            continue
        out[sha] = git(repo, "show", "-s", "--format=%an", sha, check=False)
    return out


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
        if s.verb != "test":
            continue
        executed = s.extra.get("executed")
        if executed and executed > 1:
            bits.append(f"{s.extra.get('passes', 0)}/{executed}")
            d = s.extra.get("durations_s")
            if d:
                bits.append(f"min {min(d):.3g}s")
    if not bits and sc.duration_s is not None:
        bits.append(f"{sc.duration_s:.3g}s")
    return " · ".join(bits)


# ---------------------------------------------------------------- forge links
def commit_url(repo: str) -> Optional[str]:
    """Best-effort base URL for /commit/<sha> links from origin remote."""
    url = git(repo, "remote", "get-url", "origin", check=False)
    if not url:
        return None
    url = url.strip()
    if url.startswith("git@"):
        # git@github.com:owner/repo.git -> https://github.com/owner/repo
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    if url.startswith("ssh://"):
        url = "https://" + url[len("ssh://") :].split("@")[-1]
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("http"):
        return url + "/commit/"
    return None


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


# ------------------------------------------------------------------------ HTML
_CSS = """
:root{--good:#1a7f37;--bad:#cf222e;--skip:#9a6700;--todo:#0969da;--ink:#1f2328;
--muted:#656d76;--line:#d0d7de;--panel:#f6f8fa}
*{box-sizing:border-box}
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
color:var(--ink);margin:0;padding:2rem;max-width:1100px;margin:auto}
h1{font-size:1.5rem;margin:0 0 .25rem}
.meta{color:var(--muted);margin:.15rem 0}
code{background:var(--panel);padding:.1em .35em;border-radius:5px;font-size:.9em}
.resume{display:flex;gap:.5rem;align-items:center;margin:.6rem 0}
.resume code{font-size:.95em}
button.copy{border:1px solid var(--line);background:#fff;border-radius:6px;
padding:.2rem .5rem;cursor:pointer;font-size:.8rem}
table{border-collapse:collapse;width:100%;margin:1rem 0}
th,td{border:1px solid var(--line);padding:.5rem .6rem;text-align:left;vertical-align:top}
th{background:var(--panel);font-weight:600}
.sha{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
.subj{color:var(--muted);font-size:.85rem}
.badge{display:inline-block;padding:.1rem .5rem;border-radius:999px;font-size:.8rem;
font-weight:600;color:#fff}
.badge.good{background:var(--good)}.badge.bad{background:var(--bad)}
.badge.skip{background:var(--skip)}.badge.todo{background:var(--todo)}
.firstbad{border:2px solid var(--bad);border-radius:10px;padding:1rem;margin:1rem 0;
background:#fff5f5}
.firstbad h2{margin:.1rem 0;color:var(--bad)}
.note{border-left:4px solid var(--skip);background:#fffbe6;padding:.6rem 1rem;
border-radius:6px;margin:1rem 0}
.dots span{font-size:1rem}.dots .p{color:var(--good)}.dots .f{color:var(--bad)}
details{margin:.3rem 0}summary{cursor:pointer}
.steps{margin:.4rem 0 .8rem;width:auto}
.steps td,.steps th{padding:.25rem .5rem;font-size:.85rem}
.muted{color:var(--muted)}
"""

_COPY_JS = """
function copyCmd(btn){const t=btn.previousElementSibling.innerText;
navigator.clipboard.writeText(t).then(()=>{btn.innerText='copied';
setTimeout(()=>btn.innerText='copy',1200)})}
"""


def _h(s: str) -> str:
    return html.escape(s or "")


def render_html(
    rep: Report,
    details: bool = True,
    watch: Optional[int] = None,
    base_url: Optional[str] = None,
) -> str:
    def sha_html(sha: str) -> str:
        if not sha:
            return "—"
        short = _h(rep.short(sha))
        if base_url:
            short = f'<a href="{base_url}{_h(sha)}">{short}</a>'
        meta = _h(rep.commit_meta(sha))
        return f'<span class="sha">{short}</span>' + (
            f'<div class="subj">{meta}</div>' if meta else ""
        )

    head_extra = (
        f'<meta http-equiv="refresh" content="{watch}">' if watch else ""
    )
    out: list[str] = []
    out.append("<!doctype html><html><head><meta charset='utf-8'>")
    out.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    out.append(f"<title>bisectlog report</title>{head_extra}")
    out.append(f"<style>{_CSS}</style><script>{_COPY_JS}</script></head><body>")

    out.append("<h1>Bisect report</h1>")
    ob = rep.short(rep.orig_bad)
    og = rep.short(rep.orig_goods[0]) if rep.orig_goods else "—"
    out.append(
        f"<div class='meta'><b>original range:</b> {rep.good_term} "
        f"<code>{_h(og)}</code> · {rep.bad_term} <code>{_h(ob)}</code></div>"
    )
    resume = resume_command(rep)
    if resume and not rep.first_bad:
        out.append(
            f"<div class='resume'><code>{_h(resume)}</code>"
            "<button class='copy' onclick='copyCmd(this)'>copy</button></div>"
        )

    if rep.first_bad:
        full = git(rep.repo, "show", "--stat", "--format=medium",
                   rep.first_bad, check=False)
        out.append("<div class='firstbad'>")
        out.append(f"<h2>🎯 First bad commit: <span class='sha'>{_h(rep.short(rep.first_bad))}</span></h2>")
        out.append(f"<pre>{_h(full)}</pre></div>")

    if rep.note:
        out.append(f"<div class='note'>⚠️ {_h(rep.note)}</div>")

    out.append("<table><thead><tr>"
               "<th>good</th><th>bad</th><th>midpoint</th>"
               "<th>range</th><th>status</th></tr></thead><tbody>")
    for r in rep.rows:
        rng = (
            f"<span class='muted'>{fmt_duration(r.span_seconds)} · {r.n_commits} commits</span>"
        )
        badge = f"<span class='badge {r.status}'>{STATUS_ICON.get(r.status,'')} {r.status}</span>"
        extra = _step_summary(r.sidecar)
        status_cell = badge + (f" <span class='muted'>{_h(extra)}</span>" if extra else "")
        if details and r.sidecar:
            status_cell += _render_detail_html(r)
        out.append(
            f"<tr><td>{sha_html(r.good)}</td><td>{sha_html(r.bad)}</td>"
            f"<td>{sha_html(r.midpoint)}</td><td>{rng}</td><td>{status_cell}</td></tr>"
        )
    out.append("</tbody></table>")

    out.append(f"<div class='meta muted'>generated by bisectlog {__version__}</div>")
    out.append("</body></html>")
    return "\n".join(out)


def _render_detail_html(r: Row) -> str:
    sc = r.sidecar
    parts = ["<details><summary>detail</summary>"]
    # flaky dots
    for s in sc.steps:
        if s.verb == "test" and "executed" in s.extra and "passes" in s.extra:
            executed = s.extra.get("executed", 0)
            passes = s.extra.get("passes", 0)
            if executed > 1:
                dots = "".join("<span class='p'>●</span>" for _ in range(passes)) + \
                       "".join("<span class='f'>○</span>" for _ in range(executed - passes))
                d = s.extra.get("durations_s")
                tinfo = f" · min {min(d):.3g}s" if d else ""
                parts.append(f"<div class='dots'>{dots} {passes}/{executed}{tinfo}</div>")
    parts.append("<table class='steps'><thead><tr><th>step</th><th>cmd</th>"
                 "<th>exit</th><th>time</th></tr></thead><tbody>")
    for s in sc.steps:
        running = s.code is None  # no exit code yet -> running right now
        dur = ("running…" if running else
               (f"{s.duration_s:.3g}s" if s.duration_s is not None else ""))
        code = "⏳" if running else str(s.code)
        step = (f'<a href="{_h(r.midpoint)}/{_h(s.log)}">{_h(s.verb)}</a>'
                if s.log else _h(s.verb))
        parts.append(
            f"<tr><td>{step}</td><td><code>{_h(s.cmd)}</code></td>"
            f"<td>{code}</td><td>{dur}</td></tr>"
        )
    parts.append("</tbody></table>")
    if sc.fixups:
        fx = ", ".join(
            f"{_h(f.get('kind'))}: <code>{_h(f.get('detail', f.get('path', '')))}</code>"
            for f in sc.fixups
        )
        parts.append(f"<div class='muted'>fixups: {fx}</div>")
    parts.append("</details>")
    return "".join(parts)


# --------------------------------------------------------------- terminal table
_ANSI = {"good": "32", "bad": "31", "skip": "33", "todo": "34", "abort": "91",
         "sha": "36", "dim": "2", "bold": "1"}
_MARK = {"good": "✓", "bad": "✗", "skip": "⊘", "todo": "…", "abort": "■"}


_SHA, _NW = 9, 5  # short-sha width, commit-count column width


def render_terminal(rep: Report, color: bool = True, width: Optional[int] = None) -> str:
    """A compact, aligned, colored one-line-per-evaluation table for the terminal.

    Each row mirrors the report model on one line:
        <status> <bad> <good> <midpoint> <commits> <subject>
    where bad/good are the input-range bounds before that evaluation.
    """
    def c(s: str, key: str) -> str:
        return f"\033[{_ANSI[key]}m{s}\033[0m" if color and key in _ANSI else s

    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns

    out: list[str] = []
    og = rep.short(rep.orig_goods[0]) if rep.orig_goods else "—"
    ob = rep.short(rep.orig_bad)
    head = (f"bisect  {c(rep.good_term, 'good')} {c(og, 'sha')}  "
            f"{c(rep.bad_term, 'bad')} {c(ob, 'sha')}")
    out.append(c(head, "bold") if color else head)
    if rep.first_bad:
        out.append(c(f"🎯 first bad commit  {rep.short(rep.first_bad)}  "
                     f"{rep.subject(rep.first_bad)}", "bad"))
    elif (resume := resume_command(rep)):
        out.append(c(f"resume: {resume}", "dim"))
    if rep.note:
        out.append(c(f"! {rep.note}", "skip"))
    out.append("")

    sw = max([len(r.status) for r in rep.rows] + [4])
    # prefix = mark(1)+sp + status+sp + 3*(sha+sp) + commits + 2sp
    prefix = 2 + sw + 1 + (_SHA + 1) * 3 + _NW + 2
    subj_w = max(12, width - prefix)

    # column header (dim)
    hdr = (f"  {'':<{sw}} {'good':<{_SHA}} {'bad':<{_SHA}} {'midpoint':<{_SHA}} "
           f"{'cmts':>{_NW}}  subject")
    out.append(c(hdr, "dim"))

    for r in rep.rows:
        mark = c(_MARK.get(r.status, " "), r.status)
        status = c(f"{r.status:<{sw}}", r.status)
        good = c(f"{rep.short(r.good):<{_SHA}}", "good")
        bad = c(f"{rep.short(r.bad):<{_SHA}}", "bad")
        mid = c(f"{rep.short(r.midpoint):<{_SHA}}", "sha")
        n = c(f"{r.n_commits:>{_NW}}", "dim")
        subj = rep.subject(r.midpoint)
        if len(subj) > subj_w:
            subj = subj[:subj_w - 1] + "…"
        out.append(f"{mark} {status} {good} {bad} {mid} {n}  {subj}")
    return "\n".join(out) + "\n"


# -------------------------------------------------------------------------- CLI
def _default_logs_dir(repo: str) -> Optional[str]:
    """Best guess at the bisectlib per-commit log dir, if it exists."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    base = Path(cache) / "bisectlib"
    if base.is_dir():
        # newest sub-dir wins; bisectlib keys it by <bisect-id>
        subs = sorted(base.glob("*/"), key=lambda p: p.stat().st_mtime, reverse=True)
        if subs:
            return str(subs[0])
    return None


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="bisectlog",
        description="Render a git bisect session as Markdown or HTML.",
    )
    p.add_argument("--format", choices=["term", "md", "html"], default=None,
                   help="output format (default: term when writing to a terminal)")
    p.add_argument("-o", "--output", help="write to FILE (extension implies format)")
    p.add_argument("--open", action="store_true", help="render HTML and open in browser")
    p.add_argument("--watch", nargs="?", const=2, type=int, metavar="SECS",
                   help="re-render on bisect-log change (HTML auto-refreshes)")
    p.add_argument("-C", dest="repo", default=".", help="operate on repo/worktree DIR")
    p.add_argument("--log", help="render from a saved `git bisect log` dump")
    p.add_argument("--logs", help="per-commit sidecar/log dir (for recorded detail)")
    p.add_argument("--details", action="store_true", help="include per-commit detail")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors / emoji")
    p.add_argument("--version", action="version", version=f"bisectlog {__version__}")
    args = p.parse_args(argv)

    # resolve format: explicit > implied by -o extension > html for --open >
    # term (bare invocation) unless stdout is redirected, then md
    fmt = args.format
    if fmt is None and args.output:
        fmt = "html" if args.output.endswith((".html", ".htm")) else "md"
    if args.open:
        fmt = "html"
    if fmt is None:
        fmt = "term" if sys.stdout.isatty() else "md"

    color = not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ

    log_text = None
    if args.log:
        log_text = Path(args.log).read_text()

    logs_dir = args.logs or _default_logs_dir(
        git(args.repo, "rev-parse", "--show-toplevel", check=False) or args.repo
    )

    def build() -> Optional[Report]:
        return build_report(args.repo, log_text=log_text, logs_dir=logs_dir)

    def render(rep: Report) -> str:
        if fmt == "html":
            base = commit_url(rep.repo)
            return render_html(rep, details=args.details or True,
                               watch=args.watch, base_url=base)
        if fmt == "term":
            return render_terminal(rep, color=color)
        return render_markdown(rep, details=args.details, color=not args.no_color)

    def emit(text: str) -> None:
        if args.output:
            Path(args.output).write_text(text)
        elif not args.open:
            sys.stdout.write(text)

    rep = build()
    if rep is None:
        sys.stderr.write("bisectlog: no bisect in progress in this repo.\n")
        return 1

    text = render(rep)

    if args.open:
        import tempfile
        import webbrowser
        path = args.output or os.path.join(
            tempfile.gettempdir(), "bisectlog-report.html"
        )
        Path(path).write_text(text)
        webbrowser.open(f"file://{os.path.abspath(path)}")
        sys.stderr.write(f"bisectlog: wrote {path}\n")
        return 0

    if args.watch:
        import time
        last = None
        try:
            while True:
                cur = bisect_log(args.repo) if not args.log else log_text
                if cur != last:
                    rep = build()
                    if rep:
                        emit(render(rep))
                    last = cur
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    else:
        emit(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
