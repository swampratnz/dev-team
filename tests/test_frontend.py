"""Tests for the greenfield visual baseline (frontend design guidance)."""

from __future__ import annotations

from dev_team.frontend import FRONTEND_GUIDANCE, looks_like_frontend, merge_conventions
from dev_team.models import Design, DesignComponent, FeatureRequest


def _design(**kwargs):
    kwargs.setdefault("overview", "a plan")
    return Design(**kwargs)


def test_looks_like_frontend_detects_web_ui_from_the_request():
    request = FeatureRequest(
        title="Support web app",
        description="server-rendered pages with Jinja2 templates and CSS",
    )
    assert looks_like_frontend(request, _design()) is True


def test_looks_like_frontend_detects_via_design_tech_stack():
    request = FeatureRequest(title="Dashboard", description="show the metrics")
    design = Design(overview="a service", tech_stack=["Python", "HTML", "responsive CSS"])
    assert looks_like_frontend(request, design) is True


def test_looks_like_frontend_detects_via_component_text():
    request = FeatureRequest(title="Thing", description="stuff")
    design = _design(
        components=[DesignComponent(name="templates", responsibility="Jinja2 pages")]
    )
    assert looks_like_frontend(request, design) is True


def test_looks_like_frontend_false_for_a_backend_delivery():
    request = FeatureRequest(
        title="Log parser",
        description="Read JSON log files and compute stats, exposed via a CLI",
    )
    design = Design(
        overview="A batch pipeline reading files and writing a report",
        tech_stack=["Python", "sqlite"],
    )
    assert looks_like_frontend(request, design) is False


def test_merge_conventions_without_existing():
    merged = merge_conventions(None, "TOKENS")
    assert merged.startswith("Frontend design baseline:")
    assert "TOKENS" in merged


def test_merge_conventions_preserves_existing_conventions():
    merged = merge_conventions("House style: snake_case.", "TOKENS")
    assert "House style: snake_case." in merged
    assert "Frontend design baseline:" in merged
    # learned conventions come first, the baseline is appended
    assert merged.index("House style") < merged.index("Frontend design baseline")


def test_frontend_guidance_covers_the_essentials():
    text = FRONTEND_GUIDANCE.lower()
    assert "contrast" in text  # accessibility
    assert "focus" in text  # visible focus states
    assert "responsive" in text  # layout
    assert "semantic" in text  # semantic HTML
