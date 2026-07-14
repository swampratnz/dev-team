"""Tests for the endoflife.date-backed runtime EOL/support-status scanner."""

from __future__ import annotations

import json
import os
import subprocess

from dev_team.eolscan import (
    EolScan,
    EolStatus,
    _cycle_candidates,
    _eol_verdict,
    _http_fetch,
    _match_cycle,
    detect_runtimes,
    parse_global_json_sdk,
    parse_nvmrc,
    parse_package_json_engines,
    parse_python_version,
    parse_runtime_txt,
    query_eol,
    scan_eol,
)
from dev_team.execution import InMemoryWorkspace

_PAST_EOL = "2000-01-01"
_FUTURE_EOL = "2999-01-01"


# --- parsers: well-formed, malformed, never raise ---------------------------------


def test_parse_package_json_engines():
    text = json.dumps({"engines": {"node": "^18.17.0"}})
    assert parse_package_json_engines(text) == ("nodejs", "18.17.0")


def test_parse_package_json_engines_range_and_x_range():
    assert parse_package_json_engines(
        json.dumps({"engines": {"node": ">=18.0.0 <19.0.0"}})
    ) == ("nodejs", "18.0.0")
    assert parse_package_json_engines(json.dumps({"engines": {"node": "18.x"}})) == (
        "nodejs",
        "18",
    )


def test_parse_package_json_engines_malformed_never_raises():
    assert parse_package_json_engines("not json") is None
    assert parse_package_json_engines("[]") is None
    assert parse_package_json_engines("{}") is None
    assert parse_package_json_engines(json.dumps({"engines": "nope"})) is None
    assert parse_package_json_engines(json.dumps({"engines": {"node": 18}})) is None
    assert parse_package_json_engines(json.dumps({"engines": {"node": "lts"}})) is None
    assert parse_package_json_engines("") is None


def test_parse_nvmrc():
    assert parse_nvmrc("v18.17.0\n") == ("nodejs", "18.17.0")
    assert parse_nvmrc("18") == ("nodejs", "18")


def test_parse_nvmrc_malformed_never_raises():
    assert parse_nvmrc("lts/hydrogen") is None
    assert parse_nvmrc("") is None
    assert parse_nvmrc("   \n") is None


def test_parse_runtime_txt():
    assert parse_runtime_txt("python-3.11.4\n") == ("python", "3.11.4")


def test_parse_runtime_txt_malformed_never_raises():
    assert parse_runtime_txt("ruby-3.2.0") is None
    assert parse_runtime_txt("") is None
    assert parse_runtime_txt("python-") is None


def test_parse_python_version():
    assert parse_python_version("3.11.4\n") == ("python", "3.11.4")
    assert parse_python_version("3.11.4\n3.10.0\n") == ("python", "3.11.4")


def test_parse_python_version_malformed_never_raises():
    assert parse_python_version("") is None
    assert parse_python_version("   \n") is None
    assert parse_python_version("system") is None


def test_parse_global_json_sdk():
    text = json.dumps({"sdk": {"version": "8.0.100"}})
    assert parse_global_json_sdk(text) == ("dotnet", "8.0.100")


def test_parse_global_json_sdk_malformed_never_raises():
    assert parse_global_json_sdk("not json") is None
    assert parse_global_json_sdk("[]") is None
    assert parse_global_json_sdk(json.dumps({"sdk": "nope"})) is None
    assert parse_global_json_sdk(json.dumps({"sdk": {}})) is None
    assert parse_global_json_sdk(json.dumps({"sdk": {"version": 8}})) is None
    assert parse_global_json_sdk("") is None


# --- detect_runtimes ----------------------------------------------------------------


def test_detect_runtimes_dedupes_across_agreeing_files():
    ws = InMemoryWorkspace(
        {
            ".nvmrc": "18.17.0",
            "package.json": json.dumps({"engines": {"node": "18.17.0"}}),
        }
    )
    runtimes = detect_runtimes(ws)
    assert [(r.product, r.version) for r in runtimes] == [("nodejs", "18.17.0")]


def test_detect_runtimes_empty_workspace():
    assert detect_runtimes(InMemoryWorkspace({"README.md": "hi"})) == []


def test_detect_runtimes_multiple_products():
    ws = InMemoryWorkspace(
        {
            ".nvmrc": "18.17.0",
            ".python-version": "3.11.4",
            "global.json": json.dumps({"sdk": {"version": "8.0.100"}}),
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version) for r in runtimes) == [
        ("dotnet", "8.0.100"),
        ("nodejs", "18.17.0"),
        ("python", "3.11.4"),
    ]


def test_detect_runtimes_skips_recognised_file_with_malformed_content():
    ws = InMemoryWorkspace(
        {".nvmrc": "lts/hydrogen", ".python-version": "3.11.4"}
    )
    runtimes = detect_runtimes(ws)
    assert [(r.product, r.version) for r in runtimes] == [("python", "3.11.4")]


def test_detect_runtimes_never_returns_unsupported_product(monkeypatch):
    import dev_team.eolscan as eolscan

    monkeypatch.setitem(
        eolscan._PARSERS, "weird.txt", lambda text: ("rustlang", "1.0.0")
    )
    ws = InMemoryWorkspace({"weird.txt": "anything"})
    assert detect_runtimes(ws) == []


def test_detect_runtimes_tolerates_unreadable_file():
    class _Flaky(InMemoryWorkspace):
        def read_text(self, path):
            raise OSError("nope")

    assert detect_runtimes(_Flaky({".nvmrc": "18"})) == []


# --- cycle matching -------------------------------------------------------------


def test_cycle_candidates_order_most_specific_first():
    assert _cycle_candidates("18.17.0") == ["18.17.0", "18.17", "18"]
    assert _cycle_candidates("8") == ["8"]


def test_match_cycle_by_major_only():
    cycles = [{"cycle": "18", "eol": _FUTURE_EOL}, {"cycle": "16", "eol": _PAST_EOL}]
    assert _match_cycle("18.17.0", cycles) == cycles[0]


def test_match_cycle_by_major_minor():
    cycles = [{"cycle": "3.11", "eol": _FUTURE_EOL}]
    assert _match_cycle("3.11.4", cycles) == cycles[0]


def test_match_cycle_no_match_returns_none():
    cycles = [{"cycle": "18", "eol": _FUTURE_EOL}]
    assert _match_cycle("99.0.0", cycles) is None


def test_match_cycle_tolerates_non_list_and_non_dict_entries():
    assert _match_cycle("18.0.0", "not a list") is None
    assert _match_cycle("18.0.0", ["not a dict", {"no": "cycle field"}]) is None
    assert _match_cycle("18.0.0", [{"cycle": None}]) is None


# --- boundary: eol verdicts -----------------------------------------------------


def test_eol_verdict_past_date_is_end_of_life():
    assert _eol_verdict({"eol": _PAST_EOL}) == (True, _PAST_EOL)


def test_eol_verdict_future_date_is_supported():
    assert _eol_verdict({"eol": _FUTURE_EOL}) == (False, _FUTURE_EOL)


def test_eol_verdict_false_means_no_planned_eol():
    assert _eol_verdict({"eol": False}) == (False, None)


def test_eol_verdict_ambiguous_shapes_degrade_to_unknown():
    assert _eol_verdict({}) == ("unknown", None)
    assert _eol_verdict({"eol": True}) == ("unknown", None)
    assert _eol_verdict({"eol": "not-a-date"}) == ("unknown", None)
    assert _eol_verdict({"eol": ""}) == ("unknown", None)
    assert _eol_verdict({"eol": 123}) == ("unknown", None)


# --- query_eol -------------------------------------------------------------------


def test_query_eol_resolves_matching_cycle():
    def fetch(product):
        assert product == "nodejs"
        return [{"cycle": "18", "eol": _PAST_EOL}]

    assert query_eol("nodejs", "18.17.0", fetch=fetch) == (True, _PAST_EOL)


def test_query_eol_unknown_when_cycle_not_in_response():
    def fetch(_product):
        return [{"cycle": "16", "eol": _PAST_EOL}]

    assert query_eol("nodejs", "99.0.0", fetch=fetch) == ("unknown", None)


def test_query_eol_raises_on_non_list_response():
    import pytest

    with pytest.raises(ValueError):
        query_eol("nodejs", "18.0.0", fetch=lambda _p: {"not": "a list"})


# --- scan_eol: the degrade contract ----------------------------------------------


def test_scan_eol_reports_statuses_for_detected_runtimes():
    ws = InMemoryWorkspace(
        {
            ".nvmrc": "18.17.0",
            ".python-version": "3.11.4",
            "global.json": json.dumps({"sdk": {"version": "8.0.100"}}),
        }
    )

    def fetch(product):
        if product == "nodejs":
            return [{"cycle": "18", "eol": _PAST_EOL}]
        if product == "python":
            return [{"cycle": "3.11", "eol": _FUTURE_EOL}]
        return [{"cycle": "6.0", "eol": _FUTURE_EOL}]  # no cycle "8.0" -> unmatched

    scan = scan_eol(ws, fetch=fetch)
    assert scan.queried is True
    assert scan.error is None
    by_product = {s.runtime.product: s for s in scan.statuses}
    assert by_product["nodejs"].end_of_life is True
    assert by_product["nodejs"].eol_date == _PAST_EOL
    assert by_product["python"].end_of_life is False
    assert by_product["dotnet"].end_of_life == "unknown"
    rendered = scan.render()
    assert "END OF LIFE" in rendered
    assert "supported" in rendered
    assert "support status unknown" in rendered
    as_dict = scan.to_dict()
    assert as_dict["statuses"][0]["runtime"]["product"] in {"nodejs", "python", "dotnet"}


def test_scan_eol_degrades_on_fetch_failure():
    ws = InMemoryWorkspace({".nvmrc": "18.17.0"})

    def broken_fetch(_product):
        raise OSError("network down")

    scan = scan_eol(ws, fetch=broken_fetch)
    assert scan.queried is False
    assert "network down" in scan.error
    assert scan.statuses == [EolStatus(scan.runtimes[0], "unknown", None)]
    assert "unavailable" in scan.render()


def test_scan_eol_degrades_on_malformed_response():
    ws = InMemoryWorkspace(
        {".nvmrc": "18.17.0", "global.json": json.dumps({"sdk": {"version": "8.0.0"}})}
    )

    def fetch(product):
        if product == "nodejs":
            return [{"cycle": "18", "eol": _PAST_EOL}]
        return {"unexpected": "shape"}  # dotnet: not a list

    scan = scan_eol(ws, fetch=fetch)
    # One product's malformed response invalidates the whole batch: nothing
    # is published half-resolved, mirroring depscan's atomic degrade.
    assert scan.queried is False
    assert scan.error is not None
    assert all(status.end_of_life == "unknown" for status in scan.statuses)
    assert len(scan.statuses) == 2


def test_scan_eol_disabled_skips_network_entirely():
    ws = InMemoryWorkspace({".nvmrc": "18.17.0"})
    calls = []

    def fetch(product):
        calls.append(product)
        return []

    scan = scan_eol(ws, fetch=fetch, enabled=False)
    assert scan.queried is False
    assert scan.error == "scan disabled"
    assert scan.statuses == [EolStatus(scan.runtimes[0], "unknown", None)]
    assert calls == []
    assert "scan disabled" in scan.render()


def test_scan_eol_empty_workspace_never_queries():
    calls = []

    def fetch(product):
        calls.append(product)
        return []

    scan = scan_eol(InMemoryWorkspace({"README.md": "hi"}), fetch=fetch)
    assert scan.runtimes == []
    assert scan.queried is False
    assert calls == []
    assert scan.render() == ""


def test_scan_eol_at_most_one_request_per_distinct_product():
    # .nvmrc and package.json agree on nodejs; .python-version adds a second
    # distinct product. Total requests must be 2, not 3 — one per matched
    # file would over-count the agreeing nodejs pair.
    ws = InMemoryWorkspace(
        {
            ".nvmrc": "18.17.0",
            "package.json": json.dumps({"engines": {"node": "18.17.0"}}),
            ".python-version": "3.11.4",
        }
    )
    calls = []

    def fetch(product):
        calls.append(product)
        return [{"cycle": "18", "eol": _FUTURE_EOL}, {"cycle": "3.11", "eol": _FUTURE_EOL}]

    scan = scan_eol(ws, fetch=fetch)
    assert scan.queried is True
    assert sorted(calls) == ["nodejs", "python"]


def test_eol_scan_render_without_runtimes_is_empty():
    assert EolScan().render() == ""


# --- security: crafted version strings never reach subprocess/fs/false-match ------


def test_crafted_version_strings_degrade_to_unknown_not_a_false_match():
    malicious = ["; rm -rf /", "../../etc/passwd", "$(whoami)", "`id`"]
    cycles = [{"cycle": "18", "eol": _PAST_EOL}, {"cycle": "3.11", "eol": _FUTURE_EOL}]
    for version in malicious:
        assert _match_cycle(version, cycles) is None
        assert query_eol("nodejs", version, fetch=lambda _p: cycles) == (
            "unknown",
            None,
        )


def test_crafted_manifest_content_causes_no_subprocess_or_filesystem_writes(
    monkeypatch,
):
    def _boom(*_args, **_kwargs):
        raise AssertionError("eolscan must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)

    ws = InMemoryWorkspace(
        {
            ".nvmrc": "1.0.0; rm -rf /",
            ".python-version": "../../etc/passwd",
            "global.json": json.dumps({"sdk": {"version": "$(whoami)"}}),
            "runtime.txt": "python-`id`",
            "package.json": json.dumps({"engines": {"node": ">=1 && curl evil.sh"}}),
        }
    )
    monkeypatch.setattr(ws, "write_text", _boom)
    scan = scan_eol(
        ws,
        fetch=lambda _p: [{"cycle": "1", "eol": _PAST_EOL}, {"cycle": "1.0", "eol": _PAST_EOL}],
    )
    # None of these crash, and the module never touched subprocess/os.system
    # or wrote to the workspace — verified by the monkeypatched raisers above.
    assert isinstance(scan, EolScan)


# --- security: no credential/env-var reads --------------------------------------


def test_no_credential_or_env_var_reads(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    ws = InMemoryWorkspace(
        {".nvmrc": "18.17.0", "global.json": json.dumps({"sdk": {"version": "8.0.0"}})}
    )
    runtimes = detect_runtimes(ws)
    assert len(runtimes) == 2
    end_of_life, eol_date = query_eol(
        "nodejs", "18.17.0", fetch=lambda _p: [{"cycle": "18", "eol": _PAST_EOL}]
    )
    assert (end_of_life, eol_date) == (True, _PAST_EOL)


# --- _http_fetch: the default network call ---------------------------------------


def test_http_fetch_gets_product_endpoint(monkeypatch):
    import urllib.request

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'[{"cycle": "18", "eol": false}]'

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = _http_fetch("nodejs")
    assert result == [{"cycle": "18", "eol": False}]
    assert captured["url"] == "https://endoflife.date/api/nodejs.json"
    assert captured["timeout"] == 30.0
