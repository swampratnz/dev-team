"""Package-level import and metadata tests."""

from __future__ import annotations

import importlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_version_exposed():
    import dev_team

    assert dev_team.__version__ == "0.7.0"
    assert "DevTeam" in dev_team.__all__
    assert "DeliveryEngine" in dev_team.__all__


def test_public_symbols_importable():
    import dev_team

    for name in dev_team.__all__:
        assert hasattr(dev_team, name), name


def test_main_module_imports():
    module = importlib.import_module("dev_team.__main__")
    assert hasattr(module, "main")


def test_assessment_sample_doc_ships():
    sample = _REPO_ROOT / "docs" / "examples" / "assessment-sample.md"
    assert sample.is_file(), sample
    # The sample mirrors render_report, which always emits a Classification.
    assert "Classification:" in sample.read_text(encoding="utf-8")
