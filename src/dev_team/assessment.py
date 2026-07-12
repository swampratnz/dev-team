"""Read-only repository assessment: the audit engine.

The third engine, next to simulation and delivery. Point it at an existing
repository and the specialist roles audit it in phases — inventory
(architect), buildability (DevOps), risk (security), test/doc reality (QA),
and a classification with a sequenced remediation plan (product manager) —
then the technical writer distils an executive summary and the findings are
rendered into a single cited markdown report.

Unlike delivery, assessment **never mutates the repository**: no branch, no
baseline commit, no gates, no ``.dev_team/`` bookkeeping. Auditing roles get
read-only tools (`Read`/`Grep`/`Glob`) rooted at the workspace so claims come
from the actual files, and every phase's contract demands a file-path
citation per claim — ambiguity is stated, not guessed away. The only write is
the report itself, and only when a ``report_path`` is configured.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

from .agents import (
    ArchitectAgent,
    DevOpsAgent,
    ProductManagerAgent,
    QAAgent,
    SecurityEngineerAgent,
    TechnicalWriterAgent,
)
from .agents.base import READ_ONLY_TOOLS
from .budget import Budget, BudgetExceededError
from .context import build_repo_context
from .errors import AgentResponseError
from .events import AgentEvent, Listener, emit
from .execution import InMemoryWorkspace, Workspace
from .instrument import InstrumentedRunner
from .interaction import (
    Choice,
    InteractionChannel,
    Question,
    ask_in_thread,
)
from .persona import Roster
from .profile import ProjectProfile, detect_project
from .sdk import AgentRunner
from .trace import Tracer

# Assessments read more of the tree than feature planning does.
_TREE_ENTRIES = 400

CLASSIFICATIONS = (
    "revive-in-place",
    "dependency-surgery",
    "strangler-rewrite",
    "archive",
)

#: Shared evidence discipline appended to every phase prompt.
_DISCIPLINE = """
Rules of evidence:
- Cite a repository file path for every claim (an "evidence" field). If a
  claim rests on the absence of something, cite where you looked.
- If something is ambiguous, state the ambiguity instead of guessing.
- Flag anything that looks like a workaround for a bug in an old dependency
  version — upgrades will break those.
- You may read files with your tools; prefer reading over inferring.
Respond with a single JSON object exactly matching the requested shape."""

_INVENTORY_PROMPT = """\
Phase 1 of a repository audit: INVENTORY. Map what is actually in this
repository: languages, frameworks and their versions, the frontend/backend
boundary and how the halves communicate, entry points, build scripts, CI
configuration, deployment artifacts, and directories that look dead.

<repo-context>
{evidence}
</repo-context>
{focus}
JSON shape:
{{"summary": "...",
  "components": [{{"name": "...", "path": "...", "purpose": "...",
                   "stack": "..."}}],
  "boundary": "how frontend and backend communicate, with evidence",
  "entry_points": [{{"path": "...", "kind": "build|run|ci|deploy"}}],
  "findings": [{{"claim": "...", "evidence": "path or where you looked"}}]}}
""" + _DISCIPLINE

_BUILDABILITY_PROMPT = """\
Phase 2 of a repository audit: BUILDABILITY. Determine whether this project
could build today. Check lockfiles, pinned dependency resolvability, runtime
version requirements (SDK versions, target frameworks, Node/Python versions),
and build tooling. Do NOT run installs or builds — report what would likely
break and why. The detected project profile is: {profile}.

<repo-context>
{evidence}
</repo-context>

Inventory summary from phase 1: {inventory}
{focus}
JSON shape:
{{"summary": "...",
  "verdict": "likely|unlikely|unknown",
  "blockers": [{{"claim": "...", "evidence": "...",
                 "category": "must-fix-to-build|will-bite-later"}}],
  "runtime_requirements": [{{"runtime": "...", "required": "...",
                             "evidence": "..."}}]}}
""" + _DISCIPLINE

_RISK_PROMPT = """\
Phase 3 of a repository audit: RISK. Assess dependencies (EOL, abandoned, or
with known vulnerabilities — separate "must fix to run" from "should fix for
security", and state that CVE knowledge comes from your training data, not a
live scan), hardcoded secrets (credentials, keys, connection strings), the
data layer (database engines, ORM versions, migration state), and every
external service called — flagging any likely to have changed or died since
the repository went dormant.

<repo-context>
{evidence}
</repo-context>

Inventory summary from phase 1: {inventory}
{focus}
JSON shape:
{{"summary": "...",
  "dependencies": [{{"name": "...", "version": "...", "status": "...",
                     "action": "must-fix|should-fix|ok", "evidence": "..."}}],
  "secrets": [{{"claim": "...", "evidence": "..."}}],
  "data_layer": [{{"claim": "...", "evidence": "..."}}],
  "external_services": [{{"name": "...", "risk": "...", "evidence": "..."}}]}}
""" + _DISCIPLINE

_COVERAGE_PROMPT = """\
Phase 4 of a repository audit: TESTS AND DOCS. What test infrastructure
exists and does it plausibly still run? What documentation exists, and where
does it diverge from what the code actually does?

<repo-context>
{evidence}
</repo-context>

Inventory summary from phase 1: {inventory}
{focus}
JSON shape:
{{"summary": "...",
  "tests": [{{"claim": "...", "evidence": "..."}}],
  "documentation": [{{"claim": "...", "evidence": "..."}}]}}
""" + _DISCIPLINE

_RECOMMENDATION_PROMPT = """\
Phase 5 of a repository audit: RECOMMENDATION. Based on the phase findings
below, classify the repository and give a sequenced remediation plan with an
effort estimate per step. Explicitly name the single highest-risk item
blocking a first build.

Phase findings:
<audit-findings>
{findings}
</audit-findings>
{focus}
JSON shape:
{{"summary": "...",
  "classification": "revive-in-place|dependency-surgery|strangler-rewrite|archive",
  "rationale": "...",
  "highest_risk": "the single highest-risk item blocking a first build",
  "plan": [{{"step": "...", "effort": "e.g. days/weeks", "detail": "..."}}]}}
""" + _DISCIPLINE

_EXEC_SUMMARY_PROMPT = """\
Write a crisp executive summary (a few short paragraphs, plain prose) of this
repository audit for the person deciding what to do with the repo. Lead with
the classification and the single highest-risk item.

<audit-findings>
{findings}
</audit-findings>

Respond with a single JSON object: {{"summary": "..."}}
"""

#: Required top-level keys per phase; a response missing any is an error.
_REQUIRED_KEYS: Mapping[str, Sequence[str]] = {
    "inventory": ("summary", "components", "findings"),
    "buildability": ("summary", "verdict", "blockers"),
    "risk": ("summary", "dependencies"),
    "coverage": ("summary", "tests"),
    "recommendation": ("summary", "classification", "highest_risk", "plan"),
}


@dataclass(frozen=True)
class AssessConfig:
    """Tunable settings for an assessment run.

    Attributes:
        model: Model for all auditing agents (``role_models`` overrides).
        role_models: Per-role model overrides, keyed by role.
        focus: Optional scoping brief woven into every phase prompt.
        report_path: Workspace-relative path the markdown report is written
            to; ``None`` skips writing (the report is still returned).
        json_retries: Corrective retries for malformed agent JSON.
    """

    model: Optional[str] = None
    role_models: Mapping[str, str] = field(default_factory=dict)
    focus: Optional[str] = None
    report_path: Optional[str] = "audit/assessment.md"
    json_retries: int = 1


@dataclass
class InventoryStats:
    """Deterministic repository statistics (no model involved)."""

    total_files: int = 0
    loc_by_top: Dict[str, int] = field(default_factory=dict)
    files_by_extension: Dict[str, int] = field(default_factory=dict)
    unreadable_files: int = 0

    def render(self) -> str:
        lines = [f"Deterministic inventory: {self.total_files} file(s)."]
        if self.loc_by_top:
            top = sorted(self.loc_by_top.items(), key=lambda kv: -kv[1])
            lines.append(
                "Lines by top-level entry: "
                + ", ".join(f"{name}={loc}" for name, loc in top)
            )
        if self.files_by_extension:
            exts = sorted(self.files_by_extension.items(), key=lambda kv: -kv[1])
            lines.append(
                "Files by extension: "
                + ", ".join(f"{ext}={n}" for ext, n in exts[:15])
            )
        if self.unreadable_files:
            lines.append(f"Unreadable/binary files skipped: {self.unreadable_files}")
        return "\n".join(lines)


def inventory_stats(workspace: Workspace) -> InventoryStats:
    """Count files, lines, and extensions — exactly, without a model."""

    stats = InventoryStats()
    for path in workspace.list_files():
        if path.startswith(".dev_team/") or path.startswith(".git/"):
            continue
        stats.total_files += 1
        top = path.split("/", 1)[0] if "/" in path else "(root)"
        name = path.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1] if "." in name[1:] else "(none)"
        stats.files_by_extension[ext] = stats.files_by_extension.get(ext, 0) + 1
        try:
            loc = workspace.read_text(path).count("\n") + 1
        except (UnicodeDecodeError, OSError, ValueError):
            stats.unreadable_files += 1
            continue
        stats.loc_by_top[top] = stats.loc_by_top.get(top, 0) + loc
    return stats


@dataclass
class PhaseResult:
    """One audit phase's outcome: the agent's JSON, or why it failed."""

    phase: str
    role: str
    data: Dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class AssessmentOutcome:
    """Everything an assessment run produced."""

    profile: ProjectProfile
    stats: InventoryStats
    phases: Dict[str, PhaseResult]
    executive_summary: str
    report_markdown: str
    report_path: Optional[str]
    budget: Budget
    tracer: Tracer
    focus: Optional[str] = None
    aborted: bool = False

    @property
    def success(self) -> bool:
        """True when nothing failed: no abort and every phase produced data."""

        return not self.aborted and all(p.ok for p in self.phases.values())

    @property
    def classification(self) -> Optional[str]:
        rec = self.phases.get("recommendation")
        if rec is None or not rec.ok:
            # A failed phase's data may hold an unvalidated value; never
            # surface that as the verdict.
            return None
        value = rec.data.get("classification")
        return value if isinstance(value, str) else None

    @property
    def cost_usd(self) -> float:
        return self.budget.spent


def scope_question(inventory_summary: str, *, asked_by: str) -> Question:
    """The post-inventory scope check. Default (unattended): continue."""

    return Question(
        topic="audit-scope",
        prompt="Inventory is done. Adjust the audit scope before the deep phases?",
        choices=(
            Choice("continue", "audit everything"),
            Choice("focus", "narrow the scope", accepts_text=True),
            Choice("abort", "stop the assessment"),
        ),
        context=inventory_summary,
        asked_by=asked_by,
    )


class AssessmentEngine:
    """Runs the five-phase audit over a workspace, read-only."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        workspace: Optional[Workspace] = None,
        config: Optional[AssessConfig] = None,
        budget: Optional[Budget] = None,
        tracer: Optional[Tracer] = None,
        listener: Optional[Listener] = None,
        roster: Optional[Roster] = None,
        interaction: Optional[InteractionChannel] = None,
    ) -> None:
        self.workspace: Workspace = workspace or InMemoryWorkspace()
        self.config = config or AssessConfig()
        self.budget = budget or Budget()
        self.tracer = tracer or Tracer()
        self.listener = listener
        self.roster = roster if roster is not None else Roster.default()
        self.interaction = interaction
        root = getattr(self.workspace, "root", None)
        self.workdir: Optional[str] = str(root) if root is not None else None

        def make(cls):
            wrapped = InstrumentedRunner(
                runner, cls.role, budget=self.budget, tracer=self.tracer
            )
            return cls(
                wrapped,
                model=self.config.role_models.get(cls.role, self.config.model),
                listener=listener,
                json_retries=self.config.json_retries,
                persona=self.roster.get(cls.role),
            )

        self.architect = make(ArchitectAgent)
        self.devops = make(DevOpsAgent)
        self.security = make(SecurityEngineerAgent)
        self.qa = make(QAAgent)
        self.manager = make(ProductManagerAgent)
        self.writer = make(TechnicalWriterAgent)

    def _event(self, stage: str, message: str, detail: Optional[str] = None) -> None:
        emit(
            self.listener,
            AgentEvent(role="assessment", stage=stage, message=message, detail=detail),
        )

    def _focus_block(self, focus: Optional[str]) -> str:
        if not focus:
            return ""
        return f"\nScope for this audit (from the requester): {focus}\n"

    async def _phase(self, phase: str, agent, prompt: str) -> PhaseResult:
        """Run one phase with read-only tools; degrade instead of unwinding."""

        span = self.tracer.start("assessment", phase)
        self._event(phase, f"{self.roster.display_name(agent.role)} auditing")
        try:
            data = await agent.ask_json(
                prompt, allowed_tools=READ_ONLY_TOOLS, cwd=self.workdir
            )
        except BudgetExceededError:
            self.tracer.end(span, "budget")
            return PhaseResult(phase=phase, role=agent.role, error="budget exhausted")
        except AgentResponseError as exc:
            self.tracer.end(span, "error")
            return PhaseResult(phase=phase, role=agent.role, error=str(exc))
        missing = [k for k in _REQUIRED_KEYS[phase] if k not in data]
        if missing:
            self.tracer.end(span, "invalid")
            return PhaseResult(
                phase=phase,
                role=agent.role,
                data=data,
                error=f"response missing required key(s): {', '.join(missing)}",
            )
        if phase == "recommendation" and data["classification"] not in CLASSIFICATIONS:
            self.tracer.end(span, "invalid")
            return PhaseResult(
                phase=phase,
                role=agent.role,
                data=data,
                error=f"unrecognised classification: {data['classification']!r}",
            )
        self.tracer.end(span, "done")
        self._event(phase, "Phase complete")
        return PhaseResult(phase=phase, role=agent.role, data=data)

    async def _check_scope(
        self, inventory: PhaseResult, focus: Optional[str]
    ) -> tuple[Optional[str], bool]:
        """Interactive scope pause after inventory: (focus, aborted)."""

        if self.interaction is None or not inventory.ok:
            return focus, False
        summary = str(inventory.data.get("summary", ""))
        reply = await ask_in_thread(
            self.interaction,
            scope_question(summary, asked_by=self.roster.display_name("architect")),
        )
        if reply.choice == "abort":
            self._event("scope", "Assessment aborted at scope check")
            return focus, True
        if reply.choice == "focus" and reply.text:
            self._event("scope", "Audit scope narrowed", detail=reply.text)
            merged = f"{focus}; {reply.text}" if focus else reply.text
            return merged, False
        self._event("scope", "Auditing everything")
        return focus, False

    async def assess(self) -> AssessmentOutcome:
        """Run the audit and return the :class:`AssessmentOutcome`."""

        run_span = self.tracer.start("assessment", "assess")
        self._event("start", "Assessing the repository")
        profile = detect_project(self.workspace)
        stats = inventory_stats(self.workspace)
        ctx = build_repo_context(self.workspace, max_tree_entries=_TREE_ENTRIES)
        evidence = "\n\n".join(part for part in (ctx.render(), stats.render()) if part)
        focus = self.config.focus

        phases: Dict[str, PhaseResult] = {}
        phases["inventory"] = await self._phase(
            "inventory",
            self.architect,
            _INVENTORY_PROMPT.format(
                evidence=evidence, focus=self._focus_block(focus)
            ),
        )
        focus, aborted = await self._check_scope(phases["inventory"], focus)
        if aborted:
            outcome = self._outcome(
                profile, stats, phases, "", focus=focus, aborted=True
            )
            self.tracer.end(run_span, "aborted")
            return outcome

        inventory_summary = str(phases["inventory"].data.get("summary", "unavailable"))
        focus_block = self._focus_block(focus)
        deep = {
            "buildability": (
                self.devops,
                _BUILDABILITY_PROMPT.format(
                    profile=f"{profile.kind} ({profile.reason})",
                    evidence=evidence,
                    inventory=inventory_summary,
                    focus=focus_block,
                ),
            ),
            "risk": (
                self.security,
                _RISK_PROMPT.format(
                    evidence=evidence, inventory=inventory_summary, focus=focus_block
                ),
            ),
            "coverage": (
                self.qa,
                _COVERAGE_PROMPT.format(
                    evidence=evidence, inventory=inventory_summary, focus=focus_block
                ),
            ),
        }
        results = await asyncio.gather(
            *(self._phase(name, agent, prompt) for name, (agent, prompt) in deep.items())
        )
        for result in results:
            phases[result.phase] = result

        findings = _render_findings_for_prompt(phases)
        phases["recommendation"] = await self._phase(
            "recommendation",
            self.manager,
            _RECOMMENDATION_PROMPT.format(findings=findings, focus=focus_block),
        )

        executive = await self._executive_summary(phases)
        outcome = self._outcome(profile, stats, phases, executive, focus=focus)
        if self.config.report_path is not None:
            self.workspace.write_text(self.config.report_path, outcome.report_markdown)
            self._event("report", f"Report written to {self.config.report_path}")
        verdict = outcome.classification or "unclassified"
        self.tracer.end(run_span)
        self._event("done", f"Assessment finished: {verdict}", detail=f"${outcome.cost_usd:.4f}")
        return outcome

    async def _executive_summary(self, phases: Dict[str, PhaseResult]) -> str:
        """The writer's summary; degrades to the recommendation's summary."""

        rec = phases["recommendation"]
        fallback = str(rec.data.get("summary", "")) if rec.data else ""
        try:
            data = await self.writer.ask_json(
                _EXEC_SUMMARY_PROMPT.format(
                    findings=_render_findings_for_prompt(phases)
                )
            )
        except (BudgetExceededError, AgentResponseError):
            return fallback
        summary = data.get("summary")
        return summary if isinstance(summary, str) and summary else fallback

    def _outcome(
        self,
        profile: ProjectProfile,
        stats: InventoryStats,
        phases: Dict[str, PhaseResult],
        executive: str,
        *,
        focus: Optional[str],
        aborted: bool = False,
    ) -> AssessmentOutcome:
        outcome = AssessmentOutcome(
            profile=profile,
            stats=stats,
            phases=phases,
            executive_summary=executive,
            report_markdown="",
            report_path=self.config.report_path if not aborted else None,
            budget=self.budget,
            tracer=self.tracer,
            focus=focus,
            aborted=aborted,
        )
        outcome.report_markdown = render_report(outcome)
        return outcome


def outcome_to_dict(outcome: AssessmentOutcome) -> Dict:
    """Serialise an :class:`AssessmentOutcome` for ``--json`` output."""

    return {
        "success": outcome.success,
        "aborted": outcome.aborted,
        "classification": outcome.classification,
        "profile": {"kind": outcome.profile.kind, "reason": outcome.profile.reason},
        "focus": outcome.focus,
        "cost_usd": outcome.cost_usd,
        "report_path": outcome.report_path,
        "executive_summary": outcome.executive_summary,
        "phases": {
            name: {
                "role": result.role,
                "ok": result.ok,
                "error": result.error,
                "data": result.data,
            }
            for name, result in outcome.phases.items()
        },
        "stats": {
            "total_files": outcome.stats.total_files,
            "loc_by_top": dict(outcome.stats.loc_by_top),
            "files_by_extension": dict(outcome.stats.files_by_extension),
            "unreadable_files": outcome.stats.unreadable_files,
        },
        "report_markdown": outcome.report_markdown,
    }


def _render_findings_for_prompt(phases: Dict[str, PhaseResult]) -> str:
    """Compact JSON-ish rendering of completed phases for downstream prompts."""

    parts = []
    for name, result in phases.items():
        if name == "recommendation":
            continue
        if result.ok:
            parts.append(f"[{name}] {result.data}")
        else:
            parts.append(f"[{name}] phase failed: {result.error}")
    return "\n\n".join(parts)


def _items(data: Dict, key: str) -> List[Dict]:
    """The list under ``key``, keeping only dict entries."""

    value = data.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _cited(lines: List[str], items: List[Dict], *fields: str) -> None:
    """Append '- field1 — field2 (evidence)' bullets for each item."""

    for item in items:
        body = " — ".join(
            str(item[f]) for f in fields if item.get(f) not in (None, "")
        )
        evidence = item.get("evidence")
        suffix = f" (evidence: {evidence})" if evidence else ""
        lines.append(f"- {body or '(unspecified)'}{suffix}")


def render_report(outcome: AssessmentOutcome) -> str:
    """Render the whole assessment as a single cited markdown document."""

    lines = ["# Repository assessment", ""]
    lines.append(
        f"Project profile: **{outcome.profile.kind}** ({outcome.profile.reason})."
    )
    if outcome.focus:
        lines.append(f"Audit scope: {outcome.focus}")
    if outcome.aborted:
        lines += ["", "**Assessment aborted at the interactive scope check.**"]
    if outcome.executive_summary:
        lines += ["", "## Executive summary", "", outcome.executive_summary]

    rec = outcome.phases.get("recommendation")
    if rec is not None and rec.data:
        lines += ["", "## Recommendation", ""]
        classification = rec.data.get("classification", "unclassified")
        lines.append(f"**Classification: {classification}**")
        rationale = rec.data.get("rationale")
        if rationale:
            lines.append(str(rationale))
        highest = rec.data.get("highest_risk")
        if highest:
            lines += ["", f"**Highest-risk item blocking a first build:** {highest}"]
        plan = _items(rec.data, "plan")
        if plan:
            lines += ["", "### Remediation plan", ""]
            for i, step in enumerate(plan, 1):
                effort = step.get("effort", "?")
                detail = step.get("detail", "")
                lines.append(f"{i}. {step.get('step', '(step)')} — *{effort}*. {detail}".rstrip())

    sections = (
        ("inventory", "Phase 1 — Inventory"),
        ("buildability", "Phase 2 — Buildability"),
        ("risk", "Phase 3 — Risk"),
        ("coverage", "Phase 4 — Tests & docs"),
    )
    for key, title in sections:
        result = outcome.phases.get(key)
        if result is None:
            continue
        auditor = result.role
        lines += ["", f"## {title}", ""]
        if not result.ok:
            lines.append(f"_Phase failed ({auditor}): {result.error}_")
            if not result.data:
                continue
        summary = result.data.get("summary")
        if summary:
            lines.append(str(summary))
        if key == "inventory":
            components = _items(result.data, "components")
            if components:
                lines += ["", "### Components", ""]
                for c in components:
                    stack = f" [{c.get('stack')}]" if c.get("stack") else ""
                    lines.append(
                        f"- **{c.get('name', '?')}** (`{c.get('path', '?')}`){stack}"
                        f" — {c.get('purpose', '')}".rstrip(" —")
                    )
            boundary = result.data.get("boundary")
            if boundary:
                lines += ["", f"**Frontend/backend boundary:** {boundary}"]
            entries = _items(result.data, "entry_points")
            if entries:
                lines += ["", "### Entry points", ""]
                for e in entries:
                    lines.append(f"- `{e.get('path', '?')}` ({e.get('kind', '?')})")
            findings = _items(result.data, "findings")
            if findings:
                lines += ["", "### Findings", ""]
                _cited(lines, findings, "claim")
        elif key == "buildability":
            lines += ["", f"**Builds today: {result.data.get('verdict', 'unknown')}**"]
            blockers = _items(result.data, "blockers")
            if blockers:
                lines += ["", "### Blockers", ""]
                _cited(lines, blockers, "claim", "category")
            runtimes = _items(result.data, "runtime_requirements")
            if runtimes:
                lines += ["", "### Runtime requirements", ""]
                _cited(lines, runtimes, "runtime", "required")
        elif key == "risk":
            deps = _items(result.data, "dependencies")
            if deps:
                lines += ["", "### Dependencies", ""]
                _cited(lines, deps, "name", "version", "status", "action")
            for sub, heading in (
                ("secrets", "Secrets"),
                ("data_layer", "Data layer"),
            ):
                items = _items(result.data, sub)
                if items:
                    lines += ["", f"### {heading}", ""]
                    _cited(lines, items, "claim")
            services = _items(result.data, "external_services")
            if services:
                lines += ["", "### External services", ""]
                _cited(lines, services, "name", "risk")
        else:  # coverage
            for sub, heading in (
                ("tests", "Test infrastructure"),
                ("documentation", "Documentation vs reality"),
            ):
                items = _items(result.data, sub)
                if items:
                    lines += ["", f"### {heading}", ""]
                    _cited(lines, items, "claim")

    lines += ["", "## Appendix — deterministic inventory", ""]
    lines.append(outcome.stats.render())
    lines.append("")
    lines.append(
        f"_Cost: ${outcome.cost_usd:.4f}. Dependency/CVE observations come from "
        "model knowledge, not a live vulnerability scan._"
    )
    return "\n".join(lines)
