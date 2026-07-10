"""Package-level import and metadata tests."""

from __future__ import annotations

import importlib


def test_version_exposed():
    import dev_team

    assert dev_team.__version__ == "0.1.0"
    assert "DevTeam" in dev_team.__all__


def test_public_symbols_importable():
    import dev_team

    for name in dev_team.__all__:
        assert hasattr(dev_team, name), name


def test_main_module_imports():
    module = importlib.import_module("dev_team.__main__")
    assert hasattr(module, "main")
