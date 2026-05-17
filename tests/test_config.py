"""Tests for project.yaml configuration parsing."""

from pathlib import Path

import pytest
import yaml

from agent.config import ProjectConfig


@pytest.fixture
def yaml_data() -> dict:
    return {
        "name": "testProject",
        "purpose": "A test project for unit testing",
        "users": "Developers",
        "stage": "mvp",
        "goals": ["Build fast", "Test everything"],
        "similar": ["Jest", "Pytest"],
        "quality": ["tests", "ci-cd"],
    }


def test_from_yaml(tmp_path: Path, yaml_data: dict) -> None:
    path = tmp_path / "project.yaml"
    path.write_text(yaml.dump(yaml_data), encoding="utf-8")

    config = ProjectConfig.from_yaml(path)

    assert config.name == "testProject"
    assert config.purpose == "A test project for unit testing"
    assert config.stage == "mvp"
    assert len(config.goals) == 2
    assert "Jest" in config.similar


def test_from_yaml_minimal(tmp_path: Path) -> None:
    path = tmp_path / "project.yaml"
    path.write_text(yaml.dump({"name": "minimal"}), encoding="utf-8")

    config = ProjectConfig.from_yaml(path)

    assert config.name == "minimal"
    assert config.purpose == ""
    assert config.goals == []
    assert config.quality == []


def test_to_context(yaml_data: dict, tmp_path: Path) -> None:
    path = tmp_path / "project.yaml"
    path.write_text(yaml.dump(yaml_data), encoding="utf-8")

    config = ProjectConfig.from_yaml(path)
    context = config.to_context()

    assert "testProject" in context
    assert "A test project" in context
    assert "Build fast" in context
    assert "Jest" in context
