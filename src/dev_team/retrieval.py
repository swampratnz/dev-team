"""Deterministic lexical retrieval over a workspace (ROADMAP #4).

The repo map (:mod:`dev_team.context`) lists *paths*; on a large repo that
leaves the architect and the described-mode engineer designing against names,
not code. :func:`retrieve` ranks the workspace's files by lexical relevance to a
query (the feature/task text) and returns the most relevant ones with bounded
excerpts, so the *right* existing code can be put in front of a role within a
size budget.

Like the repo map, it is deliberately deterministic — a BM25 score over
tokenised file content, with filename and defined-symbol matches weighted up —
so it needs no embedding provider, makes no network call, costs nothing, and
can be tested exactly. Ties break by path, so the ranking is stable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Sequence

from .context import path_excluded
from .execution import Workspace

# BM25 parameters (the standard defaults).
_K1 = 1.5
_B = 0.75

#: Extra weight (as repeated occurrences) given to tokens from the file path and
#: from defined-symbol names — a query term matching a filename or a ``def``/
#: ``class`` name is a stronger signal than one buried in a comment.
_PATH_WEIGHT = 2
_SYMBOL_WEIGHT = 3

#: Content beyond this is ignored for scoring — bounds the cost of one huge file
#: without letting it dominate the corpus statistics.
_MAX_SCAN_CHARS = 50_000

DEFAULT_MAX_FILES = 8
DEFAULT_CHAR_BUDGET = 12_000
DEFAULT_PER_FILE_CHARS = 3_000

#: Suffix marking a truncated excerpt; counted against the budget so the total
#: never exceeds ``char_budget``.
_MARKER = "\n... (truncated)"

#: Rough average characters per token for code and prose. Enough to budget
#: prompt context without pulling in a tokenizer dependency (there is none) or
#: making a network call — deliberately an estimate, matching this module's
#: deterministic, dependency-free stance.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """A cheap, deterministic token-count estimate for budgeting/reporting."""

    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def char_budget_for_tokens(tokens: int) -> int:
    """The char budget a ``tokens`` budget maps to, for :func:`retrieve`."""

    return max(0, tokens) * _CHARS_PER_TOKEN

_WORD = re.compile(r"[a-z0-9]+")
# Split an identifier at camelCase humps so "buildRepoContext" also yields
# build / repo / context; underscores and other punctuation fall to _WORD.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SYMBOL = re.compile(
    r"\b(?:def|class|func|function|const|let|var|type|struct|interface|enum)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)"
)
# Very common words carry no retrieval signal; dropping them keeps a generic
# task description ("add a function that returns ...") from matching everything.
_STOPWORDS = frozenset(
    "the and for with that this from into use uses add adds new get set "
    "return returns when then than have has all any not are was were will "
    "should must can may via per".split()
)

#: Closing fence of the block a rendered excerpt sits in; neutralised in
#: untrusted content so a file body (or a hostile filename) cannot close it early.
_FENCE = "</file-content>"


def _tokenize(text: str) -> List[str]:
    """Lowercased word/identifier tokens, camelCase-split, stopwords dropped."""

    spaced = _CAMEL.sub(" ", text)
    return [t for t in _WORD.findall(spaced.lower()) if len(t) >= 2 and t not in _STOPWORDS]


def _file_terms(path: str, content: str) -> List[str]:
    """The token bag for a file, with path and symbol names up-weighted."""

    terms = _tokenize(content)
    terms += _tokenize(path) * _PATH_WEIGHT
    for match in _SYMBOL.finditer(content):
        terms += _tokenize(match.group(1)) * _SYMBOL_WEIGHT
    return terms


def _defuse(text: str) -> str:
    """Neutralise the ``</file-content>`` fence in untrusted text (zero-width space)."""

    return text.replace(_FENCE, "<​" + _FENCE[1:])


@dataclass(frozen=True)
class RetrievedFile:
    """One relevant file: its path, BM25 score, and a bounded excerpt."""

    path: str
    score: float
    excerpt: str


@dataclass
class Retrieval:
    """The ranked, budget-bounded result of a :func:`retrieve` call."""

    files: List[RetrievedFile] = field(default_factory=list)
    considered: int = 0  # how many files were scored (for an honest "N of M" note)

    @property
    def is_empty(self) -> bool:
        return not self.files

    def render(self) -> str:
        """Render the retrieved excerpts as a prompt block (empty if none).

        Each excerpt is fenced as ``<file-content>`` (declared untrusted in the
        agents' system-prompt note) and defused so a file body or filename
        cannot break out of the block.
        """

        if not self.files:
            return ""
        lines = [f"Most relevant existing files ({len(self.files)} of {self.considered} scored):"]
        for item in self.files:
            lines.append(
                f'\n<file-content path="{_defuse(item.path)}">\n'
                f"{_defuse(item.excerpt)}\n</file-content>"
            )
        return "\n".join(lines)


def retrieve(
    workspace: Workspace,
    query: str,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    char_budget: int = DEFAULT_CHAR_BUDGET,
    per_file_chars: int = DEFAULT_PER_FILE_CHARS,
    exclude_globs: Sequence[str] = (),
) -> Retrieval:
    """Return the files in ``workspace`` most relevant to ``query``.

    Files are scored with BM25 over their tokenised content (filename and
    defined-symbol tokens up-weighted), ranked, and the top ``max_files`` are
    returned with head excerpts capped at ``per_file_chars`` and a running
    ``char_budget`` across all of them. ``.dev_team/`` and ``exclude_globs`` are
    skipped, as are unreadable/binary files. An empty query, or a corpus with no
    term overlap, yields an empty result.
    """

    candidates = []
    for path in workspace.list_files():
        if path.startswith(".dev_team/") or path_excluded(path, exclude_globs):
            continue
        try:
            content = workspace.read_text(path)
        except (UnicodeDecodeError, OSError, ValueError):
            continue  # non-UTF-8 / binary / unreadable — skip, like build_repo_context
        candidates.append((path, content[:_MAX_SCAN_CHARS]))

    query_terms = set(_tokenize(query))
    if not query_terms or not candidates:
        return Retrieval(files=[], considered=len(candidates))

    docs = []
    doc_freq: Counter = Counter()
    for path, content in candidates:
        counts = Counter(_file_terms(path, content))
        docs.append((path, content, counts, sum(counts.values())))
        doc_freq.update(counts.keys())
    total = len(docs)
    avglen = sum(length for _, _, _, length in docs) / total or 1.0

    scored = []
    for path, content, counts, length in docs:
        score = 0.0
        for term in query_terms:
            freq = counts.get(term, 0)
            if not freq:
                continue
            idf = math.log(1 + (total - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            score += idf * (freq * (_K1 + 1)) / (
                freq + _K1 * (1 - _B + _B * length / avglen)
            )
        if score > 0:
            scored.append((score, path, content))
    scored.sort(key=lambda item: (-item[0], item[1]))  # deterministic: score, then path

    files: List[RetrievedFile] = []
    used = 0
    for score, path, content in scored[:max_files]:
        room = min(per_file_chars, char_budget - used)
        if room <= 0:
            break
        if len(content) <= room:
            excerpt = content
        elif len(_MARKER) < room:
            excerpt = content[: room - len(_MARKER)] + _MARKER
        else:
            excerpt = content[:room]  # room too small even for the marker: hard cut
        files.append(RetrievedFile(path=path, score=round(score, 4), excerpt=excerpt))
        used += len(excerpt)  # excerpt is bounded by room, so used never exceeds char_budget
    return Retrieval(files=files, considered=total)
