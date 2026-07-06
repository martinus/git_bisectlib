# bisectlib

**Reliable `git bisect run` recipes in a few lines of Python.**

`git bisect` finds the commit that introduced a bug by binary-searching your history, and
`git bisect run` automates it — *if* your test script is perfect. But the script is exactly
where bisects quietly go wrong:

- A commit **doesn't build** → your script exits non-zero → git records it as **bad** → the
  search converges on the wrong commit and never tells you.
- A **flaky test** fails once → same story: an innocent commit takes the blame.
- A **performance** regression has no exit code to give — so you hand-roll timing math every
  time.

bisectlib is the recipe you *meant* to write. It knows the difference between "this commit
is **bad**" and "I **couldn't test** this commit," retries flaky tests until the verdict is
real, judges benchmarks, patches un-buildable commits on the fly — and streams a live report
you can watch.

```python
# recipe.py
from bisectlib import run, test

run("cmake -B build")                             # broken build? ABORT — don't guess
run("cmake --build build -j")
test("ctest -R foo", attempts=5, min_passes=2)    # flaky? 2 of up to 5 passes = good
# fell off the end → GOOD
```

```sh
git bisect start <BAD> <GOOD>
git bisect run python recipe.py
git bisect reset                # done — return to your branch
```

That's the whole thing — three git commands you already half-know, plus a recipe.
**Pure standard library, no dependencies** — just `git` on your `PATH`.

> **Tip:** a recipe is a plain script, so run `python recipe.py` on your current
> checkout *before* starting — exit `0` means "good", non-zero tells you it works — a
> five-second smoke test that catches a broken recipe before git spends an hour on it.

---

## Why not just a `git bisect run` shell script?

Because the naive script silently gives wrong answers, and the careful one is a pile of
plumbing you rewrite on every hard bisect. bisectlib is that plumbing, done once and done
right:

| A hand-rolled `git bisect run` script | bisectlib |
|---|---|
| A broken build exits non-zero → git reads it as **bad** → **silent mis-bisect** | `run()` **aborts** on build failure — bisect state is kept, you fix the recipe and resume |
| One flaky failure blames the wrong commit | `test(attempts=5, min_passes=2)` — a quorum that stops the moment the verdict is decided |
| Benchmarks need custom timing + threshold code | `passed=lambda r: r.seconds < 6.7` — any aggregate (min/median/all) via the quorum |
| Un-buildable ranges need manual patching each run | `fixup(patch=…)` / `replace(...)` apply a fix, then **auto-revert** to keep the tree clean |
| Progress is a wall of scrolling output | a live `.bisect/status.md` — watch the range funnel down to the culprit |

The headline is the first row: **a broken build is not a bad commit.** Treating the two the
same is the classic way a `git bisect run` script lands on the wrong answer without a single
error message. bisectlib makes that distinction the default.

## The four things it gets right

**1. Infrastructure vs. verdict.** `run()` is for configure/build/setup — if it fails, your
*harness* is probably broken, so it **aborts** the whole bisect (git keeps its state) rather
than mis-marking the commit. `test()` is the actual verdict: pass → good, fail → bad. For a
genuinely un-buildable stretch, opt into skipping with `run(..., skip_on_error=True)`.

**2. Flaky tests, both directions.** To *tolerate* a flake, run a quorum — `attempts` is the
*max* tries, `min_passes` how many must pass; it stops as soon as the outcome is locked in.
To *hunt* a rare flake, do the opposite: hammer the test and fail on the first bad run.
`for_seconds` gives a wall-clock budget instead of a fixed count, and `parallel` runs several
copies at once for throughput:

```python
test("./integration", attempts=5, min_passes=2)     # tolerate: 2 of up to 5 passes = good
hammer("./flaky")                                    # hunt: all cores for a minute, any fail = bad
```

**3. Benchmarks are just a time-aware predicate.** `passed` receives the `Result` (which
carries `.seconds`), and the quorum count expresses any aggregate:

```python
test("./bench", attempts=5, min_passes=1, passed=lambda r: r.seconds < 6.7)  # min of 5 < 6.7s
test("./bench", attempts=5,               passed=lambda r: r.seconds < 6.7)  # all 5   < 6.7s
test("./bench", attempts=5, min_passes=3, passed=lambda r: r.seconds < 6.7)  # median  < 6.7s
```

**4. Per-range build fixes that clean up after themselves.** Old commits often need a small
patch to compile with today's toolchain. Apply one for the commits that need it — it reverts
automatically so `git bisect` can move to the next commit:

```python
with fixup("fixes/missing-header.patch", when=in_range("abc123..def456")):
    run("cmake -B build")
    run("cmake --build build -j")
test("ctest -R foo")

replace("CMakeLists.txt", "c++14", "c++17")   # sed-like edit, also auto-reverted
```

You can also **decide straight from Python** after measuring something — no need to shell
back out just to compare a value:

```python
from bisectlib import check, bad

size = int(check("stat -c%s build/app").out)
if size > 5 * 1024 * 1024:
    bad("binary too big")     # exit 1; reaching the end instead is good
```

## Watch it work: `.bisect/status.md`

As the recipe runs, bisectlib writes a live Markdown report to **`.bisect/status.md`** at
the root of the repo you're bisecting. Open it in your editor and leave it open — it's a
fixed path that updates in place, so you watch the range narrow and see what's building
*right now* without babysitting a terminal.

````markdown
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
| `2801e9572` …, Bob | `79cb050c2` …, Alice | `cb5394973` …, Carol | 27d 15h · 11 commits | 🟢 good |
| `cb5394973` …, Carol | `79cb050c2` …, Alice | `95345541b` …, Dan | 12d 7h · 6 commits | 🔴 bad · 81.2s |
| `cb5394973` …, Carol | `95345541b` …, Dan | `5c9dcafb3` …, Eve | 6d 3h · 3 commits | 🔴 bad |
| `cb5394973` …, Carol | `5c9dcafb3` …, Eve | `19d89b121` …, Fay | 3d 5h · 2 commits | 🟢 good |
````

Each row reads in causal order — the **input range** (`good`/`bad`) → the **midpoint** git
chose → the **result** — so you see the range funnel down as you scan. The report is
re-rendered the moment each command *starts*, links every step to its live-streamed log
under `.bisect/<sha>/`, and — when the search resolves — shows the culprit the way
`git bisect` does, with the full commit and diffstat. When it's done, you have the answer
without another `git show` — then `git bisect reset` puts you back on the branch you
started from.

> `.bisect/` carries its own `.gitignore` of `*`, so it ignores itself entirely — it stays
> out of `git status`, is never committed, and survives the checkouts git does between
> commits, without touching your project's tracked `.gitignore` or `.git/`. Point it
> elsewhere with `configure(logs="…", status_md="…")` — a relocated dir gets the same
> `.gitignore`.

## Install

```sh
pip install git_bisectlib
```

That gives you `import bisectlib` for recipes anywhere (the distribution is
`git_bisectlib`, the import is `bisectlib` — like `pyyaml`/`yaml`). It ships a `py.typed`
marker, so editors and type-checkers resolve `run`, `test`, … with no warnings.

Prefer the bleeding edge? Install straight from `main`:

```sh
pip install --force-reinstall git+https://github.com/martinus/git_bisectlib
```

**Zero-install alternative:** running `python recipe.py` puts the recipe's own directory on
`sys.path`, so just dropping the `bisectlib/` package folder next to `recipe.py` is enough —
no install, no `PYTHONPATH`. Keep that copy **untracked** in the repo you're bisecting so it
survives every checkout (commit it and it would vanish on older commits and break the
import mid-bisect).

Requires **Python 3.10+**. No third-party dependencies.

## API cheat sheet

```python
from bisectlib import (run, test, hammer, check, once,
                       good, bad, skip, abort, replace, fixup, in_range, touches)
```

| Verb | Meaning | On failure |
|------|---------|------------|
| `run(cmd, skip_on_error=False)` | infrastructure (configure/build/setup) | **abort** (or skip) |
| `test(cmd, attempts=1, min_passes=None, passed=None, warmup=0, bad_when="fail")` | the verdict | **bad** |
| `hammer(cmd, for_seconds=60, parallel=<all cores>, passed=None, bad_when="fail")` | hunt a rare flake: run till one fails | **bad** |
| `check(cmd) -> Result` | run once, **never exits** — introspect `.ok`, `.out`, `.seconds` | — |
| `good()` / `bad()` / `skip()` / `abort()` | decide the commit **directly from Python** | — |

Every verb takes `cwd=` (relative paths resolve against the repo root; `configure(cwd=…)`
sets a default) and `timeout=` seconds. When a step exceeds `timeout`, `on_timeout` decides
the outcome — `run` defaults to `abort`, `test` to `skip`.

> **Hunting a hang?** If the regression is that a command stops terminating, set
> `test("./app", timeout=30, on_timeout="bad")` — a commit that runs forever is the bug,
> so time-out means bad. (The default `on_timeout="skip"` would route *around* every bad
> commit and stall the bisect.)

- **`hammer(cmd, for_seconds=…, parallel=…)`** — hunt a rare flake: run the command up to
  `parallel` at a time (default: **all cores**) for a wall-clock budget (default: **60s**),
  **bad on the first failing run**, good if the budget elapses clean. `passed=`/`bad_when=`
  still define what a failing run is. The report shows total runs, threads used, and runtime.
- **`once(key="setup")`** — run one-time, commit-independent setup (fetch a dependency,
  create a symlink) exactly once across the whole bisect instead of on every commit.
- **`replace(path, old, new)`** — sed-like edit, auto-reverted. `old` is a literal `str` or a
  compiled `re.Pattern` (the type decides — no `regex=` flag).
- **`fixup(patch=… | cherry_pick=…, when=…)`** — apply a patch/cherry-pick for a block, then
  revert.
- **`in_range("v1.0..v2.0")`, `touches("src/x.c")`** — predicates for `when=`, also usable in
  a plain `if`.
- **`sha()`, `subject()`, `is_clean()`** — read HEAD's full sha, its commit subject, and
  whether the tree is clean; handy inside a `when=` predicate or before a custom
  `good()`/`bad()` decision.
- **`configure(clean="reset"|"clean", color=…, cwd=…, logs=…, status_md=…)`** — tune tree
  cleanup (`"clean"` adds `git clean -fdx`, keeping `.bisect/`), force color on/off, or
  relocate the report and logs.

Mistyped a string option (`bad_when="Pass"`, `on_timeout="abrot"`) or an impossible
`min_passes`? The verb raises immediately — which **aborts** the bisect with a clear
message rather than silently defaulting and quietly bisecting in the wrong direction. The
fixed-choice options (`bad_when`, `on_timeout`, `if_missing`, `clean`) are typed with
`Literal`, so your editor autocompletes the valid values and a type-checker flags a typo
before you even run — no enum import, you still just write `on_timeout="bad"`.

### The exit-code contract

bisectlib maps outcomes to the exit codes `git bisect run` understands:

| Outcome | Exit | Meaning |
|---------|------|---------|
| good | `0` | bug absent |
| bad | `1` | bug present |
| skip | `125` | commit untestable — route around it |
| abort | `128` | harness broken — **bisect state preserved**, fix the recipe and re-run |

An uncaught exception in a recipe **aborts** (128) — it is never misread as "bad."

### Abort → fix the recipe → resume

Abort is the *"my harness is wrong"* signal, and it's built to recover from. git keeps the
whole bisect state with the failing commit checked out, so you fix the recipe and **re-run
the same command** — do *not* `git bisect start` again (that resets):

```sh
git bisect run python recipe.py     # aborts on a broken recipe → state kept
#   … edit recipe.py: add a fixup, set skip_on_error=True, fix a typo …
git bisect run python recipe.py     # SAME command → re-tests the current commit and continues
```

If it was really just *this one commit* being untestable, `git bisect skip` and carry on.

## Don't know a good commit yet? Let the recipe guide you

`git bisect` needs *two* endpoints — a bad commit **and** a good one. You almost always have
the bad one (HEAD, where you hit the bug); the good one you have to go find. bisectlib turns
that hunt into a guided loop: run the **same recipe** by hand and it tells you exactly what
to do next.

```sh
git bisect start
git bisect bad            # HEAD has the bug
python recipe.py          # ← run the recipe yourself
```

Because HEAD is already known-bad, the recipe **doesn't waste time re-testing it** — it
points you at older commits instead, spaced by a **widening time schedule** (1 day, 3 days, 1
week, 2 weeks, 1 month, 2 months back) so a handful of probes span a couple of months of
history. Each is shown `git log`-style — short sha, date, how long ago, subject, author — so
you can eyeball where to jump; copy a sha into `git checkout`:

```text
━━━ already marked bad — skipping ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ● HEAD (5fa8b7503) is already marked BAD — nothing to test.
  To find a GOOD commit, git checkout an older one and run it there:
    ec0acd2 2026-07-05 1 day ago     fix cache eviction        <Martin Leitner-Ankerl>
    7fb9e4a 2026-07-03 3 days ago    refactor loader           <Martin Leitner-Ankerl>
    356a26b 2026-06-29 1 week ago    bump deps                 <Martin Leitner-Ankerl>
    c196853 2026-06-22 2 weeks ago   tune scheduler            <Martin Leitner-Ankerl>
    85fb7c9 2026-06-06 4 weeks ago   add spec + bisectlog      <Martin Leitner-Ankerl>
    a59303d 2026-05-07 2 months ago  rework parser             <Martin Leitner-Ankerl>

    python recipe.py       # run again after checking out
```

Check out one and run again — pick a far one to cover ground fast, or a nearer one to tread
carefully. If the bug is **still there**, the recipe says so and offers a fresh batch of even
older candidates — mark it `git bisect bad` and keep going. The moment you land on a commit
where the bug is **gone**, it hands the search back to git:

```text
━━━ found a good commit ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✓ GOOD — the bug is ABSENT here (948739999).
  You found the good end of the range. Let git bisect take over:

    git bisect good
    git bisect run python recipe.py
```

From here it's the normal automated bisect. A few things worth knowing:

- **It's the same recipe, unchanged.** Guidance activates *only* while a bisect is started
  but has no good commit yet. During the real `git bisect run` (both endpoints known) and
  during a pre-start smoke test (no bisect at all) it stays completely silent.
- **A candidate that won't build is your call.** Old commits often don't build (toolchain
  drift), and that's neither good nor bad. Rather than guess, the recipe lays out the
  directions and lets **you** choose — jump **older** past the broken range, or come back
  **newer** toward code that builds — and reminds you that if it's the *recipe* that's broken
  you can fix it or set `run(…, skip_on_error=True)`:

  ```text
  ━━━ can't build this commit — you decide where to go ━━━━━━━━━━━━━━━━━━━━
    ⚠ `cmake --build build` failed at 2addbb8b — this commit won't build.
    An unbuildable commit is neither good nor bad; nothing was
    recorded. git checkout a commit and re-run — your call which way:

    OLDER — jump past the break (often toolchain drift):
      378ac9e 2026-05-01 9 weeks ago  fix cache eviction   <Martin Leitner-Ankerl>
      50fa29e 2026-04-25 2 months ago tune scheduler       <Martin Leitner-Ankerl>
    NEWER — come back toward code that builds:
      1785673 2026-07-02 4 days ago   refactor loader      <Martin Leitner-Ankerl>
      9c2d4f1 2026-06-28 1 week ago   bump deps            <Martin Leitner-Ankerl>
      a30b6ee 2026-06-14 3 weeks ago  tune scheduler       <Martin Leitner-Ankerl>

      python recipe.py       # run again after checking out
  ```
- **`--force`** re-evaluates the current commit even when it's already marked bad:
  `python recipe.py --force`.

## Examples

Runnable recipes in [`examples/`](examples/):

| File | Shows |
|------|-------|
| `minimal.py` | the simplest recipe: build + test |
| `flaky_with_fixup.py` | a flaky test (`attempts`/`min_passes`) plus a per-range patch `fixup` |
| `flaky_hunt.py` | hunt a *rare* flake: hammer a test `parallel`-wide for `for_seconds`, any fail = bad |
| `perf_regression.py` | a benchmark verdict via a time-aware `passed` predicate + `replace` |
| `find_when_fixed.py` | `bad_when="pass"` — find when something started *working* |
| `bisect_on_output.py` | bisect on output *content* (when a warning first appeared) |
| `metric_binary_size.py` | a numeric-budget bisect (binary size crossed a threshold) |
| `build_fix_cherrypick.py` | keep an un-buildable range testable via `fixup(cherry_pick=…)` |

## The mental model

A recipe is a normal script that `git bisect run` executes once per commit:

- A **passing step continues** to the next line.
- A failing **`test()` is bad**; a failing **`run()` aborts** (or skips).
- **Falling off the end is good.**

Because passing steps continue, listing several `test()` calls just **ANDs** them — every one
must pass for the commit to count as good. That's the entire thing to remember.

See [`SPEC.md`](SPEC.md) for the full design rationale, and run the tests with
`python -m unittest discover -s tests -v`.

## License

MIT © Martin Leitner-Ankerl
