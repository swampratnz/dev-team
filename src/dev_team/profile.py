"""Detect what kind of project a workspace holds and how to verify it.

Static gate configuration is what breaks deliveries on repos whose stack the
caller didn't anticipate: `pytest -q` against a Node project fails every
attempt with baffling feedback. :func:`detect_project` inspects the
workspace's manifests and proposes the verify (and setup) commands that match
what is actually there; the engine uses it whenever no explicit
``verify_command`` was configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .execution import Workspace

# Fallback when nothing recognisable is present (greenfield included): the
# team's own QA authors pytest tests, so pytest is the sensible default.
_FALLBACK: Tuple[str, ...] = ("pytest", "-q")


@dataclass(frozen=True)
class ProjectProfile:
    """What the workspace looks like and how to build/verify it.

    ``verify_command`` is ``None`` when the stack was recognised but cannot
    be built or tested on this machine (``locally_runnable`` is then False):
    the delivery engine degrades to evidence-based review or a remote CI
    gate instead of failing every task on a command that can never work.
    """

    kind: str
    verify_command: Optional[Tuple[str, ...]]
    setup_command: Optional[Tuple[str, ...]] = None
    security_scan_command: Optional[Tuple[str, ...]] = None
    reason: str = ""
    locally_runnable: bool = True


# Markers of old-style MSBuild project XML, which the cross-platform `dotnet`
# CLI cannot restore, build, or test on any OS.
_LEGACY_CSPROJ_MARKERS = ("<TargetFrameworkVersion>", "ToolsVersion=")

# How many project files to sample for legacy markers before concluding.
_LEGACY_CSPROJ_SAMPLE = 5


def _legacy_dotnet_reason(workspace: Workspace, files: set) -> Optional[str]:
    """Why the workspace looks like legacy .NET Framework, or ``None``.

    ``packages.config`` anywhere in the tree means legacy NuGet restore;
    old-style project XML (``ToolsVersion``/``TargetFrameworkVersion``) means
    the project predates the SDK-style format. Either way ``dotnet test``
    fails before running a single test.
    """

    for path in sorted(files):
        if path == "packages.config" or path.endswith("/packages.config"):
            return f"{path} (legacy NuGet restore)"
    csprojs = sorted(f for f in files if f.endswith(".csproj"))
    for path in csprojs[:_LEGACY_CSPROJ_SAMPLE]:
        try:
            head = workspace.read_text(path)[:4_000]
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        for marker in _LEGACY_CSPROJ_MARKERS:
            if marker in head:
                return f"{marker.strip('<>=')} in {path}"
    return None


def detect_project(workspace: Workspace) -> ProjectProfile:
    """Inspect ``workspace`` and return the best-matching profile.

    Only root-level manifests are considered — that is where build tooling
    lives in every ecosystem this recognises. Detection order puts the most
    specific manifests first.
    """

    files = set(workspace.list_files())

    # .NET is checked before node: a full-stack .NET monolith commonly keeps
    # a package.json at the root for frontend assets, but the solution file
    # is what defines how the repo builds and tests.
    dotnet_markers = sorted(
        f
        for f in files
        if "/" not in f
        and (f.endswith(".sln") or f.endswith(".csproj") or f == "global.json")
    )
    if dotnet_markers:
        legacy = _legacy_dotnet_reason(workspace, files)
        if legacy is not None:
            # .NET Framework: buildable only by MSBuild on Windows. No local
            # command can verify it, so none is proposed.
            return ProjectProfile(
                kind="dotnet-framework",
                verify_command=None,
                reason=f"{dotnet_markers[0]} at workspace root; {legacy}",
                locally_runnable=False,
            )
        return ProjectProfile(
            kind="dotnet",
            verify_command=("dotnet", "test"),
            setup_command=("dotnet", "restore"),
            security_scan_command=(
                "dotnet", "list", "package", "--vulnerable", "--include-transitive",
            ),
            reason=f"{dotnet_markers[0]} at workspace root",
        )

    if "package.json" in files:
        return ProjectProfile(
            kind="node",
            verify_command=("npm", "test"),
            setup_command=("npm", "install"),
            security_scan_command=("npm", "audit", "--audit-level=high"),
            reason="package.json at workspace root",
        )
    if "Cargo.toml" in files:
        return ProjectProfile(
            kind="rust",
            verify_command=("cargo", "test"),
            reason="Cargo.toml at workspace root",
        )
    if "go.mod" in files:
        return ProjectProfile(
            kind="go",
            verify_command=("go", "test", "./..."),
            reason="go.mod at workspace root",
        )
    python_markers = {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "requirements.txt"}
    marker = sorted(python_markers & files)
    if marker:
        setup = ("pip", "install", "-r", "requirements.txt") if "requirements.txt" in files else None
        return ProjectProfile(
            kind="python",
            verify_command=_FALLBACK,
            setup_command=setup,
            security_scan_command=("bandit", "-r", ".", "-q", "-x", "./tests,./.dev_team"),
            reason=f"{marker[0]} at workspace root",
        )
    return ProjectProfile(
        kind="unknown",
        verify_command=_FALLBACK,
        reason="no recognised manifest; defaulting to pytest",
    )
