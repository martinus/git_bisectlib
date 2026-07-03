# bisectlib

**A Python toolkit for automated `git bisect`.** Write a tiny recipe — a normal
Python script — that builds your project, runs your (possibly flaky) tests, and
reports a verdict; then let `git bisect run` drive it to the exact commit that
introduced a regression. `bisectlib` handles the fiddly parts that make
hand-rolled bisect scripts painful:

- **Builds vs. results.** A broken build is *infrastructure*, not a verdict —
  `run()` **aborts** the bisect so you can fix the recipe and resume, instead of
  silently skipping commits and mis-bisecting. `test()` is the actual verdict.
- **Flaky tests.** `test("…", attempts=5, min_passes=2)` — pass 2 of up to 5
  tries (it stops as soon as the verdict is decided).
- **Benchmarks.** Give `test()` a time-aware predicate: `passed=lambda r: r.seconds < 6.7`.
  Combined with the quorum it expresses any aggregate — e.g. "min of 5 runs < 6.7s".
- **Per-range build fixes.** `fixup(patch=…)` / `replace(...)` apply a patch or a
  sed-like edit for the commits that need it, then **auto-revert** so the tree
  stays clean for the next checkout.
- **A clear report.** Every run records what happened; the companion
  [`bisectlog`](#bisectlog-the-report-renderer) tool renders the whole session as
  Markdown or HTML.

It is **pure standard library** — no dependencies, just `git` on your `PATH`.

See [`SPEC.md`](SPEC.md) for the full design rationale.

## Install

```sh
pip install git+https://github.com/martinus/bisectlib
```

That single command gives you `import bisectlib` for recipes plus the
`bisectlog` / `git-bisectlog` report CLIs. It's pure standard library — no
runtime dependencies — and ships type information (`py.typed`), so editors and
type-checkers resolve `run`, `test`, … without warnings. See
[Install / how a recipe finds `bisectlib`](#install--how-a-recipe-finds-bisectlib)
for a zero-install alternative.

**Updating a git install:** re-running the command above is a no-op if the
version number hasn't changed — pip sees it already installed and skips the
rebuild. To force a refresh from the latest `main`:

```sh
pip install --force-reinstall git+https://github.com/martinus/bisectlib
```

## A recipe in 4 lines

```python
# recipe.py
from bisectlib import run, test

run("cmake -B build")                 # infra: a broken configure ABORTS (exit 128)
run("cmake --build build -j")         # infra: a broken build ABORTS
test("ctest --test-dir build -R foo", attempts=5, min_passes=2)   # 2 of up to 5 => good
# reaching the end == GOOD
```

```sh
git bisect start <BAD> <GOOD>
git bisect run python recipe.py
```

A passing step continues to the next line; a failing `test()` is **bad**, a
broken `run()` **aborts** (or **skips** with `run(..., skip_on_error=True)`).
Falling off the end is **good**. That is the whole mental model.

Because passing steps continue, you can use **several `test()` calls** and they
combine with logical AND — any one failing is **bad**, all passing is **good**:

```python
run("make")
test("./unit_tests")                  # both must pass for the
test("./integration_tests --quick")   # commit to count as good
test("./bench", attempts=5, min_passes=1,           # …and the fastest of 5
     passed=lambda r: r.seconds < 6.7)              # runs is under 6.7s
```

## The API

| Verb | Meaning | On failure |
|------|---------|------------|
| `run(cmd, skip_on_error=False)` | infrastructure (configure/build/setup) | **abort** (or skip) |
| `test(cmd, attempts=1, min_passes=None, passed=None, warmup=0, bad_when="fail")` | the verdict | **bad** |
| `check(cmd) -> Result` | run once, **never exits** (introspection: `.ok`, `.out`, `.seconds`) | — |
| `good()` / `bad()` / `skip()` / `abort()` | decide the commit **directly from Python** | — |

The verdict primitives let you decide from arbitrary Python after measuring something with
`check()` — no need to shell back out to `test` just to compare values:

```python
size = int(check("stat -c%s build/app").out)
if size > 5 * 1024 * 1024:
    bad("binary too big")     # exit 1; reaching the end instead would be good
```

All three verbs accept **`cwd=`** to set the working directory for the command (relative
paths resolve against the repo root, so `cwd="build"` means `<repo>/build`; absolute paths
are honoured). Set a default for every command with `configure(cwd="build")`. Commands
otherwise run at the repo root.

**Flaky & benchmark tests.** `attempts` is the *max* tries, `min_passes` how many must
pass (default: all); evaluation stops as soon as the verdict is decided. `passed` is a
predicate over the `Result` (`.ok`, `.seconds`, …) deciding if one attempt passed —
default `lambda r: r.ok`. Because it sees `.seconds`, timing thresholds are just
predicates plus the quorum:

```python
test("./bench", attempts=5, min_passes=1, passed=lambda r: r.seconds < 6.7)  # min < 6.7s
test("./bench", attempts=5,               passed=lambda r: r.seconds < 6.7)  # all 5 < 6.7s
test("./bench", attempts=5, min_passes=3, passed=lambda r: r.seconds < 6.7)  # median < 6.7s
```
(`min(times)<T` → `min_passes=1`; `max(times)<T` → all; `median<T` → majority.)

```python
from bisectlib import (run, test, check, once,
                       good, bad, skip, abort, replace, fixup, in_range)
```

- **`once(key="setup")`** — guard one-time, commit-independent setup so it doesn't
  repeat on every commit:

  ```python
  if once():                       # default key, for a single setup block
      run("./fetch-deps")          # fetch a dependency, create a symlink, …
      run("ln -fs $(pwd)/… …")

  if once("fetch-agent"):          # independent keys → independent markers
      run("./gradlew :nativesdk:fetchAgent")
  ```

  Returns True the first time each `key` is seen in the bisect, False after. A
  key's "already ran" marker (scoped to the bisect id) is committed only once an
  evaluation that armed it finishes with a real verdict — *not* on abort. Keys
  committed by an earlier evaluation stay done; every key armed in an evaluation
  that then aborts re-runs next time (keep each block idempotent). Its artifacts
  must survive `git checkout` (untracked / outside the tree). Use it for what's
  the same on every commit; use `run` for what must be rebuilt per commit.
- **`replace(path, old, new)`** — sed-like edit, auto-reverted. `old` is a literal
  `str` or a compiled `re.Pattern` (the *type* decides; no `regex=` flag).
- **`fixup(patch=… | cherry_pick=…, when=…)`** — context manager that applies a
  patch/cherry-pick for its block, then reverts.
- **`in_range("v1.0..v2.0")`, `touches("src/x.c")`** — predicates for `when=`.

### Exit-code contract

`bisectlib` maps outcomes to the exit codes `git bisect run` understands:

| Outcome | Exit | Meaning |
|---------|------|---------|
| good | `0` | bug absent |
| bad | `1` | bug present |
| skip | `125` | commit untestable |
| abort | `128` | harness broken — bisect state preserved, fix the recipe and re-run |

An uncaught exception in a recipe **aborts** (128) — never misread as "bad".

### Abort → fix the recipe → resume

Abort is the *"my harness is wrong"* signal, and it's designed to be recovered
from: git keeps the whole bisect state (good/bad/skip refs) when the recipe
exits ≥128, with the failing commit checked out. Fix the recipe, then **re-run
the same command** — do *not* run `git bisect start` again, which would reset:

```sh
git bisect start <bad> <good>
git bisect run python recipe.py     # aborts on a broken recipe → state kept
#   … edit recipe.py (add a fixup, set skip_on_error=True, fix a typo) …
git bisect run python recipe.py     # SAME command → re-tests the current commit and continues
```

If the abort was really just *this one commit* being untestable (not a recipe
bug), skip it and carry on instead of changing anything:

```sh
git bisect skip                 # mark the current aborted commit untestable
git bisect run python recipe.py # continue — git routes around it
#   git bisect skip <sha>  /  git bisect skip A..B   # skip a specific commit/range
```

(To skip *every* unbuildable commit automatically instead of aborting, set
`skip_on_error=True` on that `run()` step — best for a whole known-bad band,
whereas `git bisect skip` is best for a one-off.)

## bisectlog (the report renderer)

`bisectlog` is a standalone, **read-only** CLI that renders any `git bisect`
session (recipe-driven or hand-run) as Markdown or HTML. It derives the entire
report from only `git bisect log` + per-commit information (git metadata, plus
each commit's optional `eval.json` sidecar that `bisectlib` records). No reflog,
no `/proc`, no heuristics.

```sh
bisectlog                       # colored, aligned table in the terminal (default)
bisectlog --format md           # Markdown
bisectlog --format html -o report.html
bisectlog --open                # render HTML and open in the browser
bisectlog --watch               # re-render as the bisect progresses
bisectlog --details             # include recorded commands/timings per commit (md/html)
```

Run bare, it prints a compact terminal table — one line per evaluation (input range
`good`/`bad` → `midpoint` → status), colored by status, `cmts` = commits still in the
range, subjects shortened to fit, with the first-bad commit called out:

```
bisect  good 2801e957a  bad 79cb050c2
🎯 first bad commit  5c9dcafb3  commit 8: tune the allocator

       good      bad       midpoint   cmts  subject
✓ good 2801e957a 79cb050c2 9a8b7c9d1    11  refactor the parser subsystem
✗ bad  9a8b7c9d1 79cb050c2 95345541b     6  add a caching layer
✗ bad  9a8b7c9d1 95345541b 5c9dcafb3     3  tune the allocator
✓ good 9a8b7c9d1 5c9dcafb3 19d89b121     2  optimize the hot loop
```

```
# Bisect report
**original range:** good `2801e9572` · bad `79cb050c2`

## 🎯 First bad commit: `5c9dcafb3` — commit 8: change subsystem 8

```
commit 5c9dcafb3a1e2f00d4c8b9a7e6f5d4c3b2a10987
Author: Eve <eve@example.com>
Date:   2026-06-15 11:40:00 +0200

    commit 8: change subsystem 8

 src/subsystem8.c | 12 ++++++------
 1 file changed, 6 insertions(+), 6 deletions(-)
```

| good | bad | midpoint | range | status |
|------|-----|----------|-------|--------|
| `2801e9572` 2026-05-28 22:06, Bob | `79cb050c2` 2026-06-24 13:02, Alice | `cb5394973` 2026-06-12 06:06, Carol | 27d 15h · 11 commits | 🟢 good |
| `cb5394973` 2026-06-12 06:06, Carol | `79cb050c2` 2026-06-24 13:02, Alice | `95345541b` 2026-06-18 09:12, Dan | 12d 7h · 6 commits | 🔴 bad · 81.2s |
| `cb5394973` 2026-06-12 06:06, Carol | `95345541b` 2026-06-18 09:12, Dan | `5c9dcafb3` 2026-06-15 11:40, Eve | 6d 3h · 3 commits | 🔴 bad |
| `cb5394973` 2026-06-12 06:06, Carol | `5c9dcafb3` 2026-06-15 11:40, Eve | `19d89b121` 2026-06-13 08:20, Fay | 3d 5h · 2 commits | 🟢 good |
```

When the bisect finishes, the report shows the culprit **the way `git bisect`
does** — the full commit header, message, and diffstat — so you can read the
verdict without another `git show`.

`status.md` is (re)written the moment each command **starts**, not just when it
finishes, so the report always names what is running right now — the in-flight
commit shows a `⏳ running` step and the top-level row reads `⏳ running \`…\``.
Each step links to its captured log, and that log is streamed to disk line by
line as the command runs, so you can click it (HTML) or tail it and **watch the
build/test as it happens** instead of waiting for it to complete.

Each `good`/`bad`/`midpoint` cell is the commit hash plus its **commit date and
author** (the subject is omitted to keep rows compact); the **range** column is the
`good..bad` span (duration · commit count).

Each row reads in causal order: the **input range** (`good`/`bad`) → the
**midpoint** git chose → the **status**. Watch the range funnel down.

## Install / how a recipe finds `bisectlib`

Your `recipe.py` does `from bisectlib import run, test, …`. Python needs to be able
to import that module — there are two easy ways:

**1. Install it (recommended for repeated use).**
```sh
pip install -e /path/to/bisectlib   # or: pip install bisectlib
```
Now `import bisectlib` works from any repo, and you also get the `bisectlog` /
`git-bisectlog` commands. Just write `recipe.py` and run
`git bisect run python recipe.py`. The installed packages ship a `py.typed`
marker, so editors/type-checkers resolve `run`, `test`, … without warnings.
(For an *editable* dev install, add `--config-settings editable_mode=compat` so
mypy can follow it: `pip install -e . --config-settings editable_mode=compat`.)

**2. Zero-install: drop the `bisectlib/` package next to your recipe.**
When you run `python recipe.py`, Python puts the recipe's own directory on
`sys.path`, so a `bisectlib/` folder sitting beside `recipe.py` is imported
automatically — no install, no `PYTHONPATH`. Copy the `bisectlib/` directory
(and `bisectlog/` if you want the auto-rendered status report) next to the recipe.

> Keep those copies **untracked** in the repo you're bisecting. Untracked files
> survive `git checkout`, so they persist across every commit of the bisect — but
> if you *commit* them they'd vanish on older commits (which don't have them) and
> the import would fail mid-bisect. Untracked = present everywhere, part of nothing.

(A bare `import bisectlib` without either of the above fails because the package
isn't on `sys.path` — that's why the tests inject it explicitly.)

Requires Python 3.10+. No third-party dependencies.

## Examples

See [`examples/`](examples/):

| File | Shows |
|------|-------|
| `minimal.py` | the simplest recipe: build + test |
| `flaky_with_fixup.py` | flaky test (`attempts`/`min_passes`) + a per-range patch `fixup` |
| `perf_regression.py` | a benchmark verdict via a time-aware `passed` predicate + `replace` |
| `find_when_fixed.py` | `bad_when="pass"` — find when something started *working* |
| `bisect_on_output.py` | bisect on output *content* (when a warning appeared) |
| `metric_binary_size.py` | a numeric-budget bisect (binary size crossed a threshold) |
| `build_fix_cherrypick.py` | keep an un-buildable range testable via `fixup(cherry_pick=…)` |

## Development

```sh
python -m unittest discover -s tests -v
```

## License

MIT © Martin Leitner-Ankerl
