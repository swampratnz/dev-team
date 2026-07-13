"""Extract failing test identities from verify-command output.

This is what lets a delivery *tolerate* a red baseline instead of halting on
it: record which tests were already failing before any work started, then
gate each task only on **newly** failing tests. Attribution is best-effort —
when the output format isn't recognised, callers fall back to whole-gate
pass/fail semantics rather than guessing.

go and cargo failures are qualified by their package/crate so a *new* failing
test that shares a name with a baseline failure in another package stays
distinct (an unqualified name would misread the regression as inherited and
merge it green). go names are stitched to the ``FAIL\tpkg`` summary line that
closes each package; cargo names are prefixed with the crate from the
``Running … deps/<crate>-<hash>`` banner. When no package/crate marker is
present the bare test name is kept as a best-effort fallback. Pytest
identities include the file path and do not collide this way; .NET identities
are fully qualified (Namespace.Class.Method) but parameterised display names
containing spaces are truncated at the first space.
"""

from __future__ import annotations

import re
from typing import FrozenSet, List, Optional

# pytest:  "FAILED tests/test_x.py::test_y[param] - AssertionError"
#          "ERROR tests/test_x.py::test_y"
_PYTEST = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
# go test: per-test "--- FAIL: TestName (0.00s)" lines, each package then
# closed by a "FAIL\tpkg\telapsed" summary. The per-test line carries only the
# bare name, so the package is stitched on from the summary (see _go_failures).
_GO_FAIL = re.compile(r"^--- FAIL: (\S+)")
_GO_PACKAGE = re.compile(r"^FAIL\s+(\S+)")
# cargo:   "test module::name ... FAILED", each block preceded by a
# "Running ... (target/<profile>/deps/<crate>-<hash>)" banner naming the crate.
_CARGO_FAIL = re.compile(r"^test (\S+) \.\.\. FAILED")
_CARGO_BINARY = re.compile(r"\bdeps/([A-Za-z0-9_]+)-[0-9a-f]+")
# dotnet test (VSTest console): "  Failed Namespace.Class.Method [23 ms]".
# The captured name must contain a dot so restore/build noise like
# "Failed to restore ..." (captures "to") and the "Failed!" summary line
# never read as test identities.
_DOTNET_VSTEST = re.compile(r"^\s*Failed\s+(\S*\.\S+)", re.MULTILINE)
# xUnit console runner: "    Namespace.Class.Method [FAIL]"
_DOTNET_XUNIT = re.compile(r"^\s*(\S*\.\S+) \[FAIL\]", re.MULTILINE)


def _go_failures(output: str) -> List[str]:
    """Package-qualified go test identities (``pkg::TestName``).

    Buffers failing test names until the ``FAIL\tpkg`` summary that closes
    their package, then qualifies each with the package so the same test name
    in two packages stays distinct — the collision that used to merge a
    regression green. Names with no trailing summary keep the bare form.
    """

    pending: List[str] = []
    identities: List[str] = []
    for line in output.splitlines():
        test = _GO_FAIL.match(line)
        if test:
            pending.append(test.group(1))
            continue
        package = _GO_PACKAGE.match(line)
        if package and pending:
            identities.extend(f"{package.group(1)}::{name}" for name in pending)
            pending = []
    # A failure with no closing package summary keeps its bare name.
    identities.extend(pending)
    return identities


def _cargo_failures(output: str) -> List[str]:
    """Crate-qualified cargo test identities (``crate::module::name``).

    The ``Running … deps/<crate>-<hash>`` banner names the crate each block of
    ``test … FAILED`` lines belongs to; without it two crates that each define
    ``auth::login`` would collapse to one identity. Failures before any banner
    keep the bare module path.
    """

    crate: Optional[str] = None
    identities: List[str] = []
    for line in output.splitlines():
        binary = _CARGO_BINARY.search(line)
        if binary:
            crate = binary.group(1)
            continue
        test = _CARGO_FAIL.match(line)
        if test:
            name = test.group(1)
            identities.append(f"{crate}::{name}" if crate else name)
    return identities


def parse_failed_tests(output: str) -> Optional[FrozenSet[str]]:
    """Return the identities of failing tests, or ``None`` if unparseable.

    ``None`` means "this output carries no recognisable per-test failures" —
    distinct from ``frozenset()``, which would mean "parsed fine, nothing
    failed". Callers must treat ``None`` as *no attribution possible*.
    """

    if not output:
        return None
    found = set()
    for pattern in (_PYTEST, _DOTNET_VSTEST, _DOTNET_XUNIT):
        found.update(match.rstrip(" -") for match in pattern.findall(output))
    found.update(_go_failures(output))
    found.update(_cargo_failures(output))
    if not found:
        return None
    return frozenset(found)


def new_failures(
    current: Optional[FrozenSet[str]],
    baseline: Optional[FrozenSet[str]],
) -> Optional[FrozenSet[str]]:
    """Failures in ``current`` that are not inherited from ``baseline``.

    Returns ``None`` when either side is unattributable — in that case the
    caller cannot safely claim the failures are inherited.
    """

    if current is None or baseline is None:
        return None
    return current - baseline
