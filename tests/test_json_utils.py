"""Tests for JSON extraction from model output."""

from __future__ import annotations

import pytest

from dev_team.errors import JSONExtractionError
from dev_team.json_utils import _find_balanced, extract_json


def test_extract_whole_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_whole_array():
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_from_prose():
    text = 'The plan is: {"summary": "x", "tasks": []} — enjoy!'
    assert extract_json(text) == {"summary": "x", "tasks": []}


def test_extract_from_code_fence():
    text = 'Sure:\n```json\n{"ok": true}\n```\n'
    assert extract_json(text) == {"ok": True}


def test_extract_ignores_braces_inside_strings():
    assert extract_json('{"a": "}{"}') == {"a": "}{"}


def test_extract_handles_escaped_quotes():
    assert extract_json('{"a": "he said \\"hi\\""}') == {"a": 'he said "hi"'}


def test_extract_skips_unbalanced_prefix():
    text = 'broken { not json here and then {"a": 1}'
    assert extract_json(text) == {"a": 1}


def test_extract_empty_raises():
    with pytest.raises(JSONExtractionError):
        extract_json("")


def test_extract_no_json_raises():
    with pytest.raises(JSONExtractionError):
        extract_json("just some prose without structure")


def test_find_balanced_unbalanced_returns_minus_one():
    text = "{ unclosed"
    assert _find_balanced(text, 0) == -1


def test_find_balanced_nested():
    text = '{"a": {"b": 1}}'
    assert _find_balanced(text, 0) == len(text)


def test_find_balanced_handles_escaped_quote_in_string():
    # A backslash-escaped quote inside a string must not end the string early.
    text = '{"a": "b\\"c"}'
    assert _find_balanced(text, 0) == len(text)
