"""Tests for directive classes — typed prompt builders for each agent role."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from workbench.agents import Role
from workbench.directives import (
    Directive,
    FixerDirective,
    ImplementorDirective,
    MergerDirective,
    PipelineDirective,
    PlannerDirective,
    PromptContext,
    ReviewerDirective,
    ReviewerFollowupDirective,
    TddImplementorDirective,
    TddTesterDirective,
    TesterDirective,
)
from workbench.plan_parser import Task
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
def sample_task_no_files() -> Task:
    return Task(
        id="task-2",
        title="Fix bug",
        description="Fix the bug in the system.",
    )


@pytest.fixture
def sample_worktree(tmp_path: Path) -> Worktree:
    return Worktree(path=tmp_path, branch="wb/task-1-add-widget", task_id="task-1")


@pytest.fixture
def sample_ctx(sample_task, sample_worktree) -> PromptContext:
    return PromptContext(
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
    )


@pytest.fixture
def sample_ctx_with_plan(sample_task, sample_worktree) -> PromptContext:
    return PromptContext(
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
        plan_context="Python 3.12 with asyncio.",
        plan_conventions="Use type hints everywhere.",
    )


# ── DEFAULT_TEXT presence and content ─────────────────────────────────


class TestDefaultTextPresence:
    def test_implementor_default_text(self):
        assert ImplementorDirective.DEFAULT_TEXT
        assert "implementation agent" in ImplementorDirective.DEFAULT_TEXT

    def test_tester_default_text(self):
        assert TesterDirective.DEFAULT_TEXT
        assert "VERDICT: PASS" in TesterDirective.DEFAULT_TEXT

    def test_reviewer_default_text(self):
        assert ReviewerDirective.DEFAULT_TEXT
        assert "code review agent" in ReviewerDirective.DEFAULT_TEXT

    def test_fixer_default_text(self):
        assert FixerDirective.DEFAULT_TEXT
        assert "fix agent" in FixerDirective.DEFAULT_TEXT

    def test_merger_default_text(self):
        assert MergerDirective.DEFAULT_TEXT
        assert "merge conflict" in MergerDirective.DEFAULT_TEXT

    def test_reviewer_followup_default_text(self):
        assert ReviewerFollowupDirective.DEFAULT_TEXT
        assert "follow-up code review" in ReviewerFollowupDirective.DEFAULT_TEXT

    def test_tdd_tester_default_text(self):
        assert TddTesterDirective.DEFAULT_TEXT
        assert "test-driven development" in TddTesterDirective.DEFAULT_TEXT

    def test_tdd_implementor_default_text(self):
        assert TddImplementorDirective.DEFAULT_TEXT
        assert "test-driven development" in TddImplementorDirective.DEFAULT_TEXT

    def test_planner_default_text(self):
        assert PlannerDirective.DEFAULT_TEXT
        assert "planning agent" in PlannerDirective.DEFAULT_TEXT


# ── resolved_text() precedence ────────────────────────────────────────


class TestResolvedText:
    def test_returns_default_when_no_override(self):
        d = ImplementorDirective()
        assert d.resolved_text() == ImplementorDirective.DEFAULT_TEXT

    def test_returns_override_when_set(self):
        d = ImplementorDirective(directive_text="custom directive")
        assert d.resolved_text() == "custom directive"

    def test_empty_string_falls_back_to_default(self):
        d = TesterDirective(directive_text="")
        assert d.resolved_text() == TesterDirective.DEFAULT_TEXT


# ── ImplementorDirective render ───────────────────────────────────────


class TestImplementorDirectiveRender:
    def test_contains_directive(self, sample_ctx):
        prompt = ImplementorDirective().render(sample_ctx)
        assert ImplementorDirective.DEFAULT_TEXT in prompt

    def test_contains_branch_pinning(self, sample_ctx):
        prompt = ImplementorDirective().render(sample_ctx)
        assert "wb/task-1-add-widget" in prompt
        assert "Stay on this branch" in prompt

    def test_contains_task(self, sample_ctx):
        prompt = ImplementorDirective().render(sample_ctx)
        assert "## Task: Add widget" in prompt
        assert "Implement the widget module." in prompt

    def test_contains_files(self, sample_ctx):
        prompt = ImplementorDirective().render(sample_ctx)
        assert "src/widget.py" in prompt

    def test_contains_action_line(self, sample_ctx):
        prompt = ImplementorDirective().render(sample_ctx)
        assert "Implement this task and commit your changes." in prompt

    def test_no_diff(self, sample_ctx):
        prompt = ImplementorDirective().render(sample_ctx)
        assert "```diff" not in prompt

    def test_includes_plan_context(self, sample_ctx_with_plan):
        prompt = ImplementorDirective().render(sample_ctx_with_plan)
        assert "## Context" in prompt
        assert "Python 3.12 with asyncio." in prompt

    def test_includes_plan_conventions(self, sample_ctx_with_plan):
        prompt = ImplementorDirective().render(sample_ctx_with_plan)
        assert "## Conventions" in prompt
        assert "Use type hints everywhere." in prompt


# ── TesterDirective render ────────────────────────────────────────────


class TestTesterDirectiveRender:
    def test_contains_directive(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value="+ added line"):
            prompt = TesterDirective().render(sample_ctx)
        assert TesterDirective.DEFAULT_TEXT in prompt

    def test_contains_branch_pinning(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            prompt = TesterDirective().render(sample_ctx)
        assert "Stay on this branch" in prompt

    def test_contains_task(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            prompt = TesterDirective().render(sample_ctx)
        assert "## Task: Add widget" in prompt

    def test_contains_diff_with_label(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value="+ added line") as mock_diff:
            prompt = TesterDirective().render(sample_ctx)
        mock_diff.assert_called_once_with(sample_ctx.worktree, "main")
        assert "## Changes made" in prompt
        assert "```diff\n+ added line\n```" in prompt

    def test_contains_action_line(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            prompt = TesterDirective().render(sample_ctx)
        assert "Run tests and verify the implementation." in prompt


# ── ReviewerDirective render ──────────────────────────────────────────


class TestReviewerDirectiveRender:
    def test_contains_directive(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            prompt = ReviewerDirective().render(sample_ctx)
        assert ReviewerDirective.DEFAULT_TEXT in prompt

    def test_no_branch_pinning(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            prompt = ReviewerDirective().render(sample_ctx)
        assert "Stay on this branch" not in prompt

    def test_contains_diff_with_label(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value="- removed line") as mock_diff:
            prompt = ReviewerDirective().render(sample_ctx)
        mock_diff.assert_called_once_with(sample_ctx.worktree, "main")
        assert "## Diff to review" in prompt
        assert "- removed line" in prompt

    def test_contains_action_line(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            prompt = ReviewerDirective().render(sample_ctx)
        assert "Provide your review." in prompt


# ── ReviewerFollowupDirective render ──────────────────────────────────


class TestReviewerFollowupDirectiveRender:
    def test_contains_followup_directive(self, sample_ctx):
        with patch("workbench.directives.get_diff_since", return_value=""):
            d = ReviewerFollowupDirective(
                prior_review_sha="abc123",
                prior_feedback="Missing null check in foo().",
            )
            prompt = d.render(sample_ctx)
        assert ReviewerFollowupDirective.DEFAULT_TEXT in prompt

    def test_contains_task(self, sample_ctx):
        with patch("workbench.directives.get_diff_since", return_value=""):
            d = ReviewerFollowupDirective(
                prior_review_sha="abc123",
                prior_feedback="feedback",
            )
            prompt = d.render(sample_ctx)
        assert "## Task: Add widget" in prompt

    def test_contains_prior_review_feedback_section(self, sample_ctx):
        with patch("workbench.directives.get_diff_since", return_value=""):
            d = ReviewerFollowupDirective(
                prior_review_sha="abc123",
                prior_feedback="Missing null check in foo().",
            )
            prompt = d.render(sample_ctx)
        assert "## Prior review feedback" in prompt
        assert "Missing null check in foo()." in prompt

    def test_uses_diff_since(self, sample_ctx):
        with patch(
            "workbench.directives.get_diff_since", return_value="+ fixer line"
        ) as mock_since:
            d = ReviewerFollowupDirective(
                prior_review_sha="abc123",
                prior_feedback="feedback",
            )
            prompt = d.render(sample_ctx)
        mock_since.assert_called_once_with(sample_ctx.worktree, "abc123")
        assert "## Changes since prior review" in prompt
        assert "+ fixer line" in prompt

    def test_contains_followup_action_line(self, sample_ctx):
        with patch("workbench.directives.get_diff_since", return_value=""):
            d = ReviewerFollowupDirective(
                prior_review_sha="abc123",
                prior_feedback="feedback",
            )
            prompt = d.render(sample_ctx)
        assert "Verify each item in the prior review feedback" in prompt
        assert "Do not raise new issues." in prompt
        assert "Provide your review." not in prompt

    def test_no_branch_pinning(self, sample_ctx):
        with patch("workbench.directives.get_diff_since", return_value=""):
            d = ReviewerFollowupDirective(
                prior_review_sha="abc123",
                prior_feedback="feedback",
            )
            prompt = d.render(sample_ctx)
        assert "Stay on this branch" not in prompt


# ── FixerDirective render ─────────────────────────────────────────────


class TestFixerDirectiveRender:
    def test_contains_directive(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            d = FixerDirective(feedback="Tests failed", failure_kind="test", attempt=1)
            prompt = d.render(sample_ctx)
        assert FixerDirective.DEFAULT_TEXT in prompt

    def test_contains_branch_pinning(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            d = FixerDirective(feedback="Tests failed", failure_kind="test", attempt=1)
            prompt = d.render(sample_ctx)
        assert "Stay on this branch" in prompt

    def test_contains_task(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            d = FixerDirective(feedback="Tests failed", failure_kind="test", attempt=1)
            prompt = d.render(sample_ctx)
        assert "## Task: Add widget" in prompt

    def test_contains_diff_with_label(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value="diff content") as mock_diff:
            d = FixerDirective(feedback="Tests failed", failure_kind="test", attempt=1)
            prompt = d.render(sample_ctx)
        mock_diff.assert_called_once_with(sample_ctx.worktree, "main")
        assert "## Current changes" in prompt
        assert "diff content" in prompt

    def test_contains_feedback_section_test_failure(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            d = FixerDirective(feedback="missing return value", failure_kind="test", attempt=2)
            prompt = d.render(sample_ctx)
        assert "## Feedback to address" in prompt
        assert "[Test failure, attempt 2]" in prompt
        assert "missing return value" in prompt

    def test_contains_feedback_section_review_failure(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            d = FixerDirective(feedback="code quality issues", failure_kind="review", attempt=1)
            prompt = d.render(sample_ctx)
        assert "[Review failure, attempt 1]" in prompt
        assert "code quality issues" in prompt

    def test_contains_action_line(self, sample_ctx):
        with patch("workbench.directives.get_diff", return_value=""):
            d = FixerDirective(feedback="feedback", failure_kind="test", attempt=1)
            prompt = d.render(sample_ctx)
        assert "Fix the issues described above and commit your changes." in prompt


# ── TddTesterDirective render ────────────────────────────────────────


class TestTddTesterDirectiveRender:
    def test_contains_tdd_tester_directive(self, sample_ctx):
        prompt = TddTesterDirective().render(sample_ctx)
        assert TddTesterDirective.DEFAULT_TEXT in prompt

    def test_contains_branch_pinning(self, sample_ctx):
        prompt = TddTesterDirective().render(sample_ctx)
        assert "Stay on this branch" in prompt

    def test_contains_task(self, sample_ctx):
        prompt = TddTesterDirective().render(sample_ctx)
        assert "## Task: Add widget" in prompt

    def test_contains_action_line(self, sample_ctx):
        prompt = TddTesterDirective().render(sample_ctx)
        assert "Run tests and verify the implementation." in prompt

    def test_no_diff(self, sample_ctx):
        prompt = TddTesterDirective().render(sample_ctx)
        assert "```diff" not in prompt


# ── TddImplementorDirective render ───────────────────────────────────


class TestTddImplementorDirectiveRender:
    def test_contains_tdd_implementor_directive(self, sample_ctx):
        prompt = TddImplementorDirective().render(sample_ctx)
        assert TddImplementorDirective.DEFAULT_TEXT in prompt

    def test_contains_branch_pinning(self, sample_ctx):
        prompt = TddImplementorDirective().render(sample_ctx)
        assert "Stay on this branch" in prompt

    def test_contains_task(self, sample_ctx):
        prompt = TddImplementorDirective().render(sample_ctx)
        assert "## Task: Add widget" in prompt

    def test_contains_action_line(self, sample_ctx):
        prompt = TddImplementorDirective().render(sample_ctx)
        assert "Implement this task and commit your changes." in prompt

    def test_no_diff(self, sample_ctx):
        prompt = TddImplementorDirective().render(sample_ctx)
        assert "```diff" not in prompt


# ── MergerDirective render ────────────────────────────────────────────


class TestMergerDirectiveRender:
    def test_contains_merger_directive(self):
        d = MergerDirective(
            task_branch="feature/auth",
            session_branch="wb/session-1",
            conflicts=["src/auth.py", "src/config.py"],
        )
        prompt = d.render()
        assert MergerDirective.DEFAULT_TEXT in prompt

    def test_contains_branch_names(self):
        d = MergerDirective(
            task_branch="feature/auth",
            session_branch="wb/session-1",
            conflicts=["src/auth.py"],
        )
        prompt = d.render()
        assert "Merging branch 'feature/auth' into 'wb/session-1'" in prompt

    def test_contains_conflict_list(self):
        d = MergerDirective(
            task_branch="feature/auth",
            session_branch="wb/session-1",
            conflicts=["src/auth.py", "src/config.py"],
        )
        prompt = d.render()
        assert "  - src/auth.py" in prompt
        assert "  - src/config.py" in prompt

    def test_contains_action_line(self):
        d = MergerDirective(
            task_branch="feature/auth",
            session_branch="wb/session-1",
            conflicts=["src/auth.py"],
        )
        prompt = d.render()
        assert "Read each file, resolve the conflict markers, and stage with git add." in prompt

    def test_custom_directive_text(self):
        d = MergerDirective(
            directive_text="Custom merger instructions.",
            task_branch="feature/auth",
            session_branch="wb/session-1",
            conflicts=["src/auth.py"],
        )
        prompt = d.render()
        assert "Custom merger instructions." in prompt
        assert MergerDirective.DEFAULT_TEXT not in prompt


# ── PlannerDirective render ───────────────────────────────────────────


class TestPlannerDirectiveRender:
    def test_contains_planner_directive(self):
        d = PlannerDirective(output_path=Path("/repo/plan.md"), plan_guide="guide text")
        prompt = d.render()
        assert PlannerDirective.DEFAULT_TEXT in prompt

    def test_contains_plan_guide(self):
        d = PlannerDirective(output_path=Path("/repo/plan.md"), plan_guide="## Plan Format")
        prompt = d.render()
        assert "## Plan Writing Guide" in prompt
        assert "## Plan Format" in prompt

    def test_contains_output_path(self):
        d = PlannerDirective(output_path=Path("/repo/.workbench/plans/auth.md"), plan_guide="")
        prompt = d.render()
        assert "/repo/.workbench/plans/auth.md" in prompt

    def test_user_request_label_without_source(self):
        d = PlannerDirective(
            output_path=Path("/repo/plan.md"),
            user_prompt="Add JWT authentication",
            plan_guide="",
        )
        prompt = d.render()
        assert "## User Request" in prompt
        assert "Add JWT authentication" in prompt

    def test_source_content_section(self):
        d = PlannerDirective(
            output_path=Path("/repo/plan.md"),
            source_content="# Claude Plan\n\n## Step 1\nDo something\n",
            plan_guide="",
        )
        prompt = d.render()
        assert "## Source Document" in prompt
        assert "Claude Plan" in prompt
        assert "Transform it into a workbench plan" in prompt

    def test_additional_guidance_label_with_source(self):
        d = PlannerDirective(
            output_path=Path("/repo/plan.md"),
            user_prompt="Focus on security",
            source_content="# Spec\nBuild a widget.\n",
            plan_guide="",
        )
        prompt = d.render()
        assert "## Additional Guidance" in prompt
        assert "Focus on security" in prompt
        assert "## User Request" not in prompt

    def test_no_user_prompt_section_when_empty(self):
        d = PlannerDirective(output_path=Path("/repo/plan.md"), plan_guide="")
        prompt = d.render()
        assert "## User Request" not in prompt
        assert "## Additional Guidance" not in prompt

    def test_explore_codebase_instruction(self):
        d = PlannerDirective(output_path=Path("/repo/plan.md"), plan_guide="")
        prompt = d.render()
        assert "Explore the codebase thoroughly before writing." in prompt


# ── Required field enforcement ────────────────────────────────────────


class TestRequiredFields:
    def test_fixer_requires_feedback(self):
        with pytest.raises(TypeError):
            FixerDirective(failure_kind="test", attempt=1)

    def test_fixer_requires_failure_kind(self):
        with pytest.raises(TypeError):
            FixerDirective(feedback="error", attempt=1)

    def test_fixer_requires_attempt(self):
        with pytest.raises(TypeError):
            FixerDirective(feedback="error", failure_kind="test")

    def test_reviewer_followup_requires_prior_review_sha(self):
        with pytest.raises(TypeError):
            ReviewerFollowupDirective(prior_feedback="feedback")

    def test_reviewer_followup_requires_prior_feedback(self):
        with pytest.raises(TypeError):
            ReviewerFollowupDirective(prior_review_sha="abc123")

    def test_merger_requires_task_branch(self):
        with pytest.raises(TypeError):
            MergerDirective(session_branch="main", conflicts=["a.py"])

    def test_merger_requires_session_branch(self):
        with pytest.raises(TypeError):
            MergerDirective(task_branch="feature", conflicts=["a.py"])

    def test_merger_requires_conflicts(self):
        with pytest.raises(TypeError):
            MergerDirective(task_branch="feature", session_branch="main")

    def test_planner_requires_output_path(self):
        with pytest.raises(TypeError):
            PlannerDirective()


# ── Section ordering ──────────────────────────────────────────────────


class TestSectionOrdering:
    """Verify prompt sections appear in the correct order."""

    def test_implementor_section_order(self, sample_ctx_with_plan):
        prompt = ImplementorDirective().render(sample_ctx_with_plan)
        directive_pos = prompt.index("implementation agent")
        branch_pos = prompt.index("Stay on this branch")
        context_pos = prompt.index("## Context")
        conventions_pos = prompt.index("## Conventions")
        task_pos = prompt.index("## Task:")
        action_pos = prompt.index("Implement this task")
        assert directive_pos < branch_pos < context_pos < conventions_pos < task_pos < action_pos

    def test_fixer_section_order(self, sample_ctx_with_plan):
        with patch("workbench.directives.get_diff", return_value="diff"):
            d = FixerDirective(feedback="fix me", failure_kind="test", attempt=1)
            prompt = d.render(sample_ctx_with_plan)
        directive_pos = prompt.index("fix agent")
        branch_pos = prompt.index("Stay on this branch")
        task_pos = prompt.index("## Task:")
        diff_pos = prompt.index("## Current changes")
        feedback_pos = prompt.index("## Feedback to address")
        action_pos = prompt.index("Fix the issues")
        assert directive_pos < branch_pos < task_pos < diff_pos < feedback_pos < action_pos

    def test_reviewer_followup_section_order(self, sample_ctx):
        with patch("workbench.directives.get_diff_since", return_value="diff"):
            d = ReviewerFollowupDirective(
                prior_review_sha="abc123",
                prior_feedback="fix null check",
            )
            prompt = d.render(sample_ctx)
        directive_pos = prompt.index("follow-up code review")
        task_pos = prompt.index("## Task:")
        feedback_pos = prompt.index("## Prior review feedback")
        diff_pos = prompt.index("## Changes since prior review")
        action_pos = prompt.index("Verify each item")
        assert directive_pos < task_pos < feedback_pos < diff_pos < action_pos
