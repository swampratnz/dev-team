"""House conventions: captured once, followed on every later delivery.

Without this, agents "fix" an old repository in whatever style the model
defaults to — a modernisation patchwork stapled onto a codebase with its own
idioms. The conventions profile closes that loop:

- **Capture** (assessment): deterministic detection of machine-readable style
  configs (``.editorconfig``, ReSharper ``.DotSettings``, linter configs) plus
  an agent-inferred profile of naming, layout, test, and error-handling
  patterns — every claim cited.
- **Persist**: :class:`ConventionsStore` writes the profile to
  ``.dev_team/conventions.json`` in the workspace, where it survives across
  runs like the project memory does.
- **Inject** (delivery): the engine renders the stored profile into the
  engineer's and reviewer's prompts, making "follows the house style" part of
  implementation and review rather than an afterthought.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .execution import Workspace

_CONVENTIONS_PATH = ".dev_team/conventions.json"

# Exact file names (any directory) that machine-encode style rules.
_SOURCE_NAMES = frozenset(
    {
        ".editorconfig",
        ".eslintrc",
        ".eslintrc.json",
        ".eslintrc.js",
        ".eslintrc.yml",
        ".prettierrc",
        ".prettierrc.json",
        ".stylecop.json",
        "stylecop.json",
        ".clang-format",
        ".rubocop.yml",
        "checkstyle.xml",
        ".golangci.yml",
    }
)

# Style configs recognised by suffix (ReSharper settings, MSBuild rulesets).
_SOURCE_SUFFIXES = (".DotSettings", ".ruleset")

# Bound the profile so a giant DotSettings file cannot flood prompts.
_MAX_INFERRED = 20
_MAX_RENDER_CHARS = 4_000


def detect_convention_sources(workspace: Workspace) -> List[str]:
    """Paths of machine-readable style configuration files, sorted."""

    sources = []
    for path in workspace.list_files():
        if path.startswith(".dev_team/"):
            continue
        name = path.rsplit("/", 1)[-1]
        if name in _SOURCE_NAMES or name.endswith(_SOURCE_SUFFIXES):
            sources.append(path)
    return sorted(sources)


@dataclass
class ConventionsProfile:
    """The house style of a repository, ready to persist and to prompt."""

    summary: str = ""
    conventions: List[Dict[str, str]] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.summary or self.conventions or self.sources)

    def render(self) -> str:
        """A bounded prompt block agents can follow (and cite)."""

        if self.empty:
            return ""
        lines = ["House conventions for this repository (match them; do not"]
        lines.append("modernise style piecemeal):")
        if self.summary:
            lines.append(self.summary)
        for item in self.conventions[:_MAX_INFERRED]:
            aspect = item.get("aspect", "other")
            convention = item.get("convention", "")
            evidence = item.get("evidence")
            suffix = f" (evidence: {evidence})" if evidence else ""
            lines.append(f"- {aspect}: {convention}{suffix}")
        if self.sources:
            lines.append(
                "Machine-readable style configs to honour: " + ", ".join(self.sources)
            )
        return "\n".join(lines)[:_MAX_RENDER_CHARS]

    def to_dict(self) -> Dict:
        return {
            "summary": self.summary,
            "conventions": list(self.conventions),
            "sources": list(self.sources),
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ConventionsProfile":
        conventions = [
            item for item in data.get("conventions", []) if isinstance(item, dict)
        ]
        sources = [str(s) for s in data.get("sources", [])]
        return cls(
            summary=str(data.get("summary", "")),
            conventions=conventions,
            sources=sources,
        )


@dataclass
class ConventionsStore:
    """Persists a :class:`ConventionsProfile` to the workspace as JSON."""

    workspace: Workspace
    path: str = _CONVENTIONS_PATH

    def save(self, profile: ConventionsProfile) -> None:
        self.workspace.write_text(self.path, json.dumps(profile.to_dict(), indent=2))

    def load(self) -> Optional[ConventionsProfile]:
        """The stored profile, or ``None`` when absent, corrupt, or empty.

        Corrupt reads as absent on purpose: a delivery must never die because
        an earlier assessment wrote a truncated file.
        """

        if not self.workspace.exists(self.path):
            return None
        try:
            data = json.loads(self.workspace.read_text(self.path))
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        profile = ConventionsProfile.from_dict(data)
        return None if profile.empty else profile
