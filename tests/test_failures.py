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
    # No package/crate marker: the bare name survives as a best-effort fallback.
    assert parse_failed_tests("--- FAIL: TestLogin (0.01s)") == frozenset({"TestLogin"})
    assert parse_failed_tests("test auth::login ... FAILED") == frozenset({"auth::login"})


def test_go_failures_qualified_by_package():
    output = (
        "--- FAIL: TestSync (0.00s)\n"
        "FAIL\n"
        "FAIL\tgithub.com/org/alpha\t0.01s\n"
        "--- FAIL: TestSync (0.00s)\n"
        "FAIL\tgithub.com/org/beta\t0.02s\n"
        "ok  \tgithub.com/org/gamma\t0.03s\n"
    )
    assert parse_failed_tests(output) == frozenset(
        {"github.com/org/alpha::TestSync", "github.com/org/beta::TestSync"}
    )


def test_go_same_name_in_different_package_is_a_new_failure():
    # The regression this fix prevents: a NEW failure named TestSync in package
    # beta must not read as inherited from the baseline TestSync in package alpha.
    baseline = parse_failed_tests(
        "--- FAIL: TestSync (0.0s)\nFAIL\tgithub.com/org/alpha\t0.01s\n"
    )
    current = parse_failed_tests(
        "--- FAIL: TestSync (0.0s)\nFAIL\tgithub.com/org/alpha\t0.01s\n"
        "--- FAIL: TestSync (0.0s)\nFAIL\tgithub.com/org/beta\t0.02s\n"
    )
    assert new_failures(current, baseline) == frozenset({"github.com/org/beta::TestSync"})


def test_go_package_summary_without_failures_yields_nothing():
    # A package that failed to build emits a FAIL summary with no per-test
    # "--- FAIL:" line, so there is no test identity to attribute.
    assert parse_failed_tests("FAIL\tgithub.com/org/pkg [build failed]\n") is None


def test_cargo_failures_qualified_by_crate():
    output = (
        "     Running unittests src/lib.rs (target/debug/deps/alpha-9f8e7d6c5b4a3210)\n"
        "running 1 test\n"
        "test auth::login ... FAILED\n"
        "     Running unittests src/lib.rs (target/debug/deps/beta-0011223344556677)\n"
        "running 1 test\n"
        "test auth::login ... FAILED\n"
    )
    assert parse_failed_tests(output) == frozenset(
        {"alpha::auth::login", "beta::auth::login"}
    )


def test_cargo_same_name_in_different_crate_is_a_new_failure():
    banner = "     Running unittests src/lib.rs (target/debug/deps/{}-0011223344556677)\n"
    baseline = parse_failed_tests(banner.format("alpha") + "test auth::login ... FAILED\n")
    current = parse_failed_tests(
        banner.format("alpha") + "test auth::login ... FAILED\n"
        + banner.format("beta") + "test auth::login ... FAILED\n"
    )
    assert new_failures(current, baseline) == frozenset({"beta::auth::login"})


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
