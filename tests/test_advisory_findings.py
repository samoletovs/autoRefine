"""Tests for advisory findings — the ones no pull request can repair.

Some defects are real, worth scoring, and impossible to fix with a commit:
default-branch protection, org policy, infrastructure outside the repo. Left in
the planning prompt they become improvements; improvements at P0/P1 become filed
GitHub issues; an approved issue becomes a 10-30 minute Copilot Coding Agent run
that is *guaranteed* to produce nothing, or worse a plausible workflow file that
pretends to do the job. Near-certain no-op runs are the worst cost profile in the
system — worse than a burst of ideas that might at least build.

Priority is not a sufficient guard, and these tests say so explicitly. Filing is
gated on ``DEFAULT_IDEA_PRIORITIES = {"P0", "P1"}``, but a P2 finding still enters
the prompt and the model is free to answer it with a P1 improvement. The only
place the path actually closes is ``build_plan_task`` — the single point at which
a finding becomes prompt text.

These tests do not change what is flagged today: no existing check sets
``advisory``, so the filter is inert on real data until the first producer lands.
What they pin is that the mechanism is airtight before anything relies on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import ProjectConfig
from agent.foundry_agent import build_plan_task
from agent.main import DEFAULT_IDEA_PRIORITIES, _priority_in_scope, evaluate_project
from agent.tools.quality_tools import (
    QualityCoverage,
    QualityFinding,
    is_advisory,
    plannable_findings,
)


@pytest.fixture
def config() -> ProjectConfig:
    return ProjectConfig(
        name="demo", purpose="A demo", users="Devs", stage="active",
        goals=["Ship"], similar=["CompetitorX"], quality=[],
    )


def _finding(description: str, priority: str = "P1", advisory: bool = False) -> dict:
    """A finding in the flattened shape ``evaluate_project`` puts in the report."""
    return {
        "category": "security",
        "description": description,
        "priority": priority,
        "advisory": advisory,
    }


# ── is_advisory ────────────────────────────────────────────────────────────


class TestIsAdvisory:
    @pytest.mark.parametrize("value,expected", [(True, True), (False, False)])
    def test_reads_a_real_boolean(self, value: bool, expected: bool) -> None:
        assert is_advisory(_finding("x", advisory=value)) is expected

    def test_absent_means_plannable(self) -> None:
        """Every finding that exists today omits the key and must be unaffected."""
        assert is_advisory({"category": "tests", "description": "x", "priority": "P0"}) is False

    @pytest.mark.parametrize("value", ["false", "true", 1, 0, None, [], "yes"])
    def test_non_boolean_is_ignored_rather_than_coerced(self, value: object) -> None:
        """``bool("false")`` is True; one such typo would mute the model entirely."""
        assert is_advisory({"description": "x", "advisory": value}) is False

    def test_non_boolean_is_ignored_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            is_advisory({"description": "branch protection", "advisory": "true"})
        assert "non-boolean" in caplog.text
        assert "branch protection" in caplog.text


# ── plannable_findings ─────────────────────────────────────────────────────


class TestPlannableFindings:
    def test_drops_advisory_and_keeps_the_rest_in_order(self) -> None:
        findings = [
            _finding("keep one"),
            _finding("drop me", advisory=True),
            _finding("keep two"),
        ]
        assert [f["description"] for f in plannable_findings(findings)] == [
            "keep one", "keep two",
        ]

    def test_empty_in_empty_out(self) -> None:
        assert plannable_findings([]) == []

    def test_all_advisory_leaves_nothing(self) -> None:
        assert plannable_findings([_finding("a", advisory=True)]) == []


# ── The chokepoint ─────────────────────────────────────────────────────────


class TestBuildPlanTaskWithholdsAdvisory:
    def test_advisory_text_never_reaches_the_prompt(self, config: ProjectConfig) -> None:
        task = build_plan_task(
            [
                _finding("No test directory found"),
                _finding("Default branch has no protection rule", advisory=True),
            ],
            config,
        )

        assert "No test directory found" in task
        assert "Default branch has no protection rule" not in task
        assert "protection" not in task

    def test_priority_is_not_the_mechanism(self, config: ProjectConfig) -> None:
        """A P0 advisory finding — the highest-signal kind — is still withheld.

        This is the case that priority filtering cannot cover: P0 is inside
        ``DEFAULT_IDEA_PRIORITIES``, so nothing downstream would have stopped it.
        """
        assert _priority_in_scope("P0", DEFAULT_IDEA_PRIORITIES) is True

        task = build_plan_task([_finding("unfixable by any commit", "P0", advisory=True)], config)
        assert "unfixable by any commit" not in task

    def test_an_all_advisory_report_produces_no_findings_section(
        self, config: ProjectConfig
    ) -> None:
        """Identical to having had no findings — not an empty heading to fill in."""
        withheld = build_plan_task([_finding("a", advisory=True), _finding("b", advisory=True)], config)
        none_at_all = build_plan_task([], config)

        assert "## Quality check findings" not in withheld
        assert withheld == none_at_all

    def test_every_plannable_finding_survives_and_every_advisory_one_does_not(
        self, config: ProjectConfig
    ) -> None:
        """The general property, over a mixed list rather than one example."""
        findings = [
            _finding(f"finding-{i}", priority=p, advisory=(i % 3 == 0))
            for i, p in enumerate(["P0", "P1", "P2", "P3", "P0", "P1", "P2"])
        ]
        task = build_plan_task(findings, config)

        for f in findings:
            if f["advisory"]:
                assert f["description"] not in task, f"leaked {f['description']}"
            else:
                assert f["description"] in task, f"lost {f['description']}"

    def test_legacy_findings_without_the_key_are_unchanged(
        self, config: ProjectConfig
    ) -> None:
        """Today's findings omit `advisory` entirely; the prompt must not move."""
        legacy = [{"priority": "P0", "category": "tests", "description": "missing"}]
        assert "- [P0] tests: missing" in build_plan_task(legacy, config)

    def test_withholding_is_logged(
        self, config: ProjectConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            build_plan_task([_finding("quiet one", advisory=True)], config)
        assert "Withholding 1 advisory finding" in caplog.text
        assert "demo" in caplog.text


# ── The wiring, end to end ─────────────────────────────────────────────────


class TestReportToPromptChain:
    """The invariant that matters: a finding produced by a check cannot leak."""

    def _report(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                config: ProjectConfig) -> dict:
        findings = [
            QualityFinding(
                category="tests",
                description="No test directory found",
                priority="P0",
                weight=20,
            ),
            QualityFinding(
                category="security",
                description="Default branch has no protection rule",
                priority="P1",
                weight=10,
                advisory=True,
            ),
        ]
        monkeypatch.setattr(
            "agent.main.run_quality_checks_with_coverage",
            lambda _p, _c: (findings, QualityCoverage()),
        )
        return evaluate_project(tmp_path, config)

    def test_advisory_finding_survives_into_the_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: ProjectConfig
    ) -> None:
        """Withheld from the model, not hidden from humans."""
        report = self._report(monkeypatch, tmp_path, config)

        advisory = [f for f in report["findings"] if f["advisory"]]
        assert [f["description"] for f in advisory] == [
            "Default branch has no protection rule"
        ]

    def test_advisory_finding_still_costs_the_score(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: ProjectConfig
    ) -> None:
        """`advisory` governs the prompt only. Weight governs the score, separately."""
        report = self._report(monkeypatch, tmp_path, config)
        assert report["score"] == 70  # 100 - 20 (tests) - 10 (advisory security)

    def test_the_report_cannot_leak_it_into_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: ProjectConfig
    ) -> None:
        """report["findings"] is passed to plan_project unfiltered — by design.

        Safety must not depend on the caller remembering to filter, so the report
        carries everything and the chokepoint does the withholding.
        """
        report = self._report(monkeypatch, tmp_path, config)
        task = build_plan_task(report["findings"], config)

        assert "No test directory found" in task
        assert "Default branch has no protection rule" not in task


class TestChokepointStaysSingular:
    """The guard that outlives us: only one function may turn findings into prompt.

    Every test above proves ``build_plan_task`` withholds advisory findings. None
    of them would notice a *second* prompt builder taking findings — and the whole
    argument for filtering at the chokepoint rather than at the call sites rests on
    there being exactly one. So assert that, and fail loudly when it stops holding.
    """

    def test_only_build_plan_task_consumes_findings_in_the_prompt_module(self) -> None:
        import inspect

        from agent import foundry_agent

        consumers = {
            name
            for name, fn in inspect.getmembers(foundry_agent, inspect.isfunction)
            if fn.__module__ == foundry_agent.__name__
            and "findings" in inspect.signature(fn).parameters
        }

        assert consumers == {"build_plan_task"}, (
            "A new function in agent.foundry_agent accepts `findings`. If it renders "
            "them into a prompt it must call plannable_findings() first, or advisory "
            "findings will reach the model through it. Add it here once it does."
        )
