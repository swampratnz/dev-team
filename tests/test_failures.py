"""Tests for test-failure attribution parsing."""

from __future__ import annotations

from dev_team.failures import new_failures, parse_failed_tests

_PYTEST_OUTPUT = """\
=================================== FAILURES ===================================
FAILED tests/test_a.py::test_one - AssertionError: boom
ERROR tests/test_b.py::test_two
1 failed, 1 error in 0.10s
"""


def test_parse_pytest_failures():
    failed = parse_failed_tests(_PYTEST_OUTPUT)
    assert failed == frozenset({"tests/test_a.py::test_one", "tests/test_b.py::test_two"})


def test_parse_go_and_cargo_failures():
    assert parse_failed_tests("--- FAIL: TestLogin (0.01s)") == frozenset({"TestLogin"})
    assert parse_failed_tests("test auth::login ... FAILED") == frozenset({"auth::login"})


def test_parse_unrecognised_output_returns_none():
    assert parse_failed_tests("") is None
    assert parse_failed_tests("Segmentation fault (core dumped)") is None


def test_new_failures_subset_and_novel():
    baseline = frozenset({"t::a", "t::b"})
    assert new_failures(frozenset({"t::a"}), baseline) == frozenset()
    assert new_failures(frozenset({"t::a", "t::c"}), baseline) == frozenset({"t::c"})


def test_new_failures_unattributable_is_none():
    assert new_failures(None, frozenset({"t::a"})) is None
    assert new_failures(frozenset({"t::a"}), None) is None


def test_parse_dotnet_vstest_failures():
    output = (
        "  Passed MyApp.Tests.UserTests.Login_Ok [12 ms]\n"
        "  Failed MyApp.Tests.UserTests.Login_ReturnsToken [23 ms]\n"
        "Failed!  - Failed:     1, Passed:     1, Skipped:     0\n"
    )
    assert parse_failed_tests(output) == frozenset(
        {"MyApp.Tests.UserTests.Login_ReturnsToken"}
    )


def test_parse_dotnet_xunit_failures():
    output = "    MyApp.Tests.CartTests.Total_IsSummed [FAIL]\n"
    assert parse_failed_tests(output) == frozenset(
        {"MyApp.Tests.CartTests.Total_IsSummed"}
    )


def test_dotnet_restore_noise_is_not_a_test_identity():
    output = (
        "Failed to restore /src/MyApp/MyApp.csproj (in 1.2 sec).\n"
        "Failed!  - Failed:     0, Passed:     0, Skipped:     0\n"
    )
    # "to" has no dot; the csproj path is not preceded by "Failed <name>"
    assert parse_failed_tests(output) is None
