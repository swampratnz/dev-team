"""Front-door triage: route a raw request to the right engine (ROADMAP #9).

The engines each know how to do one kind of work — deliver a feature, assess a
repository, shape an idea in conversation — but nothing decides *which* of
them a raw, free-text request needs: today the human encodes that choice in
CLI flags. :class:`~dev_team.agents.intake.TriageAgent` makes that call as one
bounded model turn, and this module holds the decision contract around it.

The discipline mirrors ``verify_finding``'s verdicts: the model chooses from a
**closed set of routes** (:data:`TRIAGE_ROUTES`), and anything out of contract
degrades to ``unclear`` — never to an action. Code, not the model, owns what
happens next: the CLI *proposes* the routed command and only executes it under
an explicit ``--intake-apply`` (or an interactive confirmation), because the
mode choice is consequential — it decides spend and repo mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from .models import FeatureRequest

#: The closed set of routes triage may choose. ``deliver`` = a concrete,
#: buildable change; ``assess`` = a read-only audit of an existing codebase;
#: ``chat`` = an idea that needs shaping before any work; ``unclear`` = not a
#: software request, or the model could not tell (the fail-safe).
TRIAGE_ROUTES = ("deliver", "assess", "chat", "unclear")


@dataclass
class TriageDecision:
    """The routed outcome of one intake triage call.

    ``request`` is the distilled brief and is set only when ``route`` is
    ``deliver`` — the other routes carry no work order.
    """

    route: str
    rationale: str = ""
    request: Optional[FeatureRequest] = None


def _brief_request(data: dict) -> Optional[FeatureRequest]:
    """A :class:`FeatureRequest` from the decision payload, or ``None``.

    Tolerant by design (degrade, never raise): a ``deliver`` route without a
    usable title/description downgrades to ``unclear`` at the caller rather
    than crashing the front door on a malformed reply.
    """

    title = data.get("title")
    description = data.get("description")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    raw = data.get("constraints", [])
    constraints = (
        [c.strip() for c in raw if isinstance(c, str) and c.strip()]
        if isinstance(raw, list)
        else []
    )
    return FeatureRequest(
        title=title.strip(), description=description.strip(), constraints=constraints
    )


def triage_decision_from_dict(data: Any) -> TriageDecision:
    """Build a :class:`TriageDecision` from model JSON, fail-safe to ``unclear``.

    An unrecognised route is *not* trusted or passed through: it becomes
    ``unclear`` with the original value quoted in the rationale. A ``deliver``
    route whose brief is unusable likewise degrades to ``unclear`` — the front
    door never emits an executable decision it cannot stand behind.
    """

    if not isinstance(data, dict):
        return TriageDecision(route="unclear", rationale="unusable triage reply")
    raw_route = data.get("route")
    route = raw_route.strip().lower() if isinstance(raw_route, str) else ""
    raw_rationale = data.get("rationale")
    rationale = raw_rationale.strip() if isinstance(raw_rationale, str) else ""
    if route not in TRIAGE_ROUTES:
        return TriageDecision(
            route="unclear",
            rationale=f"triage proposed an unknown route {raw_route!r}"
            + (f": {rationale}" if rationale else ""),
        )
    if route == "deliver":
        request = _brief_request(data)
        if request is None:
            return TriageDecision(
                route="unclear",
                rationale="triage proposed delivery without a usable brief"
                + (f": {rationale}" if rationale else ""),
            )
        return TriageDecision(route="deliver", rationale=rationale, request=request)
    return TriageDecision(route=route, rationale=rationale)


def decision_to_dict(decision: TriageDecision) -> dict:
    """A JSON-safe document of the decision (the ``--json`` proposal output)."""

    request = None
    if decision.request is not None:
        request = {
            "title": decision.request.title,
            "description": decision.request.description,
            "constraints": list(decision.request.constraints),
        }
    return {
        "route": decision.route,
        "rationale": decision.rationale,
        "request": request,
        "equivalent_command": equivalent_command(decision),
    }


def equivalent_command(decision: TriageDecision) -> List[str]:
    """The ``dev-team`` argv the decision maps to (shown to the human).

    The proposal *is* this command: applying the decision runs exactly what is
    printed, so the human can also copy it and run it themselves. ``unclear``
    maps to ``--chat`` — the route for a request that needs shaping.
    """

    if decision.route == "deliver" and decision.request is not None:
        argv = ["dev-team", decision.request.title, decision.request.description]
        for constraint in decision.request.constraints:
            argv += ["-c", constraint]
        return argv + ["--deliver"]
    if decision.route == "assess":
        return ["dev-team", "--assess"]
    return ["dev-team", "--chat"]
