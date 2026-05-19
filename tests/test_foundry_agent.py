"""Regression tests for foundry_agent helpers that don't require Foundry.

Covers:
  - The fallback plan parser (_parse_plan_from_text), specifically the
    separator class which previously contained mojibake (U+00F9 'ù') and
    failed to capture rows that used Unicode separators like the middle-dot.
  - The model parameter pass-through in create_agent (smoke-checks the
    signature, no real agent is created).
"""

from __future__ import annotations

import inspect

from agent.foundry_agent import _parse_plan_from_text, create_agent


class TestParsePlanFromText:
    def test_score_extracted(self) -> None:
        # Parser requires at least one improvement row to return a plan.
        text = "Findings\n\nScore: 84/100\n\n1. **Adopt CI** \u2014 ship green.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert plan["score"] == 84

    def test_returns_none_without_improvements(self) -> None:
        """Parser bails out (returns None) when no numbered improvement rows
        are found, so the caller can fall back to other strategies."""
        plan = _parse_plan_from_text("just some text Score: 12/100 with no list")
        assert plan is None

    def test_em_dash_separator(self) -> None:
        text = "Score: 70/100\n\n1. **Add tests** \u2014 cover regressions.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert len(plan["improvements"]) == 1
        assert plan["improvements"][0]["title"] == "Add tests"

    def test_middle_dot_separator(self) -> None:
        """Regression: previously the regex contained 'ù' (U+00F9, mojibake)
        instead of '·' (U+00B7, middle dot). Lines using '·' as the
        separator were silently dropped."""
        text = "Score: 65/100\n\n1. **Refactor config** \u00b7 split module.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert len(plan["improvements"]) == 1
        assert plan["improvements"][0]["title"] == "Refactor config"

    def test_priority_tag_captured(self) -> None:
        text = "Score: 60/100\n\n1. [P0] **Add CI** \u2014 must ship.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert plan["improvements"][0]["priority"] == "P0"

    def test_multiple_separators_in_one_response(self) -> None:
        text = (
            "Score: 72/100\n\n"
            "1. [P0] **Add CI tests** \u00b7 cover quality_tools.\n"
            "2. **Replace DDG scrape** \u2014 switch to a proper API.\n"
            "3. [P1] **Retry/backoff** : transient 429s fail evals.\n"
        )
        plan = _parse_plan_from_text(text)
        assert plan is not None
        titles = [i["title"] for i in plan["improvements"]]
        assert titles == [
            "Add CI tests",
            "Replace DDG scrape",
            "Retry/backoff",
        ]
        priorities = [i["priority"] for i in plan["improvements"]]
        assert priorities == ["P0", "P2", "P1"]


class TestCreateAgentSignature:
    def test_accepts_model_kwarg(self) -> None:
        """The CLI --model flag must reach create_agent. This test guards
        against regressions where the kwarg gets dropped from the signature."""
        sig = inspect.signature(create_agent)
        assert "model" in sig.parameters
        assert sig.parameters["model"].default is None
