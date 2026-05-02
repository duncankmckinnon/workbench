"""Tests for plan context/conventions parsing and directive rendering."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from workbench.agents import AgentResult, Role, TaskStatus, _load_plan_guide, run_planner
from workbench.directives import (
    FixerDirective,
    ImplementorDirective,
    MergerDirective,
    PlannerDirective,
    PromptContext,
    ReviewerDirective,
    ReviewerFollowupDirective,
    TesterDirective,
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


# ── Directive render tests ───────────────────────────────────────────


def test_implementor_has_context(sample_task, sample_worktree):
    ctx = PromptContext(
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
        plan_context="Python 3.12 with asyncio.",
    )
    prompt = ImplementorDirective().render(ctx)
    assert "## Context" in prompt
    assert "Python 3.12 with asyncio." in prompt


def test_implementor_has_conventions(sample_task, sample_worktree):
    ctx = PromptContext(
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
        plan_conventions="Use type hints everywhere.",
    )
    prompt = ImplementorDirective().render(ctx)
    assert "## Conventions" in prompt
    assert "Use type hints everywhere." in prompt


def test_implementor_has_branch(sample_task, sample_worktree):
    ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
    prompt = ImplementorDirective().render(ctx)
    assert "wb/task-1-add-widget" in prompt
    assert "Stay on this branch" in prompt


def test_tester_has_branch_pinning(sample_task, sample_worktree):
    with patch("workbench.directives.get_diff", return_value="some diff"):
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = TesterDirective().render(ctx)
    assert "Stay on this branch" in prompt


def test_directive_override(sample_task, sample_worktree):
    custom = "You are a custom agent. Do custom things."
    ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
    prompt = ImplementorDirective(directive_text=custom).render(ctx)
    assert custom in prompt
    assert ImplementorDirective.DEFAULT_TEXT not in prompt


def test_fixer_has_feedback(sample_task, sample_worktree):
    with patch("workbench.directives.get_diff", return_value="diff content"):
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = FixerDirective(
            feedback="Tests failed: missing return value.",
            failure_kind="test",
            attempt=1,
        ).render(ctx)
    assert "## Feedback to address" in prompt
    assert "missing return value" in prompt


def test_tester_has_diff(sample_task, sample_worktree):
    with patch("workbench.directives.get_diff", return_value="+ added line") as mock_diff:
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = TesterDirective().render(ctx)
    mock_diff.assert_called_once_with(sample_worktree, "main")
    assert "## Changes made" in prompt
    assert "+ added line" in prompt


def test_reviewer_has_diff(sample_task, sample_worktree):
    with patch("workbench.directives.get_diff", return_value="- removed line") as mock_diff:
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = ReviewerDirective().render(ctx)
    mock_diff.assert_called_once_with(sample_worktree, "main")
    assert "## Diff to review" in prompt
    assert "- removed line" in prompt


def test_implementor_has_task_and_files(sample_task, sample_worktree):
    ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
    prompt = ImplementorDirective().render(ctx)
    assert "## Task: Add widget" in prompt
    assert "Implement the widget module." in prompt
    assert "src/widget.py" in prompt
    assert "Implement this task and commit your changes." in prompt


def test_default_directive_used(sample_task, sample_worktree):
    ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
    prompt = ImplementorDirective().render(ctx)
    assert ImplementorDirective.DEFAULT_TEXT in prompt


def test_merger_has_branch_info():
    prompt = MergerDirective(
        task_branch="wb/task-1-add-widget",
        session_branch="main",
        conflicts=["src/foo.py"],
    ).render()
    assert "wb/task-1-add-widget" in prompt
    assert "resolve ALL merge conflicts" in prompt


def test_merger_has_directive():
    prompt = MergerDirective(
        task_branch="feature/x",
        session_branch="main",
        conflicts=["a.py"],
    ).render()
    assert MergerDirective.DEFAULT_TEXT in prompt


def test_reviewer_no_branch_pinning(sample_task, sample_worktree):
    with patch("workbench.directives.get_diff", return_value=""):
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = ReviewerDirective().render(ctx)
    assert "Stay on this branch" not in prompt


# ── Reviewer follow-up mode ──────────────────────────────────────────


def test_reviewer_followup_uses_diff_since(sample_task, sample_worktree):
    """With prior_review_sha set, ReviewerFollowupDirective uses get_diff_since."""
    with (
        patch("workbench.directives.get_diff_since", return_value="+ fixer line") as mock_since,
        patch("workbench.directives.get_diff") as mock_full,
    ):
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = ReviewerFollowupDirective(
            prior_review_sha="abc123",
            prior_feedback="Missing null check in foo().",
        ).render(ctx)

    mock_since.assert_called_once_with(sample_worktree, "abc123")
    mock_full.assert_not_called()
    assert "## Changes since prior review" in prompt
    assert "+ fixer line" in prompt
    assert "## Diff to review" not in prompt


def test_reviewer_followup_includes_prior_feedback(sample_task, sample_worktree):
    """Follow-up review renders prior_feedback as the prior feedback section."""
    with patch("workbench.directives.get_diff_since", return_value=""):
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = ReviewerFollowupDirective(
            prior_review_sha="abc123",
            prior_feedback="Missing null check in foo().",
        ).render(ctx)
    assert "## Prior review feedback" in prompt
    assert "Missing null check in foo()." in prompt
    assert "Verify each item in the prior review feedback" in prompt


def test_reviewer_first_pass_ignores_prior_sha(sample_task, sample_worktree):
    """Without prior_review_sha, ReviewerDirective behaves as the comprehensive first pass."""
    with (
        patch("workbench.directives.get_diff", return_value="- full diff") as mock_full,
        patch("workbench.directives.get_diff_since") as mock_since,
    ):
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = ReviewerDirective().render(ctx)

    mock_full.assert_called_once_with(sample_worktree, "main")
    mock_since.assert_not_called()
    assert "## Diff to review" in prompt
    assert "## Changes since prior review" not in prompt
    assert "## Prior review feedback" not in prompt


def test_reviewer_followup_action_line(sample_task, sample_worktree):
    """Follow-up review swaps the default action line for the verification instruction."""
    with patch("workbench.directives.get_diff_since", return_value=""):
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = ReviewerFollowupDirective(
            prior_review_sha="abc123",
            prior_feedback="prior feedback",
        ).render(ctx)
    assert "Provide your review." not in prompt
    assert "Do not raise new issues." in prompt


# ── Planner prompt ───────────────────────────────────────────────────


def test_planner_prompt_contains_directive():
    prompt = PlannerDirective(
        output_path=Path("/repo/plans/auth.md"),
        user_prompt="Add auth",
        plan_guide=_load_plan_guide(),
    ).render()
    assert PlannerDirective.DEFAULT_TEXT in prompt


def test_planner_prompt_contains_user_request():
    prompt = PlannerDirective(
        output_path=Path("/repo/plan.md"),
        user_prompt="Add JWT authentication",
        plan_guide=_load_plan_guide(),
    ).render()
    assert "## User Request" in prompt
    assert "Add JWT authentication" in prompt


def test_planner_prompt_contains_output_path():
    output = Path("/repo/.workbench/plans/auth.md")
    prompt = PlannerDirective(
        output_path=output,
        user_prompt="Add auth",
        plan_guide=_load_plan_guide(),
    ).render()
    assert str(output) in prompt


def test_planner_prompt_contains_plan_guide():
    prompt = PlannerDirective(
        output_path=Path("/repo/plan.md"),
        user_prompt="Add auth",
        plan_guide=_load_plan_guide(),
    ).render()
    assert "## Plan Writing Guide" in prompt
    assert "## Plan Format" in prompt
    assert "## Task:" in prompt


def test_planner_prompt_from_source_document():
    """Source content is rendered as a Source Document section."""
    prompt = PlannerDirective(
        output_path=Path("/repo/plan.md"),
        source_content="# Claude Plan\n\n## Step 1\nDo something\n",
        plan_guide=_load_plan_guide(),
    ).render()
    assert "## Source Document" in prompt
    assert "Claude Plan" in prompt
    assert "Transform it into a workbench plan" in prompt
    assert "## User Request" not in prompt


def test_planner_prompt_source_with_guidance():
    """Both source and prompt: prompt becomes Additional Guidance."""
    prompt = PlannerDirective(
        output_path=Path("/repo/plan.md"),
        user_prompt="Focus on security",
        source_content="# Spec\nBuild a widget.\n",
        plan_guide=_load_plan_guide(),
    ).render()
    assert "## Source Document" in prompt
    assert "Build a widget" in prompt
    assert "## Additional Guidance" in prompt
    assert "Focus on security" in prompt
    assert "## User Request" not in prompt


# ── run_planner tests ────────────────────────────────────────────────


def test_run_planner_success(tmp_path):
    """run_planner spawns agent and returns result on success."""
    mock_adapter = MagicMock()
    mock_adapter.build_command.return_value = ["echo", "ok"]
    mock_adapter.parse_output.return_value = ("Plan generated.", {})

    result = asyncio.run(
        run_planner(
            repo=tmp_path,
            user_prompt="Add auth",
            plan_name="test-plan",
            use_tmux=False,
            adapter=mock_adapter,
        )
    )

    assert result.status == TaskStatus.DONE
    assert result.task_id == "planner-test-plan"
    assert (tmp_path / ".workbench" / "plans").is_dir()
    mock_adapter.build_command.assert_called_once()


def test_run_planner_failure(tmp_path):
    """run_planner returns FAILED when the subprocess exits non-zero."""
    mock_adapter = MagicMock()
    mock_adapter.build_command.return_value = ["false"]
    mock_adapter.parse_output.return_value = ("", {})

    result = asyncio.run(
        run_planner(
            repo=tmp_path,
            user_prompt="Add auth",
            plan_name="fail-plan",
            use_tmux=False,
            adapter=mock_adapter,
        )
    )

    assert result.status == TaskStatus.FAILED


def test_run_planner_exception(tmp_path):
    """run_planner returns FAILED when adapter raises."""
    mock_adapter = MagicMock()
    mock_adapter.build_command.side_effect = RuntimeError("boom")

    result = asyncio.run(
        run_planner(
            repo=tmp_path,
            user_prompt="Add auth",
            plan_name="err-plan",
            use_tmux=False,
            adapter=mock_adapter,
        )
    )

    assert result.status == TaskStatus.FAILED
    assert "boom" in result.output


def test_run_planner_with_source_content(tmp_path):
    """run_planner passes source_content through to the prompt."""
    captured_prompt = {}

    mock_adapter = MagicMock()

    def capture_command(prompt, cwd):
        captured_prompt["prompt"] = prompt
        return ["echo", "ok"]

    mock_adapter.build_command.side_effect = capture_command
    mock_adapter.parse_output.return_value = ("ok", {})

    asyncio.run(
        run_planner(
            repo=tmp_path,
            user_prompt="Focus on security",
            source_content="# Existing Plan\nDo stuff.",
            plan_name="src-plan",
            use_tmux=False,
            adapter=mock_adapter,
        )
    )

    assert "Existing Plan" in captured_prompt["prompt"]
    assert "Focus on security" in captured_prompt["prompt"]
