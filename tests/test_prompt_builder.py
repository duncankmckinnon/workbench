"""Tests for plan context/conventions parsing and prompt building."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from workbench.agents import (
    DEFAULT_DIRECTIVES,
    PLANNER_DIRECTIVE,
    REVIEWER_FOLLOWUP_DIRECTIVE,
    Role,
    build_planner_prompt,
    build_prompt,
)
from workbench.plan_parser import Task, parse_plan
from workbench.worktree import Worktree

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def sample_task() -> Task:
    return Task(
        id="task-1",
        title="Add widget",
        description="Implement the widget module.",
        files=["src/widget.py", "src/utils.py"],
    )


@pytest.fixture
def sample_worktree(tmp_path: Path) -> Worktree:
    return Worktree(path=tmp_path, branch="wb/task-1-add-widget", task_id="task-1")


# ── Plan parser tests ────────────────────────────────────────────────


def test_parse_plan_extracts_context(tmp_path: Path):
    plan_md = tmp_path / "plan.md"
    plan_md.write_text(
        "# My Plan\n\n"
        "## Context\n\n"
        "This project uses Python 3.12 and pytest.\n\n"
        "## Task: Do something\n\n"
        "Description here.\n"
    )
    plan = parse_plan(plan_md)
    assert plan.context == "This project uses Python 3.12 and pytest."


def test_parse_plan_extracts_conventions(tmp_path: Path):
    plan_md = tmp_path / "plan.md"
    plan_md.write_text(
        "# My Plan\n\n"
        "## Conventions\n\n"
        "Use snake_case for all functions.\n"
        "Keep modules under 200 lines.\n\n"
        "## Task: Do something\n\n"
        "Description here.\n"
    )
    plan = parse_plan(plan_md)
    assert "snake_case" in plan.conventions
    assert "200 lines" in plan.conventions


def test_parse_plan_no_context(tmp_path: Path):
    plan_md = tmp_path / "plan.md"
    plan_md.write_text("# My Plan\n\n" "## Task: Do something\n\n" "Description here.\n")
    plan = parse_plan(plan_md)
    assert plan.context == ""
    assert plan.conventions == ""


def test_parse_plan_extracts_both(tmp_path: Path):
    plan_md = tmp_path / "plan.md"
    plan_md.write_text(
        "# My Plan\n\n"
        "## Context\n\n"
        "Python 3.12 project.\n\n"
        "## Conventions\n\n"
        "Use type hints everywhere.\n\n"
        "## Task: First task\n\n"
        "Do the thing.\n"
    )
    plan = parse_plan(plan_md)
    assert plan.context == "Python 3.12 project."
    assert plan.conventions == "Use type hints everywhere."
    assert len(plan.tasks) == 1


def test_parse_plan_case_insensitive_headings(tmp_path: Path):
    plan_md = tmp_path / "plan.md"
    plan_md.write_text(
        "# Plan\n\n"
        "## context\n\n"
        "Some context.\n\n"
        "## CONVENTIONS\n\n"
        "Some conventions.\n\n"
        "## Task: T1\n\nDesc.\n"
    )
    plan = parse_plan(plan_md)
    assert plan.context == "Some context."
    assert plan.conventions == "Some conventions."


# ── build_prompt tests ────────────────────────────────────────────────


def test_build_prompt_implementor_has_context(sample_task, sample_worktree):
    prompt = build_prompt(
        role=Role.IMPLEMENTOR,
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
        plan_context="Python 3.12 with asyncio.",
    )
    assert "## Context" in prompt
    assert "Python 3.12 with asyncio." in prompt


def test_build_prompt_implementor_has_conventions(sample_task, sample_worktree):
    prompt = build_prompt(
        role=Role.IMPLEMENTOR,
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
        plan_conventions="Use type hints everywhere.",
    )
    assert "## Conventions" in prompt
    assert "Use type hints everywhere." in prompt


def test_build_prompt_implementor_has_branch(sample_task, sample_worktree):
    prompt = build_prompt(
        role=Role.IMPLEMENTOR,
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
    )
    assert "wb/task-1-add-widget" in prompt
    assert "Stay on this branch" in prompt


def test_build_prompt_tester_has_branch_pinning(sample_task, sample_worktree):
    with patch("workbench.agents.get_diff", return_value="some diff"):
        prompt = build_prompt(
            role=Role.TESTER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
        )
    assert "Stay on this branch" in prompt


def test_build_prompt_directive_override(sample_task, sample_worktree):
    custom = "You are a custom agent. Do custom things."
    prompt = build_prompt(
        role=Role.IMPLEMENTOR,
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
        directive=custom,
    )
    assert custom in prompt
    assert DEFAULT_DIRECTIVES[Role.IMPLEMENTOR] not in prompt


def test_build_prompt_fixer_has_feedback(sample_task, sample_worktree):
    with patch("workbench.agents.get_diff", return_value="diff content"):
        prompt = build_prompt(
            role=Role.FIXER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
            extra_context="Tests failed: missing return value.",
        )
    assert "## Feedback to address" in prompt
    assert "missing return value" in prompt


def test_build_prompt_tester_has_diff(sample_task, sample_worktree):
    with patch("workbench.agents.get_diff", return_value="+ added line") as mock_diff:
        prompt = build_prompt(
            role=Role.TESTER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
        )
    mock_diff.assert_called_once_with(sample_worktree, "main")
    assert "## Changes made" in prompt
    assert "+ added line" in prompt


def test_build_prompt_reviewer_has_diff(sample_task, sample_worktree):
    with patch("workbench.agents.get_diff", return_value="- removed line") as mock_diff:
        prompt = build_prompt(
            role=Role.REVIEWER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
        )
    mock_diff.assert_called_once_with(sample_worktree, "main")
    assert "## Diff to review" in prompt
    assert "- removed line" in prompt


def test_build_prompt_implementor_has_task_and_files(sample_task, sample_worktree):
    prompt = build_prompt(
        role=Role.IMPLEMENTOR,
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
    )
    assert "## Task: Add widget" in prompt
    assert "Implement the widget module." in prompt
    assert "src/widget.py" in prompt
    assert "Implement this task and commit your changes." in prompt


def test_build_prompt_default_directive_used(sample_task, sample_worktree):
    prompt = build_prompt(
        role=Role.IMPLEMENTOR,
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
    )
    assert DEFAULT_DIRECTIVES[Role.IMPLEMENTOR] in prompt


def test_build_prompt_merger_has_branch_pinning(sample_task, sample_worktree):
    with patch("workbench.agents.get_diff", return_value=""):
        prompt = build_prompt(
            role=Role.MERGER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
        )
    assert "wb/task-1-add-widget" in prompt
    assert "Stay on this branch" in prompt
    assert "Resolve all merge conflicts" in prompt


def test_build_prompt_merger_has_directive(sample_task, sample_worktree):
    with patch("workbench.agents.get_diff", return_value=""):
        prompt = build_prompt(
            role=Role.MERGER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
        )
    assert DEFAULT_DIRECTIVES[Role.MERGER] in prompt


def test_build_prompt_reviewer_no_branch_pinning(sample_task, sample_worktree):
    with patch("workbench.agents.get_diff", return_value=""):
        prompt = build_prompt(
            role=Role.REVIEWER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
        )
    assert "Stay on this branch" not in prompt


# ── Reviewer follow-up mode ──────────────────────────────────────────


def test_build_prompt_reviewer_followup_uses_diff_since(sample_task, sample_worktree):
    """With prior_review_sha set, REVIEWER uses get_diff_since against that SHA."""
    with (
        patch("workbench.agents.get_diff_since", return_value="+ fixer line") as mock_since,
        patch("workbench.agents.get_diff") as mock_full,
    ):
        prompt = build_prompt(
            role=Role.REVIEWER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
            directive=REVIEWER_FOLLOWUP_DIRECTIVE,
            extra_context="Missing null check in foo().",
            prior_review_sha="abc123",
        )

    mock_since.assert_called_once_with(sample_worktree, "abc123")
    mock_full.assert_not_called()
    assert "## Changes since prior review" in prompt
    assert "+ fixer line" in prompt
    assert "## Diff to review" not in prompt


def test_build_prompt_reviewer_followup_includes_prior_feedback(sample_task, sample_worktree):
    """Follow-up review renders extra_context as the prior feedback section."""
    with patch("workbench.agents.get_diff_since", return_value=""):
        prompt = build_prompt(
            role=Role.REVIEWER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
            directive=REVIEWER_FOLLOWUP_DIRECTIVE,
            extra_context="Missing null check in foo().",
            prior_review_sha="abc123",
        )
    assert "## Prior review feedback" in prompt
    assert "Missing null check in foo()." in prompt
    assert "Verify each item in the prior review feedback" in prompt


def test_build_prompt_reviewer_first_pass_ignores_prior_sha_param(sample_task, sample_worktree):
    """Without prior_review_sha, REVIEWER behaves as the comprehensive first pass."""
    with (
        patch("workbench.agents.get_diff", return_value="- full diff") as mock_full,
        patch("workbench.agents.get_diff_since") as mock_since,
    ):
        prompt = build_prompt(
            role=Role.REVIEWER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
        )

    mock_full.assert_called_once_with(sample_worktree, "main")
    mock_since.assert_not_called()
    assert "## Diff to review" in prompt
    assert "## Changes since prior review" not in prompt
    assert "## Prior review feedback" not in prompt


def test_build_prompt_reviewer_followup_action_line(sample_task, sample_worktree):
    """Follow-up review swaps the default action line for the verification instruction."""
    with patch("workbench.agents.get_diff_since", return_value=""):
        prompt = build_prompt(
            role=Role.REVIEWER,
            task=sample_task,
            worktree=sample_worktree,
            base_branch="main",
            directive=REVIEWER_FOLLOWUP_DIRECTIVE,
            extra_context="prior feedback",
            prior_review_sha="abc123",
        )
    assert "Provide your review." not in prompt
    assert "Do not raise new issues." in prompt


# ── Planner prompt ───────────────────────────────────────────────────


def test_build_planner_prompt_contains_directive():
    prompt = build_planner_prompt(Path("/repo/plans/auth.md"), user_prompt="Add auth")
    assert PLANNER_DIRECTIVE in prompt


def test_build_planner_prompt_contains_user_request():
    prompt = build_planner_prompt(Path("/repo/plan.md"), user_prompt="Add JWT authentication")
    assert "## User Request" in prompt
    assert "Add JWT authentication" in prompt


def test_build_planner_prompt_contains_output_path():
    output = Path("/repo/.workbench/plans/auth.md")
    prompt = build_planner_prompt(output, user_prompt="Add auth")
    assert str(output) in prompt


def test_build_planner_prompt_contains_plan_guide():
    prompt = build_planner_prompt(Path("/repo/plan.md"), user_prompt="Add auth")
    assert "## Plan Writing Guide" in prompt
    assert "## Plan Format" in prompt
    assert "## Task:" in prompt


def test_build_planner_prompt_from_source_document():
    """Source content is rendered as a Source Document section."""
    prompt = build_planner_prompt(
        Path("/repo/plan.md"),
        source_content="# Claude Plan\n\n## Step 1\nDo something\n",
    )
    assert "## Source Document" in prompt
    assert "Claude Plan" in prompt
    assert "Transform it into a workbench plan" in prompt
    assert "## User Request" not in prompt


def test_build_planner_prompt_source_with_guidance():
    """Both source and prompt: prompt becomes Additional Guidance."""
    prompt = build_planner_prompt(
        Path("/repo/plan.md"),
        user_prompt="Focus on security",
        source_content="# Spec\nBuild a widget.\n",
    )
    assert "## Source Document" in prompt
    assert "Build a widget" in prompt
    assert "## Additional Guidance" in prompt
    assert "Focus on security" in prompt
    assert "## User Request" not in prompt
