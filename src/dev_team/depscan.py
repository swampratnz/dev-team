"""Live dependency vulnerability scanning backed by OSV.dev.

The risk phase's CVE claims otherwise come from model knowledge — plausible,
stale, and unverifiable. This module is the deterministic counterpart: exact
pins are parsed straight out of the manifests (NuGet ``packages.config``,
``package.json``, ``requirements.txt``, PEP 621 ``pyproject.toml``,
``Cargo.toml``, Go ``go.mod``, PHP ``composer.json``, Maven ``pom.xml``)
*and* the lockfiles
(``package-lock.json``, ``poetry.lock``, ``Cargo.lock``, NuGet
``packages.lock.json``, Ruby ``Gemfile.lock``, PHP ``composer.lock``) and checked against
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

# Bounds how many stack frames a single manifest's PEP 735 [dependency-groups]
# `include-group` resolution may push in total, across every top-level group
# (see _resolve_dependency_groups) — mirrors _MAX_DEPENDENCIES's role of
# closing off unbounded work from a hostile input without ever raising.
_MAX_GROUP_EXPANSIONS = 500

#: A ``fetch`` callable posts the querybatch payload and returns the response.
Fetch = Callable[[Dict], Dict]


@dataclass(frozen=True)
class Dependency:
    """One dependency found in a manifest or lockfile.

    ``approximate`` marks a version that was *derived* from a caret/tilde range
    (see :func:`_exact_version`) rather than pinned exactly: it is the range's
    lower bound, a floor, not necessarily the version actually installed. A
    lockfile pin for the same package supersedes it (:func:`collect_dependencies`),
    and :meth:`DependencyScan.render` never presents it as an exact pin.
    """

    name: str
    version: str
    ecosystem: str
    manifest: str
    approximate: bool = False


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
        count = len(self.dependencies)
        approximate = sum(1 for dep in self.dependencies if dep.approximate)
        if approximate:
            # A range-derived entry is a lower bound, not the installed
            # version; never let the summary pass it off as an exact pin.
            summary = (
                f"Dependency scan: {count} dependencies parsed from manifests "
                f"and lockfiles ({count - approximate} exactly pinned, "
                f"{approximate} from a version range — lower bound only, not "
                "necessarily the installed version)."
            )
        else:
            summary = (
                f"Dependency scan: {count} exactly-pinned dependencies parsed "
                "from manifests and lockfiles."
            )
        lines = [summary]
        if self.queried:
            lines.append(
                f"Live OSV.dev scan: {len(self.vulnerabilities)} known "
                "vulnerability record(s) affecting them."
            )
            for vuln in self.vulnerabilities:
                dep = vuln.dependency
                # ">= x" flags a lower-bound query so the reader knows OSV was
                # asked about the range floor, not a confirmed installed pin.
                version = f">= {dep.version}" if dep.approximate else dep.version
                lines.append(
                    f"- {dep.name} {version} ({dep.ecosystem}, {dep.manifest}): "
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


def _is_range_spec(spec: str) -> bool:
    """Whether ``spec`` is a caret/tilde range rather than an exact pin.

    :func:`_exact_version` reduces ``^1.2.3``/``~1.2`` to their lower bound so
    the package stays scannable, but that lower bound is only a floor — the
    resolved (lockfile) version is what is actually installed. Flagging these
    lets a concrete pin supersede them and keeps the rendering honest.
    """

    return spec.strip()[:1] in ("^", "~")


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


def parse_pom_xml_deps(text: str, manifest: str) -> List[Dependency]:
    """Maven ``pom.xml``: top-level ``<dependencies>`` entries with a literal pin.

    Only the root's top-level ``<dependencies>`` element is read — not
    ``<dependencyManagement>`` and not per-``<profile>`` dependency blocks —
    mirroring :func:`eolscan.parse_pom_xml_java`'s scoping rationale (profile
    activation is conditional, and a ``dependencyManagement`` entry only
    applies if referenced by an actual dependency; this module has no build
    context to evaluate either). Namespace-stripped tag comparison
    (``tag.rsplit("}", 1)[-1]``) is the same idiom :func:`eolscan.parse_pom_xml_java`
    uses for this identical file format, since Maven POMs declare a default
    XML namespace plain tag comparison would otherwise miss.

    Only a literal, non-empty ``<version>`` counts as a pin: a
    ``${property}``-interpolated version (requiring a ``<properties>``
    lookup or parent-POM inheritance this module has no context for) or a
    missing ``<version>`` (inherited from ``dependencyManagement``, possibly
    in a parent POM never fetched) is skipped, never guessed at. A
    ``<dependency>`` missing ``<groupId>`` or ``<artifactId>`` is skipped too.

    ``ValueError`` is caught alongside ``ET.ParseError`` for the same
    multi-byte-encoding expat quirk :func:`eolscan.parse_pom_xml_java`
    documents.
    """

    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        return []
    deps = []
    for child in root:
        if child.tag.rsplit("}", 1)[-1] != "dependencies":
            continue
        for dependency in child:
            if dependency.tag.rsplit("}", 1)[-1] != "dependency":
                continue
            fields: Dict[str, str] = {}
            for element in dependency:
                tag = element.tag.rsplit("}", 1)[-1]
                if tag in ("groupId", "artifactId", "version"):
                    fields[tag] = (element.text or "").strip()
            group_id = fields.get("groupId")
            artifact_id = fields.get("artifactId")
            version = fields.get("version")
            if not group_id or not artifact_id or not version:
                continue
            if "${" in version:
                continue
            deps.append(Dependency(f"{group_id}:{artifact_id}", version, "Maven", manifest))
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
                deps.append(
                    Dependency(
                        name, version, "npm", manifest,
                        approximate=_is_range_spec(str(spec)),
                    )
                )
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
                deps.append(
                    Dependency(
                        name, version, "crates.io", manifest,
                        approximate=_is_range_spec(str(raw)),
                    )
                )
    return deps


def _pep508_pin(spec: str) -> Optional[tuple]:
    """Exact ``(name, version)`` pin from a PEP 508 dependency string.

    The environment marker (after ``;``) and any extras (``[...]``) are
    stripped from the name first; anything but a single ``==`` (a range, a
    comma-separated constraint, no version at all) is not a pin and yields
    ``None`` — mirrors :func:`parse_requirements_txt`'s ``==``-only pip
    behaviour rather than a full PEP 440 comparator.
    """

    without_marker = spec.split(";", 1)[0].strip()
    if without_marker.count("==") != 1:
        return None
    name_part, _, version = without_marker.partition("==")
    name = name_part.split("[", 1)[0].strip()
    version = version.strip()
    if name and version:
        return name, version
    return None


def _resolve_dependency_groups(groups: Dict) -> List[str]:
    """Flatten a PEP 735 ``[dependency-groups]`` table into PEP 508 spec
    strings, resolving ``{include-group = "..."}`` composition transitively.

    Walked with an explicit stack, not Python recursion: the table comes from
    an untrusted, cloned third-party repo whose composition graph is fully
    attacker-controlled, and an explicit stack has no ``RecursionError``
    ceiling on a deep or wide malicious chain. Each stack frame carries the
    set of group names already visited on *that* path, so re-visiting a name
    on the same path — a direct self-reference or an indirect cycle like
    ``a`` -> ``b`` -> ``a`` — is dropped rather than re-expanded. A single
    counter shared across every top-level group caps the total amount of
    work the whole table may do (:data:`_MAX_GROUP_EXPANSIONS`), closing off
    both a deep chain of pops *and* a single group whose own entry list is
    made attacker-long (a single pop can otherwise walk an unbounded ``for
    entry in entries`` loop) from unbounded work; once the cap is hit,
    resolution stops early for the rest of the manifest rather than raising.
    """

    specs: List[str] = []
    expansions = 0
    for name in groups:
        stack: List[tuple] = [(name, frozenset())]
        while stack:
            if expansions >= _MAX_GROUP_EXPANSIONS:
                return specs
            expansions += 1
            current, visited_in_path = stack.pop()
            if current in visited_in_path:
                continue
            entries = groups.get(current)
            if not isinstance(entries, list):
                continue
            next_visited = visited_in_path | {current}
            for entry in entries:
                if expansions >= _MAX_GROUP_EXPANSIONS:
                    return specs
                expansions += 1
                if isinstance(entry, str):
                    specs.append(entry)
                elif isinstance(entry, dict):
                    ref = entry.get("include-group")
                    if isinstance(ref, str):
                        stack.append((ref, next_visited))
    return specs


def parse_pyproject_toml(text: str, manifest: str) -> List[Dependency]:
    """PEP 621 ``pyproject.toml``: ``==`` pins from ``[project.dependencies]``,
    ``[project.optional-dependencies]``, and PEP 735 ``[dependency-groups]``.

    Ranges and unpinned entries are out of scope for v1 (see the module
    docstring's honest-limitations note) — only exact pins are live-scanned,
    everything else stays model knowledge. ``[dependency-groups]``
    ``{include-group = "..."}`` composition is resolved transitively — see
    :func:`_resolve_dependency_groups` for the cycle-safe, bounded walk.
    """

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    specs: List[str] = []
    project = data.get("project")
    if isinstance(project, dict):
        direct = project.get("dependencies")
        if isinstance(direct, list):
            specs.extend(spec for spec in direct if isinstance(spec, str))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    specs.extend(spec for spec in group if isinstance(spec, str))
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        specs.extend(_resolve_dependency_groups(groups))
    deps = []
    for spec in specs:
        pin = _pep508_pin(spec)
        if pin is not None:
            name, version = pin
            deps.append(Dependency(name, version, "PyPI", manifest))
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


def parse_go_mod(text: str, manifest: str) -> List[Dependency]:
    """Go ``go.mod``: every ``require`` entry is already an exact pin.

    Go's module resolution (MVS) has no version-range syntax — a ``require``
    line always names one concrete version — so, uniquely among this
    module's ecosystems, no separate lockfile is needed for an exact pin.
    """

    deps = []
    in_block = False
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()  # drop "// indirect" etc.
        if not line:
            continue
        if line == "require (":
            in_block = True
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            parts = line.split()
        elif line.startswith("require "):
            parts = line[len("require "):].split()
        else:
            continue
        if len(parts) >= 2 and parts[1].startswith("v"):
            deps.append(Dependency(parts[0], parts[1], "Go", manifest))
    return deps


def parse_gemfile_lock(text: str, manifest: str) -> List[Dependency]:
    """Ruby ``Gemfile.lock``: top-level ``GEM``/``specs:`` entries are exact
    resolved pins; deeper-indented lines are a gem's own dependency
    *constraints* on its neighbours, not pins, and are skipped.
    """

    deps = []
    in_specs = False
    for raw_line in text.splitlines():
        if not raw_line.startswith(" "):
            in_specs = raw_line.strip() == "GEM"
            continue
        if not in_specs or raw_line.strip() == "specs:":
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent != 4 or "(" not in stripped or not stripped.endswith(")"):
            continue
        name, _, rest = stripped.partition(" (")
        version = rest[:-1]
        if name and version:
            deps.append(Dependency(name, version, "RubyGems", manifest))
    return deps


def parse_composer_lock(text: str, manifest: str) -> List[Dependency]:
    """PHP ``composer.lock``: ``packages``/``packages-dev`` are exact resolved
    pins, mirroring ``package-lock.json``'s flat-array shape.

    Resolved pins here supersede :func:`parse_composer_json`'s range-derived
    lower bounds for the same package (see :func:`collect_dependencies`);
    Composer's fuller constraint algebra (OR-lists, AND-lists, branch
    aliases) still has no exact-pin equivalent without running Composer's
    dependency resolver, so — like a bare Ruby ``Gemfile`` — those forms stay
    unparsed even with this lockfile present.
    """

    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    deps = []
    for section in ("packages", "packages-dev"):
        entries = data.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name, version = entry.get("name"), entry.get("version")
            if name and version:
                deps.append(Dependency(str(name), str(version), "Packagist", manifest))
    return deps


def parse_composer_json(text: str, manifest: str) -> List[Dependency]:
    """PHP ``composer.json``: ``require``/``require-dev`` caret/tilde/bare-exact
    constraints, mirroring :func:`parse_package_json`'s treatment of npm's
    identical ``^``/``~`` range shape.

    Platform pseudo-packages (``php``, ``ext-mbstring``, ``lib-openssl``, ...)
    are not real Packagist packages and are skipped — identifiable, like in
    Composer's own platform-repository check, by having no ``/`` separating a
    vendor from a package name. Composer's fuller constraint algebra (OR-lists
    ``||``, comma-separated AND-lists, ``dev-*`` branch aliases, open ranges)
    is out of scope for v1 (see the module docstring's honest-limitations
    note); reusing :func:`_exact_version` verbatim already rejects all of
    those forms, so no extra filtering is needed here.
    """

    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    deps = []
    for section in ("require", "require-dev"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        for name, spec in sorted(entries.items()):
            if "/" not in name:
                continue
            version = _exact_version(str(spec))
            if version is not None:
                deps.append(
                    Dependency(
                        name, version, "Packagist", manifest,
                        approximate=_is_range_spec(str(spec)),
                    )
                )
    return deps


_PARSERS = {
    "packages.config": parse_packages_config,
    "package.json": parse_package_json,
    "requirements.txt": parse_requirements_txt,
    "pyproject.toml": parse_pyproject_toml,
    "Cargo.toml": parse_cargo_toml,
    "package-lock.json": parse_package_lock,
    "poetry.lock": parse_poetry_lock,
    "Cargo.lock": parse_cargo_lock,
    "packages.lock.json": parse_packages_lock_json,
    "go.mod": parse_go_mod,
    "Gemfile.lock": parse_gemfile_lock,
    "composer.json": parse_composer_json,
    "composer.lock": parse_composer_lock,
    "pom.xml": parse_pom_xml_deps,
}


def collect_dependencies(workspace: Workspace) -> List[Dependency]:
    """Parse every recognised manifest and lockfile, deduplicated.

    A caret/tilde range in a manifest yields only an approximate lower bound
    (see :func:`_exact_version`); when a lockfile — or any exact pin — resolves
    the same ``(ecosystem, name)`` to a concrete version, that lower-bound
    entry is dropped. Otherwise dedup on ``(ecosystem, name, version)`` would
    keep both the floor and the resolved version, and OSV would be queried
    about — and a CVE attributed to — a version that is not installed.
    """

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
    # An exact pin for an (ecosystem, name) supersedes any approximate,
    # range-derived lower bound for the same package: keep only the version
    # actually resolved so it can never be double-counted.
    pinned = {(dep.ecosystem, dep.name) for dep in deps if not dep.approximate}
    return [
        dep
        for dep in deps
        if not (dep.approximate and (dep.ecosystem, dep.name) in pinned)
    ]


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
        # Accumulate locally: a half-parsed response must never leave partial
        # vulnerabilities on the scan while queried stays False — that state
        # renders as "scan unavailable" yet still emits the records to
        # to_dict()/dict_to_backlog. Nothing is published until the whole
        # response parses cleanly below.
        found: List[Vulnerability] = []
        for dep, result in zip(queryable, results):
            for vuln in (result or {}).get("vulns") or []:
                vuln_id = vuln.get("id")
                if not vuln_id:
                    continue  # advisory with no id is unciteable; skip it
                found.append(Vulnerability(str(vuln_id), dep))
    except Exception as exc:  # network, JSON shape, key errors: degrade, never raise
        scan.error = f"{type(exc).__name__}: {exc}"
        return scan
    scan.vulnerabilities = found
    scan.queried = True
    return scan
