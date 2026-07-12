"""Tests for the house-conventions profile and store."""

from __future__ import annotations

from dev_team.conventions import (
    ConventionsProfile,
    ConventionsStore,
    detect_convention_sources,
)
from dev_team.execution import InMemoryWorkspace


def _profile():
    return ConventionsProfile(
        summary="C# with PascalCase members and MSTest tests.",
        conventions=[
            {
                "aspect": "naming",
                "convention": "private fields use _camelCase",
                "evidence": "BlackPearl.Core/Service.cs",
            },
            {"aspect": "tests", "convention": "MSTest with FluentAssertions"},
        ],
        sources=[".editorconfig", "Black Pearl.sln.DotSettings"],
    )


def test_detect_convention_sources():
    ws = InMemoryWorkspace(
        {
            ".editorconfig": "root = true",
            "Black Pearl.sln.DotSettings": "<wpf />",
            "rules/Custom.ruleset": "<RuleSet />",
            "web/.eslintrc.json": "{}",
            ".dev_team/conventions.json": "{}",
            "src/Program.cs": "class P {}",
        }
    )
    assert detect_convention_sources(ws) == [
        ".editorconfig",
        "Black Pearl.sln.DotSettings",
        "rules/Custom.ruleset",
        "web/.eslintrc.json",
    ]


def test_profile_render_includes_conventions_and_sources():
    rendered = _profile().render()
    assert "House conventions" in rendered
    assert "naming: private fields use _camelCase" in rendered
    assert "(evidence: BlackPearl.Core/Service.cs)" in rendered
    assert "tests: MSTest with FluentAssertions" in rendered
    assert "Black Pearl.sln.DotSettings" in rendered


def test_profile_render_is_bounded_and_caps_items():
    profile = ConventionsProfile(
        conventions=[
            {"aspect": "naming", "convention": "x" * 500} for _ in range(40)
        ]
    )
    rendered = profile.render()
    assert len(rendered) <= 4_000


def test_empty_profile_renders_empty():
    assert ConventionsProfile().render() == ""
    assert ConventionsProfile().empty is True


def test_from_dict_filters_junk():
    profile = ConventionsProfile.from_dict(
        {"summary": 3, "conventions": ["junk", {"aspect": "naming"}], "sources": [1]}
    )
    assert profile.summary == "3"
    assert profile.conventions == [{"aspect": "naming"}]
    assert profile.sources == ["1"]


def test_store_roundtrip():
    ws = InMemoryWorkspace()
    store = ConventionsStore(ws)
    store.save(_profile())
    loaded = store.load()
    assert loaded is not None
    assert loaded.summary == _profile().summary
    assert loaded.conventions == _profile().conventions
    assert loaded.sources == _profile().sources


def test_store_load_absent_corrupt_or_empty_is_none():
    ws = InMemoryWorkspace()
    store = ConventionsStore(ws)
    assert store.load() is None
    ws.write_text(store.path, "{truncated")
    assert store.load() is None
    ws.write_text(store.path, "[]")
    assert store.load() is None
    store.save(ConventionsProfile())
    assert store.load() is None
