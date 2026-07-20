"""Detect what kind of project a workspace holds and how to verify it.

Static gate configuration is what breaks deliveries on repos whose stack the
caller didn't anticipate: `pytest -q` against a Node project fails every
attempt with baffling feedback. :func:`detect_project` inspects the
workspace's manifests and proposes the verify (and setup) commands that match
what is actually there; the engine uses it whenever no explicit
``verify_command`` was configured.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
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

# Manifest filenames that identify a python project, shared by the
# root-level check and the nested (depth-1) scan below.
_PYTHON_MARKERS = frozenset(
    {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "requirements.txt"}
)


def manifest_kind_for_filename(name: str) -> Optional[str]:
    """The project ``kind`` a bare filename identifies, or ``None``.

    Mirrors the root-level manifest rules in :func:`_detect_from_manifests`
    so a nested manifest one directory down is recognised by the same
    filenames a root-level one would be. Exported (not module-private) so
    :func:`dev_team.engine.Engine._manifest_signature` can fingerprint the
    same nested manifest filenames this module's depth-1 scan reacts to.
    """

    if name.endswith(".sln") or name.endswith(".csproj") or name == "global.json":
        return "dotnet"
    if name == "package.json":
        return "node"
    if name == "Cargo.toml":
        return "rust"
    if name == "go.mod":
        return "go"
    if name in _PYTHON_MARKERS:
        return "python"
    return None


def _nested_manifest_matches(files: set) -> list:
    """``(path, kind)`` pairs for manifests exactly one directory down.

    Bounded to depth 1 (``f.count("/") == 1``) — no recursion, and reused
    from the same ``files`` set the caller already computed rather than a
    new filesystem walk.
    """

    matches = []
    for f in sorted(files):
        if f.count("/") != 1:
            continue
        _directory, name = f.split("/", 1)
        kind = manifest_kind_for_filename(name)
        if kind is not None:
            matches.append((f, kind))
    return matches


def _detect_nested_manifest(workspace: Workspace, files: set) -> ProjectProfile:
    """Depth-1 nested-manifest fallback, once no root-level manifest matches.

    A single nested directory with a single recognised kind degrades to
    evidence-based review (``locally_runnable=False``) instead of the bare
    ``unknown``/pytest guess, naming the nested path so the degrade is
    diagnosable. Two or more candidate directories or kinds are ambiguous —
    behaviour stays the unchanged ``unknown``/pytest guess, only the reason
    is enriched to name what was found.
    """

    matches = _nested_manifest_matches(files)
    if not matches:
        return ProjectProfile(
            kind="unknown",
            verify_command=_FALLBACK,
            reason="no recognised manifest; defaulting to pytest",
        )

    dirs = {path.split("/", 1)[0] for path, _kind in matches}
    kinds = {kind for _path, kind in matches}
    if len(dirs) > 1 or len(kinds) > 1:
        candidates = ", ".join(path for path, _kind in matches)
        return ProjectProfile(
            kind="unknown",
            verify_command=_FALLBACK,
            reason=(
                f"multiple nested manifest candidates found ({candidates}); "
                "no unambiguous nested stack, defaulting to pytest"
            ),
        )

    path, kind = matches[0]
    _directory, name = path.split("/", 1)
    if kind == "dotnet":
        legacy = _legacy_dotnet_reason(workspace, files)
        if legacy is not None:
            return ProjectProfile(
                kind="dotnet-framework",
                verify_command=None,
                reason=f"{path} at nested path, not workspace root; {legacy}",
                locally_runnable=False,
            )
    return ProjectProfile(
        kind=kind,
        verify_command=None,
        reason=(
            f"{name} found at {path}, not workspace root; "
            "degrading to evidence-based review"
        ),
        locally_runnable=False,
    )


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

    Root-level manifests are tried first — that is where build tooling lives
    in every ecosystem this recognises — with detection order putting the
    most specific manifests first. Only when no root-level manifest matches
    does it fall back to a depth-1 nested-manifest scan (see
    :func:`_detect_nested_manifest`), which degrades to
    ``locally_runnable=False`` rather than proposing a verify command for a
    stack that isn't at the workspace root. A profile whose verify/setup
    command names a binary that isn't actually on this machine's ``PATH`` is
    likewise degraded to ``locally_runnable=False`` (mirroring the
    legacy-.NET-Framework path below) rather than proposing a command
    guaranteed to fail every task.
    """

    return _degrade_if_toolchain_missing(_detect_from_manifests(workspace))


def _missing_binary(command: Optional[Tuple[str, ...]]) -> Optional[str]:
    """The command's binary name if it isn't found via ``shutil.which``."""

    if not command:
        return None
    return command[0] if shutil.which(command[0]) is None else None


def _degrade_if_toolchain_missing(profile: ProjectProfile) -> ProjectProfile:
    """Degrade ``profile`` to not-locally-runnable if its tools aren't on PATH.

    A recognised manifest is worthless as a local gate when the runner lacks
    the toolchain: the command would fail every task for reasons no engineer
    can fix. Already-degraded profiles (``locally_runnable=False``) are
    untouched.
    """

    if not profile.locally_runnable:
        return profile
    missing = _missing_binary(profile.verify_command) or _missing_binary(
        profile.setup_command
    )
    if missing is None:
        return profile
    return replace(
        profile,
        verify_command=None,
        locally_runnable=False,
        reason=f"{profile.reason}; {missing!r} not found on PATH",
    )


def _detect_from_manifests(workspace: Workspace) -> ProjectProfile:
    """The manifest-matching rules, before the toolchain-presence check."""

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
    marker = sorted(_PYTHON_MARKERS & files)
    if marker:
        setup = ("pip", "install", "-r", "requirements.txt") if "requirements.txt" in files else None
        return ProjectProfile(
            kind="python",
            verify_command=_FALLBACK,
            setup_command=setup,
            security_scan_command=("bandit", "-r", ".", "-q", "-x", "./tests,./.dev_team"),
            reason=f"{marker[0]} at workspace root",
        )
    return _detect_nested_manifest(workspace, files)
