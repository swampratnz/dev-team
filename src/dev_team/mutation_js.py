"""Mutation-lite: flip the first strict-equality operator in a JS/TS source file.

The JavaScript/TypeScript sibling of :mod:`dev_team.mutation` — extends the
same opt-in, advisory :attr:`~dev_team.engine.EngineConfig.mutation_check`
signal to the ``node`` profile (:doc:`../docs/BENCHMARKS.md`'s named "future
work" gap). A single flipped strict-equality operator (``===``/``!==``) that
still passes the existing suite is the same textbook "tests that pass but
don't assert" signature the Python mutator hunts for.

Python's stdlib has no JS/TS parser, so this is a minimal hand-rolled
character scanner rather than an AST walk — pure, dependency-free, no
subprocess, no network, no model call, never raises. It tracks just enough
lexical state (single- and double-quoted strings with backslash-escaped
quotes, ``//`` line comments, ``/* */`` block comments) to flip only an
operator that sits in code, never inside string or comment content. Template
literals are the one case this scanner does not attempt to track precisely
(backtick interpolation can nest arbitrarily), so it bails out unconditionally
— ``None``, no mutation attempted — the moment a backtick appears anywhere in
the source, in or out of a string/comment.

This module never mutates anything on disk itself; the caller
(:meth:`dev_team.engine.DeliveryEngine._mutation_check`) is responsible for
writing the mutated source to a real file, evaluating gates, and restoring
the original content.
"""

from __future__ import annotations

from typing import Optional


def _skip_string(source: str, start: int, quote: str) -> Optional[int]:
    """Index just past the closing ``quote`` starting the scan at ``start + 1``.

    ``start`` is the index of the opening quote character. A backslash
    always escapes the next character (so an escaped quote never closes the
    string). Returns ``None`` — never raises — if the end of ``source`` is
    reached with the string still open.
    """

    n = len(source)
    j = start + 1
    while j < n:
        ch = source[j]
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            return j + 1
        j += 1
    return None


def mutate_first_mutant_js(source: str) -> Optional[str]:
    """Flip the first code-state ``===``/``!==`` in ``source``.

    Scans left to right tracking single-quoted strings, double-quoted
    strings (both with backslash-escape awareness), ``//`` line comments,
    and ``/* */`` block comments, so an operator-looking substring inside any
    of those is never mistaken for code. Flips the first ``===`` found to
    ``!==``, or the first ``!==`` to ``===``, whichever occurs first in
    source order; never matches loose equality (``==``/``!=``) or bare
    ``=``.

    Returns ``None`` — a silent skip, never an error — when: the source
    contains a backtick anywhere (template literals are not tracked
    precisely enough to mutate safely, so this bails out unconditionally,
    even if the backtick sits inside a comment or a genuine code-state
    operator exists elsewhere); a single- or double-quoted string, or a
    ``/* */`` block comment, is left unterminated at end of input; or no
    code-state ``===``/``!==`` exists at all. This mirrors
    :func:`dev_team.mutation.mutate_first_mutant`'s "no candidate is the
    common case, never a failure" contract.
    """

    if "`" in source:
        return None

    n = len(source)
    i = 0
    while i < n:
        ch = source[i]
        if ch == "'" or ch == '"':
            nxt = _skip_string(source, i, ch)
            if nxt is None:
                return None
            i = nxt
            continue
        if source[i : i + 2] == "//":
            j = source.find("\n", i)
            i = n if j == -1 else j
            continue
        if source[i : i + 2] == "/*":
            j = source.find("*/", i + 2)
            if j == -1:
                return None
            i = j + 2
            continue
        if source[i : i + 3] == "===":
            return source[:i] + "!==" + source[i + 3 :]
        if source[i : i + 3] == "!==":
            return source[:i] + "===" + source[i + 3 :]
        i += 1
    return None
