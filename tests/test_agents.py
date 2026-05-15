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
from workbench.plan_parser import Task
from workbench.worktree import Worktree


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

    def test_resume_tdd_dropped(self, pipeline_task, pipeline_worktree, tmp_path):
        from workbench.directives import TddTesterDirective

        seen_directive_types: list[type] = []

        async def mock_run_agent(directive, ctx, *args, **kwargs):
            seen_directive_types.append(type(directive))
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
                    tdd=True,
                    resume_completed_stages=["implementor"],
                    prior_results=[prior_impl],
                )
            )

        assert TddTesterDirective in seen_directive_types
        assert prior_impl not in results
        assert len(results) > 0
        assert results[0] is not prior_impl

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
