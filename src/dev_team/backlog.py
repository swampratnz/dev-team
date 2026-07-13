"""A persistent product backlog: epics, stories, and iteration planning.

A real team works a backlog across many features, not one request in isolation.
This module models an Epic → Story hierarchy, greedy capacity-based iteration
(sprint) planning, and simple velocity, with JSON persistence to a workspace.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .execution import Workspace


class ItemStatus(str, Enum):
    """Lifecycle status of a backlog item."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass
class Epic:
    """A large body of work grouping related stories."""

    id: str
    title: str
    description: str = ""


@dataclass
class Story:
    """A single user story with an effort estimate in points.

    ``source_job`` and ``finding_id`` are optional provenance: stories bred
    from an assessment's LLM findings carry the dispatch job that produced
    the assessment and the finding's positional id (the exact id
    :func:`~dev_team.assessment.list_findings` assigns), so a story can be
    traced back to — and independently re-verified against — the claim it
    came from. Deterministic findings (dead code, dependency scan) and
    hand-written stories leave both ``None``.
    """

    id: str
    title: str
    description: str = ""
    estimate: int = 1
    status: ItemStatus = ItemStatus.TODO
    epic_id: Optional[str] = None
    source_job: Optional[str] = None
    finding_id: Optional[str] = None


@dataclass
class Iteration:
    """A planned iteration/sprint: the stories committed within a capacity."""

    number: int
    capacity: int
    stories: List[Story] = field(default_factory=list)

    @property
    def committed_points(self) -> int:
        """Total estimate committed to this iteration."""

        return sum(s.estimate for s in self.stories)


@dataclass
class Backlog:
    """An ordered collection of epics and stories."""

    epics: List[Epic] = field(default_factory=list)
    stories: List[Story] = field(default_factory=list)

    def add_epic(self, title: str, description: str = "") -> Epic:
        """Create and append an epic with an auto-assigned id."""

        epic = Epic(id=f"E{len(self.epics) + 1}", title=title, description=description)
        self.epics.append(epic)
        return epic

    def add_story(
        self,
        title: str,
        description: str = "",
        estimate: int = 1,
        epic_id: Optional[str] = None,
        *,
        source_job: Optional[str] = None,
        finding_id: Optional[str] = None,
    ) -> Story:
        """Create and append a story with an auto-assigned id."""

        if estimate < 1:
            raise ValueError("estimate must be at least 1")
        story = Story(
            id=f"S{len(self.stories) + 1}",
            title=title,
            description=description,
            estimate=estimate,
            epic_id=epic_id,
            source_job=source_job,
            finding_id=finding_id,
        )
        self.stories.append(story)
        return story

    def stories_for_epic(self, epic_id: str) -> List[Story]:
        """Return the stories belonging to ``epic_id``."""

        return [s for s in self.stories if s.epic_id == epic_id]

    def ready_stories(self) -> List[Story]:
        """Return TODO stories in backlog order."""

        return [s for s in self.stories if s.status is ItemStatus.TODO]

    def plan_iteration(self, number: int, capacity: int) -> Iteration:
        """Greedily fill an iteration with TODO stories up to ``capacity``."""

        if capacity < 0:
            raise ValueError("capacity must be non-negative")
        iteration = Iteration(number=number, capacity=capacity)
        remaining = capacity
        for story in self.ready_stories():
            if story.estimate <= remaining:
                iteration.stories.append(story)
                remaining -= story.estimate
        return iteration

    def velocity(self) -> int:
        """Total points of DONE stories."""

        return sum(s.estimate for s in self.stories if s.status is ItemStatus.DONE)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-friendly dict."""

        return {
            "epics": [asdict(e) for e in self.epics],
            "stories": [_story_to_dict(s) for s in self.stories],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Backlog":
        """Rebuild a backlog from :meth:`to_dict` output."""

        epics = [Epic(**e) for e in data.get("epics", [])]
        stories = [_story_from_dict(s) for s in data.get("stories", [])]
        return cls(epics=epics, stories=stories)


def _story_to_dict(story: Story) -> Dict[str, Any]:
    data = asdict(story)
    data["status"] = story.status.value
    # The provenance fields are serialised only when set, so backlogs written
    # before (or without) finding provenance keep their exact on-disk shape.
    for key in ("source_job", "finding_id"):
        if data[key] is None:
            del data[key]
    return data


def _story_from_dict(data: Dict[str, Any]) -> Story:
    payload = dict(data)
    payload["status"] = ItemStatus(payload.get("status", "todo"))
    # Absent provenance keys (older backlog.json files) default to None.
    return Story(**payload)


_BACKLOG_PATH = ".dev_team/backlog.json"


@dataclass
class BacklogStore:
    """Persists a :class:`Backlog` to a workspace as JSON."""

    workspace: Workspace
    path: str = _BACKLOG_PATH

    def save(self, backlog: Backlog) -> None:
        """Write ``backlog`` to the workspace."""

        self.workspace.write_text(self.path, json.dumps(backlog.to_dict(), indent=2))

    def load(self) -> Backlog:
        """Load the backlog, or return an empty one if none is stored."""

        if not self.workspace.exists(self.path):
            return Backlog()
        return Backlog.from_dict(json.loads(self.workspace.read_text(self.path)))
