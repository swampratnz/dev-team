"""Extract failing test identities from verify-command output.

This is what lets a delivery *tolerate* a red baseline instead of halting on
it: record which tests were already failing before any work started, then
gate each task only on **newly** failing tests. Attribution is best-effort —
when the output format isn't recognised, callers fall back to whole-gate
pass/fail semantics rather than guessing.

Known limitation: go and cargo failures are keyed on the bare test name
(their output carries no reliable file/package identity on the ``FAIL``
line), so a *new* failing test that shares a name with a baseline failure in
another package is misread as inherited. Pytest identities include the file
path and do not collide this way; .NET identities are fully qualified
(Namespace.Class.Method) but parameterised display names containing spaces
are truncated at the first space.
"""

from __future__ import annotations

import re
from typing import FrozenSet, Optional

# pytest:  "FAILED tests/test_x.py::test_y[param] - AssertionError"
#          "ERROR tests/test_x.py::test_y"
_PYTEST = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
# go test: "--- FAIL: TestName (0.00s)"
_GO = re.compile(r"^--- FAIL: (\S+)", re.MULTILINE)
# cargo:   "test module::name ... FAILED"
_CARGO = re.compile(r"^test (\S+) \.\.\. FAILED", re.MULTILINE)
# dotnet test (VSTest console): "  Failed Namespace.Class.Method [23 ms]".
# The captured name must contain a dot so restore/build noise like
# "Failed to restore ..." (captures "to") and the "Failed!" summary line
# never read as test identities.
_DOTNET_VSTEST = re.compile(r"^\s*Failed\s+(\S*\.\S+)", re.MULTILINE)
# xUnit console runner: "    Namespace.Class.Method [FAIL]"
_DOTNET_XUNIT = re.compile(r"^\s*(\S*\.\S+) \[FAIL\]", re.MULTILINE)


def parse_failed_tests(output: str) -> Optional[FrozenSet[str]]:
    """Return the identities of failing tests, or ``None`` if unparseable.

    ``None`` means "this output carries no recognisable per-test failures" —
    distinct from ``frozenset()``, which would mean "parsed fine, nothing
    failed". Callers must treat ``None`` as *no attribution possible*.
    """

    if not output:
        return None
    found = set()
    for pattern in (_PYTEST, _GO, _CARGO, _DOTNET_VSTEST, _DOTNET_XUNIT):
        found.update(match.rstrip(" -") for match in pattern.findall(output))
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
