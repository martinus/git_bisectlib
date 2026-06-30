#!/usr/bin/env python3
"""Example bisectlib recipe: a performance regression with a one-line build fix.

Finds the commit where the benchmark's median runtime crossed a threshold.
Run with:  git bisect start <BAD> <GOOD> && git bisect run python examples/perf_regression.py
"""
from bisectlib import run, test, replace

# A trivial build fix needed across the whole range (auto-reverted afterwards).
replace("CMakeLists.txt", "c++14", "c++17")

run("cmake -B build -DCMAKE_BUILD_TYPE=Release")
run("cmake --build build -j")

# Verdict by performance: 7 samples (2 warmup), bad if the median exceeds 4.2s.
test("./build/bench --run", attempts=7, warmup=2, max_median=4.2)
