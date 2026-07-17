"""Live runtime EOL/support-status checking backed by endoflife.date.

The risk phase's "Node.js 14 is end-of-life"-style claims otherwise come
purely from model knowledge — plausible, stale, and unverifiable for a repo
audited long after the model's training cutoff. This module is the
deterministic counterpart, mirroring :mod:`depscan`'s exact shape: runtime
versions are parsed straight out of a small, fixed set of manifest files
(``package.json`` ``engines.node``, ``.nvmrc``, ``runtime.txt``,
``.python-version``, ``global.json`` ``sdk.version``, ``.ruby-version``,
``go.mod``) and checked against endoflife.date's public, unauthenticated
API — one request per *distinct* detected product. No network (or a
failed/malformed query) degrades gracefully: the detected runtimes still
feed the report, annotated that the live check was unavailable.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple, Union

from .execution import Workspace

_EOL_ENDPOINT = "https://endoflife.date/api/{product}.json"
_HTTP_TIMEOUT_SECONDS = 30.0

#: A ``fetch`` callable takes an endoflife.date product slug and returns
#: that product's list of release-cycle records (the raw JSON response).
Fetch = Callable[[str], List[Dict]]

_DISPLAY_NAMES = {
    "nodejs": "Node.js",
    "python": "Python",
    "dotnet": ".NET",
    "ruby": "Ruby",
    "go": "Go",
}

#: The only products this module understands (every audited repo has
#: exactly one primary runtime per product). Grown one manifest at a time
#: as new bare-version-file conventions are added.
_SUPPORTED_PRODUCTS = frozenset(_DISPLAY_NAMES)

_VERSION_RE = re.compile(r"\d+(?:\.\d+){0,2}")


@dataclass(frozen=True)
class Runtime:
    """One runtime version detected from a manifest file."""

    product: str
    version: str
    manifest: str


@dataclass(frozen=True)
class EolStatus:
    """What endoflife.date says (or doesn't) about one detected runtime."""

    runtime: Runtime
    end_of_life: Union[bool, str] = "unknown"
    eol_date: Optional[str] = None


@dataclass
class EolScan:
    """What was detected, and what endoflife.date says about it."""

    runtimes: List[Runtime] = field(default_factory=list)
    statuses: List[EolStatus] = field(default_factory=list)
    queried: bool = False
    error: Optional[str] = None

    def render(self) -> str:
        """Prompt/report-ready rendering of the scan."""

        if not self.runtimes:
            return ""
        detected = ", ".join(
            f"{_DISPLAY_NAMES.get(rt.product, rt.product)} {rt.version} "
            f"({rt.manifest})"
            for rt in self.runtimes
        )
        lines = [f"EOL/support-status scan: {len(self.runtimes)} runtime(s) detected ({detected})."]
        if self.queried:
            lines.append("Live endoflife.date check:")
            for status in self.statuses:
                rt = status.runtime
                name = _DISPLAY_NAMES.get(rt.product, rt.product)
                if status.end_of_life == "unknown":
                    verdict = "support status unknown (release cycle not resolved)"
                elif status.end_of_life:
                    verdict = f"END OF LIFE ({status.eol_date})"
                else:
                    verdict = "supported" + (
                        f" (EOL {status.eol_date})" if status.eol_date else ""
                    )
                lines.append(f"- {name} {rt.version} ({rt.manifest}): {verdict}")
        else:
            lines.append(
                "Live EOL scan unavailable"
                + (f" ({self.error})" if self.error else "")
                + " — treat EOL claims as model knowledge."
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "runtimes": [vars(rt) for rt in self.runtimes],
            "statuses": [
                {
                    "runtime": vars(status.runtime),
                    "end_of_life": status.end_of_life,
                    "eol_date": status.eol_date,
                }
                for status in self.statuses
            ],
            "queried": self.queried,
            "error": self.error,
        }


def _leading_version(text: str) -> Optional[str]:
    """The leading dotted-numeric version in ``text``, or ``None``.

    Strips common range-spec prefixes (``^~=v><``) first so
    ``engines.node`` specs like ``^18.17.0`` or ``>=18.0.0 <19`` reduce to
    their leading concrete version, the same "close enough" tolerance
    :func:`depscan._exact_version` applies to manifest ranges. Anything that
    doesn't start with a version after that — including shell-metacharacter-
    or path-traversal-shaped content — degrades to ``None`` rather than
    ever being guessed at.
    """

    cleaned = text.strip().lstrip("^~=v><")
    match = _VERSION_RE.match(cleaned)
    return match.group(0) if match else None


def parse_package_json_engines(text: str) -> Optional[Tuple[str, str]]:
    """npm ``package.json``: ``engines.node``, if present and version-shaped."""

    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    engines = data.get("engines")
    if not isinstance(engines, dict):
        return None
    spec = engines.get("node")
    if not isinstance(spec, str):
        return None
    version = _leading_version(spec)
    return ("nodejs", version) if version else None


def parse_nvmrc(text: str) -> Optional[Tuple[str, str]]:
    """``.nvmrc``: a bare (or ``v``-prefixed) Node.js version."""

    version = _leading_version(text)
    return ("nodejs", version) if version else None


def parse_runtime_txt(text: str) -> Optional[Tuple[str, str]]:
    """Heroku-style ``runtime.txt``: ``python-X.Y.Z``."""

    stripped = text.strip()
    if not stripped.startswith("python-"):
        return None
    version = _leading_version(stripped[len("python-") :])
    return ("python", version) if version else None


def parse_python_version(text: str) -> Optional[Tuple[str, str]]:
    """``.python-version``: the first line's version (pyenv convention)."""

    stripped = text.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]
    version = _leading_version(first_line)
    return ("python", version) if version else None


def parse_global_json_sdk(text: str) -> Optional[Tuple[str, str]]:
    """.NET ``global.json``: ``sdk.version``."""

    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    sdk = data.get("sdk")
    if not isinstance(sdk, dict):
        return None
    spec = sdk.get("version")
    if not isinstance(spec, str):
        return None
    version = _leading_version(spec)
    return ("dotnet", version) if version else None


def parse_ruby_version(text: str) -> Optional[Tuple[str, str]]:
    """``.ruby-version``: the first line's version (pyenv/rbenv convention)."""

    stripped = text.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]
    version = _leading_version(first_line)
    return ("ruby", version) if version else None


def parse_go_mod(text: str) -> Optional[Tuple[str, str]]:
    """``go.mod``: the ``go`` directive line (e.g. ``go 1.21`` or ``go 1.21.3``)."""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("go "):
            continue
        version = _leading_version(stripped[len("go ") :])
        return ("go", version) if version else None
    return None


_PARSERS: Dict[str, Callable[[str], Optional[Tuple[str, str]]]] = {
    "package.json": parse_package_json_engines,
    ".nvmrc": parse_nvmrc,
    "runtime.txt": parse_runtime_txt,
    ".python-version": parse_python_version,
    "global.json": parse_global_json_sdk,
    ".ruby-version": parse_ruby_version,
    "go.mod": parse_go_mod,
}


def detect_runtimes(workspace: Workspace) -> List[Runtime]:
    """Parse every recognised runtime-version file, one entry per product.

    When more than one recognised file agrees on the same product (e.g.
    both ``.nvmrc`` and ``package.json``'s ``engines.node``), only the
    first (in sorted path order) is kept — a second live query for the
    same product would be redundant, not more informative.
    """

    seen: set = set()
    runtimes: List[Runtime] = []
    for path in sorted(workspace.list_files()):
        parser = _PARSERS.get(path.rsplit("/", 1)[-1])
        if parser is None:
            continue
        try:
            text = workspace.read_text(path)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        result = parser(text)
        if result is None:
            continue
        product, version = result
        if product not in _SUPPORTED_PRODUCTS or product in seen:
            continue
        seen.add(product)
        runtimes.append(Runtime(product=product, version=version, manifest=path))
    return runtimes


def _cycle_candidates(version: str) -> List[str]:
    """``version`` reduced to the cycle identifiers it might match.

    Tried most-specific first: the full version, then major.minor (Python/
    .NET-style cycles), then major only (Node-style cycles).
    """

    parts = version.split(".")
    candidates = [version]
    if len(parts) >= 2:
        candidates.append(f"{parts[0]}.{parts[1]}")
    candidates.append(parts[0])
    seen: set = set()
    ordered = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _match_cycle(version: str, cycles: List[Dict]) -> Optional[Dict]:
    """The release-cycle record matching ``version``, or ``None``."""

    if not isinstance(cycles, list):
        return None
    by_cycle: Dict[str, Dict] = {}
    for entry in cycles:
        if not isinstance(entry, dict):
            continue
        cycle = entry.get("cycle")
        if cycle is None:
            continue
        by_cycle.setdefault(str(cycle), entry)
    for candidate in _cycle_candidates(version):
        if candidate in by_cycle:
            return by_cycle[candidate]
    return None


def _eol_verdict(entry: Dict) -> Tuple[Union[bool, str], Optional[str]]:
    """``(end_of_life, eol_date)`` for a matched release-cycle record.

    ``eol: false`` is endoflife.date's "no planned EOL" convention. Any
    other non-date shape (missing, ``true``, not a string) is ambiguous and
    degrades to ``"unknown"`` — never promoted to a false positive or
    negative.
    """

    eol = entry.get("eol")
    if eol is False:
        return False, None
    if isinstance(eol, str) and eol:
        try:
            eol_date = date.fromisoformat(eol)
        except ValueError:
            return "unknown", None
        return eol_date < date.today(), eol
    return "unknown", None


def _http_fetch(product: str) -> List[Dict]:
    """GET the release-cycle list for ``product`` (the default fetch)."""

    request = urllib.request.Request(_EOL_ENDPOINT.format(product=product))
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def query_eol(
    product: str, version: str, *, fetch: Optional[Fetch] = None
) -> Tuple[Union[bool, str], Optional[str]]:
    """``(end_of_life, eol_date)`` for ``version`` of ``product``.

    Matches ``version`` to its release cycle (see :func:`_cycle_candidates`)
    and reads that cycle's ``eol`` field. A version whose cycle can't be
    resolved in the response returns ``"unknown"`` — never guessed.
    """

    cycles = (fetch or _http_fetch)(product)
    if not isinstance(cycles, list):
        raise ValueError(f"unexpected endoflife.date response shape for {product!r}")
    entry = _match_cycle(version, cycles)
    if entry is None:
        return "unknown", None
    return _eol_verdict(entry)


def scan_eol(
    workspace: Workspace, *, fetch: Optional[Fetch] = None, enabled: bool = True
) -> EolScan:
    """Detect runtimes and (when enabled) query endoflife.date about them.

    Every failure mode — scanning disabled, nothing detected, network down,
    malformed response — produces a scan whose :meth:`~EolScan.render` says
    exactly what happened; the caller never has to branch. At most one HTTP
    request is made per distinct detected product: :func:`detect_runtimes`
    already dedupes to one :class:`Runtime` per product, so one ``query_eol``
    call per runtime is already one fetch per distinct product. A failure
    for any of them degrades the *whole* scan (never a half-published mix
    of live and unknown results), mirroring :func:`depscan.scan_dependencies`.
    """

    runtimes = detect_runtimes(workspace)
    scan = EolScan(runtimes=runtimes)
    if not enabled:
        scan.error = "scan disabled"
        scan.statuses = [EolStatus(rt) for rt in runtimes]
        return scan
    if not runtimes:
        return scan

    try:
        statuses = [
            EolStatus(rt, *query_eol(rt.product, rt.version, fetch=fetch))
            for rt in runtimes
        ]
    except Exception as exc:  # network, JSON shape, key errors: degrade, never raise
        scan.error = f"{type(exc).__name__}: {exc}"
        scan.statuses = [EolStatus(rt) for rt in runtimes]
        return scan
    scan.statuses = statuses
    scan.queried = True
    return scan
