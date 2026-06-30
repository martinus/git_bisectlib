#!/usr/bin/env python3
"""Example bisectlib recipe: a flaky functional regression with a range fixup.

Run with:  git bisect start <BAD> <GOOD> && git bisect run python examples/flaky_with_fixup.py
"""
from bisectlib import run, test, fixup, in_range

# Commits in this range fail to build without a small patch; apply it just there.
with fixup("patches/missing-header.patch", when=in_range("abc123..def456")):
    run("cmake -B build")              # infra: a broken configure ABORTS (go fix it)
    run("cmake --build build -j")      # infra: a broken build ABORTS

# The verdict: the test is flaky, so require 2 passes out of 5 to call it good.
test("ctest --test-dir build -R regression", attempts=5, min_passes=2)
# reaching the end == GOOD
