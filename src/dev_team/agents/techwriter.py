"""The technical writer agent: user docs, API reference, release notes.

Docs are a shipped artifact, not a report: the writer sees the actual changed
code and produces documentation *files* that are written into the workspace
and committed with the feature. Claims must be grounded in the code shown —
documentation that describes imagined behaviour is worse than none.
"""

from __future__ import annotations

import ast
import re
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from .. import parsing
from ..models import Design, Documentation, FeatureRequest, FileChange, Implementation
from .base import UNTRUSTED_CONTENT_NOTE, BaseAgent
from .reviewer import render_changed_files

_SYSTEM = """\
You are a technical writer. You produce clear, accurate documentation for a
delivered feature: an overview, usage/API notes, and release notes. Write for
real users. Every claim and every code example must be grounded in the actual
code you are shown — never document behaviour you cannot point to. Prefer
updating existing documentation files over creating parallel ones.
Always respond with a single JSON object and nothing else."""


class TechnicalWriterAgent(BaseAgent):
    """Produces :class:`Documentation` plus doc files for a feature."""

    role = "technical-writer"
    stage = "documentation"
    system_prompt = _SYSTEM + UNTRUSTED_CONTENT_NOTE

    async def write_docs(
        self,
        request: FeatureRequest,
        design: Design,
        implementation: Implementation,
        *,
        file_contents: Optional[Mapping[str, str]] = None,
        existing_docs: Optional[Sequence[str]] = None,
    ) -> Tuple[Documentation, Implementation]:
        """Write documentation files for the delivered change.

        Returns the structured :class:`Documentation` summary plus an
        :class:`Implementation` whose files are the actual documentation to
        materialise into the workspace (e.g. a README section, CHANGELOG
        entry, or docs page).
        """

        files = render_changed_files(implementation, file_contents)
        docs_listing = "\n".join(f"- {d}" for d in (existing_docs or [])) or "- (none)"
        prompt = f"""\
Write documentation for this delivered feature, grounded in the actual code
below.

Title: {request.title}
Description:
{request.description}
Design overview: {design.overview}

Delivered code (document what this actually does):
{files}

Existing documentation files in the workspace:
{docs_listing}

Respond with JSON of the form:
{{
  "summary": "what the docs cover",
  "sections": [{{"title": "Overview", "content": "..."}}],
  "files": [
    {{"path": "docs/<feature>.md", "change_type": "create", "summary": "...", "content": "full file content"}}
  ]
}}"""
        data = await self.ask_json(prompt)
        documentation = parsing.documentation_from_dict(data)
        doc_files = parsing.implementation_from_dict(data, "DOCS")
        return documentation, doc_files


#: Chars a bare relative path may contain — no whitespace, commas, or quoting.
#: Mirrors ``assessment._BARE_PATH_CHARS_RE`` exactly: the same conservative
#: shape, kept local here to avoid a circular import (assessment.py already
#: imports the agents package).
_BARE_PATH_CHARS_RE = re.compile(r"^[\w.\-/]+$")

#: Punctuation/markdown wrappers a citation token may be embedded in
#: (backticks, brackets, quotes, list/table markers) — none of these chars
#: are valid inside a bare path per ``_BARE_PATH_CHARS_RE``, so splitting on
#: them can never truncate a real path.
_TOKEN_SPLIT_RE = re.compile(r"""[\s,;'"`*|{}()\[\]<>]+""")

#: A fenced code block: ```` ```lang\n...content...\n``` ````, both fences
#: alone on their own line. An unterminated fence (no closing ``` ``) simply
#: fails to match, so malformed input is skipped rather than raising.
_FENCE_RE = re.compile(r"^```([\w+-]*)[ \t]*\r?\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)

#: Shell-ish fence language tags whose lines get scanned for CLI invocations.
_SHELL_FENCE_LANGS = frozenset({"bash", "sh", "shell", "console", "zsh"})

#: How a documented shell line must start (as a whole leading token) to be
#: treated as invoking this project's own CLI.
_CLI_INVOCATION_PREFIXES = ("dev-team", "python -m dev_team", "python3 -m dev_team")

#: Long-flag tokens only — a bare ``--`` followed by word/hyphen characters.
#: For ``--flag=value`` this naturally matches just ``--flag``, since ``=``
#: is not in the character class.
_LONG_FLAG_RE = re.compile(r"--[\w-]+")


def _looks_like_bare_path(s: str) -> bool:
    """True only for strings that read as an unambiguous bare relative path.

    Deliberately conservative, mirroring ``assessment._looks_like_bare_path``:
    prose evidence and URLs are left alone rather than risk false positives.
    """

    if not s or "://" in s or not _BARE_PATH_CHARS_RE.match(s):
        return False
    return "." in s or "/" in s


def _strip_locator(s: str) -> str:
    """Strip a trailing ``:123`` / ``#L123`` line-anchor before the existence check."""

    return re.sub(r"(:\d+|#L\d+)$", "", s)


def _strip_sentence_punctuation(s: str) -> str:
    """Drop a trailing ``.``/``:`` left over from prose (e.g. end of sentence).

    Applied after :func:`_strip_locator`, so a genuine ``path.py:123`` anchor
    is already gone by this point — this only clears plain sentence-final
    punctuation a path citation happens to end a sentence with.
    """

    return s.rstrip(".:")


def _candidate_path_tokens(content: str) -> Iterable[str]:
    """Whitespace/markdown-punctuation-delimited tokens from doc content."""

    return (token for token in _TOKEN_SPLIT_RE.split(content) if token)


def _cli_invocation_command(line: str) -> Optional[str]:
    """The line's command text if it invokes this project's CLI, else ``None``.

    Strips one leading ``$ `` prompt marker and surrounding whitespace first,
    then matches ``dev-team``/``python -m dev_team``/``python3 -m dev_team``
    as a whole leading token (not a substring match).
    """

    text = line.strip()
    if text.startswith("$ "):
        text = text[2:].strip()
    for prefix in _CLI_INVOCATION_PREFIXES:
        if text == prefix or text.startswith(prefix + " "):
            return text
    return None


def _truncate_before_unquoted_delimiter(text: str) -> str:
    """Cut ``text`` before the first unquoted ``|``, ``&&``, ``;``, or ``#``.

    Tracks single/double-quote state left-to-right: a quote character toggles
    state, and a delimiter inside an open quote does not truncate.
    """

    quote: Optional[str] = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch in ("|", ";", "#"):
            return text[:i]
        if ch == "&" and text[i : i + 2] == "&&":
            return text[:i]
        i += 1
    return text


def _cli_flags_cited(block: str) -> Iterable[str]:
    """Long-flag tokens (``--foo``) cited on CLI-invocation lines in ``block``."""

    for line in block.splitlines():
        command = _cli_invocation_command(line)
        if command is None:
            continue
        truncated = _truncate_before_unquoted_delimiter(command)
        for flag in _LONG_FLAG_RE.findall(truncated):
            yield flag


def doc_claim_issues(
    doc_files: Sequence[FileChange], known_files: Iterable[str]
) -> List[str]:
    """Advisory findings where a shipped doc's claims don't check out.

    Deterministic and $0: no LLM call, no subprocess, no network, no I/O.
    Three checks, mirroring ``assessment.broken_citations``' precedent:

    - a bare-path-shaped citation absent from ``known_files``;
    - a ```python``` fenced block that fails ``ast.parse`` (parse-only —
      doc content is untrusted model output, never executed);
    - a line in a ``bash``/``sh``/``shell``/``console``/``zsh`` fence that
      invokes this project's own CLI and cites a ``--flag`` not recognised
      by ``dev_team.cli.build_parser()`` (regex-scanned only — never passed
      to ``subprocess``, ``os.system``, ``eval``, or ``exec``).

    Fences in any other language (or unlabelled) are left unchecked; a
    malformed, unterminated fence is silently skipped, not raised.
    """

    known = set(known_files)
    issues: List[str] = []
    known_flags: Optional[set] = None
    for doc in doc_files:
        # Path citations are a prose claim ("see src/x.py") — scan outside
        # fenced code, so identifiers that merely happen to contain a dot
        # (``os.system``, ``a.b``) are never mistaken for a file citation.
        prose = _FENCE_RE.sub("", doc.content)
        for token in _candidate_path_tokens(prose):
            candidate = _strip_sentence_punctuation(_strip_locator(token))
            if _looks_like_bare_path(candidate) and candidate not in known:
                issues.append(f"{doc.path}: cites {token!r}, not found in workspace")
        for lang, block in _FENCE_RE.findall(doc.content):
            if lang == "python":
                try:
                    ast.parse(block)
                except SyntaxError as exc:
                    issues.append(
                        f"{doc.path}: python fence has a syntax error at line {exc.lineno}"
                    )
            elif lang in _SHELL_FENCE_LANGS:
                for flag in _cli_flags_cited(block):
                    if known_flags is None:
                        from ..cli import build_parser  # deferred: avoid cli -> engine -> techwriter cycle

                        known_flags = {
                            option
                            for action in build_parser()._actions
                            for option in action.option_strings
                            if option.startswith("--")
                        }
                    if flag not in known_flags:
                        issues.append(
                            f"{doc.path}: cites CLI flag {flag!r}, not a recognised dev-team option"
                        )
    return issues
