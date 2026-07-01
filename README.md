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
- **Finding the range.** `find_anchors()` searches backward to locate a good/bad
  pair before you even start.
- **A clear report.** Every run records what happened; the companion
  [`bisectlog`](#bisectlog-the-report-renderer) tool renders the whole session as
  Markdown or HTML.

It is **pure standard library** — no dependencies, just `git` on your `PATH`.

See [`SPEC.md`](SPEC.md) for the full design rationale.

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
from bisectlib import run, test, check, replace, fixup, in_range, find_anchors, bisect
```

- **`replace(path, old, new)`** — sed-like edit, auto-reverted. `old` is a literal
  `str` or a compiled `re.Pattern` (the *type* decides; no `regex=` flag).
- **`fixup(patch=… | cherry_pick=…, when=…)`** — context manager that applies a
  patch/cherry-pick for its block, then reverts.
- **`in_range("v1.0..v2.0")`, `touches("src/x.c")`** — predicates for `when=`.
- **`find_anchors(bad="HEAD", probe=…)`** — expanding backward search for a
  good/bad pair; `probe` is a non-exiting predicate (e.g. `lambda: check("ctest").ok`).
- **`bisect(good, bad, "recipe.py")`** — convenience driver: runs the whole
  `git bisect start/run` and renders the final report.

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

The `bisect()` driver does this for you: if a bisect is already in progress it
**resumes** instead of restarting, and it never resets on abort — so you just
call `bisect(good, bad, "recipe.py")` again after fixing the recipe.

## bisectlog (the report renderer)

`bisectlog` is a standalone, **read-only** CLI that renders any `git bisect`
session (recipe-driven or hand-run) as Markdown or HTML. It derives the entire
report from only `git bisect log` + per-commit information (git metadata, plus
each commit's optional `eval.json` sidecar that `bisectlib` records). No reflog,
no `/proc`, no heuristics.

```sh
bisectlog                       # Markdown to stdout
bisectlog --format html -o report.html
bisectlog --open                # render HTML and open in the browser
bisectlog --watch               # re-render as the bisect progresses
bisectlog --details             # include recorded commands/timings per commit
```

```
# Bisect report
**original range:** good `2801e9572` · bad `79cb050c2`

## 🎯 First bad commit: `5c9dcafb3` — commit 8: change subsystem 8

| bad | good | midpoint | range | status |
|-----|------|----------|-------|--------|
| `79cb050c2`<br>commit 12 | `2801e9572`<br>commit 1 | `cb5394973`<br>commit 6 | … · 11 commits | ✅ good |
| `79cb050c2`<br>commit 12 | `cb5394973`<br>commit 6 | `95345541b`<br>commit 9 | … ·  6 commits | ❌ bad |
| `95345541b`<br>commit 9  | `cb5394973`<br>commit 6 | `5c9dcafb3`<br>commit 8 | … ·  3 commits | ❌ bad |
| `5c9dcafb3`<br>commit 8  | `cb5394973`<br>commit 6 | `19d89b121`<br>commit 7 | … ·  2 commits | ✅ good |
```

Each row reads in causal order: the **input range** (`bad`/`good`) → the
**midpoint** git chose → the **status**. Watch the range funnel down.

## Install

```sh
pip install -e .        # provides the `bisectlog` and `git-bisectlog` commands
```

Requires Python 3.10+. No third-party dependencies.

## Examples

See [`examples/`](examples/): `flaky_with_fixup.py`, `perf_regression.py`.

## Development

```sh
python -m unittest discover -s tests -v
```

## License

MIT © Martin Leitner-Ankerl
