"""Tests for TeamConfig validation."""

from __future__ import annotations

import pytest

from dev_team.config import TeamConfig


def test_defaults():
    config = TeamConfig()
    assert config.model is None
    assert config.max_task_attempts == 2
    assert config.min_coverage == 100.0
    assert config.permission_mode == "acceptEdits"
    assert config.working_dir is None


def test_custom_values():
    config = TeamConfig(model="m", max_task_attempts=3, min_coverage=80.0)
    assert config.model == "m"
    assert config.max_task_attempts == 3
    assert config.min_coverage == 80.0


def test_rejects_zero_attempts():
    with pytest.raises(ValueError, match="max_task_attempts"):
        TeamConfig(max_task_attempts=0)


@pytest.mark.parametrize("coverage", [-1.0, 101.0])
def test_rejects_out_of_range_coverage(coverage):
    with pytest.raises(ValueError, match="min_coverage"):
        TeamConfig(min_coverage=coverage)


def test_max_turns_validation():
    with pytest.raises(ValueError):
        TeamConfig(max_turns=0)
    assert TeamConfig(max_turns=None).max_turns is None
