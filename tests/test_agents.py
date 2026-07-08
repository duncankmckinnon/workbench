"""Tests for the agents module — spawning, prompts, and result parsing."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workbench.adapters import ClaudeAdapter
from workbench.agents import (
    AgentResult,
    Role,
    TaskStatus,
    run_agent,
    run_merge_resolver,
    run_pipeline,
    run_planner,
)
from workbench.directives import FixerDirective, ImplementorDirective, PromptContext
from workbench.headroom import HeadroomConfig
from workbench.plan_parser import Task
from workbench.worktree import CommitResult, Worktree


@pytest.fixture
def sample_task():
    return Task(id="task-1", title="Test Task", description="Do something", files=["src/foo.py"])


@pytest.fixture
def sample_worktree(tmp_path):
    return Worktree(path=tmp_path, branch="wb/test-task", task_id="task-1")


@pytest.fixture
def sample_ctx(sample_task, sample_worktree):
    return PromptContext(
        task=sample_task,
        worktree=sample_worktree,
        base_branch="main",
    )


class TestRunAgentTmux:
    def test_run_agent_success_tmux(self, sample_ctx, tmp_path):
        """Mock run_in_tmux returning (0, json), verify AgentResult.status == DONE."""
        output = json.dumps({"result": "all good", "cost_usd": {"input": 0.01}})
        directive = ImplementorDirective()
        with patch(
            "workbench.agents.run_in_tmux", new_callable=AsyncMock, return_value=(0, output)
        ):
            result = asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=True,
                )
            )

        assert result.status == TaskStatus.DONE
        assert result.task_id == "task-1"

    def test_run_agent_failure_tmux(self, sample_ctx, tmp_path):
        """Mock returning (1, "error"), verify FAILED."""
        directive = ImplementorDirective()
        with patch(
            "workbench.agents.run_in_tmux", new_callable=AsyncMock, return_value=(1, "error")
        ):
            result = asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=True,
                )
            )

        assert result.status == TaskStatus.FAILED


class TestRunAgentSubprocess:
    def test_run_agent_no_tmux(self, sample_ctx, tmp_path):
        """use_tmux=False, mock create_subprocess_exec, verify it's called."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"done", b"")
        mock_proc.returncode = 0

        directive = ImplementorDirective()
        with patch(
            "workbench.agents.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_exec:
            result = asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=False,
                )
            )

        mock_exec.assert_called_once()
        assert result.status == TaskStatus.DONE


class TestPromptBuilding:
    def test_implementor_prompt_has_branch(self, sample_task, sample_worktree):
        """Verify branch name present in rendered implementor prompt."""
        ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
        prompt = ImplementorDirective().render(ctx)
        assert "wb/test-task" in prompt

    def test_fixer_prompt_has_branch(self, sample_task, sample_worktree):
        """Same for fixer role."""
        with patch("workbench.directives.get_diff", return_value="some diff"):
            ctx = PromptContext(task=sample_task, worktree=sample_worktree, base_branch="main")
            prompt = FixerDirective(feedback="", failure_kind="test", attempt=1).render(ctx)
        assert "wb/test-task" in prompt


class TestAgentResult:
    def test_agent_result_passed(self):
        """AgentResult with 'VERDICT: PASS' → .passed == True."""
        result = AgentResult(
            task_id="task-1",
            role=Role.TESTER,
            status=TaskStatus.DONE,
            output="All tests passed.\nVERDICT: PASS",
        )
        assert result.passed is True

    def test_agent_result_failed_verdict(self):
        """'VERDICT: FAIL' → .passed == False."""
        result = AgentResult(
            task_id="task-1",
            role=Role.TESTER,
            status=TaskStatus.DONE,
            output="Some tests failed.\nVERDICT: FAIL",
        )
        assert result.passed is False

    def test_agent_result_feedback(self):
        """Text before VERDICT line extracted by .feedback."""
        result = AgentResult(
            task_id="task-1",
            role=Role.REVIEWER,
            status=TaskStatus.DONE,
            output="Missing error handling in foo().\nNeeds type hints.\nVERDICT: FAIL",
        )
        assert "Missing error handling" in result.feedback
        assert "Needs type hints" in result.feedback
        assert "VERDICT" not in result.feedback


class TestAgentsConfigPathsForwarding:
    def test_run_agent_accepts_agents_config_paths_kwarg(self, sample_ctx, tmp_path):
        """run_agent forwards agents_config_paths to get_adapter."""
        paths = [tmp_path / "plan.yaml", tmp_path / "project.yaml"]
        directive = ImplementorDirective()
        with (
            patch(
                "workbench.agents.get_adapter", return_value=ClaudeAdapter()
            ) as mock_get_adapter,
            patch(
                "workbench.agents.run_in_tmux",
                new_callable=AsyncMock,
                return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
            ),
        ):
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=True,
                    agents_config_paths=paths,
                )
            )

        mock_get_adapter.assert_called_once_with("claude", paths)

    def test_run_merge_resolver_accepts_agents_config_paths_kwarg(self, tmp_path):
        """run_merge_resolver forwards agents_config_paths to get_adapter."""
        paths = [tmp_path / "plan.yaml", tmp_path / "project.yaml"]
        with (
            patch(
                "workbench.agents.get_adapter", return_value=ClaudeAdapter()
            ) as mock_get_adapter,
            patch(
                "workbench.agents.run_in_tmux",
                new_callable=AsyncMock,
                return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
            ),
        ):
            asyncio.run(
                run_merge_resolver(
                    task_branch="wb/task-1",
                    session_branch="wb/session",
                    merge_dir=tmp_path,
                    conflicts=["src/foo.py"],
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=True,
                    agents_config_paths=paths,
                )
            )

        mock_get_adapter.assert_called_once_with("claude", paths)

    def test_run_planner_accepts_agents_config_paths_kwarg(self, tmp_path):
        """run_planner forwards agents_config_paths to get_adapter."""
        paths = [tmp_path / "plan.yaml", tmp_path / "project.yaml"]
        with (
            patch(
                "workbench.agents.get_adapter", return_value=ClaudeAdapter()
            ) as mock_get_adapter,
            patch(
                "workbench.agents.run_in_tmux",
                new_callable=AsyncMock,
                return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
            ),
        ):
            asyncio.run(
                run_planner(
                    repo=tmp_path,
                    user_prompt="build a thing",
                    plan_name="myplan",
                    agent_cmd="claude",
                    use_tmux=True,
                    agents_config_paths=paths,
                )
            )

        mock_get_adapter.assert_called_once_with("claude", paths)

    def test_run_agent_default_uses_project_agents_yaml(self, sample_ctx, tmp_path):
        """When agents_config_paths is None, defaults to project agents.yaml path."""
        directive = ImplementorDirective()
        with (
            patch(
                "workbench.agents.get_adapter", return_value=ClaudeAdapter()
            ) as mock_get_adapter,
            patch(
                "workbench.agents.run_in_tmux",
                new_callable=AsyncMock,
                return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
            ),
        ):
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=True,
                )
            )

        mock_get_adapter.assert_called_once_with(
            "claude", [tmp_path / ".workbench" / "agents.yaml"]
        )


# ---------------------------------------------------------------------------
# Pipeline streaming (on_result callback) and resume behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline_task():
    return Task(id="task-1", title="Add feature", description="Do X", files=["src/x.py"])


@pytest.fixture
def pipeline_worktree(tmp_path):
    return Worktree(path=tmp_path, branch="wb/task-1", task_id="task-1")


def _result(role: Role, passed: bool = True, status: TaskStatus = TaskStatus.DONE) -> AgentResult:
    output = "ok\nVERDICT: PASS" if passed else "issues\nVERDICT: FAIL"
    return AgentResult(task_id="task-1", role=role, status=status, output=output)


def _done(role: Role) -> AgentResult:
    return AgentResult(task_id="task-1", role=role, status=TaskStatus.DONE, output="done")


class TestOnResultStreaming:
    def test_on_result_fires_for_each_completed_agent(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            return _result(directive.role, passed=True)

        collected: list[AgentResult] = []

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    on_result=collected.append,
                )
            )

        assert len(collected) == 3
        assert [r.role for r in collected] == [Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER]

    def test_on_result_fires_during_pipeline_not_at_end(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            if directive.role == Role.TESTER:
                await asyncio.sleep(0.02)
            return _result(directive.role, passed=True)

        timestamps: list[tuple[Role, float]] = []

        def on_result(r: AgentResult) -> None:
            timestamps.append((r.role, time.monotonic()))

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    on_result=on_result,
                )
            )

        by_role = {role: t for role, t in timestamps}
        assert by_role[Role.IMPLEMENTOR] < by_role[Role.TESTER]
        assert by_role[Role.TESTER] < by_role[Role.REVIEWER]

    def test_on_result_includes_fixer_results(self, pipeline_task, pipeline_worktree, tmp_path):
        tester_calls = {"n": 0}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            if role == Role.TESTER:
                tester_calls["n"] += 1
                return _result(role, passed=tester_calls["n"] >= 2)
            if role == Role.FIXER:
                return _done(role)
            return _result(role, passed=True)

        collected: list[AgentResult] = []

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    on_result=collected.append,
                    max_retries=1,
                )
            )

        assert [r.role for r in collected] == [
            Role.IMPLEMENTOR,
            Role.TESTER,
            Role.FIXER,
            Role.TESTER,
            Role.REVIEWER,
        ]

    def test_on_result_not_called_for_skipped_stages(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            return _result(directive.role, passed=True)

        collected: list[AgentResult] = []

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    on_result=collected.append,
                    skip_test=True,
                    skip_review=True,
                )
            )

        assert [r.role for r in collected] == [Role.IMPLEMENTOR]

    def test_pipeline_commits_mutating_stages_before_followup_review(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        review_calls = {"n": 0}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            if directive.role == Role.REVIEWER:
                review_calls["n"] += 1
                return _result(Role.REVIEWER, passed=review_calls["n"] >= 2)
            if directive.role == Role.FIXER:
                return _done(Role.FIXER)
            return _result(directive.role, passed=True)

        commits: list[str] = []

        def mock_commit(worktree, message):
            commits.append(message)
            return CommitResult(success=True, committed=True, message="committed")

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.commit_worktree_changes", side_effect=mock_commit),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    max_retries=1,
                )
            )

        assert [r.role for r in results] == [
            Role.IMPLEMENTOR,
            Role.TESTER,
            Role.REVIEWER,
            Role.FIXER,
            Role.REVIEWER,
        ]
        assert commits == [
            "wb: task-1 implement",
            "wb: task-1 test#1",
            "wb: task-1 review-fix#1",
        ]

    def test_pipeline_fails_when_mutating_stage_commit_fails(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            return _result(directive.role, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch(
                "workbench.agents.commit_worktree_changes",
                return_value=CommitResult(
                    success=False,
                    committed=False,
                    message="nothing added to commit",
                ),
            ),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                )
            )

        assert [r.role for r in results] == [Role.IMPLEMENTOR, Role.IMPLEMENTOR]
        assert results[-1].status == TaskStatus.FAILED
        assert "Commit failed after implement" in results[-1].output

    def test_pipeline_works_without_on_result_callback(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            return _result(directive.role, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    on_result=None,
                )
            )

        assert [r.role for r in results] == [Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER]


class TestPipelineResume:
    def test_resume_skips_implementor(self, pipeline_task, pipeline_worktree, tmp_path):
        calls: dict[Role, int] = {}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            calls[directive.role] = calls.get(directive.role, 0) + 1
            return _result(directive.role, passed=True)

        prior_impl = _result(Role.IMPLEMENTOR, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    resume_completed_stages=["implementor"],
                    prior_results=[prior_impl],
                )
            )

        assert Role.IMPLEMENTOR not in calls
        assert calls.get(Role.TESTER) == 1
        assert calls.get(Role.REVIEWER) == 1
        assert results[0] is prior_impl
        assert [r.role for r in results] == [Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER]

    def test_resume_skips_implementor_and_tester(self, pipeline_task, pipeline_worktree, tmp_path):
        calls: dict[Role, int] = {}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            calls[directive.role] = calls.get(directive.role, 0) + 1
            return _result(directive.role, passed=True)

        prior_impl = _result(Role.IMPLEMENTOR, passed=True)
        prior_test = _result(Role.TESTER, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    resume_completed_stages=["implementor", "tester"],
                    prior_results=[prior_impl, prior_test],
                )
            )

        assert Role.IMPLEMENTOR not in calls
        assert Role.TESTER not in calls
        assert calls.get(Role.REVIEWER) == 1
        assert [r.role for r in results] == [Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER]

    def test_resume_empty_runs_full_pipeline(self, pipeline_task, pipeline_worktree, tmp_path):
        calls: dict[Role, int] = {}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            calls[directive.role] = calls.get(directive.role, 0) + 1
            return _result(directive.role, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    resume_completed_stages=[],
                    prior_results=[],
                )
            )

        assert calls.get(Role.IMPLEMENTOR) == 1
        assert calls.get(Role.TESTER) == 1
        assert calls.get(Role.REVIEWER) == 1
        assert len(results) == 3

    def test_resume_none_runs_full_pipeline(self, pipeline_task, pipeline_worktree, tmp_path):
        calls: dict[Role, int] = {}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            calls[directive.role] = calls.get(directive.role, 0) + 1
            return _result(directive.role, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    resume_completed_stages=None,
                    prior_results=None,
                )
            )

        assert calls.get(Role.IMPLEMENTOR) == 1
        assert calls.get(Role.TESTER) == 1
        assert calls.get(Role.REVIEWER) == 1
        assert len(results) == 3

    def test_resume_tdd_skips_completed_phases(self, pipeline_task, pipeline_worktree, tmp_path):
        from workbench.directives import (
            ReviewerDirective,
            TddImplementorDirective,
            TddTesterDirective,
            TesterDirective,
        )

        seen_directive_types: list[type] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            seen_directive_types.append(type(directive))
            return _result(directive.role, passed=True)

        # Distinguish prior results from freshly-produced ones. AgentResult is
        # a value-equality dataclass, so `in` would match any equal result.
        prior_tdd_test = AgentResult(
            task_id="task-1",
            role=Role.TESTER,
            status=TaskStatus.DONE,
            output="PRIOR_TDD_TEST",
            step="tdd-test",
        )
        prior_tdd_impl = AgentResult(
            task_id="task-1",
            role=Role.IMPLEMENTOR,
            status=TaskStatus.DONE,
            output="PRIOR_TDD_IMPLEMENT",
            step="tdd-implement",
        )

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    tdd=True,
                    resume_completed_stages=["tdd-test", "tdd-implement"],
                    prior_results=[prior_tdd_test, prior_tdd_impl],
                )
            )

        # Both TDD phases were already completed on a prior run — they must
        # not be re-run, and verification test/review must still happen.
        assert TddTesterDirective not in seen_directive_types
        assert TddImplementorDirective not in seen_directive_types
        assert TesterDirective in seen_directive_types
        assert ReviewerDirective in seen_directive_types
        assert any(r is prior_tdd_test for r in results)
        assert any(r is prior_tdd_impl for r in results)

    def test_resume_skip_test_and_completed_test_both_skip(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        calls: dict[Role, int] = {}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            calls[directive.role] = calls.get(directive.role, 0) + 1
            return _result(directive.role, passed=True)

        prior_impl = _result(Role.IMPLEMENTOR, passed=True)
        prior_test = _result(Role.TESTER, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    skip_test=True,
                    resume_completed_stages=["implementor", "tester"],
                    prior_results=[prior_impl, prior_test],
                )
            )

        assert Role.TESTER not in calls
        assert Role.IMPLEMENTOR not in calls
        assert calls.get(Role.REVIEWER) == 1

    def test_resume_streams_only_new_results_to_on_result(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        async def mock_run_agent(directive, ctx, *args, **kwargs):
            return _result(directive.role, passed=True)

        prior_impl = _result(Role.IMPLEMENTOR, passed=True)
        prior_test = _result(Role.TESTER, passed=True)
        collected: list[AgentResult] = []

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            results = asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    on_result=collected.append,
                    resume_completed_stages=["implementor", "tester"],
                    prior_results=[prior_impl, prior_test],
                )
            )

        assert [r.role for r in collected] == [Role.REVIEWER]
        assert [r.role for r in results] == [Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER]


# ---------------------------------------------------------------------------
# Session metadata wiring (env injection + in-prompt block)
# ---------------------------------------------------------------------------


from workbench.adapters import AgentConfig, ConfigAdapter  # noqa: E402
from workbench.session_metadata import SessionMetadata  # noqa: E402


class TestSessionMetadataWiring:
    def test_run_agent_env_injected_for_builtin_adapter(self, sample_ctx, tmp_path):
        """trace_env=True + inject_env=True adapter → run_in_tmux gets WB_* env."""
        directive = ImplementorDirective()
        meta = SessionMetadata(plan="p", wave=1, task="task-1", task_title="t")

        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
        ) as mock_tmux:
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=True,
                    meta=meta,
                    trace_env=True,
                )
            )

        _args, kwargs = mock_tmux.call_args
        env = kwargs["env"]
        assert env is not None
        assert env["WB_PLAN"] == "p"
        assert env["WB_TASK"] == "task-1"
        assert env["WB_AGENT"] == Role.IMPLEMENTOR.value
        assert "OTEL_RESOURCE_ATTRIBUTES" in env

    def test_run_agent_trace_prompt_prepends_block(self, sample_ctx, tmp_path):
        """trace_prompt=True → build_command receives prompt starting with the block."""
        directive = ImplementorDirective()
        meta = SessionMetadata(plan="p", wave=1, task="task-1", task_title="t")

        adapter = ClaudeAdapter()
        captured: dict[str, str] = {}
        original_build = adapter.build_command

        def spy_build(prompt, cwd, model=None):
            captured["prompt"] = prompt
            return original_build(prompt, cwd, model)

        adapter.build_command = spy_build  # type: ignore[assignment]

        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
        ):
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    adapter=adapter,
                    use_tmux=True,
                    meta=meta,
                    trace_prompt=True,
                )
            )

        assert captured["prompt"].splitlines()[0] == "```wb-session"

    def test_run_agent_trace_env_false_passes_none(self, sample_ctx, tmp_path):
        """trace_env=False → env=None to run_in_tmux even when meta is set."""
        directive = ImplementorDirective()
        meta = SessionMetadata(plan="p", task="task-1")

        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
        ) as mock_tmux:
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    use_tmux=True,
                    meta=meta,
                    trace_env=False,
                )
            )

        _args, kwargs = mock_tmux.call_args
        assert kwargs["env"] is None

    def test_run_agent_headroom_applies_when_trace_env_false(self, sample_ctx, tmp_path):
        """headroom env overlay applies independently of trace env injection."""
        directive = ImplementorDirective()
        meta = SessionMetadata(plan="p", task="task-1")

        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
        ) as mock_tmux:
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=True,
                    meta=meta,
                    trace_env=False,
                    headroom=HeadroomConfig(enabled=True, port=9999),
                )
            )

        _args, kwargs = mock_tmux.call_args
        env = kwargs["env"]
        assert env is not None
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"

    def test_run_agent_headroom_none_keeps_existing_env_behavior(self, sample_ctx, tmp_path):
        """headroom=None leaves env unchanged from the existing trace_env=False path."""
        directive = ImplementorDirective()

        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(0, json.dumps({"result": "ok", "cost_usd": {}})),
        ) as mock_tmux:
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    agent_cmd="claude",
                    use_tmux=True,
                    trace_env=False,
                    headroom=None,
                )
            )

        _args, kwargs = mock_tmux.call_args
        assert kwargs["env"] is None

    def test_run_agent_inject_env_false_skips_env(self, sample_ctx, tmp_path):
        """Adapter.config.inject_env=False → env=None even when trace_env=True."""
        directive = ImplementorDirective()
        meta = SessionMetadata(plan="p", task="task-1")
        config = AgentConfig(command="claude", args=["{prompt}"], inject_env=False)
        adapter = ConfigAdapter(name="claude", config=config)

        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(0, "ok"),
        ) as mock_tmux:
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    adapter=adapter,
                    use_tmux=True,
                    meta=meta,
                    trace_env=True,
                )
            )

        _args, kwargs = mock_tmux.call_args
        assert kwargs["env"] is None

    def test_run_pipeline_threads_step_and_agent_to_run_agent(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        """Verify the meta.step + meta.agent sequence for impl → test#1 fail → fix → test#2 → review."""
        tester_calls = {"n": 0}
        captured: list[tuple[str, str]] = []  # (agent, step)

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            role = directive.role
            meta = kwargs.get("meta")
            # Mirror real run_agent: stamp agent from directive.role.
            effective_agent = role.value if meta is not None else ""
            effective_step = meta.step if meta is not None else ""
            captured.append((effective_agent, effective_step))

            if role == Role.TESTER:
                tester_calls["n"] += 1
                return _result(role, passed=tester_calls["n"] >= 2)
            if role == Role.FIXER:
                return _done(role)
            return _result(role, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    plan_name="myplan",
                    wave_num=2,
                    max_retries=1,
                )
            )

        steps = [step for _agent, step in captured]
        assert steps == ["implement", "test#1", "test-fix#1", "test#2", "review#1"]

        agent_by_step = {step: agent for agent, step in captured}
        assert agent_by_step["implement"] == Role.IMPLEMENTOR.value
        assert agent_by_step["test#1"] == Role.TESTER.value
        assert agent_by_step["test-fix#1"] == Role.FIXER.value


# ---------------------------------------------------------------------------
# Model resolution + wiring
# ---------------------------------------------------------------------------


from workbench.agents import resolve_model  # noqa: E402
from workbench.profile import Profile, RoleConfig  # noqa: E402


class TestResolveModel:
    def test_cli_agent_key_beats_profile(self):
        profile = Profile.default()
        profile.implementor = RoleConfig(agent="claude", model="profile-model")
        assert (
            resolve_model(
                role="implementor",
                agent="claude",
                cli_models={"claude": "cli-model"},
                profile=profile,
                plan_models={"claude": "plan-model"},
            )
            == "cli-model"
        )

    def test_cli_empty_key_used_when_no_agent_key(self):
        assert (
            resolve_model(
                role="implementor",
                agent="claude",
                cli_models={"": "shared-model"},
                profile=None,
                plan_models=None,
            )
            == "shared-model"
        )

    def test_cli_agent_key_preferred_over_empty_key(self):
        assert (
            resolve_model(
                role="implementor",
                agent="codex",
                cli_models={"": "shared", "codex": "specific"},
                profile=None,
                plan_models=None,
            )
            == "specific"
        )

    def test_profile_beats_plan(self):
        profile = Profile.default()
        profile.tester = RoleConfig(agent="claude", model="profile-model")
        assert (
            resolve_model(
                role="tester",
                agent="claude",
                cli_models=None,
                profile=profile,
                plan_models={"claude": "plan-model"},
            )
            == "profile-model"
        )

    def test_plan_agent_key(self):
        assert (
            resolve_model(
                role="reviewer",
                agent="codex",
                cli_models=None,
                profile=None,
                plan_models={"codex": "plan-codex"},
            )
            == "plan-codex"
        )

    def test_plan_empty_key_fallback(self):
        assert (
            resolve_model(
                role="reviewer",
                agent="codex",
                cli_models=None,
                profile=None,
                plan_models={"": "plan-any"},
            )
            == "plan-any"
        )

    def test_returns_none_when_nothing_set(self):
        assert (
            resolve_model(
                role="implementor",
                agent="claude",
                cli_models=None,
                profile=Profile.default(),
                plan_models=None,
            )
            is None
        )

    def test_empty_profile_model_is_skipped(self):
        profile = Profile.default()
        profile.implementor = RoleConfig(agent="claude", model="")
        assert (
            resolve_model(
                role="implementor",
                agent="claude",
                cli_models=None,
                profile=profile,
                plan_models={"claude": "plan-model"},
            )
            == "plan-model"
        )

    def test_empty_dicts_treated_as_unset(self):
        assert (
            resolve_model(
                role="implementor",
                agent="claude",
                cli_models={},
                profile=None,
                plan_models={},
            )
            is None
        )


class TestRunAgentPassesModel:
    def test_model_threaded_to_build_command(self, sample_ctx, tmp_path):
        """run_agent passes model to adapter.build_command as third positional arg."""
        directive = ImplementorDirective()
        adapter = MagicMock()
        adapter.config = MagicMock(inject_env=False)
        adapter.build_command.return_value = ["echo", "hi"]
        adapter.parse_output.return_value = ("ok", {})

        with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock, return_value=(0, "ok")):
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    adapter=adapter,
                    use_tmux=True,
                    model="claude-opus-4-8",
                )
            )

        args, _kwargs = adapter.build_command.call_args
        assert args[2] == "claude-opus-4-8"

    def test_model_none_threaded_to_build_command(self, sample_ctx, tmp_path):
        """When model is not provided, build_command receives None."""
        directive = ImplementorDirective()
        adapter = MagicMock()
        adapter.config = MagicMock(inject_env=False)
        adapter.build_command.return_value = ["echo", "hi"]
        adapter.parse_output.return_value = ("ok", {})

        with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock, return_value=(0, "ok")):
            asyncio.run(
                run_agent(
                    directive,
                    sample_ctx,
                    repo=tmp_path,
                    adapter=adapter,
                    use_tmux=True,
                )
            )

        args, _kwargs = adapter.build_command.call_args
        assert args[2] is None


class TestRunPipelineModelResolution:
    def test_pipeline_resolves_per_role_models(self, pipeline_task, pipeline_worktree, tmp_path):
        """Each stage in the pipeline gets the model resolved for its role."""
        captured: list[tuple[Role, str | None]] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append((directive.role, kwargs.get("model")))
            return _result(directive.role, passed=True)

        profile = Profile.default()
        profile.tester = RoleConfig(agent="claude", model="profile-tester-model")

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                    cli_models={"claude": "cli-impl-model"},
                )
            )

        by_role = dict(captured)
        # CLI wins for all roles using the claude agent.
        assert by_role[Role.IMPLEMENTOR] == "cli-impl-model"
        # Tester also resolves to CLI since CLI has agent-key precedence.
        assert by_role[Role.TESTER] == "cli-impl-model"
        assert by_role[Role.REVIEWER] == "cli-impl-model"

    def test_pipeline_falls_back_to_profile_model(
        self, pipeline_task, pipeline_worktree, tmp_path
    ):
        """Without CLI models, profile.<role>.model is used."""
        captured: dict[Role, str | None] = {}

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured[directive.role] = kwargs.get("model")
            return _result(directive.role, passed=True)

        profile = Profile.default()
        profile.tester = RoleConfig(agent="claude", model="tester-model")
        profile.reviewer = RoleConfig(agent="claude", model="reviewer-model")

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                    profile=profile,
                )
            )

        assert captured[Role.IMPLEMENTOR] is None
        assert captured[Role.TESTER] == "tester-model"
        assert captured[Role.REVIEWER] == "reviewer-model"

    def test_pipeline_default_no_model(self, pipeline_task, pipeline_worktree, tmp_path):
        """No model configured anywhere → model=None for every stage."""
        captured: list[str | None] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            captured.append(kwargs.get("model"))
            return _result(directive.role, passed=True)

        with (
            patch("workbench.agents.run_agent", side_effect=mock_run_agent),
            patch("workbench.agents.get_main_branch", return_value="main"),
            patch("workbench.agents.get_head_sha", return_value="abc"),
        ):
            asyncio.run(
                run_pipeline(
                    task=pipeline_task,
                    worktree=pipeline_worktree,
                    repo=tmp_path,
                    use_tmux=False,
                )
            )

        assert captured == [None, None, None]


class TestRunPlannerAndMergeResolverModel:
    def test_run_planner_passes_model(self, tmp_path):
        adapter = MagicMock()
        adapter.config = MagicMock(inject_env=False)
        adapter.build_command.return_value = ["echo", "hi"]
        adapter.parse_output.return_value = ("ok", {})

        with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock, return_value=(0, "ok")):
            asyncio.run(
                run_planner(
                    repo=tmp_path,
                    user_prompt="build a thing",
                    plan_name="myplan",
                    adapter=adapter,
                    use_tmux=True,
                    model="planner-model",
                )
            )

        args, _kwargs = adapter.build_command.call_args
        assert args[2] == "planner-model"

    def test_run_merge_resolver_passes_model(self, tmp_path):
        adapter = MagicMock()
        adapter.config = MagicMock(inject_env=False)
        adapter.build_command.return_value = ["echo", "hi"]
        adapter.parse_output.return_value = ("ok", {})

        with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock, return_value=(0, "ok")):
            asyncio.run(
                run_merge_resolver(
                    task_branch="wb/task-1",
                    session_branch="wb/session",
                    merge_dir=tmp_path,
                    conflicts=["src/foo.py"],
                    repo=tmp_path,
                    adapter=adapter,
                    use_tmux=True,
                    model="merger-model",
                )
            )

        args, _kwargs = adapter.build_command.call_args
        assert args[2] == "merger-model"
