"""Tests for the mutation-lite AST helper (issue #176)."""

from __future__ import annotations

from dev_team.mutation import mutate_first_comparison


def test_flips_eq_to_noteq():
    mutated = mutate_first_comparison("def f(a, b):\n    return a == b\n")
    assert mutated is not None
    assert "a != b" in mutated
    assert "a == b" not in mutated


def test_flips_noteq_to_eq():
    mutated = mutate_first_comparison("def f(a, b):\n    return a != b\n")
    assert mutated is not None
    assert "a == b" in mutated


def test_flips_lt_to_gte():
    mutated = mutate_first_comparison("def f(a, b):\n    return a < b\n")
    assert mutated is not None
    assert "a >= b" in mutated


def test_flips_gte_to_lt():
    mutated = mutate_first_comparison("def f(a, b):\n    return a >= b\n")
    assert mutated is not None
    assert "a < b" in mutated


def test_flips_gt_to_lte():
    mutated = mutate_first_comparison("def f(a, b):\n    return a > b\n")
    assert mutated is not None
    assert "a <= b" in mutated


def test_flips_lte_to_gt():
    mutated = mutate_first_comparison("def f(a, b):\n    return a <= b\n")
    assert mutated is not None
    assert "a > b" in mutated


def test_returns_none_for_no_comparison():
    assert mutate_first_comparison("def f(a, b):\n    return a + b\n") is None


def test_returns_none_for_unparseable_source():
    assert mutate_first_comparison("def f(:\n    broken syntax here\n") is None


def test_deterministic_across_repeated_calls():
    source = "def f(a, b):\n    return a == b\n"
    first = mutate_first_comparison(source)
    second = mutate_first_comparison(source)
    assert first is not None
    assert first == second


def test_picks_first_comparison_in_source_order():
    source = (
        "def f(a, b, c, d):\n"
        "    if a == b:\n"
        "        return True\n"
        "    return c == d\n"
    )
    mutated = mutate_first_comparison(source)
    assert mutated is not None
    assert "a != b" in mutated
    assert "c == d" in mutated


def test_skips_chained_comparison_with_no_other_candidate():
    # `a < b < c` has two ops on one Compare node — not a supported single-op
    # flip site, so it must be skipped rather than mutated incorrectly.
    assert mutate_first_comparison("def f(a, b, c):\n    return a < b < c\n") is None


def test_skips_unsupported_operator_with_no_other_candidate():
    # `is`/`in` are outside the supported flip set (v1 is comparison-flips
    # only); a source with only such comparisons has no mutable site.
    assert mutate_first_comparison("def f(a, b):\n    return a is b\n") is None


def test_skips_chained_comparison_and_mutates_the_next_candidate():
    source = (
        "def f(a, b, c, d):\n"
        "    if a < b < c:\n"
        "        return True\n"
        "    return c == d\n"
    )
    mutated = mutate_first_comparison(source)
    assert mutated is not None
    assert "a < b < c" in mutated
    assert "c != d" in mutated
