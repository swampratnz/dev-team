"""Robust JSON extraction from language-model output.

Models often wrap JSON in prose or Markdown code fences, and agentic
transcripts frequently narrate (including prose fragments like ``[1]``)
before the final answer. These helpers therefore prefer the LAST well-formed
JSON object in the text, falling back to the last array; bare scalars never
qualify.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import JSONExtractionError

_OPENERS = {"{": "}", "[": "]"}

_MISSING = object()


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


def extract_json(text: str) -> Any:
    """Extract and parse the best JSON value found in ``text``.

    Scans for top-level balanced JSON values (skipping past each parsed value
    so nested objects are not considered on their own) and returns the last
    parseable object, or — when no object is present — the last parseable
    array.

    Raises:
        JSONExtractionError: If no valid JSON object or array is present.
    """

    last_object: Any = _MISSING
    last_array: Any = _MISSING
    index = 0
    while index < len(text):
        if text[index] in _OPENERS:
            end = _find_balanced(text, index)
            if end != -1:
                try:
                    value = json.loads(text[index:end])
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(value, dict):
                        last_object = value
                    else:
                        last_array = value
                    index = end
                    continue
        index += 1
    if last_object is not _MISSING:
        return last_object
    if last_array is not _MISSING:
        return last_array
    raise JSONExtractionError(text)
