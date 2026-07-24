"""Tests for the OSV-backed dependency scanner."""

from __future__ import annotations

import builtins
import json
import os
import subprocess

from dev_team.depscan import (
    Dependency,
    DependencyScan,
    Vulnerability,
    _exact_version,
    _MAX_DEPENDENCIES,
    collect_dependencies,
    parse_cargo_toml,
    parse_composer_lock,
    parse_gemfile_lock,
    parse_go_mod,
    parse_package_json,
    parse_packages_config,
    parse_pyproject_toml,
    parse_requirements_txt,
    scan_dependencies,
)
from dev_team.execution import InMemoryWorkspace

_PACKAGES_CONFIG = """\
<?xml version="1.0" encoding="utf-8"?>
<packages>
  <package id="Moq" version="4.2.1409.1722" targetFramework="net452" />
  <package id="FluentAssertions" version="5.10.3" />
  <package id="broken" />
</packages>
"""


def test_exact_version_accepts_pins_and_rejects_ranges():
    assert _exact_version("1.2.3") == "1.2.3"
    assert _exact_version("^1.2.3") == "1.2.3"
    assert _exact_version("~2.0") == "2.0"
    assert _exact_version("v3.1.4") == "3.1.4"
    assert _exact_version("1.0.0-beta.2") == "1.0.0-beta.2"
    assert _exact_version(">=1.0 <2.0") is None
    assert _exact_version("*") is None
    assert _exact_version("latest") is None
    assert _exact_version("") is None


def test_parse_packages_config():
    deps = parse_packages_config(_PACKAGES_CONFIG, "App/packages.config")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("Moq", "4.2.1409.1722", "NuGet"),
        ("FluentAssertions", "5.10.3", "NuGet"),
    ]
    assert parse_packages_config("not xml", "p") == []


def test_parse_package_json():
    text = json.dumps(
        {
            "dependencies": {"left-pad": "^1.3.0", "weird": "git://x"},
            "devDependencies": {"jest": "29.0.0"},
            "peerDependencies": {"react": "18.0.0"},
        }
    )
    deps = parse_package_json(text, "package.json")
    assert [(d.name, d.version) for d in deps] == [
        ("left-pad", "1.3.0"),
        ("jest", "29.0.0"),
    ]
    assert parse_package_json("[]", "package.json") == []
    assert parse_package_json("{bad", "package.json") == []
    assert parse_package_json('{"dependencies": []}', "package.json") == []


def test_parse_requirements_txt():
    text = "requests==2.31.0\nflask>=2  # not a pin\n# comment\n==\nnumpy == 1.26.4\n"
    deps = parse_requirements_txt(text, "requirements.txt")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("requests", "2.31.0", "PyPI"),
        ("numpy", "1.26.4", "PyPI"),
    ]


def test_parse_cargo_toml():
    text = """
[dependencies]
serde = "1.0.196"
tokio = { version = "1.36.0", features = ["full"] }
local = { path = "../local" }
open = ">=0.5"

[dev-dependencies]
insta = "1.34.0"
"""
    deps = parse_cargo_toml(text, "Cargo.toml")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("serde", "1.0.196", "crates.io"),
        ("tokio", "1.36.0", "crates.io"),
        ("insta", "1.34.0", "crates.io"),
    ]
    assert parse_cargo_toml("not = toml =", "Cargo.toml") == []
    assert parse_cargo_toml("[dependencies]\nbroken = 5\n", "Cargo.toml") == []


def test_parse_pyproject_toml_exact_pins():
    text = """
[project]
dependencies = ["requests==2.31.0"]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("requests", "2.31.0", "PyPI"),
    ]


def test_parse_pyproject_toml_strips_extras_and_markers():
    text = """
[project]
dependencies = ["httpx[http2]==0.27.0; python_version >= \\"3.8\\""]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version) for d in deps] == [("httpx", "0.27.0")]


def test_parse_pyproject_toml_optional_dependencies():
    text = """
[project]
dependencies = ["requests==2.31.0"]

[project.optional-dependencies]
dev = ["pytest==8.0.0"]
docs = ["sphinx>=7.0"]
malformed = "not-a-list"
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version) for d in deps] == [
        ("requests", "2.31.0"),
        ("pytest", "8.0.0"),
    ]


def test_parse_pyproject_toml_skips_non_exact_specs():
    text = """
[project]
dependencies = ["httpx>=0.27,<1.0", "foo", "bar~=1.2", "requests==2.31.0"]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version) for d in deps] == [("requests", "2.31.0")]


def test_parse_pyproject_toml_malformed_toml():
    assert parse_pyproject_toml("not = toml =", "pyproject.toml") == []


def test_parse_pyproject_toml_no_project_table():
    assert parse_pyproject_toml("[tool.other]\nx = 1\n", "pyproject.toml") == []


def test_parse_pyproject_toml_adversarial_shapes_degrade_safely():
    # dependencies as a string instead of a list
    assert (
        parse_pyproject_toml(
            '[project]\ndependencies = "requests==2.31.0"\n', "pyproject.toml"
        )
        == []
    )
    # optional-dependencies as a list instead of a table
    assert (
        parse_pyproject_toml(
            '[project]\noptional-dependencies = ["requests==2.31.0"]\n',
            "pyproject.toml",
        )
        == []
    )
    # list entries that are dicts/ints instead of strings
    text = """
[project]
dependencies = [1, true]

[project.optional-dependencies]
dev = [{name = "requests"}, 42]
"""
    assert parse_pyproject_toml(text, "pyproject.toml") == []
    # a dependency string with an embedded null byte or thousands of chars
    huge = "requests" + "x" * 5000 + "==2.31.0"
    null_byte = "requests\x00==2.31.0"
    # json's double-quote escaping is a valid subset of TOML basic-string
    # escaping, so this is a safe way to embed a null byte / huge string.
    text = f"""
[project]
dependencies = [{json.dumps(huge)}, {json.dumps(null_byte)}]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version) for d in deps] == [
        ("requests" + "x" * 5000, "2.31.0"),
        ("requests\x00", "2.31.0"),
    ]


def test_parse_pyproject_toml_skips_empty_name_after_stripping():
    text = """
[project]
dependencies = ["==1.0", "[extra]==1.0"]
"""
    assert parse_pyproject_toml(text, "pyproject.toml") == []


def test_parse_pyproject_toml_dependency_groups_exact_pins():
    text = """
[dependency-groups]
test = ["pytest==8.0.0"]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("pytest", "8.0.0", "PyPI"),
    ]


def test_parse_pyproject_toml_dependency_groups_multiple_groups():
    text = """
[dependency-groups]
test = ["pytest==8.0.0"]
docs = ["sphinx==7.0.0"]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version) for d in deps] == [
        ("pytest", "8.0.0"),
        ("sphinx", "7.0.0"),
    ]


def test_parse_pyproject_toml_dependency_groups_strips_extras_and_markers():
    text = """
[dependency-groups]
test = ["httpx[http2]==0.27.0; python_version >= \\"3.8\\""]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version) for d in deps] == [("httpx", "0.27.0")]


def test_parse_pyproject_toml_dependency_groups_skips_non_exact_specs():
    text = """
[dependency-groups]
test = ["foo>=1.0", "bar", "baz~=2.0", "pytest==8.0.0"]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version) for d in deps] == [("pytest", "8.0.0")]


def test_parse_pyproject_toml_dependency_groups_include_group_skipped():
    text = """
[dependency-groups]
test = ["pytest==8.0.0"]
all = [{include-group = "test"}]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    # The "all" group's include-group reference is not resolved — only
    # "test"'s own directly-listed package is scanned.
    assert [(d.name, d.version) for d in deps] == [("pytest", "8.0.0")]


def test_parse_pyproject_toml_dependency_groups_adversarial_shapes():
    # dependency-groups present but not a table
    assert (
        parse_pyproject_toml(
            'dependency-groups = ["test"]\n', "pyproject.toml"
        )
        == []
    )
    # a group's value not a list
    assert (
        parse_pyproject_toml(
            '[dependency-groups]\ntest = "pytest==8.0.0"\n', "pyproject.toml"
        )
        == []
    )
    # a list entry that is neither str nor dict, and a malformed include-group
    text = """
[dependency-groups]
test = [42, true, {not-include-group = "x"}, {include-group = 1}]
"""
    assert parse_pyproject_toml(text, "pyproject.toml") == []


def test_parse_pyproject_toml_dependency_groups_union_with_project_tables():
    text = """
[project]
dependencies = ["requests==2.31.0"]

[project.optional-dependencies]
dev = ["pytest==8.0.0"]

[dependency-groups]
test = ["sphinx==7.0.0"]
"""
    deps = parse_pyproject_toml(text, "pyproject.toml")
    assert [(d.name, d.version) for d in deps] == [
        ("requests", "2.31.0"),
        ("pytest", "8.0.0"),
        ("sphinx", "7.0.0"),
    ]


def test_collect_dependencies_dedupes_across_manifests():
    ws = InMemoryWorkspace(
        {
            "A/packages.config": _PACKAGES_CONFIG,
            "B/packages.config": _PACKAGES_CONFIG,
            "README.md": "hi",
        }
    )
    deps = collect_dependencies(ws)
    assert len(deps) == 2


def test_collect_dependencies_tolerates_unreadable_manifest():
    class _Flaky(InMemoryWorkspace):
        def read_text(self, path):
            raise OSError("nope")

    assert collect_dependencies(_Flaky({"packages.config": "x"})) == []


def _fake_fetch(vulns_by_index):
    def fetch(payload):
        results = []
        for i, _query in enumerate(payload["queries"]):
            ids = vulns_by_index.get(i, [])
            results.append({"vulns": [{"id": v} for v in ids]} if ids else {})
        return {"results": results}

    return fetch


def test_scan_dependencies_reports_vulnerabilities():
    ws = InMemoryWorkspace({"App/packages.config": _PACKAGES_CONFIG})
    scan = scan_dependencies(ws, fetch=_fake_fetch({0: ["GHSA-xxxx-1234"]}))
    assert scan.queried is True
    assert scan.error is None
    assert [(v.id, v.dependency.name) for v in scan.vulnerabilities] == [
        ("GHSA-xxxx-1234", "Moq")
    ]
    rendered = scan.render()
    assert "GHSA-xxxx-1234" in rendered
    assert "https://osv.dev/vulnerability/GHSA-xxxx-1234" in rendered
    as_dict = scan.to_dict()
    assert as_dict["vulnerabilities"][0]["dependency"]["name"] == "Moq"


def test_scan_dependencies_degrades_on_fetch_failure():
    ws = InMemoryWorkspace({"requirements.txt": "requests==2.31.0"})

    def broken_fetch(_payload):
        raise OSError("network down")

    scan = scan_dependencies(ws, fetch=broken_fetch)
    assert scan.queried is False
    assert "network down" in scan.error
    assert "unavailable" in scan.render()


def test_scan_dependencies_rejects_mismatched_results():
    ws = InMemoryWorkspace({"requirements.txt": "requests==2.31.0"})
    scan = scan_dependencies(ws, fetch=lambda _p: {"results": []})
    assert scan.queried is False
    assert "0 results for 1 queries" in scan.error


def test_scan_dependencies_disabled_and_empty():
    ws = InMemoryWorkspace({"requirements.txt": "requests==2.31.0"})
    disabled = scan_dependencies(ws, enabled=False)
    assert disabled.queried is False
    assert disabled.error == "scan disabled"
    assert "scan disabled" in disabled.render()

    empty = scan_dependencies(InMemoryWorkspace({"README.md": "x"}), fetch=_fake_fetch({}))
    assert empty.dependencies == []
    assert empty.queried is False
    assert empty.render() == ""


def test_scan_dependencies_truncates_over_batch_limit():
    lines = "\n".join(f"pkg{i}==1.0.{i}" for i in range(_MAX_DEPENDENCIES + 3))
    ws = InMemoryWorkspace({"requirements.txt": lines})
    scan = scan_dependencies(ws, fetch=_fake_fetch({}))
    assert scan.truncated == 3
    assert scan.queried is True
    assert "3 additional dependencies were not scanned" in scan.render()


def test_scan_handles_null_result_entries():
    ws = InMemoryWorkspace({"requirements.txt": "requests==2.31.0"})
    scan = scan_dependencies(ws, fetch=lambda _p: {"results": [None]})
    assert scan.queried is True
    assert scan.vulnerabilities == []


def test_collect_dependencies_drops_range_lowerbound_superseded_by_lockfile():
    ws = InMemoryWorkspace(
        {
            "package.json": json.dumps({"dependencies": {"left-pad": "^1.3.0"}}),
            "package-lock.json": json.dumps(
                {"packages": {"node_modules/left-pad": {"version": "1.3.5"}}}
            ),
        }
    )
    deps = collect_dependencies(ws)
    # Only the lockfile-resolved 1.3.5 survives; the ^1.3.0 floor (1.3.0) is
    # dropped, so OSV is never asked about a version that is not installed and
    # a CVE can't be attributed to it.
    assert [(d.name, d.version, d.approximate) for d in deps] == [
        ("left-pad", "1.3.5", False)
    ]


def test_collect_dependencies_keeps_unresolved_range_lowerbound():
    # With no lockfile the ^ range's floor is all we have; keep it (flagged
    # approximate) rather than dropping a scannable dependency entirely.
    ws = InMemoryWorkspace(
        {"package.json": json.dumps({"dependencies": {"left-pad": "^1.3.0"}})}
    )
    deps = collect_dependencies(ws)
    assert [(d.name, d.version, d.approximate) for d in deps] == [
        ("left-pad", "1.3.0", True)
    ]


def test_render_does_not_call_range_derived_entries_exact_pins():
    scan = DependencyScan(
        dependencies=[
            Dependency("left-pad", "1.3.0", "npm", "package.json", approximate=True),
            Dependency("requests", "2.31.0", "PyPI", "requirements.txt"),
        ]
    )
    rendered = scan.render()
    assert "exactly-pinned" not in rendered
    assert "1 exactly pinned" in rendered
    assert "1 from a version range" in rendered


def test_render_annotates_range_derived_vulnerability():
    dep = Dependency("left-pad", "1.3.0", "npm", "package.json", approximate=True)
    scan = DependencyScan(
        dependencies=[dep],
        vulnerabilities=[Vulnerability("GHSA-range", dep)],
        queried=True,
    )
    rendered = scan.render()
    assert "left-pad >= 1.3.0" in rendered
    assert "GHSA-range" in rendered


def test_scan_skips_vuln_entries_without_id():
    ws = InMemoryWorkspace({"requirements.txt": "requests==2.31.0"})

    def fetch(_payload):
        # a well-formed advisory alongside one missing its id
        return {"results": [{"vulns": [{"id": "GHSA-real"}, {"aliases": ["x"]}]}]}

    scan = scan_dependencies(ws, fetch=fetch)
    assert scan.queried is True
    assert [v.id for v in scan.vulnerabilities] == ["GHSA-real"]


def test_scan_discards_partial_vulns_when_response_malformed():
    ws = InMemoryWorkspace({"requirements.txt": "a==1.0\nb==2.0"})

    def fetch(_payload):
        # first dep parses fine, second dep's vulns entry is not a dict and
        # raises while parsing — the partial "GHSA-a" must not survive.
        return {
            "results": [
                {"vulns": [{"id": "GHSA-a"}]},
                {"vulns": ["not-a-dict"]},
            ]
        }

    scan = scan_dependencies(ws, fetch=fetch)
    assert scan.queried is False
    assert scan.vulnerabilities == []
    assert scan.error is not None
    assert "unavailable" in scan.render()


def test_dependency_scan_render_without_query_or_error():
    scan = DependencyScan(dependencies=[Dependency("x", "1", "PyPI", "requirements.txt")])
    rendered = scan.render()
    assert "unavailable" in rendered
    assert "(" not in rendered.splitlines()[1]


def test_http_fetch_posts_querybatch(monkeypatch):
    import urllib.request

    import dev_team.depscan as depscan

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"results": []}'

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = depscan._http_fetch({"queries": [{"version": "1.0"}]})
    assert result == {"results": []}
    assert captured["url"].endswith("/v1/querybatch")
    assert captured["body"] == {"queries": [{"version": "1.0"}]}
    assert captured["timeout"] == 30.0


# --- lockfile parsers -------------------------------------------------------------


def test_parse_package_lock_v3_packages_map():
    from dev_team.depscan import parse_package_lock

    text = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-app", "version": "1.0.0"},
                "node_modules/left-pad": {"version": "1.3.0"},
                "node_modules/@scope/pkg": {"version": "2.0.0"},
                "node_modules/aliased": {"name": "real-name", "version": "3.0.0"},
                "node_modules/linked": {"link": True},
                "node_modules/broken": "not a dict",
                "node_modules/unversioned": {},
                "node_modules/": {"version": "1.0.0"},
            },
        }
    )
    deps = parse_package_lock(text, "package-lock.json")
    assert [(d.name, d.version) for d in deps] == [
        ("@scope/pkg", "2.0.0"),
        ("real-name", "3.0.0"),
        ("left-pad", "1.3.0"),
    ]
    assert all(d.ecosystem == "npm" for d in deps)


def test_parse_package_lock_v1_nested_dependencies():
    from dev_team.depscan import parse_package_lock

    text = json.dumps(
        {
            "lockfileVersion": 1,
            "dependencies": {
                "a": {"version": "1.0.0", "dependencies": {"b": {"version": "2.0.0"}}},
                "unversioned": {},
                "broken": "not a dict",
            },
        }
    )
    deps = parse_package_lock(text, "package-lock.json")
    assert [(d.name, d.version) for d in deps] == [("a", "1.0.0"), ("b", "2.0.0")]


def test_parse_package_lock_rejects_junk():
    from dev_team.depscan import parse_package_lock

    assert parse_package_lock("not json", "l") == []
    assert parse_package_lock('"a string"', "l") == []
    assert parse_package_lock("{}", "l") == []


def test_parse_poetry_lock():
    from dev_team.depscan import parse_poetry_lock

    text = """\
[[package]]
name = "requests"
version = "2.31.0"

[[package]]
name = "versionless"
"""
    deps = parse_poetry_lock(text, "poetry.lock")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("requests", "2.31.0", "PyPI"),
    ]
    assert parse_poetry_lock("not = = toml", "l") == []
    assert parse_poetry_lock('[package]\nname = "table-not-array"', "l") == []
    assert parse_poetry_lock('package = ["not a dict"]', "l") == []


def test_parse_cargo_lock_skips_workspace_crates():
    from dev_team.depscan import parse_cargo_lock

    text = """\
[[package]]
name = "my-own-crate"
version = "0.1.0"

[[package]]
name = "serde"
version = "1.0.190"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "sourced-but-versionless"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""
    deps = parse_cargo_lock(text, "Cargo.lock")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("serde", "1.0.190", "crates.io"),
    ]
    assert parse_cargo_lock("not = = toml", "l") == []
    assert parse_cargo_lock('name = "no packages"', "l") == []
    assert parse_cargo_lock('package = ["not a dict"]', "l") == []


def test_parse_packages_lock_json():
    from dev_team.depscan import parse_packages_lock_json

    text = json.dumps(
        {
            "version": 1,
            "dependencies": {
                ".NETFramework,Version=v4.7": {
                    "Newtonsoft.Json": {"type": "Direct", "resolved": "9.0.1"},
                    "My.Sibling": {"type": "Project"},
                    "Unresolved": {"type": "Direct"},
                    "BadResolved": {"type": "Direct", "resolved": 9},
                    "broken": "not a dict",
                },
                "netstandard2.0": {"Newtonsoft.Json": {"type": "Direct", "resolved": "9.0.1"}},
                "broken-framework": "not a dict",
            },
        }
    )
    deps = parse_packages_lock_json(text, "packages.lock.json")
    # the duplicate across frameworks survives here; collect_dependencies dedupes
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("Newtonsoft.Json", "9.0.1", "NuGet"),
        ("Newtonsoft.Json", "9.0.1", "NuGet"),
    ]
    assert parse_packages_lock_json("not json", "l") == []
    assert parse_packages_lock_json('"a string"', "l") == []
    assert parse_packages_lock_json('{"dependencies": "not a dict"}', "l") == []


def test_collect_dependencies_reads_lockfiles_and_dedupes():
    ws = InMemoryWorkspace(
        {
            # an open range: the manifest alone yields nothing scannable
            "package.json": json.dumps({"dependencies": {"left-pad": ">=1.0.0"}}),
            "package-lock.json": json.dumps(
                {"packages": {"node_modules/left-pad": {"version": "1.3.5"}}}
            ),
            "backend/poetry.lock": '[[package]]\nname = "requests"\nversion = "2.31.0"\n',
            "rust/Cargo.lock": (
                '[[package]]\nname = "serde"\nversion = "1.0.190"\nsource = "registry+x"\n'
            ),
            "dotnet/packages.lock.json": json.dumps(
                {
                    "dependencies": {
                        "net47": {"Moq": {"type": "Direct", "resolved": "4.2.0"}},
                        "net48": {"Moq": {"type": "Direct", "resolved": "4.2.0"}},
                    }
                }
            ),
        }
    )
    deps = collect_dependencies(ws)
    assert {(d.ecosystem, d.name, d.version) for d in deps} == {
        ("npm", "left-pad", "1.3.5"),
        ("PyPI", "requests", "2.31.0"),
        ("crates.io", "serde", "1.0.190"),
        ("NuGet", "Moq", "4.2.0"),
    }
    assert len(deps) == 4  # cross-framework NuGet duplicate deduplicated


def test_collect_dependencies_pyproject_toml_dedupes_against_poetry_lock():
    ws = InMemoryWorkspace(
        {
            "pyproject.toml": (
                '[project]\ndependencies = ["requests==2.31.0"]\n'
            ),
            "poetry.lock": '[[package]]\nname = "requests"\nversion = "2.31.0"\n',
        }
    )
    deps = collect_dependencies(ws)
    # the pyproject.toml-derived pin and the poetry.lock-resolved pin agree,
    # so only one Dependency survives dedup — no spurious duplicate.
    assert [(d.ecosystem, d.name, d.version) for d in deps] == [
        ("PyPI", "requests", "2.31.0"),
    ]


# --- go.mod / Gemfile.lock parsers -------------------------------------------------


def test_parse_go_mod_single_line_and_block():
    text = """\
module example.com/mymod

go 1.21

require example.com/x v1.2.3

require (
\texample.com/y v2.0.0 // indirect
\texample.com/z v3.1.4
)
"""
    deps = parse_go_mod(text, "go.mod")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("example.com/x", "v1.2.3", "Go"),
        ("example.com/y", "v2.0.0", "Go"),
        ("example.com/z", "v3.1.4", "Go"),
    ]


def test_parse_go_mod_no_require_yields_empty():
    assert parse_go_mod("module example.com/mymod\n\ngo 1.21\n", "go.mod") == []


def test_parse_go_mod_malformed_or_empty():
    assert parse_go_mod("", "go.mod") == []
    assert parse_go_mod("not a go.mod at all\nrandom junk\n", "go.mod") == []


def test_parse_go_mod_skips_entries_missing_a_version():
    text = """\
require example.com/noversion

require (
\texample.com/alsonoversion
\texample.com/notv 1.2.3
)
"""
    assert parse_go_mod(text, "go.mod") == []


def test_parse_gemfile_lock_top_level_specs_only():
    text = """\
GEM
  remote: https://rubygems.org/
  specs:
    rack (2.2.3)
    rack-test (1.1.0)
      rack (>= 1.0, < 3)

PLATFORMS
  ruby

DEPENDENCIES
  rack-test
"""
    deps = parse_gemfile_lock(text, "Gemfile.lock")
    assert [(d.name, d.version, d.ecosystem) for d in deps] == [
        ("rack", "2.2.3", "RubyGems"),
        ("rack-test", "1.1.0", "RubyGems"),
    ]


def test_parse_gemfile_lock_no_gem_section_yields_empty():
    text = """\
GIT
  remote: https://github.com/foo/bar.git
  revision: abcdef1234567890
  specs:
    foo (1.0)

PLATFORMS
  ruby
"""
    assert parse_gemfile_lock(text, "Gemfile.lock") == []


def test_parse_gemfile_lock_malformed_or_empty():
    assert parse_gemfile_lock("", "Gemfile.lock") == []
    assert parse_gemfile_lock("not a lockfile\n", "Gemfile.lock") == []


def test_parse_gemfile_lock_skips_unparseable_top_level_lines():
    text = """\
GEM
  specs:
    no-parens-here
    unterminated (1.0
    name ()
"""
    assert parse_gemfile_lock(text, "Gemfile.lock") == []


def test_parse_composer_lock_packages_and_packages_dev():
    text = json.dumps(
        {
            "packages": [
                {"name": "monolog/monolog", "version": "2.9.1"},
                {"name": "symfony/console", "version": "6.3.0"},
            ],
            "packages-dev": [
                {"name": "phpunit/phpunit", "version": "10.1.0"},
            ],
        }
    )
    deps = parse_composer_lock(text, "composer.lock")
    assert [(d.name, d.version, d.ecosystem, d.manifest, d.approximate) for d in deps] == [
        ("monolog/monolog", "2.9.1", "Packagist", "composer.lock", False),
        ("symfony/console", "6.3.0", "Packagist", "composer.lock", False),
        ("phpunit/phpunit", "10.1.0", "Packagist", "composer.lock", False),
    ]


def test_parse_composer_lock_skips_malformed_entries():
    text = json.dumps(
        {
            "packages": [
                {"name": "vendor/good", "version": "1.0.0"},
                "not-a-dict",
                {"name": "vendor/no-version"},
                {"version": "1.0.0"},
            ],
            "packages-dev": [
                {"name": "vendor/dev-good", "version": "2.0.0"},
                42,
            ],
        }
    )
    deps = parse_composer_lock(text, "composer.lock")
    assert [(d.name, d.version) for d in deps] == [
        ("vendor/good", "1.0.0"),
        ("vendor/dev-good", "2.0.0"),
    ]


def test_parse_composer_lock_rejects_adversarial_json():
    assert parse_composer_lock("not json at all", "composer.lock") == []
    assert parse_composer_lock('"a string"', "composer.lock") == []
    assert parse_composer_lock("[]", "composer.lock") == []
    assert parse_composer_lock("{}", "composer.lock") == []
    assert parse_composer_lock(
        json.dumps({"packages": "not-a-list"}), "composer.lock"
    ) == []
    assert parse_composer_lock(
        json.dumps({"packages-dev": "not-a-list"}), "composer.lock"
    ) == []


def test_parsers_registered_in_parsers_table():
    from dev_team.depscan import _PARSERS

    assert _PARSERS["go.mod"] is parse_go_mod
    assert _PARSERS["Gemfile.lock"] is parse_gemfile_lock
    assert _PARSERS["composer.lock"] is parse_composer_lock


def test_collect_dependencies_reads_go_mod_and_gemfile_lock():
    ws = InMemoryWorkspace(
        {
            "go.mod": "require example.com/x v1.2.3\n",
            "backend/Gemfile.lock": (
                "GEM\n  specs:\n    rack (2.2.3)\n"
            ),
            "requirements.txt": "requests==2.31.0\n",
        }
    )
    deps = collect_dependencies(ws)
    assert {(d.ecosystem, d.name, d.version) for d in deps} == {
        ("Go", "example.com/x", "v1.2.3"),
        ("RubyGems", "rack", "2.2.3"),
        ("PyPI", "requests", "2.31.0"),
    }


def test_collect_dependencies_dedupes_go_mod_and_gemfile_lock():
    ws = InMemoryWorkspace(
        {
            "a/go.mod": "require example.com/x v1.2.3\n",
            "b/go.mod": "require example.com/x v1.2.3\n",
            "a/Gemfile.lock": "GEM\n  specs:\n    rack (2.2.3)\n",
            "b/Gemfile.lock": "GEM\n  specs:\n    rack (2.2.3)\n",
        }
    )
    deps = collect_dependencies(ws)
    assert len(deps) == 2


def test_collect_dependencies_reads_composer_lock():
    ws = InMemoryWorkspace(
        {
            "composer.lock": json.dumps(
                {"packages": [{"name": "monolog/monolog", "version": "2.9.1"}]}
            ),
            "requirements.txt": "requests==2.31.0\n",
        }
    )
    deps = collect_dependencies(ws)
    assert {(d.ecosystem, d.name, d.version) for d in deps} == {
        ("Packagist", "monolog/monolog", "2.9.1"),
        ("PyPI", "requests", "2.31.0"),
    }


def test_collect_dependencies_dedupes_composer_lock():
    ws = InMemoryWorkspace(
        {
            "a/composer.lock": json.dumps(
                {"packages": [{"name": "monolog/monolog", "version": "2.9.1"}]}
            ),
            "b/composer.lock": json.dumps(
                {"packages": [{"name": "monolog/monolog", "version": "2.9.1"}]}
            ),
        }
    )
    deps = collect_dependencies(ws)
    assert len(deps) == 1


def test_scan_dependencies_go_and_rubygems_vulnerabilities():
    ws = InMemoryWorkspace(
        {
            "go.mod": "require example.com/x v1.2.3\n",
            "Gemfile.lock": "GEM\n  specs:\n    rack (2.2.3)\n",
        }
    )
    scan = scan_dependencies(
        ws,
        # collect_dependencies sorts by path; "Gemfile.lock" < "go.mod" (ASCII
        # 'G' < 'g'), so the RubyGems query lands at index 0, Go at index 1.
        fetch=_fake_fetch({0: ["GHSA-rb-5678"], 1: ["GHSA-go-1234"]}),
    )
    assert scan.queried is True
    assert scan.error is None
    assert {(v.id, v.dependency.name, v.dependency.ecosystem) for v in scan.vulnerabilities} == {
        ("GHSA-go-1234", "example.com/x", "Go"),
        ("GHSA-rb-5678", "rack", "RubyGems"),
    }
    rendered = scan.render()
    assert "GHSA-go-1234" in rendered
    assert "GHSA-rb-5678" in rendered


# --- security: crafted go.mod/Gemfile.lock content never reaches subprocess/eval ---


def test_crafted_manifest_content_never_raises():
    # Shell-metacharacter- and path-traversal-shaped content parses to plain
    # strings (or is skipped) — never raises, regardless of shape.
    malicious = ["; rm -rf /", "../../etc/passwd", "$(whoami)", "`id`"]
    for content in malicious:
        parse_go_mod(f"require {content} v1.0.0\n", "go.mod")
        parse_gemfile_lock(f"GEM\n  specs:\n    {content} (1.0)\n", "Gemfile.lock")
        parse_composer_lock(
            json.dumps({"packages": [{"name": content, "version": "1.0.0"}]}),
            "composer.lock",
        )


def test_crafted_manifest_content_causes_no_subprocess_or_eval(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("depscan must never invoke a subprocess or eval/exec")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(builtins, "eval", _boom)
    monkeypatch.setattr(builtins, "exec", _boom)

    ws = InMemoryWorkspace(
        {
            "go.mod": "require example.com/x $(whoami) v1.0.0\n\nrequire (\n\t; rm -rf / v1.0.0\n)\n",
            "Gemfile.lock": (
                "GEM\n  specs:\n    `id` (1.0)\n"
                "    ../../etc/passwd (1.0)\n"
            ),
        }
    )
    scan = scan_dependencies(ws, fetch=_fake_fetch({}))
    # None of these crash, and the module never touched subprocess/os.system
    # or eval/exec — verified by the monkeypatched raisers above.
    assert isinstance(scan, DependencyScan)
