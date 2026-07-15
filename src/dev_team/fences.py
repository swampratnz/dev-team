"""Neutralise untrusted content that could forge a prompt fence.

Untrusted text — file bodies, diffs, tool and scanner output, model-origin
summaries — is shown to agents inside delimited ``<...>`` blocks that the system
prompt declares off-limits (see ``UNTRUSTED_CONTENT_NOTE``). A hostile string
containing the block's *own* closing tag could otherwise end the block early and
have whatever follows read as trusted instructions.

:func:`defuse` inserts a zero-width space into each named closing tag: identical
to a human reading it, but no longer a structural match. Apply it to the
untrusted content a site wraps, naming the fence(s) that content sits inside.
The content a site emits never legitimately contains its own wrapper's closing
tag (nested renders use different tags), so defusing is always safe — and
idempotent, since an already-defused string has no literal closing tag left.
"""

from __future__ import annotations

#: Zero-width space (U+200B): invisible to a human, breaks the tag match.
ZERO_WIDTH_SPACE = "\u200b"


def defuse(text: str, *tags: str) -> str:
    """Neutralise the closing tag ``</tag>`` for each ``tag`` in ``text``."""

    for tag in tags:
        closing = f"</{tag}>"
        text = text.replace(closing, "<" + ZERO_WIDTH_SPACE + closing[1:])
    return text
