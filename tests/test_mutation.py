"""Tests for the mutation-lite AST helper (issues #176, #197)."""

from __future__ import annotations

from dev_team.mutation import mutate_first_mutant


def test_flips_eq_to_noteq():
    mutated = mutate_first_mutant("def f(a, b):\n    return a == b\n")
    assert mutated is not None
    assert "a != b" in mutated
    assert "a == b" not in mutated


def test_flips_noteq_to_eq():
    mutated = mutate_first_mutant("def f(a, b):\n    return a != b\n")
    assert mutated is not None
    assert "a == b" in mutated


def test_flips_lt_to_gte():
    mutated = mutate_first_mutant("def f(a, b):\n    return a < b\n")
    assert mutated is not None
    assert "a >= b" in mutated


def test_flips_gte_to_lt():
    mutated = mutate_first_mutant("def f(a, b):\n    return a >= b\n")
    assert mutated is not None
    assert "a < b" in mutated


def test_flips_gt_to_lte():
    mutated = mutate_first_mutant("def f(a, b):\n    return a > b\n")
    assert mutated is not None
    assert "a <= b" in mutated


def test_flips_lte_to_gt():
    mutated = mutate_first_mutant("def f(a, b):\n    return a <= b\n")
    assert mutated is not None
    assert "a > b" in mutated


def test_returns_none_for_no_comparison():
    assert mutate_first_mutant("def f(a, b):\n    return a + b\n") is None


def test_returns_none_for_unparseable_source():
    assert mutate_first_mutant("def f(:\n    broken syntax here\n") is None


def test_deterministic_across_repeated_calls():
    source = "def f(a, b):\n    return a == b\n"
    first = mutate_first_mutant(source)
    second = mutate_first_mutant(source)
    assert first is not None
    assert first == second


def test_picks_first_comparison_in_source_order():
    source = (
        "def f(a, b, c, d):\n"
        "    if a == b:\n"
        "        return True\n"
        "    return c == d\n"
    )
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a != b" in mutated
    assert "c == d" in mutated


def test_skips_chained_comparison_with_no_other_candidate():
    # `a < b < c` has two ops on one Compare node — not a supported single-op
    # flip site, so it must be skipped rather than mutated incorrectly.
    assert mutate_first_mutant("def f(a, b, c):\n    return a < b < c\n") is None


def test_skips_unsupported_operator_with_no_other_candidate():
    # `is`/`in` are outside the supported flip set (v1 is comparison-flips
    # only); a source with only such comparisons has no mutable site.
    assert mutate_first_mutant("def f(a, b):\n    return a is b\n") is None


def test_skips_chained_comparison_and_mutates_the_next_candidate():
    source = (
        "def f(a, b, c, d):\n"
        "    if a < b < c:\n"
        "        return True\n"
        "    return c == d\n"
    )
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a < b < c" in mutated
    assert "c != d" in mutated


# --- boolean-operator flips (issue #197) ---------------------------------


def test_flips_and_to_or():
    mutated = mutate_first_mutant("def f(a, b):\n    return a and b\n")
    assert mutated is not None
    assert "a or b" in mutated
    assert "a and b" not in mutated


def test_flips_or_to_and():
    mutated = mutate_first_mutant("def f(a, b):\n    return a or b\n")
    assert mutated is not None
    assert "a and b" in mutated
    assert "a or b" not in mutated


def test_comparison_before_boolop_wins():
    source = "def f(a, b, c):\n    if a == b:\n        return a and c\n    return False\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a != b" in mutated
    assert "a and c" in mutated


def test_boolop_before_comparison_wins():
    source = "def f(a, b, c):\n    if a and b:\n        return a == c\n    return False\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a or b" in mutated
    assert "a == c" in mutated


def test_returns_none_for_no_comparison_or_boolop():
    assert mutate_first_mutant("def f(a, b):\n    return a + b\n") is None


def test_boolop_only_file_now_returns_a_mutant():
    # Pre-#197 behaviour: this file has no comparison, so v1 returned None.
    source = "def f(a, b):\n    if a and b:\n        return 1\n    return 0\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "if a or b" in mutated


def test_chained_comparison_still_excluded_with_boolop_present():
    assert mutate_first_mutant("def f(a, b, c):\n    return a < b < c\n") is None


def test_unsupported_comparison_operator_still_excluded_with_boolop_present():
    assert mutate_first_mutant("def f(a, b):\n    return a is b\n") is None


def test_nested_mixed_and_or_flips_the_earliest_node():
    # `a and b or c` parses as BoolOp(Or, [BoolOp(And, [a, b]), c]) — both
    # nodes share the same (lineno, col_offset) since both start at `a`; the
    # outer node is discovered first by `ast.walk` and wins the tie.
    source = "def f(a, b, c):\n    return a and b or c\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "(a and b) and c" in mutated


def test_deterministic_across_repeated_calls_for_boolop():
    source = "def f(a, b):\n    return a and b\n"
    first = mutate_first_mutant(source)
    second = mutate_first_mutant(source)
    assert first is not None
    assert first == second
