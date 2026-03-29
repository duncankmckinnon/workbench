"""Tests for orchestrator module."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workbench.orchestrator import run_plan
from workbench.plan_parser import Plan, Task


def _make_plan(title: str = "Test Plan", tasks: list[Task] | None = None) -> Plan:
    """Create a minimal plan for testing."""
    if tasks is None:
        tasks = [
            Task(
                id="task-1",
                title="Test Task",
                description="A test task",
                files=["test.py"],
                depends_on=[],
            )
        ]
    return Plan(title=title, tasks=tasks, source=Path("/fake/plan.md"), context="", conventions="")


@pytest.mark.asyncio
async def test_run_plan_tdd_mode(tmp_path):
    """run_plan with tdd=True should pass tdd=True to run_pipeline."""
    plan = _make_plan()
    repo = tmp_path

    captured_kwargs = {}

    async def fake_pipeline(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    with patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"), \
         patch("workbench.orchestrator.create_worktree") as mock_wt, \
         patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline), \
         patch("workbench.orchestrator.merge_into_session") as mock_merge, \
         patch("workbench.orchestrator.get_main_branch", return_value="main"):

        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            tdd=True,
            use_tmux=False,
        )

    assert captured_kwargs.get("tdd") is True


@pytest.mark.asyncio
async def test_run_plan_tdd_false_by_default(tmp_path):
    """run_plan without tdd should pass tdd=False to run_pipeline."""
    plan = _make_plan()
    repo = tmp_path

    captured_kwargs = {}

    async def fake_pipeline(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    with patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"), \
         patch("workbench.orchestrator.create_worktree") as mock_wt, \
         patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline), \
         patch("workbench.orchestrator.merge_into_session") as mock_merge, \
         patch("workbench.orchestrator.get_main_branch", return_value="main"):

        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
        )

    assert captured_kwargs.get("tdd") is False
