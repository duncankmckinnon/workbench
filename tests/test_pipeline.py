"""Tests for the TDD pipeline execution path in run_pipeline()."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from workbench.agents import (
    DEFAULT_DIRECTIVES,
    REVIEWER_FOLLOWUP_DIRECTIVE,
    TDD_DIRECTIVES,
    AgentResult,
    Role,
    TaskStatus,
    run_pipeline,
)
from workbench.directives import (
    FixerDirective,
    ImplementorDirective,
    PipelineDirective,
    PromptContext,
    ReviewerDirective,
    ReviewerFollowupDirective,
    TddImplementorDirective,
    TddTesterDirective,
    TesterDirective,
)
from workbench.plan_parser import Task
from workbench.profile import ModeConfig, Profile, RoleConfig
from workbench.worktree import Worktree


@pytest.fixture
def sample_task():
    return Task(id="task-1", title="Add feature", description="Implement X", files=["src/x.py"])


@pytest.fixture
def sample_worktree(tmp_path):
    return Worktree(path=tmp_path, branch="wb/task-1", task_id="task-1")


def _make_result(role: Role, status: TaskStatus, output: str) -> AgentResult:
    return AgentResult(task_id="task-1", role=role, status=status, output=output)


def _pass_result(role: Role) -> AgentResult:
    return _make_result(role, TaskStatus.DONE, "All good.\nVERDICT: PASS")


def _fail_verdict_result(role: Role) -> AgentResult:
    return _make_result(role, TaskStatus.DONE, "Something wrong.\nVERDICT: FAIL")


def _crash_result(role: Role) -> AgentResult:
    return _make_result(role, TaskStatus.FAILED, "Agent error: crash")


def _done_result(role: Role) -> AgentResult:
    return _make_result(role, TaskStatus.DONE, "Done implementing.")


class TestTDDPipelineFullPass:
    def test_pipeline_tdd_full_pass(self, sample_task, sample_worktree, tmp_path):
        """TDD: tester writes tests (DONE), implementor implements (PASS verdict),
        test verification passes, review passes.
        Verify roles are: TESTER, IMPLEMENTOR, TESTER, REVIEWER."""

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            if isinstance(directive, TddTesterDirective):
                return _done_result(role)
            if isinstance(directive, TddImplementorDirective):
                return _pass_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                )
            )

        assert len(results) == 4
        assert [r.role for r in results] == [
            Role.TESTER,
            Role.IMPLEMENTOR,
            Role.TESTER,
            Role.REVIEWER,
        ]


class TestTDDTestWriteFails:
    def test_pipeline_tdd_test_write_fails(self, sample_task, sample_worktree, tmp_path):
        """TDD tester crashes -> pipeline stops after 1 result."""

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            return _crash_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                )
            )

        assert len(results) == 1
        assert results[0].role == Role.TESTER
        assert results[0].status == TaskStatus.FAILED


class TestTDDImplVerdictFail:
    def test_pipeline_tdd_impl_verdict_fail(self, sample_task, sample_worktree, tmp_path):
        """TDD implementor returns VERDICT: FAIL (tests fail or not comprehensive) -> pipeline stops."""

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            if isinstance(directive, TddTesterDirective):
                return _done_result(directive.role)
            return _fail_verdict_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                )
            )

        assert len(results) == 2
        assert results[0].role == Role.TESTER
        assert results[1].role == Role.IMPLEMENTOR
        assert not results[1].passed


class TestTDDImplFails:
    def test_pipeline_tdd_impl_fails(self, sample_task, sample_worktree, tmp_path):
        """TDD implementation crashes -> pipeline stops after 2 results."""

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            if isinstance(directive, TddTesterDirective):
                return _done_result(directive.role)
            return _crash_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                )
            )

        assert len(results) == 2
        assert results[0].role == Role.TESTER
        assert results[1].role == Role.IMPLEMENTOR
        assert results[1].status == TaskStatus.FAILED


class TestTDDDirectives:
    def test_pipeline_tdd_uses_tdd_directives(self, sample_task, sample_worktree, tmp_path):
        """Verify TDD directive DEFAULT_TEXT is used when no override provided."""
        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            if isinstance(directive, TddTesterDirective):
                return _done_result(directive.role)
            return _pass_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                )
            )

        # First call: TDD tester directive uses its DEFAULT_TEXT
        assert isinstance(captured[0], TddTesterDirective)
        assert captured[0].resolved_text() == TddTesterDirective.DEFAULT_TEXT
        # Second call: TDD implementor directive uses its DEFAULT_TEXT
        assert isinstance(captured[1], TddImplementorDirective)
        assert captured[1].resolved_text() == TddImplementorDirective.DEFAULT_TEXT

    def test_pipeline_tdd_directive_override(self, sample_task, sample_worktree, tmp_path):
        """Custom directives dict overrides TDD defaults."""
        custom_tester = "Custom tester directive"
        custom_impl = "Custom implementor directive"
        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            if isinstance(directive, TddTesterDirective):
                return _done_result(directive.role)
            return _pass_result(directive.role)

        custom_directives = {
            Role.TESTER: custom_tester,
            Role.IMPLEMENTOR: custom_impl,
        }

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                    directives=custom_directives,
                )
            )

        assert captured[0].resolved_text() == custom_tester
        assert captured[1].resolved_text() == custom_impl


class TestTDDVerificationFailsThenFixes:
    def test_pipeline_tdd_verification_fails_then_fixes(
        self, sample_task, sample_worktree, tmp_path
    ):
        """After TDD impl, verification test fails, fixer runs, test passes on retry."""
        call_count: dict[str, int] = {}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            key = type(directive).__name__
            call_count[key] = call_count.get(key, 0) + 1
            if isinstance(directive, TddTesterDirective):
                return _done_result(role)
            if isinstance(directive, TddImplementorDirective):
                return _pass_result(role)
            if isinstance(directive, TesterDirective):
                tester_n = call_count.get("TesterDirective", 0)
                if tester_n == 1:
                    return _fail_verdict_result(role)
                return _pass_result(role)
            if isinstance(directive, FixerDirective):
                return _done_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                )
            )

        assert len(results) == 6
        assert [r.role for r in results] == [
            Role.TESTER,
            Role.IMPLEMENTOR,
            Role.TESTER,
            Role.FIXER,
            Role.TESTER,
            Role.REVIEWER,
        ]


# ---------------------------------------------------------------------------
# Profile integration tests
# ---------------------------------------------------------------------------


class TestPipelineUsesProfileAgent:
    def test_pipeline_uses_profile_agent(self, sample_task, sample_worktree, tmp_path):
        """Profile with reviewer.agent='gemini' should pass that agent to run_agent for the reviewer role."""
        profile = Profile.default()
        profile.reviewer.agent = "gemini"

        captured_calls: list[dict] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            captured_calls.append({"role": role, "agent_cmd": kwargs.get("agent_cmd"), **kwargs})
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                )
            )

        # Find the reviewer call
        reviewer_calls = [c for c in captured_calls if c["role"] == Role.REVIEWER]
        assert len(reviewer_calls) == 1
        # The reviewer should use the profile's agent
        assert reviewer_calls[0]["agent_cmd"] == "gemini"

        # Other roles should still use default "claude"
        impl_calls = [c for c in captured_calls if c["role"] == Role.IMPLEMENTOR]
        assert len(impl_calls) == 1
        assert impl_calls[0]["agent_cmd"] == "claude"


class TestPipelineUsesProfileDirective:
    def test_pipeline_uses_profile_directive(self, sample_task, sample_worktree, tmp_path):
        """Profile with custom tester directive should pass it through via directive_text."""
        profile = Profile.default()
        custom_directive = "Custom tester directive from profile"
        profile.tester.directive = custom_directive

        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            return _pass_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                )
            )

        # The tester call should use the profile's directive
        tester_directives = [d for d in captured if isinstance(d, TesterDirective)]
        assert len(tester_directives) == 1
        assert tester_directives[0].directive_text == custom_directive


class TestPipelineCLIDirectiveOverridesProfile:
    def test_pipeline_cli_directive_overrides_profile(
        self, sample_task, sample_worktree, tmp_path
    ):
        """CLI directive takes priority over profile directive for the same role."""
        profile = Profile.default()
        profile.tester.directive = "Profile tester directive"
        cli_tester_directive = "CLI tester directive wins"

        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            return _pass_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                    directives={Role.TESTER: cli_tester_directive},
                )
            )

        tester_directives = [d for d in captured if isinstance(d, TesterDirective)]
        assert len(tester_directives) == 1
        # CLI directive should win over profile
        assert tester_directives[0].directive_text == cli_tester_directive


class TestPipelineProfileNoneUsesDefaults:
    def test_pipeline_profile_none_uses_defaults(self, sample_task, sample_worktree, tmp_path):
        """profile=None should behave the same as before — default directives, default agent."""
        captured: list[tuple[PipelineDirective, dict]] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append((directive, kwargs))
            return _pass_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=None,
                )
            )

        # All calls should use default agent_cmd
        for _, kw in captured:
            assert kw.get("agent_cmd", "claude") == "claude"

        # Directives should use empty directive_text (falling back to DEFAULT_TEXT)
        impl_directives = [d for d, _ in captured if isinstance(d, ImplementorDirective)]
        assert len(impl_directives) == 1
        assert impl_directives[0].directive_text == ""
        assert impl_directives[0].resolved_text() == ImplementorDirective.DEFAULT_TEXT


class TestPipelineProfileFixerAgent:
    def test_pipeline_profile_fixer_uses_profile_agent_on_retry(
        self, sample_task, sample_worktree, tmp_path
    ):
        """When test fails and fixer runs, fixer should use its profile agent."""
        profile = Profile.default()
        profile.fixer.agent = "codex"

        captured_calls: list[dict] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            captured_calls.append({"role": role, "agent_cmd": kwargs.get("agent_cmd"), **kwargs})
            if isinstance(directive, ImplementorDirective):
                return _pass_result(role)
            if (
                isinstance(directive, TesterDirective)
                and len([c for c in captured_calls if c["role"] == Role.TESTER]) == 1
            ):
                return _fail_verdict_result(role)  # first test fails
            if isinstance(directive, FixerDirective):
                return _done_result(role)
            return _pass_result(role)  # second test + reviewer pass

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                )
            )

        fixer_calls = [c for c in captured_calls if c["role"] == Role.FIXER]
        assert len(fixer_calls) == 1
        assert fixer_calls[0]["agent_cmd"] == "codex"


class TestPipelineExplicitAgentCmdOverridesProfile:
    def test_explicit_agent_cmd_overrides_profile(self, sample_task, sample_worktree, tmp_path):
        """When agent_cmd != 'claude' (explicit override), it should win over profile."""
        profile = Profile.default()
        profile.implementor.agent = "gemini"

        captured_calls: list[dict] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            captured_calls.append({"role": role, "agent_cmd": kwargs.get("agent_cmd"), **kwargs})
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                    agent_cmd="codex",  # explicit CLI override
                )
            )

        # All roles should use "codex" because explicit agent_cmd overrides profile
        for c in captured_calls:
            assert c["agent_cmd"] == "codex"


class TestPipelineProfileMultipleRolesCustomized:
    def test_multiple_roles_customized(self, sample_task, sample_worktree, tmp_path):
        """Profile with different agents for different roles should dispatch correctly."""
        profile = Profile.default()
        profile.implementor.agent = "gemini"
        profile.tester.agent = "codex"
        profile.reviewer.agent = "gemini"

        captured_calls: list[dict] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            captured_calls.append({"role": role, "agent_cmd": kwargs.get("agent_cmd"), **kwargs})
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                )
            )

        impl_calls = [c for c in captured_calls if c["role"] == Role.IMPLEMENTOR]
        tester_calls = [c for c in captured_calls if c["role"] == Role.TESTER]
        reviewer_calls = [c for c in captured_calls if c["role"] == Role.REVIEWER]

        assert impl_calls[0]["agent_cmd"] == "gemini"
        assert tester_calls[0]["agent_cmd"] == "codex"
        assert reviewer_calls[0]["agent_cmd"] == "gemini"


class TestRunAgentDirective:
    """Test run_agent with the new directive-based signature."""

    def test_run_agent_custom_directive_text(self, sample_task, sample_worktree, tmp_path):
        """Custom directive_text is rendered in the prompt."""
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        directive = ImplementorDirective(
            directive_text="Custom implementor directive",
        )

        with patch("workbench.agents.get_adapter") as mock_adapter:
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("VERDICT: PASS", {})
            mock_adapter.return_value = mock_adapter_instance

            from workbench.agents import run_agent

            result = asyncio.run(run_agent(directive, ctx, tmp_path, use_tmux=False))

        prompt_arg = mock_adapter_instance.build_command.call_args[0][0]
        assert "Custom implementor directive" in prompt_arg

    def test_run_agent_default_directive_text(self, sample_task, sample_worktree, tmp_path):
        """Empty directive_text falls back to DEFAULT_TEXT."""
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        directive = ImplementorDirective()

        with patch("workbench.agents.get_adapter") as mock_adapter:
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("VERDICT: PASS", {})
            mock_adapter.return_value = mock_adapter_instance

            from workbench.agents import run_agent

            result = asyncio.run(run_agent(directive, ctx, tmp_path, use_tmux=False))

        prompt_arg = mock_adapter_instance.build_command.call_args[0][0]
        assert ImplementorDirective.DEFAULT_TEXT in prompt_arg

    def test_run_agent_uses_directive_role(self, sample_task, sample_worktree, tmp_path):
        """AgentResult.role is taken from the directive."""
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        directive = TesterDirective()

        with (
            patch("workbench.agents.get_adapter") as mock_adapter,
            patch("workbench.directives.get_diff", return_value="some diff"),
        ):
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("VERDICT: PASS", {})
            mock_adapter.return_value = mock_adapter_instance

            from workbench.agents import run_agent

            result = asyncio.run(run_agent(directive, ctx, tmp_path, use_tmux=False))

        assert result.role == Role.TESTER


# ---------------------------------------------------------------------------
# TDD + Profile integration tests
# ---------------------------------------------------------------------------


class TestReviewerFollowupContext:
    """Reviewer on attempt N > 1 receives the immediately prior review's
    feedback and SHA, and runs under the follow-up directive."""

    def test_reviewer_attempt_2_receives_prior_feedback_and_sha(
        self, sample_task, sample_worktree, tmp_path
    ):
        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            captured.append(directive)
            reviewer_count = len([d for d in captured if d.role == Role.REVIEWER])
            if isinstance(directive, ImplementorDirective):
                return _done_result(role)
            if isinstance(directive, TesterDirective):
                return _pass_result(role)
            if isinstance(directive, ReviewerDirective):
                return _make_result(
                    role,
                    TaskStatus.DONE,
                    "Missing null check in foo().\nVERDICT: FAIL",
                )
            if isinstance(directive, ReviewerFollowupDirective):
                return _pass_result(role)
            if isinstance(directive, FixerDirective):
                return _done_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_head_sha", return_value="sha-1"),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                )
            )

        reviewer_directives = [d for d in captured if d.role == Role.REVIEWER]
        assert len(reviewer_directives) == 2

        # Attempt 1: full review — ReviewerDirective (no followup fields).
        assert isinstance(reviewer_directives[0], ReviewerDirective)

        # Attempt 2: follow-up — ReviewerFollowupDirective with prior SHA and feedback.
        followup = reviewer_directives[1]
        assert isinstance(followup, ReviewerFollowupDirective)
        assert followup.prior_review_sha == "sha-1"
        assert "Missing null check in foo()." in followup.prior_feedback

    def test_reviewer_attempt_3_uses_attempt_2_sha_and_feedback(
        self, sample_task, sample_worktree, tmp_path
    ):
        """Follow-up always compares against the IMMEDIATELY prior review, not the original."""
        captured: list[PipelineDirective] = []

        # Simulate HEAD advancing as the fixer commits between reviews.
        head_shas = iter(["sha-review-1", "sha-review-2", "sha-review-3"])

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            captured.append(directive)
            reviewer_count = len([d for d in captured if d.role == Role.REVIEWER])
            if isinstance(directive, ImplementorDirective):
                return _done_result(role)
            if isinstance(directive, TesterDirective):
                return _pass_result(role)
            if isinstance(directive, ReviewerDirective):
                return _make_result(role, TaskStatus.DONE, "issue A\nVERDICT: FAIL")
            if isinstance(directive, ReviewerFollowupDirective):
                if reviewer_count == 2:
                    return _make_result(role, TaskStatus.DONE, "issue B\nVERDICT: FAIL")
                return _pass_result(role)
            if isinstance(directive, FixerDirective):
                return _done_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_head_sha", side_effect=lambda *_a, **_k: next(head_shas)),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    max_retries=3,
                )
            )

        reviewer_directives = [d for d in captured if d.role == Role.REVIEWER]
        assert len(reviewer_directives) == 3

        # Attempt 2 compares against attempt 1's SHA and gets attempt 1's feedback.
        followup_2 = reviewer_directives[1]
        assert isinstance(followup_2, ReviewerFollowupDirective)
        assert followup_2.prior_review_sha == "sha-review-1"
        assert "issue A" in followup_2.prior_feedback

        # Attempt 3 compares against attempt 2's SHA and gets attempt 2's feedback
        # (NOT attempt 1's).
        followup_3 = reviewer_directives[2]
        assert isinstance(followup_3, ReviewerFollowupDirective)
        assert followup_3.prior_review_sha == "sha-review-2"
        assert "issue B" in followup_3.prior_feedback
        assert "issue A" not in followup_3.prior_feedback


class TestReviewerCrashDuringFollowup:
    """Reviewer agent crash on attempt > 1 stops the pipeline."""

    def test_reviewer_crash_on_followup_stops_pipeline(
        self, sample_task, sample_worktree, tmp_path
    ):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            if isinstance(directive, ImplementorDirective):
                return _done_result(role)
            if isinstance(directive, TesterDirective):
                return _pass_result(role)
            if isinstance(directive, ReviewerDirective):
                return _fail_verdict_result(role)
            if isinstance(directive, ReviewerFollowupDirective):
                return _crash_result(role)
            if isinstance(directive, FixerDirective):
                return _done_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_head_sha", return_value="sha-1"),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                )
            )

        reviewer_calls = [r for r in results if r.role == Role.REVIEWER]
        assert len(reviewer_calls) == 2
        assert reviewer_calls[1].status == TaskStatus.FAILED


class TestReviewFixerCrashStopsPipeline:
    """Fixer crash during review retry stops the pipeline."""

    def test_review_fixer_crash_stops_pipeline(self, sample_task, sample_worktree, tmp_path):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            if isinstance(directive, ImplementorDirective):
                return _done_result(role)
            if isinstance(directive, TesterDirective):
                return _pass_result(role)
            if isinstance(directive, (ReviewerDirective, ReviewerFollowupDirective)):
                return _fail_verdict_result(role)
            if isinstance(directive, FixerDirective):
                return _crash_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_head_sha", return_value="sha-1"),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                )
            )

        fixer_calls = [r for r in results if r.role == Role.FIXER]
        assert len(fixer_calls) == 1
        assert fixer_calls[0].status == TaskStatus.FAILED


class TestReviewRetriesExhausted:
    """Pipeline fails when review retries are exhausted."""

    def test_review_retries_exhausted(self, sample_task, sample_worktree, tmp_path):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            if isinstance(directive, ImplementorDirective):
                return _done_result(role)
            if isinstance(directive, TesterDirective):
                return _pass_result(role)
            if isinstance(directive, (ReviewerDirective, ReviewerFollowupDirective)):
                return _fail_verdict_result(role)
            if isinstance(directive, FixerDirective):
                return _done_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_head_sha", return_value="sha-1"),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    max_retries=1,
                )
            )

        reviewer_calls = [r for r in results if r.role == Role.REVIEWER]
        # 1 initial + 1 retry = 2 reviews, both FAIL
        assert len(reviewer_calls) == 2
        assert all(not r.passed for r in reviewer_calls)


class TestTDDPipelineUsesProfileAgent:
    def test_tdd_pipeline_uses_profile_agent(self, sample_task, sample_worktree, tmp_path):
        """In TDD mode, profile agent should be used for TDD phases."""
        profile = Profile.default()
        profile.tester.agent = "gemini"
        profile.implementor.agent = "codex"

        captured_calls: list[dict] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            captured_calls.append({"role": role, "agent_cmd": kwargs.get("agent_cmd"), **kwargs})
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                    profile=profile,
                )
            )

        # TDD Phase 1 tester should use profile's "gemini"
        tester_calls = [c for c in captured_calls if c["role"] == Role.TESTER]
        assert len(tester_calls) >= 1
        assert tester_calls[0]["agent_cmd"] == "gemini"

        # TDD Phase 2 implementor should use profile's "codex"
        impl_calls = [c for c in captured_calls if c["role"] == Role.IMPLEMENTOR]
        assert len(impl_calls) == 1
        assert impl_calls[0]["agent_cmd"] == "codex"

    def test_tdd_pipeline_uses_tdd_default_over_profile_main(
        self, sample_task, sample_worktree, tmp_path
    ):
        """In TDD mode with no tdd sub-mode in profile, TDD DEFAULT_TEXT is used
        (not the profile's main directive)."""
        profile = Profile.default()
        profile.tester.directive = "Profile tester directive"

        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            return _pass_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                    profile=profile,
                )
            )

        # TDD Phase 1 tester: profile has no tdd sub-mode, so directive_text is ""
        # which means resolved_text() falls back to DEFAULT_TEXT
        tdd_tester = [d for d in captured if isinstance(d, TddTesterDirective)]
        assert len(tdd_tester) >= 1
        assert tdd_tester[0].directive_text == ""
        assert tdd_tester[0].resolved_text() == TddTesterDirective.DEFAULT_TEXT

    def test_tdd_pipeline_cli_directive_overrides_tdd_and_profile(
        self, sample_task, sample_worktree, tmp_path
    ):
        """In TDD mode, CLI directive should override both TDD directives and profile."""
        profile = Profile.default()
        profile.tester.directive = "Profile tester directive"
        cli_directive = "CLI tester directive wins"

        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            return _pass_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                    profile=profile,
                    directives={Role.TESTER: cli_directive},
                )
            )

        tdd_tester = [d for d in captured if isinstance(d, TddTesterDirective)]
        assert len(tdd_tester) >= 1
        assert tdd_tester[0].resolved_text() == cli_directive


# ---------------------------------------------------------------------------
# New profile sub-mode integration tests
# ---------------------------------------------------------------------------


class TestTDDProfileSubModeHonored:
    """TDD profile sub-mode is honored."""

    def test_tdd_profile_sub_mode_overrides_default(self, sample_task, sample_worktree, tmp_path):
        """With profile.tester.tdd = ModeConfig(directive="custom TDD test"),
        running run_pipeline(..., tdd=True, profile=...) uses that directive."""
        profile = Profile.default()
        profile.tester.tdd = ModeConfig(directive="custom TDD test")

        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            return _pass_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                    profile=profile,
                )
            )

        tdd_tester = [d for d in captured if isinstance(d, TddTesterDirective)]
        assert len(tdd_tester) >= 1
        assert tdd_tester[0].resolved_text() == "custom TDD test"
        assert tdd_tester[0].resolved_text() != TddTesterDirective.DEFAULT_TEXT


class TestCLIFlagWinsOverTDDProfile:
    """CLI flag wins over TDD profile sub-mode."""

    def test_cli_overrides_tdd_profile(self, sample_task, sample_worktree, tmp_path):
        """With both directives={Role.TESTER: "from CLI"} and
        profile.tester.tdd.directive = "from profile", the prompt contains "from CLI"."""
        profile = Profile.default()
        profile.tester.tdd = ModeConfig(directive="from profile")

        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            return _pass_result(directive.role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                    profile=profile,
                    directives={Role.TESTER: "from CLI"},
                )
            )

        tdd_tester = [d for d in captured if isinstance(d, TddTesterDirective)]
        assert len(tdd_tester) >= 1
        assert tdd_tester[0].resolved_text() == "from CLI"


class TestReviewerFollowupUsesProfileSubMode:
    """Reviewer followup uses profile sub-mode."""

    def test_reviewer_followup_profile_sub_mode(self, sample_task, sample_worktree, tmp_path):
        """With profile.reviewer.followup = ModeConfig(directive="custom followup"),
        the second review attempt's directive contains "custom followup"."""
        profile = Profile.default()
        profile.reviewer.followup = ModeConfig(directive="custom followup")

        captured: list[PipelineDirective] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(directive)
            role = directive.role
            if isinstance(directive, ImplementorDirective):
                return _done_result(role)
            if isinstance(directive, TesterDirective):
                return _pass_result(role)
            if isinstance(directive, ReviewerDirective):
                return _fail_verdict_result(role)
            if isinstance(directive, ReviewerFollowupDirective):
                return _pass_result(role)
            if isinstance(directive, FixerDirective):
                return _done_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_head_sha", return_value="sha-1"),
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                )
            )

        followups = [d for d in captured if isinstance(d, ReviewerFollowupDirective)]
        assert len(followups) == 1
        assert followups[0].resolved_text() == "custom followup"


class TestPlannerUsesProfile:
    """Planner uses profile."""

    def test_planner_profile_directive(self, tmp_path):
        """With profile.planner.directive = "custom planner",
        run_planner(..., profile=...) produces a prompt containing "custom planner"."""
        from workbench.agents import run_planner

        profile = Profile.default()
        profile.planner.directive = "custom planner"

        with (
            patch("workbench.agents.get_adapter") as mock_adapter,
            patch("workbench.agents._load_plan_guide", return_value="guide text"),
        ):
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("plan output", {})
            mock_adapter.return_value = mock_adapter_instance

            result = asyncio.run(
                run_planner(
                    repo=tmp_path,
                    user_prompt="Build X",
                    use_tmux=False,
                    profile=profile,
                )
            )

        prompt_arg = mock_adapter_instance.build_command.call_args[0][0]
        assert "custom planner" in prompt_arg


class TestMergerUsesProfile:
    """Merger uses profile."""

    def test_merger_profile_directive(self, tmp_path):
        """With profile.merger.directive = "custom merger",
        run_merge_resolver(..., profile=...) produces a prompt containing "custom merger"."""
        from workbench.agents import run_merge_resolver

        profile = Profile.default()
        profile.merger.directive = "custom merger"

        with patch("workbench.agents.get_adapter") as mock_adapter:
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("merge output", {})
            mock_adapter.return_value = mock_adapter_instance

            result = asyncio.run(
                run_merge_resolver(
                    task_branch="feature/x",
                    session_branch="main",
                    merge_dir=tmp_path,
                    conflicts=["file1.py", "file2.py"],
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                )
            )

        prompt_arg = mock_adapter_instance.build_command.call_args[0][0]
        assert "custom merger" in prompt_arg
