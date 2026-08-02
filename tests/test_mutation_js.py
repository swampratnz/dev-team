"""Tests for the JS/TS mutation-lite scanner (issue #302)."""

from __future__ import annotations

from dev_team.mutation_js import mutate_first_mutant_js


# --- acceptance criteria 1-2: flips the first strict-equality operator ----


def test_flips_strict_eq_to_strict_noteq():
    mutated = mutate_first_mutant_js("if (a === b) { return 1; }")
    assert mutated == "if (a !== b) { return 1; }"


def test_flips_strict_noteq_to_strict_eq():
    mutated = mutate_first_mutant_js("if (a !== b) { return 1; }")
    assert mutated == "if (a === b) { return 1; }"


def test_picks_first_operator_in_source_order():
    source = "function f(a, b, c, d) {\n  if (a === b) {}\n  return c === d;\n}\n"
    mutated = mutate_first_mutant_js(source)
    assert mutated is not None
    assert "a !== b" in mutated
    assert "c === d" in mutated


# --- acceptance criterion 3: string/comment content is never mutated ------


def test_returns_none_for_operator_only_in_single_quoted_string():
    assert mutate_first_mutant_js("'a === b'\n") is None


def test_returns_none_for_operator_only_in_double_quoted_string():
    assert mutate_first_mutant_js('"a === b"\n') is None


def test_returns_none_for_operator_only_in_line_comment():
    assert mutate_first_mutant_js("// a === b\n") is None


def test_returns_none_for_operator_only_in_line_comment_at_end_of_input():
    # No trailing newline: the line-comment scan must terminate at end of
    # input, not just at a newline.
    assert mutate_first_mutant_js("// a === b") is None


def test_returns_none_for_operator_only_in_block_comment():
    assert mutate_first_mutant_js("/* a === b */\n") is None


# --- acceptance criterion 4: backslash-escaped quotes stay in-string ------


def test_returns_none_for_escaped_double_quote_inside_double_quoted_string():
    assert mutate_first_mutant_js('"a \\" === b"') is None


def test_returns_none_for_escaped_single_quote_inside_single_quoted_string():
    assert mutate_first_mutant_js("'a \\' === b'") is None


# --- acceptance criterion 5: unconditional backtick bail-out --------------


def test_returns_none_when_backtick_present_in_code_alongside_real_operator():
    source = "let t = `abc`;\nif (a === b) {}\n"
    assert mutate_first_mutant_js(source) is None


def test_returns_none_when_backtick_present_inside_line_comment():
    source = "// uses `template` literal below\nif (a === b) {}\n"
    assert mutate_first_mutant_js(source) is None


def test_returns_none_when_backtick_present_inside_block_comment():
    source = "/* `template` note */\nif (a === b) {}\n"
    assert mutate_first_mutant_js(source) is None


# --- acceptance criterion 6: never raises, None on malformed input --------


def test_returns_none_for_empty_string():
    assert mutate_first_mutant_js("") is None


def test_returns_none_for_whitespace_only_source():
    assert mutate_first_mutant_js("   \n\t  \n") is None


def test_returns_none_for_unterminated_single_quoted_string():
    assert mutate_first_mutant_js("'a === b") is None


def test_returns_none_for_unterminated_double_quoted_string():
    assert mutate_first_mutant_js('"a === b') is None


def test_returns_none_for_unterminated_block_comment():
    assert mutate_first_mutant_js("/* a === b") is None


# --- acceptance criterion 7: loose equality / bare `=` are never flipped --


def test_does_not_flip_loose_equality():
    assert mutate_first_mutant_js("if (a == b) {}") is None


def test_does_not_flip_loose_inequality():
    assert mutate_first_mutant_js("if (a != b) {}") is None


# --- acceptance criterion 9: security sweep --------------------------------


def test_security_sweep_never_mutates_string_comment_or_template_content():
    fixtures = [
        "'a === b'\n",
        '"a === b"\n',
        "// a === b\n",
        "/* a === b */\n",
        '"a \\" === b"',
        "'a \\' === b'",
        "let t = `abc`;\nif (a === b) {}\n",
        "// `template`\nif (a === b) {}\n",
        "/* `template` */\nif (a === b) {}\n",
    ]
    for source in fixtures:
        assert mutate_first_mutant_js(source) is None
