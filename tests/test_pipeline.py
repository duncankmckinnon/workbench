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
from workbench.plan_parser import Task
from workbench.profile import Profile, RoleConfig
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
        side_effects = [
            _done_result(Role.TESTER),  # TDD Phase 1: write failing tests (no verdict needed)
            _pass_result(Role.IMPLEMENTOR),  # TDD Phase 2: implement (tests pass + comprehensive)
            _pass_result(Role.TESTER),  # Verification: test
            _pass_result(Role.REVIEWER),  # Review
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
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
        side_effects = [
            _crash_result(Role.TESTER),
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
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
        side_effects = [
            _done_result(Role.TESTER),
            _fail_verdict_result(Role.IMPLEMENTOR),
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
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
        side_effects = [
            _done_result(Role.TESTER),
            _crash_result(Role.IMPLEMENTOR),
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
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
        """Verify TDD_DIRECTIVES are used (not DEFAULT_DIRECTIVES) when no override provided."""
        captured_directives = []

        async def mock_run_agent(*args, **kwargs):
            captured_directives.append(kwargs.get("directive"))
            role = args[0]
            if role == Role.TESTER and len(captured_directives) == 1:
                return _done_result(role)  # TDD tester: no verdict
            return _pass_result(role)

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                )
            )

        # First call: TDD tester directive
        assert captured_directives[0] == TDD_DIRECTIVES[Role.TESTER]
        # Second call: TDD implementor directive
        assert captured_directives[1] == TDD_DIRECTIVES[Role.IMPLEMENTOR]

    def test_pipeline_tdd_directive_override(self, sample_task, sample_worktree, tmp_path):
        """Custom directives dict overrides TDD defaults."""
        custom_tester = "Custom tester directive"
        custom_impl = "Custom implementor directive"
        captured_directives = []

        async def mock_run_agent(*args, **kwargs):
            captured_directives.append(kwargs.get("directive"))
            role = args[0]
            if role == Role.TESTER and len(captured_directives) == 1:
                return _done_result(role)  # TDD tester: no verdict
            return _pass_result(role)

        custom_directives = {
            Role.TESTER: custom_tester,
            Role.IMPLEMENTOR: custom_impl,
        }

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                    directives=custom_directives,
                )
            )

        assert captured_directives[0] == custom_tester
        assert captured_directives[1] == custom_impl


class TestTDDVerificationFailsThenFixes:
    def test_pipeline_tdd_verification_fails_then_fixes(
        self, sample_task, sample_worktree, tmp_path
    ):
        """After TDD impl, verification test fails, fixer runs, test passes on retry."""
        side_effects = [
            _done_result(Role.TESTER),  # TDD Phase 1: write tests (no verdict)
            _pass_result(Role.IMPLEMENTOR),  # TDD Phase 2: implement (PASS verdict)
            _fail_verdict_result(Role.TESTER),  # Verification: test FAILS
            _done_result(Role.FIXER),  # Fixer addresses issues
            _pass_result(Role.TESTER),  # Verification retry: PASS
            _pass_result(Role.REVIEWER),  # Review: PASS
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
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

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append(
                {"role": args[0], "agent_cmd": kwargs.get("agent_cmd"), **kwargs}
            )
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
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
        """Profile with custom tester directive should pass it through to run_agent."""
        profile = Profile.default()
        custom_directive = "Custom tester directive from profile"
        profile.tester.directive = custom_directive

        captured_calls: list[dict] = []

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append({"role": args[0], **kwargs})
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                )
            )

        # The tester call should use the profile's directive
        tester_calls = [c for c in captured_calls if c["role"] == Role.TESTER]
        assert len(tester_calls) == 1
        assert tester_calls[0]["directive"] == custom_directive


class TestPipelineCLIDirectiveOverridesProfile:
    def test_pipeline_cli_directive_overrides_profile(
        self, sample_task, sample_worktree, tmp_path
    ):
        """CLI directive takes priority over profile directive for the same role."""
        profile = Profile.default()
        profile.tester.directive = "Profile tester directive"
        cli_tester_directive = "CLI tester directive wins"

        captured_calls: list[dict] = []

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append({"role": args[0], **kwargs})
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                    directives={Role.TESTER: cli_tester_directive},
                )
            )

        tester_calls = [c for c in captured_calls if c["role"] == Role.TESTER]
        assert len(tester_calls) == 1
        # CLI directive should win over profile
        assert tester_calls[0]["directive"] == cli_tester_directive


class TestPipelineProfileNoneUsesDefaults:
    def test_pipeline_profile_none_uses_defaults(self, sample_task, sample_worktree, tmp_path):
        """profile=None should behave the same as before — default directives, default agent."""
        captured_calls: list[dict] = []

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append({"role": args[0], **kwargs})
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
            results = asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=None,
                )
            )

        # All calls should use default agent_cmd
        for c in captured_calls:
            assert c.get("agent_cmd", "claude") == "claude"

        # Directives should be None (letting run_agent apply defaults)
        impl_calls = [c for c in captured_calls if c["role"] == Role.IMPLEMENTOR]
        assert impl_calls[0].get("directive") is None


class TestPipelineProfileFixerAgent:
    def test_pipeline_profile_fixer_uses_profile_agent_on_retry(
        self, sample_task, sample_worktree, tmp_path
    ):
        """When test fails and fixer runs, fixer should use its profile agent."""
        profile = Profile.default()
        profile.fixer.agent = "codex"

        captured_calls: list[dict] = []

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append(
                {"role": args[0], "agent_cmd": kwargs.get("agent_cmd"), **kwargs}
            )
            role = args[0]
            if role == Role.IMPLEMENTOR:
                return _pass_result(role)
            if (
                role == Role.TESTER
                and len([c for c in captured_calls if c["role"] == Role.TESTER]) == 1
            ):
                return _fail_verdict_result(role)  # first test fails
            if role == Role.FIXER:
                return _done_result(role)
            return _pass_result(role)  # second test + reviewer pass

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
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

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append(
                {"role": args[0], "agent_cmd": kwargs.get("agent_cmd"), **kwargs}
            )
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
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

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append(
                {"role": args[0], "agent_cmd": kwargs.get("agent_cmd"), **kwargs}
            )
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
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


class TestRunAgentProfileRoleConfig:
    """Test run_agent's profile_role_config parameter directly."""

    def test_run_agent_profile_role_config_sets_directive(
        self, sample_task, sample_worktree, tmp_path
    ):
        """profile_role_config.directive is used when no explicit directive passed."""
        rc = RoleConfig(agent="claude", directive="Profile directive for implementor")

        with (
            patch("workbench.agents.get_adapter") as mock_adapter,
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("VERDICT: PASS", {})
            mock_adapter.return_value = mock_adapter_instance

            from workbench.agents import run_agent

            result = asyncio.run(
                run_agent(
                    Role.IMPLEMENTOR,
                    sample_task,
                    sample_worktree,
                    tmp_path,
                    use_tmux=False,
                    profile_role_config=rc,
                )
            )

        # The prompt should contain the profile directive
        prompt_arg = mock_adapter_instance.build_command.call_args[0][0]
        assert "Profile directive for implementor" in prompt_arg

    def test_run_agent_explicit_directive_overrides_profile_role_config(
        self, sample_task, sample_worktree, tmp_path
    ):
        """Explicit directive parameter should override profile_role_config.directive."""
        rc = RoleConfig(agent="claude", directive="Profile directive")
        explicit = "Explicit directive wins"

        with (
            patch("workbench.agents.get_adapter") as mock_adapter,
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("VERDICT: PASS", {})
            mock_adapter.return_value = mock_adapter_instance

            from workbench.agents import run_agent

            result = asyncio.run(
                run_agent(
                    Role.IMPLEMENTOR,
                    sample_task,
                    sample_worktree,
                    tmp_path,
                    use_tmux=False,
                    directive=explicit,
                    profile_role_config=rc,
                )
            )

        prompt_arg = mock_adapter_instance.build_command.call_args[0][0]
        assert "Explicit directive wins" in prompt_arg
        assert "Profile directive" not in prompt_arg

    def test_run_agent_profile_role_config_sets_agent(
        self, sample_task, sample_worktree, tmp_path
    ):
        """profile_role_config.agent is used when agent_cmd is default 'claude'."""
        rc = RoleConfig(agent="gemini", directive=DEFAULT_DIRECTIVES[Role.IMPLEMENTOR])

        with (
            patch("workbench.agents.get_adapter") as mock_adapter,
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("VERDICT: PASS", {})
            mock_adapter.return_value = mock_adapter_instance

            from workbench.agents import run_agent

            result = asyncio.run(
                run_agent(
                    Role.IMPLEMENTOR,
                    sample_task,
                    sample_worktree,
                    tmp_path,
                    use_tmux=False,
                    profile_role_config=rc,
                )
            )

        # get_adapter should have been called with "gemini"
        mock_adapter.assert_called_once_with("gemini", tmp_path / ".workbench" / "agents.yaml")

    def test_run_agent_explicit_agent_cmd_overrides_profile_role_config(
        self, sample_task, sample_worktree, tmp_path
    ):
        """Explicit agent_cmd != 'claude' should not be overridden by profile_role_config.agent."""
        rc = RoleConfig(agent="gemini", directive=DEFAULT_DIRECTIVES[Role.IMPLEMENTOR])

        with (
            patch("workbench.agents.get_adapter") as mock_adapter,
            patch("workbench.agents.get_main_branch", return_value="main"),
        ):
            mock_adapter_instance = MagicMock()
            mock_adapter_instance.build_command.return_value = ["echo", "test"]
            mock_adapter_instance.parse_output.return_value = ("VERDICT: PASS", {})
            mock_adapter.return_value = mock_adapter_instance

            from workbench.agents import run_agent

            result = asyncio.run(
                run_agent(
                    Role.IMPLEMENTOR,
                    sample_task,
                    sample_worktree,
                    tmp_path,
                    agent_cmd="codex",  # explicit non-default
                    use_tmux=False,
                    profile_role_config=rc,
                )
            )

        # get_adapter should have been called with "codex" (not "gemini" from profile)
        mock_adapter.assert_called_once_with("codex", tmp_path / ".workbench" / "agents.yaml")


# ---------------------------------------------------------------------------
# TDD + Profile integration tests
# ---------------------------------------------------------------------------


class TestReviewerFollowupContext:
    """Reviewer on attempt N > 1 receives the immediately prior review's
    feedback and SHA, and runs under the follow-up directive."""

    def test_reviewer_attempt_2_receives_prior_feedback_and_sha(
        self, sample_task, sample_worktree, tmp_path
    ):
        captured_calls: list[dict] = []

        async def mock_run_agent(*args, **kwargs):
            role = args[0]
            captured_calls.append({"role": role, **kwargs})
            count = len([c for c in captured_calls if c["role"] == role])
            if role == Role.IMPLEMENTOR:
                return _done_result(role)
            if role == Role.TESTER:
                return _pass_result(role)
            if role == Role.REVIEWER:
                if count == 1:
                    return _make_result(
                        role,
                        TaskStatus.DONE,
                        "Missing null check in foo().\nVERDICT: FAIL",
                    )
                return _pass_result(role)
            if role == Role.FIXER:
                return _done_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_head_sha", return_value="sha-1"),
        ):
            asyncio.run(
                run_pipeline(
                    task=sample_task,
                    worktree=sample_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                )
            )

        reviewer_calls = [c for c in captured_calls if c["role"] == Role.REVIEWER]
        assert len(reviewer_calls) == 2

        # Attempt 1: full review — no prior SHA, no prior feedback, default directive.
        assert reviewer_calls[0].get("prior_review_sha") is None
        assert reviewer_calls[0].get("extra_context", "") == ""
        assert reviewer_calls[0].get("directive") != REVIEWER_FOLLOWUP_DIRECTIVE

        # Attempt 2: follow-up — SHA captured before attempt 1, feedback from attempt 1,
        # and the follow-up directive.
        assert reviewer_calls[1]["prior_review_sha"] == "sha-1"
        assert "Missing null check in foo()." in reviewer_calls[1]["extra_context"]
        assert reviewer_calls[1]["directive"] == REVIEWER_FOLLOWUP_DIRECTIVE

    def test_reviewer_attempt_3_uses_attempt_2_sha_and_feedback(
        self, sample_task, sample_worktree, tmp_path
    ):
        """Follow-up always compares against the IMMEDIATELY prior review, not the original."""
        captured_calls: list[dict] = []

        # Simulate HEAD advancing as the fixer commits between reviews.
        head_shas = iter(["sha-review-1", "sha-review-2", "sha-review-3"])

        async def mock_run_agent(*args, **kwargs):
            role = args[0]
            captured_calls.append({"role": role, **kwargs})
            count = len([c for c in captured_calls if c["role"] == role])
            if role == Role.IMPLEMENTOR:
                return _done_result(role)
            if role == Role.TESTER:
                return _pass_result(role)
            if role == Role.REVIEWER:
                if count == 1:
                    return _make_result(role, TaskStatus.DONE, "issue A\nVERDICT: FAIL")
                if count == 2:
                    return _make_result(role, TaskStatus.DONE, "issue B\nVERDICT: FAIL")
                return _pass_result(role)
            if role == Role.FIXER:
                return _done_result(role)
            return _pass_result(role)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_head_sha", side_effect=lambda *_a, **_k: next(head_shas)),
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

        reviewer_calls = [c for c in captured_calls if c["role"] == Role.REVIEWER]
        assert len(reviewer_calls) == 3

        # Attempt 2 compares against attempt 1's SHA and gets attempt 1's feedback.
        assert reviewer_calls[1]["prior_review_sha"] == "sha-review-1"
        assert "issue A" in reviewer_calls[1]["extra_context"]

        # Attempt 3 compares against attempt 2's SHA and gets attempt 2's feedback
        # (NOT attempt 1's).
        assert reviewer_calls[2]["prior_review_sha"] == "sha-review-2"
        assert "issue B" in reviewer_calls[2]["extra_context"]
        assert "issue A" not in reviewer_calls[2]["extra_context"]


class TestTDDPipelineUsesProfileAgent:
    def test_tdd_pipeline_uses_profile_agent(self, sample_task, sample_worktree, tmp_path):
        """In TDD mode, profile agent should be used for TDD phases."""
        profile = Profile.default()
        profile.tester.agent = "gemini"
        profile.implementor.agent = "codex"

        captured_calls: list[dict] = []

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append(
                {"role": args[0], "agent_cmd": kwargs.get("agent_cmd"), **kwargs}
            )
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
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

    def test_tdd_pipeline_uses_tdd_directives_over_profile(
        self, sample_task, sample_worktree, tmp_path
    ):
        """In TDD mode, TDD directives should be used over profile directives (but CLI wins)."""
        profile = Profile.default()
        profile.tester.directive = "Profile tester directive"

        captured_calls: list[dict] = []

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append({"role": args[0], **kwargs})
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
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

        # TDD Phase 1 tester should use TDD directive (not profile directive)
        tester_calls = [c for c in captured_calls if c["role"] == Role.TESTER]
        assert len(tester_calls) >= 1
        assert tester_calls[0]["directive"] == TDD_DIRECTIVES[Role.TESTER]

    def test_tdd_pipeline_cli_directive_overrides_tdd_and_profile(
        self, sample_task, sample_worktree, tmp_path
    ):
        """In TDD mode, CLI directive should override both TDD directives and profile."""
        profile = Profile.default()
        profile.tester.directive = "Profile tester directive"
        cli_directive = "CLI tester directive wins"

        captured_calls: list[dict] = []

        async def mock_run_agent(*args, **kwargs):
            captured_calls.append({"role": args[0], **kwargs})
            return _pass_result(args[0])

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
            results = asyncio.run(
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

        tester_calls = [c for c in captured_calls if c["role"] == Role.TESTER]
        assert len(tester_calls) >= 1
        assert tester_calls[0]["directive"] == cli_directive
