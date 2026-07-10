"""Robust JSON extraction from language-model output.

Models often wrap JSON in prose or Markdown code fences. These helpers pull the
first well-formed JSON object or array out of such text.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import JSONExtractionError

_OPENERS = {"{": "}", "[": "]"}


def _find_balanced(text: str, start: int) -> int:
    """Return the index just past the JSON value that begins at ``start``.

    ``text[start]`` must be ``{`` or ``[``. Returns ``-1`` if the value is not
    balanced (i.e. the closing bracket is missing).
    """

    opener = text[start]
    closer = _OPENERS[opener]
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _iter_candidates(text: str) -> Any:
    """Yield candidate JSON substrings from ``text`` in priority order."""

    stripped = text.strip()
    # 1. The whole thing might already be valid JSON.
    yield stripped
    # 2. Any balanced object/array starting at each opener character.
    for index, char in enumerate(text):
        if char in _OPENERS:
            end = _find_balanced(text, index)
            if end != -1:
                yield text[index:end]


def extract_json(text: str) -> Any:
    """Extract and parse the first valid JSON value found in ``text``.

    Raises:
        JSONExtractionError: If no valid JSON object or array is present.
    """

    for candidate in _iter_candidates(text):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise JSONExtractionError(text)
