"""Live dependency vulnerability scanning backed by OSV.dev.

The risk phase's CVE claims otherwise come from model knowledge — plausible,
stale, and unverifiable. This module is the deterministic counterpart: exact
pins are parsed straight out of the manifests (NuGet ``packages.config``,
``package.json``, ``requirements.txt``, ``Cargo.toml``) *and* the lockfiles
(``package-lock.json``, ``poetry.lock``, ``Cargo.lock``, NuGet
``packages.lock.json``) and checked against
the OSV.dev batch API, which covers every major
ecosystem through one endpoint. Lockfiles matter on range-specified projects:
a ``package.json`` full of ``^`` ranges yields nothing scannable, but its
lockfile pins every resolved version exactly. No network (or a failed query)
degrades gracefully: the parsed inventory still feeds the report, annotated
that the live scan was unavailable.
"""

from __future__ import annotations

import json
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .execution import Workspace

_OSV_ENDPOINT = "https://api.osv.dev/v1/querybatch"
_HTTP_TIMEOUT_SECONDS = 30.0

# One batch, bounded: enough for any sane repo, small enough to never abuse
# the API. Overflow is recorded, not silently dropped.
_MAX_DEPENDENCIES = 500

#: A ``fetch`` callable posts the querybatch payload and returns the response.
Fetch = Callable[[Dict], Dict]


@dataclass(frozen=True)
class Dependency:
    """One exactly-pinned dependency found in a manifest."""

    name: str
    version: str
    ecosystem: str
    manifest: str


@dataclass
class Vulnerability:
    """An OSV advisory affecting one scanned dependency."""

    id: str
    dependency: Dependency

    @property
    def url(self) -> str:
        return f"https://osv.dev/vulnerability/{self.id}"


@dataclass
class DependencyScan:
    """What the manifests pin, and what OSV says about it."""

    dependencies: List[Dependency] = field(default_factory=list)
    vulnerabilities: List[Vulnerability] = field(default_factory=list)
    queried: bool = False
    truncated: int = 0
    error: Optional[str] = None

    def render(self) -> str:
        """Prompt/report-ready rendering of the scan."""

        if not self.dependencies:
            return ""
        lines = [
            f"Dependency scan: {len(self.dependencies)} exactly-pinned "
            "dependencies parsed from manifests and lockfiles."
        ]
        if self.queried:
            lines.append(
                f"Live OSV.dev scan: {len(self.vulnerabilities)} known "
                "vulnerability record(s) affecting them."
            )
            for vuln in self.vulnerabilities:
                dep = vuln.dependency
                lines.append(
                    f"- {dep.name} {dep.version} ({dep.ecosystem}, {dep.manifest}): "
                    f"{vuln.id} — {vuln.url}"
                )
        else:
            lines.append(
                "Live OSV.dev scan unavailable"
                + (f" ({self.error})" if self.error else "")
                + " — treat CVE/EOL claims as model knowledge."
            )
        if self.truncated:
            lines.append(
                f"({self.truncated} additional dependencies were not scanned: "
                "batch limit reached.)"
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "dependencies": [vars(d) for d in self.dependencies],
            "vulnerabilities": [
                {"id": v.id, "url": v.url, "dependency": vars(v.dependency)}
                for v in self.vulnerabilities
            ],
            "queried": self.queried,
            "truncated": self.truncated,
            "error": self.error,
        }


def _exact_version(spec: str) -> Optional[str]:
    """The exact version pinned by ``spec``, or ``None`` for a range.

    OSV queries need a concrete version; ``^``/``~`` prefixes are close
    enough to their lower bound to be worth checking, open ranges are not.
    """

    cleaned = spec.strip().lstrip("^~=v")
    if (
        cleaned
        and cleaned[0].isdigit()
        and all(part.isalnum() for part in cleaned.replace("-", ".").split("."))
    ):
        return cleaned
    return None


def parse_packages_config(text: str, manifest: str) -> List[Dependency]:
    """NuGet ``packages.config``: ``<package id=... version=... />`` entries."""

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    deps = []
    for element in root.iter():
        if not element.tag.endswith("package"):
            continue
        name, version = element.get("id"), element.get("version")
        if name and version:
            deps.append(Dependency(name, version, "NuGet", manifest))
    return deps


def parse_package_json(text: str, manifest: str) -> List[Dependency]:
    """npm ``package.json``: dependencies and devDependencies with usable pins."""

    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    deps = []
    for section in ("dependencies", "devDependencies"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        for name, spec in sorted(entries.items()):
            version = _exact_version(str(spec))
            if version is not None:
                deps.append(Dependency(name, version, "npm", manifest))
    return deps


def parse_requirements_txt(text: str, manifest: str) -> List[Dependency]:
    """pip ``requirements.txt``: ``name==version`` pins only."""

    deps = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if "==" not in stripped:
            continue
        name, _, version = stripped.partition("==")
        name = name.strip()
        version = version.strip()
        if name and version:
            deps.append(Dependency(name, version, "PyPI", manifest))
    return deps


def parse_cargo_toml(text: str, manifest: str) -> List[Dependency]:
    """Cargo ``[dependencies]`` with string or ``{version = ...}`` pins."""

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    deps = []
    for section in ("dependencies", "dev-dependencies"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        for name, spec in sorted(entries.items()):
            raw = spec if isinstance(spec, str) else (
                spec.get("version") if isinstance(spec, dict) else None
            )
            version = _exact_version(str(raw)) if raw else None
            if version is not None:
                deps.append(Dependency(name, version, "crates.io", manifest))
    return deps


def parse_package_lock(text: str, manifest: str) -> List[Dependency]:
    """npm ``package-lock.json``: exact resolved versions (v1, v2, and v3).

    v2/v3 lockfiles carry a flat ``packages`` map keyed by install path; v1
    lockfiles nest a ``dependencies`` tree. Both pin exactly, so every entry
    is scannable — this is what rescues range-specified ``package.json``
    projects from the model-knowledge fallback.
    """

    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    deps: List[Dependency] = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        for path, info in sorted(packages.items()):
            # "" is the project itself; link entries point at workspace dirs.
            if not path or not isinstance(info, dict) or info.get("link"):
                continue
            version = info.get("version")
            name = info.get("name") or path.rpartition("node_modules/")[2]
            if name and isinstance(version, str) and version:
                deps.append(Dependency(name, version, "npm", manifest))
        return deps

    def walk(entries: Dict) -> None:
        for name, info in sorted(entries.items()):
            if not isinstance(info, dict):
                continue
            version = info.get("version")
            if isinstance(version, str) and version:
                deps.append(Dependency(name, version, "npm", manifest))
            nested = info.get("dependencies")
            if isinstance(nested, dict):
                walk(nested)

    entries = data.get("dependencies")
    if isinstance(entries, dict):
        walk(entries)
    return deps


def parse_poetry_lock(text: str, manifest: str) -> List[Dependency]:
    """Poetry ``poetry.lock``: every ``[[package]]`` is an exact pin."""

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    entries = data.get("package")
    if not isinstance(entries, list):
        return []
    deps = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name, version = entry.get("name"), entry.get("version")
        if name and version:
            deps.append(Dependency(str(name), str(version), "PyPI", manifest))
    return deps


def parse_cargo_lock(text: str, manifest: str) -> List[Dependency]:
    """Cargo ``Cargo.lock``: exact pins for every external ``[[package]]``.

    The workspace's own crates appear in the lockfile too, distinguishable by
    their missing ``source`` — scanning the project against OSV as if it were
    its own dependency would only produce noise, so those are skipped.
    """

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    entries = data.get("package")
    if not isinstance(entries, list):
        return []
    deps = []
    for entry in entries:
        if not isinstance(entry, dict) or "source" not in entry:
            continue
        name, version = entry.get("name"), entry.get("version")
        if name and version:
            deps.append(Dependency(str(name), str(version), "crates.io", manifest))
    return deps


def parse_packages_lock_json(text: str, manifest: str) -> List[Dependency]:
    """NuGet ``packages.lock.json``: ``resolved`` versions per framework.

    ``type: Project`` entries are references to sibling projects in the same
    solution, not packages — skipped.
    """

    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    frameworks = data.get("dependencies")
    if not isinstance(frameworks, dict):
        return []
    deps = []
    for _, entries in sorted(frameworks.items()):
        if not isinstance(entries, dict):
            continue
        for name, info in sorted(entries.items()):
            if not isinstance(info, dict):
                continue
            if str(info.get("type", "")).lower() == "project":
                continue
            version = info.get("resolved")
            if isinstance(version, str) and version:
                deps.append(Dependency(name, version, "NuGet", manifest))
    return deps


_PARSERS = {
    "packages.config": parse_packages_config,
    "package.json": parse_package_json,
    "requirements.txt": parse_requirements_txt,
    "Cargo.toml": parse_cargo_toml,
    "package-lock.json": parse_package_lock,
    "poetry.lock": parse_poetry_lock,
    "Cargo.lock": parse_cargo_lock,
    "packages.lock.json": parse_packages_lock_json,
}


def collect_dependencies(workspace: Workspace) -> List[Dependency]:
    """Parse every recognised manifest and lockfile, deduplicated."""

    seen = set()
    deps: List[Dependency] = []
    for path in sorted(workspace.list_files()):
        parser = _PARSERS.get(path.rsplit("/", 1)[-1])
        if parser is None:
            continue
        try:
            text = workspace.read_text(path)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        for dep in parser(text, path):
            key = (dep.ecosystem, dep.name, dep.version)
            if key in seen:
                continue
            seen.add(key)
            deps.append(dep)
    return deps


def _http_fetch(payload: Dict) -> Dict:
    """POST ``payload`` to the OSV querybatch endpoint (the default fetch)."""

    request = urllib.request.Request(
        _OSV_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def scan_dependencies(
    workspace: Workspace,
    *,
    fetch: Optional[Fetch] = None,
    enabled: bool = True,
) -> DependencyScan:
    """Collect pinned dependencies and (when enabled) query OSV about them.

    Every failure mode — scanning disabled, nothing pinned, network down,
    malformed response — produces a scan whose :meth:`~DependencyScan.render`
    says exactly what happened; the caller never has to branch.
    """

    scan = DependencyScan(dependencies=collect_dependencies(workspace))
    if len(scan.dependencies) > _MAX_DEPENDENCIES:
        scan.truncated = len(scan.dependencies) - _MAX_DEPENDENCIES
    queryable = scan.dependencies[:_MAX_DEPENDENCIES]
    if not enabled:
        scan.error = "scan disabled"
        return scan
    if not queryable:
        return scan
    payload = {
        "queries": [
            {
                "package": {"name": dep.name, "ecosystem": dep.ecosystem},
                "version": dep.version,
            }
            for dep in queryable
        ]
    }
    try:
        response = (fetch or _http_fetch)(payload)
        results = response["results"]
        if len(results) != len(queryable):
            raise ValueError(
                f"OSV returned {len(results)} results for {len(queryable)} queries"
            )
        for dep, result in zip(queryable, results):
            for vuln in (result or {}).get("vulns") or []:
                scan.vulnerabilities.append(Vulnerability(str(vuln["id"]), dep))
    except Exception as exc:  # network, JSON shape, key errors: degrade, never raise
        scan.error = f"{type(exc).__name__}: {exc}"
        return scan
    scan.queried = True
    return scan
