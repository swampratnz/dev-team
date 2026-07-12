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

One opt-in exception: :class:`AssessConfig.build_probe` executes the detected
profile's setup/verify commands so buildability rests on real exit codes.
That runs the repository's own build — arbitrary code, with a build's usual
side effects on the working tree — which is why it is off by default.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Mapping, Optional, Sequence

from .agents import (
    ArchitectAgent,
    DevOpsAgent,
    ProductManagerAgent,
    QAAgent,
    SecurityEngineerAgent,
    TechnicalWriterAgent,
)
from .agents.base import READ_ONLY_TOOLS
from .backlog import Backlog, BacklogStore, Story
from .budget import Budget, BudgetExceededError
from .context import build_repo_context, path_excluded
from .conventions import (
    ConventionsProfile,
    ConventionsStore,
    detect_convention_sources,
)
from .deadcode import DeadCodeReport, detect_dead_code
from .depscan import DependencyScan, Fetch, scan_dependencies
from .errors import AgentResponseError
from .events import AgentEvent, Listener, emit
from .execution import (
    CommandRunner,
    InMemoryWorkspace,
    SubprocessCommandRunner,
    Workspace,
)
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

#: Noise no audit should spend its tree budget on: vendored dependencies and
#: build output, at the root or nested, plus compiled binaries.
DEFAULT_EXCLUDE_GLOBS: Sequence[str] = (
    "node_modules/*",
    "*/node_modules/*",
    "packages/*",
    "*/packages/*",
    "bin/*",
    "*/bin/*",
    "obj/*",
    "*/obj/*",
    "vendor/*",
    "*/vendor/*",
    "dist/*",
    "*/dist/*",
    "target/*",
    "*/target/*",
    "*.dll",
    "*.exe",
    "*.pdb",
)

#: Manifest names that mark a sub-project (component) for the fan-out.
_COMPONENT_MANIFESTS = frozenset(
    {"package.json", "Cargo.toml", "go.mod", "pyproject.toml"}
)

#: Enrichment phases: their failure degrades the report, not the audit.
_ADVISORY_PHASES = frozenset({"conventions", "components"})

CLASSIFICATIONS = (
    "revive-in-place",
    "dependency-surgery",
    "strangler-rewrite",
    "rebuild",
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
break and why. If the repo-context includes build-probe results (the
project's own commands executed for real), treat their exit codes and output
as ground truth over static inference. The detected project profile is:
{profile}.

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

_CONVENTIONS_PROMPT = """\
Repository audit side-phase: HOUSE CONVENTIONS. Identify the coding style
future work on this repository must follow: naming conventions, file and
project organisation, test framework and test naming patterns, error
handling, logging, and structural patterns (dependency injection, layering).
Read representative source files rather than guessing. Machine-readable
style configuration files detected: {sources}.

<repo-context>
{evidence}
</repo-context>
{focus}
JSON shape:
{{"summary": "a one-paragraph style portrait of this codebase",
  "conventions": [{{"aspect": "naming|layout|tests|errors|logging|patterns|other",
                    "convention": "...", "evidence": "..."}}]}}
""" + _DISCIPLINE

_COMPONENT_PROMPT = """\
Repository audit component deep-dive. Component: {name} (directory
`{path}`, detected from `{manifest}`). Audit ONLY this component: its
purpose, internal structure, quality hot-spots, suspicious or dead-looking
areas, and how it couples to the rest of the repository.

<repo-context>
{evidence}
</repo-context>
{focus}
JSON shape:
{{"summary": "...",
  "findings": [{{"claim": "...", "evidence": "path or where you looked"}}]}}
""" + _DISCIPLINE

_RECOMMENDATION_PROMPT = """\
Phase 5 of a repository audit: RECOMMENDATION. Based on the phase findings
below, classify the repository and give a sequenced remediation plan with an
effort estimate per step. Explicitly name the single highest-risk item
blocking a first build.

Classification vocabulary (pick exactly one):
- revive-in-place: the codebase is worth keeping; fix forward inside it.
- dependency-surgery: the code is sound but the dependency stack needs
  targeted replacement before anything else.
- strangler-rewrite: replace it incrementally behind the existing system
  while it keeps serving.
- rebuild: the code is not worth carrying forward — build a replacement from
  scratch and plan the data/traffic migration; the old system is a
  requirements document, not a foundation.
- archive: retire it; any path back costs more than it returns.

Phase findings:
<audit-findings>
{findings}
</audit-findings>
{focus}
JSON shape:
{{"summary": "...",
  "classification": "revive-in-place|dependency-surgery|strangler-rewrite|rebuild|archive",
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
    "conventions": ("summary", "conventions"),
    "recommendation": ("summary", "classification", "highest_risk", "plan"),
}


#: How much of a probe command's output survives as evidence.
_PROBE_OUTPUT_TAIL = 4_000


@dataclass(frozen=True)
class ProbeCommandResult:
    """One build-probe command's real outcome."""

    command: tuple
    exit_code: int
    output_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class BuildProbe:
    """What actually happened when the project's own commands were run.

    The probe is the assessment's one opt-in departure from read-only: it
    executes the detected profile's setup and verify commands in the
    workspace so buildability rests on real exit codes instead of static
    inference. Running a repository's own build is arbitrary code execution —
    that is why it is off by default and callers are told to sandbox
    untrusted repos.
    """

    requested: bool = False
    commands: List[ProbeCommandResult] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    not_run: List[tuple] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return self.requested and self.skipped_reason is None

    @property
    def succeeded(self) -> Optional[bool]:
        """True/False for a probe that ran; ``None`` when it did not."""

        if not self.ran:
            return None
        return all(result.ok for result in self.commands)

    def render(self) -> str:
        """Prompt/report-ready rendering; empty when never requested."""

        if not self.requested:
            return ""
        if self.skipped_reason is not None:
            return f"Build probe: skipped ({self.skipped_reason})."
        lines = [
            "Build probe: the project's own commands were executed in the "
            "workspace. Exit codes below are ground truth."
        ]
        for result in self.commands:
            verdict = "ok" if result.ok else f"FAILED (exit {result.exit_code})"
            lines.append(f"- `{' '.join(result.command)}` — {verdict}")
            if result.output_tail:
                lines.append("  output tail:")
                lines.extend(
                    f"    {line}" for line in result.output_tail.splitlines()
                )
        for command in self.not_run:
            lines.append(
                f"- `{' '.join(command)}` — not run (a previous command failed)"
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "requested": self.requested,
            "ran": self.ran,
            "succeeded": self.succeeded,
            "skipped_reason": self.skipped_reason,
            "commands": [
                {
                    "command": list(result.command),
                    "exit_code": result.exit_code,
                    "output_tail": result.output_tail,
                }
                for result in self.commands
            ],
            "not_run": [list(command) for command in self.not_run],
        }


def run_build_probe(
    profile: ProjectProfile,
    runner: Optional[CommandRunner],
    workdir: Optional[str],
    *,
    timeout: float,
) -> BuildProbe:
    """Execute the profile's setup and verify commands for real.

    Commands run in order and stop at the first failure — running the test
    suite after a failed restore would only bury the signal. Every skip
    (no real directory, a profile with no locally runnable commands) is
    recorded as a reason, never silent.
    """

    probe = BuildProbe(requested=True)
    if runner is None or workdir is None:
        probe.skipped_reason = "no real workspace directory to run commands in"
        return probe
    commands = [
        tuple(command)
        for command in (profile.setup_command, profile.verify_command)
        if command
    ]
    if not commands:
        probe.skipped_reason = (
            f"the {profile.kind} profile proposes no locally runnable commands"
        )
        return probe
    for index, command in enumerate(commands):
        result = runner.run(list(command), cwd=workdir, timeout=timeout)
        probe.commands.append(
            ProbeCommandResult(
                command=command,
                exit_code=result.exit_code,
                output_tail=result.output[-_PROBE_OUTPUT_TAIL:],
            )
        )
        if not result.ok:
            probe.not_run = commands[index + 1 :]
            break
    return probe


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
        exclude_globs: Paths matching any glob are invisible to the audit
            (tree, statistics, component detection). Defaults keep vendored
            dependencies and build output from eating the tree budget.
        max_tree_entries: How many tree entries the evidence block may list.
        component_fanout: Audit each detected sub-project with its own
            parallel deep-dive after inventory.
        max_components: Cap on parallel component deep-dives.
        osv_scan: Query OSV.dev about exactly-pinned dependencies parsed
            from manifests (degrades gracefully offline).
        save_conventions: Persist the captured house-conventions profile to
            ``.dev_team/conventions.json`` so later delivery runs follow it.
            This and the report are the assessment's only writes.
        update_backlog: Convert findings into stories in the persistent
            backlog (``.dev_team/backlog.json``) so delivery runs can work
            them off. Off by default: it writes to the workspace.
        dormancy_days: Age gap (directory last commit vs repo head) beyond
            which the dead-code probe calls a directory dormant.
        build_probe: Actually run the detected profile's setup and verify
            commands so buildability rests on real exit codes. Off by
            default — it executes the repository's own build (arbitrary
            code) and mutates the working tree the way any build does;
            sandbox untrusted repos.
        build_probe_timeout: Per-command ceiling for the probe, in seconds.
    """

    model: Optional[str] = None
    role_models: Mapping[str, str] = field(default_factory=dict)
    focus: Optional[str] = None
    report_path: Optional[str] = "audit/assessment.md"
    json_retries: int = 1
    exclude_globs: Sequence[str] = DEFAULT_EXCLUDE_GLOBS
    max_tree_entries: int = _TREE_ENTRIES
    component_fanout: bool = False
    max_components: int = 12
    osv_scan: bool = True
    save_conventions: bool = True
    update_backlog: bool = False
    dormancy_days: int = 365
    build_probe: bool = False
    build_probe_timeout: float = 600.0


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


def inventory_stats(
    workspace: Workspace, *, exclude_globs: Sequence[str] = ()
) -> InventoryStats:
    """Count files, lines, and extensions — exactly, without a model."""

    stats = InventoryStats()
    for path in workspace.list_files():
        if path.startswith(".dev_team/") or path.startswith(".git/"):
            continue
        if path_excluded(path, exclude_globs):
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


@dataclass(frozen=True)
class Component:
    """A sub-project detected from a manifest somewhere in the tree."""

    name: str
    path: str
    manifest: str


def detect_components(
    workspace: Workspace, exclude_globs: Sequence[str] = ()
) -> List[Component]:
    """Sub-projects found by their manifests, one per directory, sorted."""

    components: Dict[str, Component] = {}
    for file in sorted(workspace.list_files()):
        if file.startswith(".dev_team/") or path_excluded(file, exclude_globs):
            continue
        name = file.rsplit("/", 1)[-1]
        if name not in _COMPONENT_MANIFESTS and not name.endswith(".csproj"):
            continue
        directory = file.rsplit("/", 1)[0] if "/" in file else ""
        if directory in components:
            continue
        display = directory.rsplit("/", 1)[-1] if directory else "(root)"
        components[directory] = Component(name=display, path=directory, manifest=file)
    return list(components.values())


def _components_block(components: Sequence[Component]) -> str:
    """Deterministic component listing for the evidence block."""

    if not components:
        return ""
    lines = [f"Detected components ({len(components)}):"]
    for component in components[:30]:
        location = component.path or "(root)"
        lines.append(f"- {location} — manifest: {component.manifest}")
    if len(components) > 30:
        lines.append(f"- ... and {len(components) - 30} more")
    return "\n".join(lines)


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
    dead_code: DeadCodeReport = field(default_factory=DeadCodeReport)
    dependency_scan: DependencyScan = field(default_factory=DependencyScan)
    detected_components: List[Component] = field(default_factory=list)
    conventions: Optional[ConventionsProfile] = None
    backlog_stories: List[str] = field(default_factory=list)
    build_probe: BuildProbe = field(default_factory=BuildProbe)
    blind_spots: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True when nothing failed: no abort and every core phase produced data.

        Advisory phases (conventions, component deep-dives) enrich the report;
        their failure is recorded in it but does not void the audit verdict.
        """

        return not self.aborted and all(
            result.ok
            for name, result in self.phases.items()
            if name not in _ADVISORY_PHASES
        )

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
        command_runner: Optional[CommandRunner] = None,
        osv_fetch: Optional[Fetch] = None,
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
        # Read-only git queries (dead-code dormancy) need a real directory.
        self.command_runner = command_runner or (
            SubprocessCommandRunner() if self.workdir is not None else None
        )
        self._osv_fetch = osv_fetch

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
        excludes = self.config.exclude_globs
        profile = detect_project(self.workspace)
        build_probe = BuildProbe()
        if self.config.build_probe:
            build_probe = run_build_probe(
                profile,
                self.command_runner,
                self.workdir,
                timeout=self.config.build_probe_timeout,
            )
            if build_probe.skipped_reason is not None:
                self._event(
                    "build-probe",
                    f"Build probe skipped: {build_probe.skipped_reason}",
                )
            else:
                verdict = "green" if build_probe.succeeded else "red"
                self._event(
                    "build-probe",
                    f"Build probe {verdict}: "
                    f"{len(build_probe.commands)} command(s) executed",
                )
        stats = inventory_stats(self.workspace, exclude_globs=excludes)
        dead_code = detect_dead_code(
            self.workspace,
            runner=self.command_runner,
            workdir=self.workdir,
            dormancy_days=self.config.dormancy_days,
        )
        if dead_code.findings:
            self._event(
                "dead-code",
                f"Dead-code probes: {len(dead_code.findings)} finding(s)",
            )
        scan = scan_dependencies(
            self.workspace, fetch=self._osv_fetch, enabled=self.config.osv_scan
        )
        if scan.queried:
            self._event(
                "dependencies",
                f"OSV scan: {len(scan.vulnerabilities)} vulnerability record(s) "
                f"across {len(scan.dependencies)} pinned dependencies",
            )
        components = detect_components(self.workspace, excludes)
        convention_sources = detect_convention_sources(self.workspace)
        ctx = build_repo_context(
            self.workspace,
            max_tree_entries=self.config.max_tree_entries,
            exclude_globs=excludes,
        )
        evidence = "\n\n".join(
            part
            for part in (
                ctx.render(),
                stats.render(),
                _components_block(components),
                dead_code.render(),
                scan.render(),
                build_probe.render(),
            )
            if part
        )
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
                profile,
                stats,
                phases,
                "",
                focus=focus,
                aborted=True,
                dead_code=dead_code,
                dependency_scan=scan,
                detected_components=components,
                build_probe=build_probe,
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
        tasks = [
            self._phase(name, agent, prompt) for name, (agent, prompt) in deep.items()
        ]
        tasks.append(
            self._phase(
                "conventions",
                self.architect,
                _CONVENTIONS_PROMPT.format(
                    sources=", ".join(convention_sources) or "(none found)",
                    evidence=evidence,
                    focus=focus_block,
                ),
            )
        )
        if self.config.component_fanout and components:
            tasks.append(self._component_phase(components, evidence, focus_block))
        results = await asyncio.gather(*tasks)
        for result in results:
            phases[result.phase] = result

        conventions_profile = self._persist_conventions(phases, convention_sources)

        findings = _render_findings_for_prompt(phases)
        phases["recommendation"] = await self._phase(
            "recommendation",
            self.manager,
            _RECOMMENDATION_PROMPT.format(findings=findings, focus=focus_block),
        )

        executive = await self._executive_summary(phases)
        outcome = self._outcome(
            profile,
            stats,
            phases,
            executive,
            focus=focus,
            dead_code=dead_code,
            dependency_scan=scan,
            detected_components=components,
            conventions=conventions_profile,
            build_probe=build_probe,
        )
        if self.config.report_path is not None:
            self.workspace.write_text(self.config.report_path, outcome.report_markdown)
            self._event("report", f"Report written to {self.config.report_path}")
        if self.config.update_backlog:
            self._update_backlog(outcome)
        verdict = outcome.classification or "unclassified"
        self.tracer.end(run_span)
        self._event("done", f"Assessment finished: {verdict}", detail=f"${outcome.cost_usd:.4f}")
        return outcome

    async def _component_phase(
        self, components: Sequence[Component], evidence: str, focus_block: str
    ) -> PhaseResult:
        """Parallel per-component deep-dives merged into one phase result."""

        capped = list(components[: self.config.max_components])
        self._event("components", f"Deep-diving {len(capped)} component(s) in parallel")

        async def audit(component: Component) -> Dict:
            span = self.tracer.start(
                "assessment", f"component:{component.path or '(root)'}"
            )
            prompt = _COMPONENT_PROMPT.format(
                name=component.name,
                path=component.path or ".",
                manifest=component.manifest,
                evidence=evidence,
                focus=focus_block,
            )
            entry: Dict = {"name": component.name, "path": component.path or "(root)"}
            try:
                data = await self.architect.ask_json(
                    prompt, allowed_tools=READ_ONLY_TOOLS, cwd=self.workdir
                )
            except BudgetExceededError:
                self.tracer.end(span, "budget")
                entry["error"] = "budget exhausted"
                return entry
            except AgentResponseError as exc:
                self.tracer.end(span, "error")
                entry["error"] = str(exc)
                return entry
            self.tracer.end(span, "done")
            entry["summary"] = str(data.get("summary", ""))
            entry["findings"] = [
                item for item in data.get("findings", []) if isinstance(item, dict)
            ]
            return entry

        audited = await asyncio.gather(*(audit(c) for c in capped))
        skipped = len(components) - len(capped)
        summary = f"{len(capped)} component(s) audited in parallel"
        if skipped:
            summary += (
                f"; {skipped} skipped (max_components={self.config.max_components})"
            )
        return PhaseResult(
            phase="components",
            role="architect",
            data={"summary": summary, "components": list(audited)},
        )

    def _persist_conventions(
        self, phases: Dict[str, PhaseResult], sources: List[str]
    ) -> Optional[ConventionsProfile]:
        """Build the conventions profile and save it for delivery runs."""

        result = phases.get("conventions")
        if result is None or not result.ok:
            return None
        profile = ConventionsProfile.from_dict(
            {
                "summary": result.data.get("summary", ""),
                "conventions": result.data.get("conventions", []),
                "sources": sources,
            }
        )
        if profile.empty:
            return None
        if self.config.save_conventions:
            store = ConventionsStore(self.workspace)
            store.save(profile)
            self._event("conventions", f"House conventions saved to {store.path}")
        return profile

    def _update_backlog(self, outcome: AssessmentOutcome) -> None:
        """Mirror the findings into the persistent backlog as stories."""

        store = BacklogStore(self.workspace)
        backlog = store.load()
        stories = outcome_to_backlog(outcome, backlog)
        if stories:
            store.save(backlog)
        outcome.backlog_stories = [s.title for s in stories]
        self._event(
            "backlog", f"{len(stories)} remediation story(ies) added to the backlog"
        )

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

    def _blind_spot_ignores(self) -> Sequence[str]:
        """The engine's own report directory is not an audit subject."""

        path = self.config.report_path
        if path is None or "/" not in path:
            return ()
        return (path.split("/", 1)[0],)

    def _outcome(
        self,
        profile: ProjectProfile,
        stats: InventoryStats,
        phases: Dict[str, PhaseResult],
        executive: str,
        *,
        focus: Optional[str],
        aborted: bool = False,
        dead_code: Optional[DeadCodeReport] = None,
        dependency_scan: Optional[DependencyScan] = None,
        detected_components: Optional[List[Component]] = None,
        conventions: Optional[ConventionsProfile] = None,
        build_probe: Optional[BuildProbe] = None,
    ) -> AssessmentOutcome:
        dead_code = dead_code or DeadCodeReport()
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
            dead_code=dead_code,
            dependency_scan=dependency_scan or DependencyScan(),
            detected_components=list(detected_components or []),
            conventions=conventions,
            build_probe=build_probe or BuildProbe(),
            blind_spots=audit_blind_spots(
                stats, phases, dead_code, ignore=self._blind_spot_ignores()
            ),
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
        "dead_code": outcome.dead_code.to_dict(),
        "dependency_scan": outcome.dependency_scan.to_dict(),
        "build_probe": outcome.build_probe.to_dict(),
        "blind_spots": list(outcome.blind_spots),
        "detected_components": [vars(c) for c in outcome.detected_components],
        "conventions": (
            outcome.conventions.to_dict() if outcome.conventions is not None else None
        ),
        "backlog_stories": list(outcome.backlog_stories),
        "report_markdown": outcome.report_markdown,
    }


def _effort_points(effort: str) -> int:
    """Map a free-text effort estimate onto story points, conservatively."""

    text = effort.lower()
    if "month" in text:
        return 13
    if "week" in text:
        return 8
    if "day" in text:
        return 3
    return 2


def outcome_to_backlog(outcome: AssessmentOutcome, backlog: Backlog) -> List[Story]:
    """Convert assessment findings into backlog stories a delivery can work.

    This is the bridge from "audited" to "remediated": remediation-plan steps,
    build blockers, must-fix dependencies, hardcoded secrets, dead-code probe
    hits, and live vulnerability records each become a story under one
    "Assessment remediation" epic. Stories deduplicate by title, so re-running
    an assessment refreshes the backlog instead of flooding it.
    """

    epic_title = "Assessment remediation"
    epic = next((e for e in backlog.epics if e.title == epic_title), None)
    if epic is None:
        epic = backlog.add_epic(
            epic_title,
            f"From repository assessment (classification: "
            f"{outcome.classification or 'unclassified'})",
        )
    existing = {s.title for s in backlog.stories}
    added: List[Story] = []

    def add(title: str, description: str, estimate: int = 2) -> None:
        title = title[:200]
        if title in existing:
            return
        existing.add(title)
        added.append(
            backlog.add_story(title, description, estimate=estimate, epic_id=epic.id)
        )

    recommendation = outcome.phases.get("recommendation")
    if recommendation is not None and recommendation.ok:
        for step in recommendation.data.get("plan", []):
            if not isinstance(step, dict):
                continue
            name = str(step.get("step", "")).strip()
            if name:
                add(
                    name,
                    str(step.get("detail", "")),
                    _effort_points(str(step.get("effort", ""))),
                )
    buildability = outcome.phases.get("buildability")
    if buildability is not None and buildability.ok:
        for blocker in buildability.data.get("blockers", []):
            if not isinstance(blocker, dict):
                continue
            if blocker.get("category") != "must-fix-to-build":
                continue
            claim = str(blocker.get("claim", "")).strip()
            if claim:
                add(
                    f"Fix build blocker: {claim}",
                    f"Evidence: {blocker.get('evidence', '')}",
                    3,
                )
    risk = outcome.phases.get("risk")
    if risk is not None and risk.ok:
        for dep in risk.data.get("dependencies", []):
            if isinstance(dep, dict) and dep.get("action") == "must-fix":
                add(
                    f"Upgrade or replace dependency {dep.get('name', '?')}",
                    f"{dep.get('version', '')} {dep.get('status', '')} "
                    f"(evidence: {dep.get('evidence', '')})".strip(),
                    3,
                )
        for secret in risk.data.get("secrets", []):
            if isinstance(secret, dict) and secret.get("claim"):
                add(
                    f"Remove hardcoded secret: {secret['claim']}",
                    f"Evidence: {secret.get('evidence', '')}",
                    1,
                )
    by_probe: Dict[str, List[str]] = {}
    for finding in outcome.dead_code.findings:
        by_probe.setdefault(finding.probe, []).append(finding.path)
    for probe, paths in sorted(by_probe.items()):
        listed = ", ".join(paths[:20]) + (" …" if len(paths) > 20 else "")
        add(f"Remove dead code ({probe}: {len(paths)} path(s))", listed, 3)
    for vulnerability in outcome.dependency_scan.vulnerabilities:
        dep = vulnerability.dependency
        add(
            f"Patch {dep.name} {dep.version}: {vulnerability.id}",
            f"{vulnerability.url} (manifest: {dep.manifest})",
        )
    return added


#: Keys whose string values count as a citation of a repository path.
_CITATION_KEYS = frozenset({"evidence", "path"})


def _cited_strings(value, *, under_citation: bool = False) -> Iterator[str]:
    """Every string cited under an ``evidence``/``path`` key, recursively.

    Handles both shapes agents produce: a plain string and a list of strings.
    """

    if isinstance(value, str):
        if under_citation:
            yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _cited_strings(item, under_citation=key in _CITATION_KEYS)
    elif isinstance(value, list):
        for item in value:
            yield from _cited_strings(item, under_citation=under_citation)


def _mentions(text: str, top: str) -> bool:
    """Whether ``text`` cites a path inside the top-level entry ``top``."""

    for token in re.split(r"""[\s,;'"`()\[\]<>]+""", text):
        token = token.rstrip(".:")
        if token == top or token.startswith(top + "/"):
            return True
    return False


def audit_blind_spots(
    stats: InventoryStats,
    phases: Dict[str, PhaseResult],
    dead_code: DeadCodeReport,
    *,
    ignore: Sequence[str] = (),
) -> List[str]:
    """Top-level directories no finding ever cited — exact, no model.

    Agents sample files on large repositories, and a sampled audit reads as
    a complete one unless the gaps are named. A directory the deterministic
    inventory counted but no phase finding (nor dead-code probe) cited was
    never actually examined; the report says so instead of implying coverage.

    ``ignore`` names top-level entries that are not audit subjects at all —
    the engine passes its own report directory, so a re-assessment does not
    flag the previous run's report as "unexamined".
    """

    cited: List[str] = [f.path for f in dead_code.findings]
    for result in phases.values():
        cited.extend(_cited_strings(result.data))
    return [
        top
        for top in sorted(stats.loc_by_top)
        if top != "(root)"
        and top not in ignore
        and not any(_mentions(text, top) for text in cited)
    ]


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

    conventions = outcome.phases.get("conventions")
    if conventions is not None:
        lines += ["", "## House conventions", ""]
        if not conventions.ok:
            lines.append(f"_Phase failed ({conventions.role}): {conventions.error}_")
        summary = conventions.data.get("summary")
        if summary:
            lines.append(str(summary))
        items = _items(conventions.data, "conventions")
        if items:
            lines.append("")
            _cited(lines, items, "aspect", "convention")
        if outcome.conventions is not None and outcome.conventions.sources:
            lines += [
                "",
                "Machine-readable style configs: "
                + ", ".join(f"`{s}`" for s in outcome.conventions.sources),
            ]

    component_phase = outcome.phases.get("components")
    if component_phase is not None and component_phase.data:
        lines += ["", "## Component deep-dives", ""]
        lines.append(str(component_phase.data.get("summary", "")))
        for entry in _items(component_phase.data, "components"):
            lines += ["", f"### {entry.get('name', '?')} (`{entry.get('path', '?')}`)", ""]
            if entry.get("error"):
                lines.append(f"_Deep-dive failed: {entry['error']}_")
                continue
            if entry.get("summary"):
                lines.append(str(entry["summary"]))
            findings = [f for f in entry.get("findings", []) if isinstance(f, dict)]
            if findings:
                lines.append("")
                _cited(lines, findings, "claim")

    lines += ["", "## Appendix — deterministic inventory", ""]
    lines.append(outcome.stats.render())
    for block in (
        outcome.dead_code.render(),
        outcome.dependency_scan.render(),
        outcome.build_probe.render(),
    ):
        if block:
            lines += ["", block]
    if outcome.blind_spots:
        lines += [
            "",
            "Audit blind spots — top-level directories no finding cited: "
            + ", ".join(f"`{name}/`" for name in outcome.blind_spots)
            + ". Treat them as unexamined, not as clean.",
        ]
    lines.append("")
    if outcome.dependency_scan.queried:
        lines.append(
            f"_Cost: ${outcome.cost_usd:.4f}. Dependency findings include a live "
            "OSV.dev vulnerability scan of the exactly-pinned dependencies; other "
            "CVE/EOL observations come from model knowledge._"
        )
    else:
        lines.append(
            f"_Cost: ${outcome.cost_usd:.4f}. Dependency/CVE observations come from "
            "model knowledge, not a live vulnerability scan._"
        )
    return "\n".join(lines)
