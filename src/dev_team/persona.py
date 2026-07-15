"""Named personas for the agent roster.

A persona gives an agent a name and a professional identity that shows up in
its system prompt, progress events, and any interactive surface. Personas are
presentation and temperament, never identity: everything internal (events'
``role``, checkpoints, memory, commits) stays keyed by role, so renaming the
cast can never break a resume or a report.

Persona text is *additive* to the role's system prompt — it must not weaken
the role's contract (JSON-only responses, evidence requirements). Keep styles
identity-level (background, communication style); temperament that could bias
judgement (e.g. an extra-sceptical reviewer) is a deliberate, opt-in choice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

from .errors import DevTeamError


@dataclass(frozen=True)
class Persona:
    """A name and professional identity for one agent role.

    Attributes:
        name: The agent's given name, shown in events and prompts.
        role: The role this persona belongs to (e.g. ``"engineer"``).
        style: A short professional identity woven into the system prompt —
            background and communication style, phrased in second person
            ("You are direct and cite evidence").
    """

    name: str
    role: str
    style: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("persona name must not be empty")
        if not self.role.strip():
            raise ValueError("persona role must not be empty")

    def preamble(self) -> str:
        """The system-prompt preamble introducing this persona."""

        base = f"Your name is {self.name}."
        if self.style:
            return f"{base} {self.style}"
        return base


DEFAULT_CAST: Mapping[str, Persona] = {
    "product-manager": Persona(
        name="Priya",
        role="product-manager",
        style=(
            "You are a pragmatic delivery lead: you keep scope small, say no "
            "to gold-plating, and phrase everything in terms of user value."
        ),
    ),
    "architect": Persona(
        name="Anders",
        role="architect",
        style=(
            "You are a systems thinker who values boring technology, states "
            "trade-offs explicitly, and designs for the team that maintains "
            "the code after you."
        ),
    ),
    "engineer": Persona(
        name="Sam",
        role="engineer",
        style=(
            "You are a hands-on implementer: you read existing code before "
            "changing it, keep diffs small, and let tests speak for you."
        ),
    ),
    "reviewer": Persona(
        name="Rey",
        role="reviewer",
        style=(
            "You are a calm, evidence-first code reviewer: every comment "
            "cites the line it is about, and praise is as specific as "
            "criticism."
        ),
    ),
    "qa": Persona(
        name="Quinn",
        role="qa",
        style=(
            "You are a quality engineer who trusts failing tests over "
            "promises and hunts the edge case everyone else forgot."
        ),
    ),
    "security-engineer": Persona(
        name="Sasha",
        role="security-engineer",
        style=(
            "You are a security engineer who thinks in threat models and "
            "blast radii, and blocks only with evidence in hand."
        ),
    ),
    "technical-writer": Persona(
        name="Wren",
        role="technical-writer",
        style=(
            "You are a technical writer who explains systems plainly, "
            "prefers examples over adjectives, and writes for the reader "
            "who arrived five minutes ago."
        ),
    ),
    "sre": Persona(
        name="Riley",
        role="sre",
        style=(
            "You are a site reliability engineer: you assume things fail, "
            "ask how you would know, and want a rollback for everything."
        ),
    ),
    "devops": Persona(
        name="Devon",
        role="devops",
        style=(
            "You are a DevOps engineer who automates the boring parts and "
            "treats deployment as code with a tested rollback path."
        ),
    ),
    "retrospective": Persona(
        name="Remy",
        role="retrospective",
        style=(
            "You run blameless retrospectives: you trace a bad outcome back to "
            "its root cause and turn it into one concrete change for next time."
        ),
    ),
}


@dataclass(frozen=True)
class Roster:
    """The cast of personas for a team run, keyed by role.

    An empty roster (``Roster.anonymous()``) disables personas entirely;
    :meth:`default` ships the standard cast. Custom casts come from
    :meth:`from_dict` / :meth:`from_file`, which overlay the default cast so a
    user may rename a single agent without redefining the team.
    """

    personas: Mapping[str, Persona] = field(default_factory=dict)

    def get(self, role: str) -> Optional[Persona]:
        """The persona for ``role``, or ``None`` when the role is uncast."""

        return self.personas.get(role)

    def display_name(self, role: str) -> str:
        """The name shown for ``role`` (falls back to the role itself)."""

        persona = self.get(role)
        return persona.name if persona is not None else role

    @classmethod
    def default(cls) -> "Roster":
        """The shipped cast: every role named."""

        return cls(personas=dict(DEFAULT_CAST))

    @classmethod
    def anonymous(cls) -> "Roster":
        """No personas: agents present as their bare roles."""

        return cls(personas={})

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Roster":
        """Build a roster from ``{role: {"name": ..., "style": ...}}``.

        Entries overlay the default cast; unknown roles are rejected so a
        typo ("architekt") fails loudly instead of silently uncasting an
        agent.
        """

        personas: Dict[str, Persona] = dict(DEFAULT_CAST)
        for role, spec in data.items():
            if role not in DEFAULT_CAST:
                known = ", ".join(sorted(DEFAULT_CAST))
                raise DevTeamError(f"unknown roster role '{role}' (known: {known})")
            if not isinstance(spec, Mapping):
                raise DevTeamError(f"roster entry for '{role}' must be an object")
            name = spec.get("name")
            if not isinstance(name, str) or not name.strip():
                raise DevTeamError(f"roster entry for '{role}' needs a non-empty 'name'")
            style = spec.get("style", DEFAULT_CAST[role].style)
            if not isinstance(style, str):
                raise DevTeamError(f"roster 'style' for '{role}' must be a string")
            personas[role] = Persona(name=name.strip(), role=role, style=style)
        return cls(personas=personas)

    @classmethod
    def from_file(cls, path: str) -> "Roster":
        """Load a roster overlay from a JSON file (see :meth:`from_dict`)."""

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise DevTeamError(f"cannot read roster file {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DevTeamError(f"roster file {path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise DevTeamError(f"roster file {path} must contain a JSON object")
        return cls.from_dict(data)
