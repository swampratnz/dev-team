"""Deterministic dead-code probes for the assessment engine.

Model-inferred "this looks unused" claims are cheap to make and expensive to
verify. These probes are the opposite: pure, exact analyses that need no LLM
and produce citable findings. Each probe is independent and skips itself
(with a recorded reason) when its preconditions are missing, so the set is
pluggable per ecosystem:

- ``unreferenced-sources`` — old-style MSBuild projects list every compiled
  file as an explicit ``<Compile Include>`` item, so a ``.cs`` file on disk
  that no project references is *literally* dead: it ships in the repo but is
  never built.
- ``orphaned-projects`` — a ``.csproj`` on disk that no ``.sln`` includes is
  a whole project the build never sees.
- ``dormant-directories`` — a top-level directory whose last commit is far
  older than the repository's newest commit, in an otherwise active repo, is
  a strong dormancy signal (requires git; skipped without it).
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .execution import CommandRunner, Workspace

# Build output and bookkeeping are never "dead code".
_IGNORED_PREFIXES = ("bin/", "obj/", ".git/", ".dev_team/", "packages/")

# How many top-level directories the dormancy probe will ask git about.
_MAX_DORMANCY_DIRS = 50

_SECONDS_PER_DAY = 86_400

# Solution-file project lines: Project("{guid}") = "Name", "rel\path.csproj", ...
_SLN_PROJECT = re.compile(r'=\s*"[^"]+",\s*"([^"]+\.csproj)"', re.IGNORECASE)


@dataclass
class DeadCodeFinding:
    """One probe hit: a path and why it looks dead."""

    probe: str
    path: str
    detail: str


@dataclass
class DeadCodeReport:
    """Everything the dead-code probes produced (and what was skipped)."""

    findings: List[DeadCodeFinding] = field(default_factory=list)
    probes_run: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def render(self) -> str:
        """Prompt/report-ready rendering; exact, no speculation."""

        if not self.probes_run and not self.skipped:
            return ""
        lines = [
            "Deterministic dead-code probes "
            f"({len(self.findings)} finding(s) from {', '.join(self.probes_run) or 'none'}):"
        ]
        for finding in self.findings:
            lines.append(f"- [{finding.probe}] {finding.path} — {finding.detail}")
        for reason in self.skipped:
            lines.append(f"- (probe skipped: {reason})")
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "findings": [vars(f) for f in self.findings],
            "probes_run": list(self.probes_run),
            "skipped": list(self.skipped),
        }


def _ignored(path: str) -> bool:
    return path.startswith(_IGNORED_PREFIXES) or any(
        segment in ("bin", "obj") for segment in path.split("/")[:-1]
    )


def _norm_relative(base_dir: str, relative: str) -> str:
    """Resolve an MSBuild-style relative path against a project directory."""

    cleaned = relative.replace("\\", "/")
    return posixpath.normpath(posixpath.join(base_dir, cleaned))


def _csproj_compile_items(xml_text: str) -> Optional[Set[str]]:
    """Explicit ``<Compile Include>`` values of a project file.

    Returns ``None`` when the project cannot be analysed precisely: malformed
    XML, no explicit Compile items (SDK-style projects glob implicitly), or
    wildcard includes.
    """

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    items: Set[str] = set()
    for element in root.iter():
        if not element.tag.endswith("Compile"):
            continue
        include = element.get("Include")
        if not include:
            continue
        if "*" in include or "?" in include:
            return None
        items.add(include)
    return items or None


def probe_unreferenced_sources(
    workspace: Workspace, files: Sequence[str]
) -> Tuple[List[DeadCodeFinding], Optional[str]]:
    """``.cs`` files on disk that no legacy project compiles.

    Only directories owned by an *analysable* project (explicit Compile
    items, no wildcards) are judged — an SDK-style or wildcard project globs
    sources implicitly, so absence from its XML proves nothing.
    """

    csprojs = [f for f in files if f.endswith(".csproj") and not _ignored(f)]
    if not csprojs:
        return [], "unreferenced-sources: no .csproj files found"
    referenced: Set[str] = set()
    owned_dirs: List[str] = []
    # A project at the repo root owns only root-level files. Recording its base
    # as "" and using startswith("") would match *every* path in the repo —
    # flagging files compiled by other, unanalysable projects — so root
    # ownership is tracked with this dedicated flag instead of a prefix.
    owns_root = False
    analysable = 0
    for project in csprojs:
        try:
            items = _csproj_compile_items(workspace.read_text(project))
        except (OSError, UnicodeDecodeError, ValueError):
            items = None
        if items is None:
            continue
        analysable += 1
        base = posixpath.dirname(project)
        if base:
            owned_dirs.append(f"{base}/")
        else:
            owns_root = True
        referenced.update(_norm_relative(base, item) for item in items)
    if not analysable:
        return [], (
            "unreferenced-sources: no analysable legacy project "
            "(explicit <Compile> items) found"
        )
    findings = []
    for path in sorted(files):
        if not path.endswith(".cs") or _ignored(path):
            continue
        if path in referenced:
            continue
        if "/" in path:
            owned = any(path.startswith(prefix) for prefix in owned_dirs)
        else:
            # A root-level file (no "/") is owned only by a root-level project.
            owned = owns_root
        if owned:
            findings.append(
                DeadCodeFinding(
                    probe="unreferenced-sources",
                    path=path,
                    detail="on disk but not referenced by any project's <Compile> items",
                )
            )
    return findings, None


def probe_orphaned_projects(
    workspace: Workspace, files: Sequence[str]
) -> Tuple[List[DeadCodeFinding], Optional[str]]:
    """``.csproj`` files that no solution file includes."""

    solutions = [f for f in files if f.endswith(".sln") and not _ignored(f)]
    if not solutions:
        return [], "orphaned-projects: no .sln files found"
    included: Set[str] = set()
    for sln in solutions:
        try:
            text = workspace.read_text(sln)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        base = posixpath.dirname(sln)
        for match in _SLN_PROJECT.finditer(text):
            included.add(_norm_relative(base, match.group(1)))
    findings = [
        DeadCodeFinding(
            probe="orphaned-projects",
            path=path,
            detail="project file not referenced by any solution",
        )
        for path in sorted(files)
        if path.endswith(".csproj") and not _ignored(path) and path not in included
    ]
    return findings, None


def probe_dormant_directories(
    runner: CommandRunner,
    workdir: str,
    files: Sequence[str],
    *,
    dormancy_days: int = 365,
) -> Tuple[List[DeadCodeFinding], Optional[str]]:
    """Top-level directories whose last commit lags the repo head by a lot."""

    head = runner.run(["git", "log", "-1", "--format=%ct"], cwd=workdir)
    try:
        # A failed git run has no epoch on stdout, so one parse covers both.
        head_time = int(head.stdout.strip())
    except ValueError:
        return [], "dormant-directories: not a git repository (or no commits)"
    top_dirs = sorted({f.split("/", 1)[0] for f in files if "/" in f and not _ignored(f)})
    findings = []
    for directory in top_dirs[:_MAX_DORMANCY_DIRS]:
        result = runner.run(
            ["git", "log", "-1", "--format=%ct", "--", directory], cwd=workdir
        )
        try:
            last_time = int(result.stdout.strip())
        except ValueError:
            # Untracked directory: git has no history for it at all.
            continue
        age_days = (head_time - last_time) // _SECONDS_PER_DAY
        if age_days >= dormancy_days:
            findings.append(
                DeadCodeFinding(
                    probe="dormant-directories",
                    path=f"{directory}/",
                    detail=(
                        f"last commit {age_days} day(s) before the repository head "
                        "— dormant while the repo stayed active"
                    ),
                )
            )
    return findings, None


def detect_dead_code(
    workspace: Workspace,
    *,
    runner: Optional[CommandRunner] = None,
    workdir: Optional[str] = None,
    dormancy_days: int = 365,
) -> DeadCodeReport:
    """Run every applicable probe and aggregate a :class:`DeadCodeReport`."""

    files = [f for f in workspace.list_files() if not f.startswith(".dev_team/")]
    report = DeadCodeReport()

    static_probes = (
        ("unreferenced-sources", lambda: probe_unreferenced_sources(workspace, files)),
        ("orphaned-projects", lambda: probe_orphaned_projects(workspace, files)),
    )
    for name, probe in static_probes:
        findings, skipped = probe()
        if skipped is not None:
            report.skipped.append(skipped)
            continue
        report.probes_run.append(name)
        report.findings.extend(findings)

    if runner is not None and workdir is not None:
        findings, skipped = probe_dormant_directories(
            runner, workdir, files, dormancy_days=dormancy_days
        )
        if skipped is not None:
            report.skipped.append(skipped)
        else:
            report.probes_run.append("dormant-directories")
            report.findings.extend(findings)
    else:
        report.skipped.append("dormant-directories: no git working directory available")
    return report
