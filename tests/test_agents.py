"""Tests for the agents module — spawning, prompts, and result parsing."""

import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from workbench.agents import AgentResult, Role, TaskStatus, build_prompt, run_agent
from workbench.plan_parser import Task
from workbench.worktree import Worktree


@pytest.fixture
def sample_task():
    return Task(id="task-1", title="Test Task", description="Do something", files=["src/foo.py"])


@pytest.fixture
def sample_worktree(tmp_path):
    return Worktree(path=tmp_path, branch="wb/test-task", task_id="task-1")


class TestRunAgentTmux:
    def test_run_agent_success_tmux(self, sample_task, sample_worktree, tmp_path):
        """Mock run_in_tmux returning (0, json), verify AgentResult.status == DONE."""
        output = json.dumps({"result": "all good", "cost_usd": {"input": 0.01}})
        with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock, return_value=(0, output)), \
             patch("workbench.agents.get_main_branch", return_value="main"), \
             patch("workbench.agents.get_diff", return_value=""):
            result = asyncio.run(run_agent(
                role=Role.IMPLEMENTOR,
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                agent_cmd="claude",
                use_tmux=True,
            ))

        assert result.status == TaskStatus.DONE
        assert result.task_id == "task-1"

    def test_run_agent_failure_tmux(self, sample_task, sample_worktree, tmp_path):
        """Mock returning (1, "error"), verify FAILED."""
        with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock, return_value=(1, "error")), \
             patch("workbench.agents.get_main_branch", return_value="main"), \
             patch("workbench.agents.get_diff", return_value=""):
            result = asyncio.run(run_agent(
                role=Role.IMPLEMENTOR,
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                agent_cmd="claude",
                use_tmux=True,
            ))

        assert result.status == TaskStatus.FAILED


class TestRunAgentSubprocess:
    def test_run_agent_no_tmux(self, sample_task, sample_worktree, tmp_path):
        """use_tmux=False, mock create_subprocess_exec, verify it's called."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"done", b"")
        mock_proc.returncode = 0

        with patch("workbench.agents.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec, \
             patch("workbench.agents.get_main_branch", return_value="main"), \
             patch("workbench.agents.get_diff", return_value=""):
            result = asyncio.run(run_agent(
                role=Role.IMPLEMENTOR,
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                agent_cmd="claude",
                use_tmux=False,
            ))

        mock_exec.assert_called_once()
        assert result.status == TaskStatus.DONE


class TestPromptBuilding:
    def test_implementor_prompt_has_branch(self, sample_task, sample_worktree):
        """Capture the prompt passed to adapter, verify branch name present."""
        with patch("workbench.agents.get_diff", return_value=""):
            prompt = build_prompt(
                role=Role.IMPLEMENTOR,
                task=sample_task,
                worktree=sample_worktree,
                base_branch="main",
            )
        assert "wb/test-task" in prompt

    def test_fixer_prompt_has_branch(self, sample_task, sample_worktree):
        """Same for fixer role."""
        with patch("workbench.agents.get_diff", return_value="some diff"):
            prompt = build_prompt(
                role=Role.FIXER,
                task=sample_task,
                worktree=sample_worktree,
                base_branch="main",
            )
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
