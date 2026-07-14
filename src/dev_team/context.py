"""A compact, deterministic map of the workspace for planning agents.

Brownfield work fails when the planner and architect design against an
imagined codebase. :func:`build_repo_context` distils what is actually there —
the file tree, the heads of the manifests and README, and where the tests
live — into a bounded prompt block. It is deliberately deterministic (no LLM
summarisation) so it costs nothing and can be tested exactly.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from .execution import Workspace

# Root-level files whose beginnings orient a planner better than any listing.
_MANIFESTS = (
    "README.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "setup.py",
    "global.json",
    "packages.config",
    "Directory.Build.props",
)

MAX_TREE_ENTRIES = 150
MANIFEST_HEAD_CHARS = 1_500


def path_excluded(path: str, exclude_globs: Sequence[str]) -> bool:
    """Whether ``path`` matches any exclude glob (fnmatch semantics)."""

    return any(fnmatch.fnmatch(path, pattern) for pattern in exclude_globs)


#: Structural fence tokens whose literal appearance inside untrusted manifest
#: text could try to break out of the block it is interpolated into.
_FENCE_TOKENS = ("</manifest-content>", "</repo-context>")


def _defuse(text: str) -> str:
    """Neutralise structural fence tokens in untrusted manifest/README text.

    The manifest head is interpolated raw between ``<manifest-content>`` … tags
    that later nest inside assessment's ``<repo-context>`` block. A hostile file
    whose body contains a literal ``</manifest-content>`` or ``</repo-context>``
    could otherwise try to close the block early and smuggle instructions after
    it. Insert a zero-width space into each closing token so it reads
    identically to a human but no longer matches the structural tag. Only the
    untrusted head is defused; the renderer's own emitted tags are untouched.
    """

    for token in _FENCE_TOKENS:
        # Zero-width space (U+200B) between "<" and "/": invisible to a human,
        # but the token no longer matches the structural closing tag.
        text = text.replace(token, "<\u200b" + token[1:])
    return text


@dataclass
class RepoContext:
    """What the workspace holds, in prompt-ready form."""

    files: List[str] = field(default_factory=list)
    total_files: int = 0
    manifest_heads: Dict[str, str] = field(default_factory=dict)
    test_paths: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether there is anything to describe (greenfield)."""

        return self.total_files == 0

    def render(self) -> str:
        """Render the context as a compact prompt block."""

        if self.is_empty:
            return ""
        lines = [f"The workspace contains {self.total_files} file(s):"]
        lines.extend(f"- {path}" for path in self.files)
        if self.total_files > len(self.files):
            lines.append(f"- ... and {self.total_files - len(self.files)} more")
        if self.test_paths:
            lines.append(f"Tests live under: {', '.join(self.test_paths)}")
        for name, head in self.manifest_heads.items():
            lines.append(
                f'\n<manifest-content name="{name}">\n{head}\n</manifest-content>'
            )
        return "\n".join(lines)


def build_repo_context(
    workspace: Workspace,
    *,
    max_tree_entries: int = MAX_TREE_ENTRIES,
    manifest_head_chars: int = MANIFEST_HEAD_CHARS,
    exclude_globs: Sequence[str] = (),
) -> RepoContext:
    """Inspect ``workspace`` and return its :class:`RepoContext`.

    ``exclude_globs`` drops noise (vendored packages, build output) from the
    tree before the entry cap applies, so a monolith's signal is not spent on
    its ``packages/`` directory.
    """

    files = [
        f
        for f in workspace.list_files()
        if not f.startswith(".dev_team/") and not path_excluded(f, exclude_globs)
    ]
    heads: Dict[str, str] = {}
    for name in _MANIFESTS:
        if name in files:
            try:
                content = workspace.read_text(name)
            except (UnicodeDecodeError, OSError, ValueError):
                # A non-UTF-8 or otherwise unreadable manifest must not unwind
                # the whole read-only assess() run over one bad file; skip it,
                # matching the guarded readers in assessment.inventory_stats
                # and depscan.
                continue
            head = content[:manifest_head_chars]
            if len(head) < len(content):
                head += "\n... (truncated)"
            # The head is untrusted repo content; neutralise any structural
            # fence tokens before it is interpolated into the prompt block.
            heads[name] = _defuse(head)
    test_locations = set()
    for path in files:
        root = path.split("/")[0]
        if root in ("tests", "test"):
            test_locations.add(root)
        if path.rsplit("/", 1)[-1].startswith("test_"):
            test_locations.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    test_dirs = sorted(test_locations)
    return RepoContext(
        files=files[:max_tree_entries],
        total_files=len(files),
        manifest_heads=heads,
        test_paths=test_dirs,
    )
