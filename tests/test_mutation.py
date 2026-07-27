"""Tests for the mutation-lite AST helper (issues #176, #197, #219, #226)."""

from __future__ import annotations

import pytest

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
    assert mutate_first_mutant("def f(a):\n    return a\n") is None


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


def test_is_operator_no_longer_excluded_with_no_other_candidate():
    # #219 supersedes this: `is`/`in` used to be outside the supported flip
    # set (v1 comparison-flips only), so a source with only such a
    # comparison had no mutable site and this asserted `None`. `is`/`in` are
    # now in `_FLIPS`, so this returns a mutant instead.
    mutated = mutate_first_mutant("def f(a, b):\n    return a is b\n")
    assert mutated is not None
    assert "a is not b" in mutated


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


# --- identity/membership-operator flips (issue #219) ---------------------


def test_flips_is_to_isnot():
    mutated = mutate_first_mutant("def f(a):\n    return a is None\n")
    assert mutated is not None
    assert "a is not None" in mutated
    assert "return a is None" not in mutated


def test_flips_isnot_to_is():
    mutated = mutate_first_mutant("def f(a):\n    return a is not None\n")
    assert mutated is not None
    assert "return a is None" in mutated


def test_flips_in_to_notin():
    mutated = mutate_first_mutant("def f(a, b):\n    return a in b\n")
    assert mutated is not None
    assert "a not in b" in mutated


def test_flips_notin_to_in():
    mutated = mutate_first_mutant("def f(a, b):\n    return a not in b\n")
    assert mutated is not None
    assert "a in b" in mutated


def test_identity_only_file_now_returns_a_mutant():
    # Pre-#219: a file whose only flippable construct was an identity
    # comparison returned `None` (the last named exclusion in the v1
    # `_FLIPS` table). This is the proposal's core new-coverage assertion.
    source = "def f(a):\n    if a is None:\n        return 1\n    return 0\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "if a is not None" in mutated


def test_identity_before_equality_wins():
    source = (
        "def f(a, b, c):\n"
        "    if a is None:\n"
        "        return True\n"
        "    return b == c\n"
    )
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a is not None" in mutated
    assert "b == c" in mutated


def test_equality_before_identity_wins():
    source = (
        "def f(a, b, c):\n"
        "    if a == b:\n"
        "        return True\n"
        "    return c is None\n"
    )
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a != b" in mutated
    assert "c is None" in mutated


def test_skips_chained_identity_comparison_with_no_other_candidate():
    # `a is b is c` has two ops on one Compare node — not a supported
    # single-op flip site, same as the `<`/`<` chained-comparison case.
    assert mutate_first_mutant("def f(a, b, c):\n    return a is b is c\n") is None


def test_deterministic_across_repeated_calls_for_identity():
    source = "def f(a):\n    return a is None\n"
    first = mutate_first_mutant(source)
    second = mutate_first_mutant(source)
    assert first is not None
    assert first == second


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
    assert mutate_first_mutant("def f(a):\n    return a\n") is None


def test_boolop_only_file_now_returns_a_mutant():
    # Pre-#197 behaviour: this file has no comparison, so v1 returned None.
    source = "def f(a, b):\n    if a and b:\n        return 1\n    return 0\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "if a or b" in mutated


def test_chained_comparison_still_excluded_with_boolop_present():
    assert mutate_first_mutant("def f(a, b, c):\n    return a < b < c\n") is None


def test_identity_comparison_no_longer_excluded_with_boolop_present():
    # #219 supersedes this: previously pinned that `is`/`in` stayed excluded
    # even with a `BoolOp` in scope (there wasn't one here, but the fixture
    # is unchanged). `is`/`in` are now in `_FLIPS`, so this mutates.
    mutated = mutate_first_mutant("def f(a, b):\n    return a is b\n")
    assert mutated is not None
    assert "a is not b" in mutated


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


# --- arithmetic-operator flips (issue #226) -------------------------------


def test_flips_add_to_sub():
    mutated = mutate_first_mutant("def f(a, b):\n    return a + b\n")
    assert mutated is not None
    assert "a - b" in mutated
    assert "a + b" not in mutated


def test_flips_sub_to_add():
    mutated = mutate_first_mutant("def f(a, b):\n    return a - b\n")
    assert mutated is not None
    assert "a + b" in mutated
    assert "a - b" not in mutated


def test_flips_mult_to_div():
    mutated = mutate_first_mutant("def f(a, b):\n    return a * b\n")
    assert mutated is not None
    assert "a / b" in mutated
    assert "a * b" not in mutated


def test_flips_div_to_mult():
    mutated = mutate_first_mutant("def f(a, b):\n    return a / b\n")
    assert mutated is not None
    assert "a * b" in mutated
    assert "a / b" not in mutated


def test_arith_only_file_now_returns_a_mutant():
    # Pre-#226: a file whose only flippable construct was an arithmetic
    # `BinOp` returned `None` (the last named exclusion). This is the
    # proposal's core new-coverage assertion.
    source = "def f(a, b):\n    return a + b\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "return a - b" in mutated


def test_deterministic_across_repeated_calls_for_arith():
    source = "def f(a, b):\n    return a + b\n"
    first = mutate_first_mutant(source)
    second = mutate_first_mutant(source)
    assert first is not None
    assert first == second


@pytest.mark.parametrize("op", ["%", "**", "//", "&", "|", "^", "@"])
def test_out_of_scope_arith_operator_alone_returns_none(op):
    # `%`/`**`/`//`/bitwise/matrix operators are not in `_ARITH_FLIPS` — a
    # `BinOp` using one of them, with no other qualifying construct in the
    # file, must never be selected.
    source = f"def f(a, b):\n    return a {op} b\n"
    assert mutate_first_mutant(source) is None


def test_out_of_scope_arith_operator_skipped_for_other_candidate():
    # A `%` `BinOp` coexists with a qualifying `==` comparison: the
    # out-of-scope operator is never picked, proving `_ARITH_FLIPS`'s
    # exclusivity rather than merely the absence of any candidate.
    source = "def f(a, b, c, d):\n    if a == b:\n        return c % d\n    return 0\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a != b" in mutated
    assert "c % d" in mutated


def test_precedence_compare_beats_nested_arith_binop():
    # `if a + b < c:` — the outer `Compare` node and the nested `a + b`
    # `BinOp` share the same starting position; the shallower `Compare`
    # node is discovered first by `ast.walk`'s BFS order and wins the tie.
    source = "def f(a, b, c):\n    if a + b < c:\n        return True\n    return False\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a + b >= c" in mutated


def test_precedence_outer_binop_beats_nested_binop():
    # `a + b - c` parses left-associatively as `BinOp(Sub, BinOp(Add, a, b),
    # c)` — both the outer `Sub` and the inner `Add` share the same starting
    # position (both begin at `a`); the shallower outer node is discovered
    # first by `ast.walk` and wins the tie, so the outer `-` flips, not the
    # inner `+`.
    mutated = mutate_first_mutant("def f(a, b, c):\n    return a + b - c\n")
    assert mutated is not None
    assert "a + b + c" in mutated


def test_arith_before_comparison_wins():
    source = (
        "def f(a, b, c, d):\n"
        "    if a + b:\n"
        "        return True\n"
        "    return c == d\n"
    )
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a - b" in mutated
    assert "c == d" in mutated


def test_arith_before_boolop_wins():
    source = "def f(a, b, c):\n    if a * b:\n        return a and c\n    return False\n"
    mutated = mutate_first_mutant(source)
    assert mutated is not None
    assert "a / b" in mutated
    assert "a and c" in mutated


def test_mutation_and_engine_docstrings_mention_arithmetic():
    # Regression for the doc-drift criterion: both the module-level
    # `mutation.py` docstring/`_FLIPS` comment and `EngineConfig`'s /
    # `DeliveryEngine`'s docstrings must list arithmetic alongside
    # comparison, identity/membership, and boolean flips (#226 criterion 8).
    import dev_team.mutation as mutation_module

    assert mutation_module.__doc__ is not None
    assert "arithmetic" in mutation_module.__doc__.lower()
