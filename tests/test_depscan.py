"""Tests for the OSV-backed dependency scanner."""

from __future__ import annotations

import json

from dev_team.depscan import (
    Dependency,
    DependencyScan,
    _exact_version,
    _MAX_DEPENDENCIES,
    collect_dependencies,
    parse_cargo_toml,
    parse_package_json,
    parse_packages_config,
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
