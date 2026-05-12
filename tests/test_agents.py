"""Tests for the agents module — spawning, prompts, and result parsing."""

import asyncio
import json
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
