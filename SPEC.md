# `bisectlib` — a Python toolkit for automated `git bisect`

> **Status:** **finalized.** One package: **`bisectlib`** — the recipe engine
> (`run`/`test`/`check`/`fixup`/`replace`) plus a built-in report renderer
> (`bisectlib._report`) that writes a live Markdown status page as the bisect runs.
>
> **Renderer contract (load-bearing):** the report derives from only (1) `git bisect log`
> and (2) per-commit information (git metadata + each commit's optional `eval.json` sidecar
> of recorded facts). **No reflog, no `/proc`, no PID, no heuristic inference** anywhere —
> if a fact wasn't logged or recorded, it isn't shown.

## 1. Goal

A small Python library for writing **`git bisect run` recipes** as short scripts.
It should make these things easy:

1. Run a sequence of commands per commit (configure → build → test).
2. Apply a patch / cherry-pick / one-line edit to fix build errors over a commit range.
3. Handle flaky tests (e.g. "passes 2 of 5 runs").
4. Detect performance regressions (benchmark against a max runtime).
5. Distinguish **infrastructure failure** (can't test this commit) from a **real
   good/bad result**, and give the human a chance to fix the recipe and resume.

The user writes a tiny recipe; runs `git bisect run python recipe.py`.

---

## 2. The two hard constraints that drive the whole design

### 2.1 Exit codes are the contract

`git bisect run <script>` interprets the script's **process exit code**. This single
fact gives the "infrastructure vs. result" distinction for free:

| Exit code | git bisect meaning | our concept |
|---|---|---|
| `0` | good | bug **absent** |
| `1`–`124`, `126`, `127` | bad | bug **present** |
| `125` | **skip** — commit untestable | known-broken build, can't decide |
| `≥128` | **abort the whole run** (state preserved) | infra broken; human must fix recipe |

- **SKIP (125)** = per-commit: "can't test *this* commit, route around it."
- **ABORT (128)** = global: "my harness is wrong; stop." git bisect persists its
  good/bad/skip state, so after aborting you fix the recipe and re-run
  `git bisect run …` and it resumes exactly where it stopped.

`Outcome` enum:

```python
class Outcome(Enum):
    GOOD  = 0
    BAD   = 1
    SKIP  = 125
    ABORT = 128
```

### 2.2 The working tree must stay clean

git bisect checks out the next commit between runs and refuses to move if the tree is
dirty. Therefore **any mutation (patch, cherry-pick, file edit) MUST be reverted before
the process exits**, even on exception or early exit.

- Implemented with context managers + `try/finally` and/or an at-exit restore registry.
- An immediate `sys.exit()` raises `SystemExit`, which unwinds `with` blocks, so cleanup
  still runs before the process dies.
- Since the bisect tree starts clean and fixup targets are tracked files, reverting is
  just `git checkout -- <path>` (and `git reset --hard` for cherry-picks). Default
  cleanup leaves gitignored `build/` alone (so incremental builds survive); opt-in
  `git clean -fdx` for pristine-every-time.

---

## 3. The recipe model

A recipe is a **one-shot script**: `git bisect run` spawns a fresh process per commit,
so there is exactly **one session per process**. Consequences:

- No `ctx` object, no decorator, no return value needed. State (status writer, repo,
  log dir, config) lives module-global.
- **Falling off the end of the script = GOOD (exit 0).**
- The role of each step is in the **verb**, not a flag:
  - `run(cmd)` — an **infrastructure step** (configure/build/setup). It must succeed to
    proceed; if it fails the harness is presumed broken, so it **aborts** the whole bisect
    (exit 128) by default — set `skip_on_error=True` to skip this commit instead.
  - `test(cmd)` — the **verdict**: pass ⇒ good (continue), fail ⇒ bad (exit 1).
- A step that produces a conclusive result **exits the process immediately**. A passing
  step continues to the next line, so ANDing several tests is just listing them.

Minimal recipe:

```python
from bisectlib import run, test

run("cmake -B build")                 # infra: fail -> abort (exit 128)
run("cmake --build build -j")         # infra: fail -> abort (exit 128)
test("ctest -R foo", attempts=5, min_passes=2)  # flaky verdict: 2 of up to 5
# reached the end, no failure -> exit 0 (good)
```

Run it: `git bisect start <BAD> <GOOD> && git bisect run python recipe.py`.

---

## 4. API surface

Keep it tiny. Everything is a module-level function. Use **type dispatch instead of
boolean flags** where a type can decide (consistent across the API).

### Console echo (all of `run`/`test`/`check`)

Before a step launches, echo the command to **stderr** in color (stderr so it never mixes
with captured stdout or anything parsing it). `git bisect run` forwards the script's
stderr to your terminal live, so you watch progress as it goes:

```
▶ [9a8b7c] run   cmake --build build -j          # cyan, printed BEFORE launch
✓ [9a8b7c] test  ctest -R foo            (2.3s)  # green=good / red=bad / yellow=skip, AFTER
```

Color only when `stderr.isatty()` and `NO_COLOR` is unset; otherwise plain text. The line
includes the short sha + verb so interleaved output stays readable. The command's combined
stdout+stderr is **streamed live to stderr as it runs** (so you watch the build/test; git
bisect run forwards it to your terminal) while also being captured to the per-commit log
dir. Each step is appended to that commit's `eval.json` sidecar (cmd, exit code, duration,
flaky/benchmark stats, fixups) — the recorded facts the report reads for its detail view (§6).

### 4.1 `run` — infrastructure steps (abort on error by default)

For configure/build/setup. The premise: if a build step breaks, your harness is usually
wrong, so the safe default is to **stop and let a human fix the recipe** rather than
silently routing around commits and possibly mis-bisecting. A genuinely un-buildable
commit range is the exception — opt into skipping with `skip_on_error=True`.

```python
run(cmd,
    skip_on_error=False,  # default: failure ABORTS the bisect (exit 128).
                          # True: failure SKIPS just this commit (exit 125).
    timeout=None,         # per-execution timeout in seconds
    on_timeout="abort",   # "abort"(default) | "skip"
    name=None)            # label in the status MD
```

| outcome | default (`skip_on_error=False`) | `skip_on_error=True` |
|---|---|---|
| passed | continue | continue |
| failed | `exit 128` (abort, state preserved) | `exit 125` (skip commit) |
| timed out | per `on_timeout` (default abort) | per `on_timeout` |

Because abort preserves git bisect's state, the loop is: build breaks → bisect stops →
you fix the recipe (add a `fixup`/`replace`, or set `skip_on_error=True` for that step) →
re-run `git bisect run …` → it resumes where it stopped.

```python
run("cmake -B build")                          # broken configure -> abort, go fix it
run("cmake --build build -j")                  # broken build -> abort
run("cmake --build build", skip_on_error=True) # known-unbuildable range -> skip instead
```

### 4.2 `test` — the verdict

The deciding step: pass ⇒ good (continue), fail ⇒ bad (exit 1). This is where flakiness
and benchmarking live (the old `flaky`/`benchmark` ideas folded in here).

```python
test(cmd,
    attempts=1,         # MAX number of tries (default: single run)
    min_passes=None,    # passes required for good; default None = all attempts
    passed=None,        # callable(Result)->bool: did one attempt pass? default r.ok
    warmup=0,           # extra leading throwaway runs (excluded from the pass count)
    bad_when="fail",    # "fail"(default) | "pass" to invert the bug direction
    timeout=None,
    on_timeout="skip",  # "skip"(default) | "bad" | "abort"
    name=None)
```

Evaluation **stops as soon as the verdict is known** — when `min_passes` is reached (good)
or can no longer be reached (bad) — so `attempts` is an upper bound, not a fixed count.

**Benchmarking is just a time-aware `passed`.** There is no `max_median`: `passed` receives
the `Result` (which carries `.seconds`), so a timing threshold is an ordinary predicate, and
the quorum count expresses any aggregate over the attempts:

```python
test("ctest -R foo")                                       # single run: pass=good, fail=bad
test("ctest -R foo", attempts=5, min_passes=2)             # flaky: 2 of up to 5 => good
test("./bench", attempts=5, min_passes=1, passed=lambda r: r.seconds < 6.7)  # min(5) < 6.7s
test("./bench", attempts=5,               passed=lambda r: r.seconds < 6.7)  # all 5  < 6.7s
test("./bench", attempts=5, min_passes=3, passed=lambda r: r.seconds < 6.7)  # median  < 6.7s
test("./repro", bad_when="pass")                           # bisecting when a bug got "fixed"
```

Aggregate ↔ quorum: `min(times) < T` → `min_passes=1`; `max(times) < T` (all) →
`min_passes=attempts`; `median < T` → `min_passes=attempts//2 + 1`. Combine functional +
perf with `passed=lambda r: r.ok and r.seconds < T`.

### 4.3 `check` — the dumb verb that never decides

```python
check(cmd) -> Result    # runs once, NEVER exits the process
# Result: .code (int), .out (str, combined stdout+stderr), .seconds (float)
```

Escape hatch for introspection / parsing:

```python
if "ERROR" in check("make").out:
    ...
```

### 4.4 `replace` — sed-like in-file edit (auto-reverted)

```python
replace(path, old, new,
        count=0,            # 0 = all occurrences; N = first N
        when=None,          # only apply if predicate true (e.g. in_range(...))
        if_missing="skip")  # "skip"(default) | "abort" | "ignore"
```

- **Type dispatch, no `regex=` flag:** `old: str` → literal substring; `old: re.Pattern`
  → regex (with `new` as a replacement template supporting `\1` backrefs; pattern carries
  its own flags like `re.I`/`re.M`).
- Literal strings with regex metacharacters match literally → safe by default.
- Reverted via `git checkout -- path` at process exit (or `with`-block exit if scoped).

**`if_missing` protects bisect correctness** — a fixup that silently doesn't match would
build the *unpatched* tree and produce a false good/bad:

| `if_missing=` | when `old` not found |
|---|---|
| `"skip"` *(default)* | `exit 125` — fixup doesn't fit this commit, route around it |
| `"abort"` | `exit 128` — recipe is wrong, stop |
| `"ignore"` | no-op, keep going |

```python
import re
replace("CMakeLists.txt", "c++14", "c++17")                                  # literal
replace("src/foo.c", re.compile(r"#include <old\.h>"), "#include <new.h>")   # regex
replace("Makefile", "-Werror", "", when=in_range("abc".."def"))              # range-scoped
```

(Only `replace` for now; add `append` / `insert_after` / `delete_lines` later if a real
recipe needs them — `replace` + regex covers the sed 90%.)

### 4.5 `fixup` — apply a patch / cherry-pick over a range (auto-reverted)

```python
with fixup(patch="fixes/build.patch", when=in_range("abc".."def")):
    run("cmake -B build")
    run("cmake --build build -j")
# patch reverted here

with fixup(cherry_pick="fix_sha", when=touches("src/legacy.c")):
    ...
```

- `patch=` → `git apply` (revert: `git checkout -- .` / reverse-apply).
- `cherry_pick=` → `git cherry-pick --no-commit <sha>` (revert: `git reset --hard`).
- `when=` predicate: only applied when true; otherwise the block runs unpatched.
- Also usable to *scope* the revert of `replace` edits (revert at block exit instead of
  process exit).

Optional declarative form for several fixups:

```python
apply_fixups([
    Fixup(when=in_range("abc".."def"), patch="fixes/missing-header.patch"),
    Fixup(when=touches("src/legacy.c"), cherry_pick="fix_sha"),
])
```

### 4.6 git helpers (module functions)

```python
sha()                       # current commit hash
subject()                   # commit subject line
is_clean()                  # working tree clean?
in_range("v1.0".."v2.0")    # is HEAD inside this range? -> predicate/bool
touches("src/parser.c")     # did this commit touch a path? -> predicate/bool
```

`in_range`/`touches` return predicates usable as `when=` for `fixup`/`replace`, and are
truthy for direct `if` use.

### 4.7 `configure` — optional, zero-config defaults otherwise

```python
configure(status_md=None,   # default: <repo>/.bisect/status.md (fixed, watchable path)
                            # Set a path to override.
          logs=None,        # default: <repo>/.bisect/ ; per-commit logs in .bisect/<sha>/
          clean="reset",    # "reset" (keep build/) | "clean" (git clean -fdx, keeps .bisect/)
          color=None,       # None=auto (tty & !NO_COLOR) | True | False
          cwd=None)         # default working dir for commands (relative to repo root)
```

### 4.8 verdict primitives — decide directly from Python

`good()` / `bad()` / `skip()` / `abort()` exit immediately with the matching bisect exit
code, so a recipe can decide from arbitrary Python after measuring with `check()` — no need
to shell back out to `test` just to compare values:

```python
size = int(check("stat -c%s build/app").out)
if size > 5 * 1024 * 1024:
    bad("binary too big")     # exit 1; reaching the end instead is good
```

> **Out of scope (removed):** a library-driven `bisect(...)` convenience driver was
> considered but cut — the recipe-as-script model (`git bisect run python recipe.py`) plus
> manual `git bisect start` is the whole surface. Finding the *good* anchor is **advised, not
> driven**: see §4.9 (guided mode) — the recipe never runs `git` state-changing commands on
> your behalf, it only prints the ones for you to copy-paste.

### 4.9 guided mode — hunting for the good anchor

`git bisect` can't binary-search until it has **both** a bad and a good commit. The bad one is
usually HEAD; the good one you find by walking backwards through history. Guided mode makes
each manual `python recipe.py` run of that hunt self-explaining — it is **advisory only**
(prints copy-pasteable commands, changes no git state itself).

**Activation (and the silence guarantee).** Guided output appears **only** when a bisect is
started, a bad commit is known, and **no good commit exists yet** (`refs/bisect/good-*` empty
*and* no good in `git bisect log`). Therefore it is silent during the real `git bisect run`
(both endpoints known) and during a pre-start smoke test (no bisect at all) — the automated
search is never polluted with next-step spam. This "no good endpoint yet" test is the whole
trigger; there is deliberately **no** fragile "am I running under `git bisect run`?" sniffing.

**Behaviour when active:**

- **Already-bad guard.** Before the first step runs, if HEAD is a commit git already knows is
  bad, the recipe is **not executed** (there's nothing to learn) — it exits `0` after printing
  older candidates to try. `--force` (in `sys.argv`) overrides this and evaluates anyway.
- **After a verdict**, the finalizer prints the next step for that outcome:
  - **good** → "you found the good end": `git bisect good` then `git bisect run python recipe.py`.
  - **bad** → `git bisect bad`, then a few older candidate commits to `git checkout` and re-run.
  - **skip** → older candidates to try instead.
  - **abort — recipe error** (uncaught exception, unrunnable test) → "fix the recipe"; *not* a
    good/bad verdict, upholding §2.1's infrastructure-vs-result distinction.
  - **abort — build failure** (a `run()` command exited non-zero) → a commit that won't build
    is untestable, and *why* (toolchain drift on an old commit vs. a broken recipe) is a
    judgement the library can't make. So instead of guessing it presents a **direction
    choice**: an **older** commit (jump past the break) and a **newer** one (back toward code
    that builds), plus the reminder that a broken recipe can be fixed or made to route around
    the commit with `run(…, skip_on_error=True)`. The user decides — the library never marks
    the commit or moves HEAD.
- **Candidate commits follow a widening time schedule.** People reason about regressions in
  calendar terms ("try a month ago"), so probes step back through *time* from HEAD's commit
  date on a widening schedule — 1 day, 3 days, 1 week, 2 weeks, 1 month, 2 months, stopping
  there (`_TIME_OFFSETS_DAYS`). Each offset resolves to an ancestor of HEAD by date
  (`git rev-list -1 --before=<date> HEAD`), deduped nearest-first; offsets past the start of
  history are dropped. Candidates are rendered as `git log` one-liners (short sha, committer
  date, relative age, subject, author — colored to the terminal) so the user can eyeball where
  to jump and copy a sha into `git checkout`. The build-failure menu's **older** direction is
  this same list; **newer** applies the same schedule *forward* from HEAD toward the newest bad
  (never reaching it), so both directions offer a spread of commits, not a single jump.

---

## 5. Locked-in semantic defaults

These are the decisions made during brainstorming; treat as the starting defaults:

1. `run` (infrastructure) **aborts on error by default** (`skip_on_error=False`) → a
   broken build stops the bisect so a human can fix the recipe, rather than silently
   skipping commits and risking a wrong result. `skip_on_error=True` for genuinely
   un-buildable ranges. `test` failure ⇒ bad.
2. `test`: `attempts=1`, `min_passes=None` (=all) → single run unless asked otherwise.
3. `warmup` excludes runs from **timing stats only**; the pass quorum is judged over the
   `runs - warmup` non-warmup executions.
4. `passed` is a predicate over the `Result` (incl. `.seconds`); the quorum
   (`attempts`/`min_passes`) aggregates per-attempt passes into the verdict.
5. `run` timeout → **abort** by default (broken harness); `test` timeout → `skip` by
   default (likely an infra hang); override `on_timeout="bad"` for infinite-loop hunts.
6. `replace`/`fixup` `if_missing="skip"` / unmatched `when` → never silently build wrong.
7. Tree cleanup defaults to `git reset --hard` + `git checkout -- .` (keeps gitignored
   `build/` for fast incremental builds); `clean="clean"` opts into `git clean -fdx`.
8. Timing thresholds live in the `passed` predicate (`r.seconds`), not a dedicated knob;
   any aggregate (min/median/max < T) is expressed via `min_passes`. Relative-to-baseline
   ("15% slower") would need a calibration run at the good anchor — a later addition.

---

## 6. Status MD report

Header = original anchors + estimated remaining steps (~log₂ of range). Body = one row per
tested commit with links to per-commit command logs. Record when a fixup/replace was
applied (so a verdict reached via a patch is transparent).

### The report is a pure render of `git bisect log` — no state kept

`git bisect log` already records, in order, every marking (`good` / `bad` / `skip`) and
the sha it applied to. That **is** the evaluation history — so the status MD is a
**stateless render of `git bisect log` + ordinary git metadata**. Nothing is appended or
tracked between the fresh per-commit `python recipe.py` processes; the file is fully
regenerated each time and self-heals if deleted. git's log is the single source of truth.

**Reconstruction algorithm** (run at the top of each evaluation, and on demand):

1. Parse `git bisect log` into ordered ops: `("start", [revs])` and `(term, sha)` markings
   (`term` ∈ {bad-term, good-term, `skip`}; terms via `git bisect terms`).
2. Walk the ops carrying `bad` / `goods` bounds. While **not** both a bad and a good are
   known yet, the markings are **anchors** (the original range), not evaluation rows — this
   correctly handles both `git bisect start <bad> <good>` (anchors on the start line) and
   the interactive form (separate first `bad`/`good` lines).
3. Once both bounds exist (bisect is "ready"), every subsequent marking is one **row**:
   - `probe` = the marking's sha
   - `status`   = the marking's term (good/bad/skip)
   - input range `bad`/`good` = the bounds *as they stood before this marking*
   - then apply: `bad`→lowers `bad`, `good`→raises `good` (newest-by-ancestry good is the
     range bound), `skip`→bounds unchanged
4. `range` cell per row = git queries on the two bound shas: dates via `git show -s
   --format=%ci`, duration = their delta, count = `git rev-list --count <good>..<bad>`.
5. **In-flight row:** the current `HEAD` is the probe being tested right now and is not
   in the log yet — synthesize a trailing `🕒 todo` row for it (from `git rev-parse HEAD`)
   when `HEAD` isn't already the last logged marking.
6. Header `original range` + `resume:` line come from the anchors / current bounds; both
   fall straight out of the same walk.

Because every column derives from `git bisect log`, resume "just works": after an abort,
the log is intact, so the next render reproduces the full table — no continuity key, no
merge logic, no write races.

> **Finalization.** `git bisect run` records a commit's verdict *after* the recipe process
> exits, and stops after the final commit — so nothing would re-render the **last**
> evaluation, leaving it stuck on `🕒 todo`. The engine handles this at exit: its finalize
> render feeds the reconstruction a log with its own just-decided mark appended, so the
> saved `status.md` names the first-bad commit without any extra command.

> **What's *not* in the log:** per-eval wall-clock timing, flaky ratios ("2/5 pass"), and
> which fixup/replace applied. The 5-column table doesn't need them (they live in the
> per-commit log files). If you ever want them in the report, write a tiny sidecar in the
> per-commit log dir and let the renderer enrich rows from it when present — the core
> render stays state-free.

### Finding the *original* good/bad anchors

The current refs are **not** the originals: `refs/bisect/bad` is a single ref that git
**moves inward** as it narrows, so the original bad is overwritten and unrecoverable from
it (only the `good-*` refs accumulate). The authoritative source is the **bisect replay
log**, `git bisect log` (worktree-safe; same content as `.git/BISECT_LOG`), which records
every marking in order — the **first** `bad` and **first** `good` are the originals.

Two wrinkles the parser must handle:
- **Custom terms.** Bisect terms may be `old`/`new` (or anything) instead of `good`/`bad`.
  Read them with `git bisect terms --term-bad` / `--term-good` (fallback: the two lines of
  `.git/BISECT_TERMS`, bad first, good second).
- **Anchors on the `start` line.** `git bisect start <bad> <good>` records the anchors as
  positional args on the `start` line (first positional = bad, the rest = good), *not* as
  separate `bad`/`good` lines. The interactive form (`git bisect start` then `git bisect
  bad` / `git bisect good <sha>`) records them as separate lines. Parse both.

```python
def original_anchors():
    """(original_bad_sha, [original_good_shas]) from the bisect replay log."""
    bad_term  = git("bisect", "terms", "--term-bad").strip()    # "bad"  (or "new")
    good_term = git("bisect", "terms", "--term-good").strip()   # "good" (or "old")
    bad, goods = None, []
    for line in git("bisect", "log").splitlines():
        if not line.startswith("git bisect "):
            continue
        verb, *args = line.split()[2:]                # drop the "git bisect" prefix
        revs = [a.strip("'\"") for a in args if not a.startswith("-")]
        if verb == "start" and revs:                  # start <bad> <good...>
            bad = bad or revs[0]
            goods += revs[1:]
        elif verb == bad_term and revs:               # bad <rev>
            bad = bad or revs[0]
        elif verb == good_term and revs:              # good <rev>
            goods.append(revs[0])
    bad = git("rev-parse", bad) if bad else None
    goods = sorted({git("rev-parse", g) for g in goods})
    return bad, goods
```

These originals feed two things: the **header "original range"** (with subjects via
`git show -s --format=%s`), and the **`<bisect-id>`** below. The replay log persists for
the whole session, so both stay stable across abort→resume.

> `BISECT_START` is **not** the anchors — it only records the branch/commit to restore on
> `git bisect reset`. Don't use it for the range.

### File location & name

Since the report is a stateless render, the location only needs to **locate** the file at a
predictable path you can keep open — there's no continuity to preserve.

- **A single `.bisect/` directory at the repo root**, holding `status.md` at a fixed path
  and the per-commit logs under `.bisect/<sha>/`. The point is watchability: `.bisect/status.md`
  is always the same path, so you open it once in your editor and it refreshes in place —
  no per-run subdir to chase. `configure(status_md=…, logs=…)` overrides.
- **`.bisect/` is registered in the repo's local excludes** (`.git/info/exclude`, appended
  once — never the project's tracked `.gitignore`). That keeps it out of `git status` (so it
  isn't committed by accident and doesn't trip the recipe's own `is_clean()` checks) while
  `git checkout` between commits leaves the untracked directory untouched. When
  `clean="clean"`, the `git clean -fdx` excludes `.bisect/` so the report survives.
- **`status.md` is re-rendered after every step** (not just at the end), and the current
  commit's `eval.json` is flushed alongside it, so the file is a live view of the in-flight
  commit's progress — open it in your editor and watch a long build/test as it runs.
- **Single session per repo.** git allows only one bisect per worktree, so a flat `.bisect/`
  matches git's own model (its `BISECT_*` state is likewise one-per-worktree). An `id` file
  holds the **`<bisect-id>`** (short hash of `worktree_path + original anchors`, from
  `original_anchors()` / `git bisect log`). Every process of one bisect sees the same id and
  shares the directory; starting a *different* bisect (new anchors) changes the id, so the
  first process of the new run clears the previous report, logs and `once()` markers, while a
  resume (same anchors) keeps them so `once()` setup stays done.
- No PID, reflog, `/proc`, or other brittle/process-specific signal is used anywhere — the
  identity and the whole report derive only from `git bisect log` + commit metadata.

**Each row carries the input range as it stood before that evaluation**; restarting is
then just **read the last row → `git bisect start <bad> <good>`** (or copy the top
`resume:` line). A SKIP row doesn't move the bounds, so it repeats the previous range.

Each row reads in causal order: **the input range (`bad` / `good`) → the `probe` we
picked to evaluate → the `status` result.** `bad`/`good` are the range *as it stood
before this evaluation* (the range that made git bisect pick this probe), so you watch
the range funnel down as you scan top-to-bottom.

Five columns:

| Column | Content |
|---|---|
| **good** | good bound of the input range — **commit hash + date + author** |
| **bad** | bad bound of the input range — **commit hash + date + author** |
| **probe** | the commit picked to evaluate this step — **commit hash + date + author** |
| **range** | the `good..bad` range as **duration · commit count** (no dates) |
| **status** | evaluation result with an icon: `🕒 todo` / `🟢 good` / `🔴 bad` / `⏭️ skip` (`🛑 abort`) |

Each commit cell shows the short hash, its commit date, and author (the subject is
omitted to keep rows compact). `status: 🕒 todo` marks the in-flight commit; once the
recipe locks in a verdict its sidecar records it (`pending: false`) so the saved
`status.md` shows the real result even before git records the mark.

```markdown
# Bisect: my_flaky_test regression
**original range:** good v1.0 `a1b2c3` · bad HEAD `f6e5d4`
**resume:** `git bisect start 5d6e7f 9a8b7c`   ← post-last-result range; copy-paste to restart

| good | bad | probe | range | status |
|------|-----|----------|-------|--------|
| `a1b2c3` 2026-03-01 09:12, Ada | `f6e5d4` 2026-05-23 20:41, Bruno | `9a8b7c` 2026-04-15 11:06, Cleo | 83d 11h · 264 commits | 🟢 good |
| `9a8b7c` 2026-04-15 11:06, Cleo | `f6e5d4` 2026-05-23 20:41, Bruno | `5d6e7f` 2026-05-04 14:20, Dev | 41d 06h · 132 commits | 🔴 bad |
| `9a8b7c` 2026-04-15 11:06, Cleo | `5d6e7f` 2026-05-04 14:20, Dev | `3c4d5e` 2026-04-28 08:33, Eli | 16d 19h · 64 commits | ⏭️ skip |
```

- The **range** cell is computed from the current bounds: duration = the delta
  between the good and bad commit dates (e.g. `7d 25h 15m`), commit count = the
  candidate commits git still has to consider, `git rev-list --count <bad> --not
  <every good so far>` (excluding ancestors of **all** goods, not just the latest —
  in a merge DAG the goods can diverge, so counting only `latest_good..bad` overcounts).
- The result **updates the bounds for the *next* row**: a `good` raises `good` to the
  probe; a `bad` lowers `bad` to it; a `skip` leaves the bounds unchanged (git bisect
  just picks another probe inside the same range — see rows 2→3).
- The top-of-file **`resume:`** line is the *post-last-result* range, as a copy-pasteable
  `git bisect start <bad> <good>` — restarting needs only that one line.
- When the range's commit count reaches 1, the `bad` commit is the **first bad commit**
  (the answer); the report should flag that explicitly.

### The report renderer (`bisectlib._report`)

Because the report derives purely from `git bisect log` + git, the renderer is a
**self-contained, read-only** module (`bisectlib._report`). It works on *any* bisect —
including ones run by hand with no `bisectlib` recipe — and the engine calls it after every
step to (re)write `status.md`. Importable as
`from bisectlib._report import build_report, render_markdown`.

- `build_report(repo, log_text=None, logs_dir=None) -> Report | None` runs the
  reconstruction walk (anchors → rows → bounds), synthesizes the in-flight `todo` row from
  `HEAD`, and queries git for dates/counts. Returns `None` when no bisect is in progress.
- `render_markdown(report, details=False)` emits the 5-column table; with `details=True` it
  adds the per-commit detail sections. When the range narrows to 1, it renders a prominent
  **"First bad commit"** block with the full commit header, message and diffstat
  (`git show --stat`), the way `git bisect` reports it. Terminal states are surfaced too
  ("only skipped commits left — cannot conclude").

The reconstruction walk is stdlib-only and never touches the repo, so it's the load-bearing
core the rest of the design relies on.

#### Data sources — the renderer's hard contract

The renderer reads **only two things**, both keyed by commit. Nothing is inferred;
there is **no brittle heuristic logic**:

1. **`git bisect log`** — the structure: which commits were evaluated, in what order, with
   what verdict, plus the original anchors.
2. **Per-commit information** for each sha in the log:
   - **git metadata** — `git show -s` (subject, author, dates) and `git rev-list --count
     <good>..<bad>` (range size). Robust, always available.
   - **the commit's optional `eval.json` sidecar** — *recorded facts* written by the engine
     for that commit (exact commands, exit codes, measured timings, flaky ratio, benchmark
     timing/fixups). Read by sha; absent → that row just shows the log+metadata view.

**Explicitly NOT used** (removed as brittle): HEAD-reflog timing inference, `/proc` PID
walking, or any other guesswork. Everything shown is either in the log or a recorded fact
about a commit — never approximated.

#### Showing more (commands, timings) — only from recorded facts

The richer detail (commands, per-step exit codes, exact timings, flaky ratio, benchmark
timing, fixups) comes **solely from each commit's `eval.json` sidecar**, which the engine
writes into the per-commit log dir (`.bisect/<sha>/`) next to the captured `*.log` files
(named `NN-<verb>-<slug-of-command>.log`, e.g. `01-run-cmake-b-build.log`; a `test` records
the last attempt's file, `NN-test-<slug>-<attempt>.log`). Each step's `log` is a real
filename, so the report's **step** cell links to it (`<sha>/<log>`, relative to `status.md`):

```json
{
  "sha": "9a8b7c…", "outcome": "good", "exit_code": 0, "pending": false, "duration_s": 192.4,
  "steps": [
    {"verb":"run","cmd":"cmake -B build","code":0,"duration_s":4.1,"log":"01-run-cmake-b-build.log"},
    {"verb":"run","cmd":"cmake --build build -j","code":0,"duration_s":151.2,"log":"02-run-cmake-build-build-j.log"},
    {"verb":"test","cmd":"ctest -R foo","outcome":"good","attempts":5,"executed":5,"passes":2,"min_passes":2,
     "durations_s":[1.7,1.9,2.4,1.6,1.8],"log":"03-test-ctest-r-foo.log"}
  ],
  "fixups": [{"kind":"replace","path":"CMakeLists.txt","detail":"c++14→c++17"}]
}
```

Keyed by sha, so partial coverage is fine — a row with no sidecar (e.g. a hand-run bisect,
or evals that predate the engine) shows only the log+metadata view, with no fabricated
numbers. Timings are always the engine's **measured** durations, never inferred.

**Presentation when a sidecar is present:** the 5-column table stays compact, with the
`status` cell gaining inline recorded detail — `🟢 good · 2/5 · 1.8s`, or `⏳ running \`…\``
while a step is still executing. Below the table (with `details=True`), a per-commit
**details section** lists each step's command, exit code and duration, notes the fixups
applied, and links each step to its captured `*.log`.

> Engine side: writing `eval.json` + per-step `*.log` is part of `run`/`test`/`check`
> (§4) and the per-commit log dir (§6). The renderer never *requires* it — when absent the
> report is a clean function of `git bisect log` + git commit metadata.

---

## 7. Full example recipes

### 7.1 Flaky functional regression with a range fixup

```python
from bisectlib import run, test, fixup, in_range

with fixup("fixes/missing-header.patch", when=in_range("abc123".."def456")):
    run("cmake -B build")             # broken build -> abort (go fix the recipe)
    run("cmake --build build -j")
test("ctest -R regression", attempts=5, min_passes=2)   # flaky: 2 of up to 5 => good
```

### 7.2 Perf regression with a one-line build fix

```python
from bisectlib import run, test, replace

replace("CMakeLists.txt", "c++14", "c++17")          # reverted automatically
run("cmake -B build")
run("cmake --build build -j")
test("./build/bench --json", attempts=5, min_passes=1, passed=lambda r: r.seconds < 4.2)
```

### 7.3 Introspection with `check`, plus a known-unbuildable range

```python
from bisectlib import run, test, check

out = check("make 2>&1").out
if "deprecated API" in out:
    run("python fix_deprecation.py")   # or replace(...) etc.
run("make", skip_on_error=True)        # this range never builds cleanly -> skip, don't abort
test("./run_tests")
```

---

## 8. Implementation notes / build order

1. **Core engine:** `run` / `test` / `check`, subprocess execution with capture + timing +
   timeout, the `Outcome` → exit-code mapping (`run` aborts on error by default; `test`
   ⇒ good/bad), the "exit immediately on conclusive result; end-of-script = GOOD" control
   flow.
2. **Clean-tree machinery:** at-exit restore registry + `fixup`/`replace` context
   managers; cleanup runs on `SystemExit` too.
3. **git helpers:** `sha`/`subject`/`is_clean`/`in_range`/`touches` (shell out to git).
4. **`replace`** with str/`re.Pattern` type dispatch + `if_missing`.
5. **`fixup`** (`patch=` / `cherry_pick=`) + `apply_fixups`.
6. **`bisectlib._report` (built-in renderer)** — *stateless, stdlib-only, read-only*.
   Parse `git bisect log`, run the reconstruction walk (anchors → rows → bounds),
   synthesize the in-flight `todo` row from `HEAD`, query git for dates/counts, emit the
   Markdown `status.md`. The engine calls it after every step; per-commit log dirs hold full
   command output (+ optional sidecar for timing/flaky detail). **Good candidate to build
   first** — it nails down the `git bisect log` parsing everything else relies on.
7. **Console echo** in color to stderr (auto tty/`NO_COLOR` detection).
8. **Dry-run mode:** outside a live bisect (no `refs/bisect/*`), `python recipe.py` still
   runs the steps against HEAD and prints the verdict, gracefully skipping the range
   columns — so recipes can be iterated on without starting a bisect.
9. Tests: a fixture git repo with a planted regression; assert the recipe drives bisect
   to the right commit, that SKIP/ABORT behave, and that the tree is always clean between
   commits.

### Packaging
- Standalone repo / pip package, **stdlib only** — no runtime dependencies.
- One package: **`bisectlib`** (the recipe engine) with a built-in `bisectlib._report`
  renderer module. Ships a `py.typed` marker so installed usage is fully typed.
- Python 3.10+ (uses `re.Pattern`, `match`/`Enum`, etc.).

---

## 9. Open decisions still to confirm

- **Implicit-exit vs return-and-decide.** Current design: `run`/`test` exit the process on
  a conclusive result (no return, no ctx — clean recipes). Tradeoff: implicit control flow;
  you can't inspect a step's output afterward (that's what `check` is for). The explicit
  alternative is `v = test(...); decide(v)` at the bottom. **Leaning implicit.**
- **Benchmark baseline:** absolute thresholds in the `passed` predicate now vs.
  auto-calibrated relative-to-good-anchor later.
- **Result caching:** cache outcome keyed by commit SHA + recipe hash so re-runs/revisits
  are instant (`--no-cache` to bypass)? **Leaning yes.**

> **Resolved during design:** the verb split is `run` (infrastructure; **aborts** on error
> by default, `skip_on_error=True` to skip) + `test` (the verdict) + `check` (never
> decides). The earlier `run(fail=...)` single-verb knob is dropped.
```