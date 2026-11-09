"""Tests for the self-guided onboarding page."""

from pathlib import Path

from agent.main import run_onboarding_mode
from agent.onboarding import render_onboarding_html


def test_onboarding_page_covers_setup_and_usage_steps() -> None:
    page = render_onboarding_html()

    assert "Install autoRefine" in page
    assert "Describe your project" in page
    assert "Run your first evaluation" in page
    assert "Explore a plan safely" in page
    assert "project.yaml" in page
    assert "file-ideas --dry-run" in page
    assert 'role="tooltip"' in page


def test_onboarding_page_collects_local_feedback_and_supports_export() -> None:
    page = render_onboarding_html()

    assert "autorefine.onboarding.v1" in page
    assert 'name="rating"' in page
    assert "Save feedback" in page
    assert "Download feedback JSON" in page
    assert "new Blob" in page
    assert "Feedback and progress stay in this browser" in page
    assert "fetch(" not in page


def test_run_onboarding_mode_writes_self_contained_page(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "onboarding.html"

    run_onboarding_mode(str(output))

    page = output.read_text(encoding="utf-8")
    assert page.startswith("<!DOCTYPE html>")
    assert "Welcome to autoRefine" in page
