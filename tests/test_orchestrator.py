"""Tests for orchestrator module."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workbench.orchestrator import run_plan
from workbench.plan_parser import Plan, Task
from workbench.profile import Profile


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

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):

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

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):

        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
        )

    assert captured_kwargs.get("tdd") is False


@pytest.mark.asyncio
async def test_run_plan_with_profile_path(tmp_path):
    """run_plan with profile_path should call Profile.resolve and pass profile to run_pipeline."""
    plan = _make_plan()
    repo = tmp_path

    # Create a dummy profile YAML file
    profile_path = tmp_path / "custom_profile.yaml"
    profile_path.write_text("roles:\n  reviewer:\n    agent: gemini\n")

    captured_kwargs = {}

    async def fake_pipeline(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    fake_profile = Profile.default()
    fake_profile.reviewer.agent = "gemini"

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
        patch("workbench.orchestrator.Profile.resolve", return_value=fake_profile) as mock_resolve,
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            profile_path=profile_path,
        )

    # Profile.resolve should have been called with the repo and profile_path
    mock_resolve.assert_called_once_with(repo, profile_path=profile_path, profile_name=None)
    # The resolved profile should be passed to run_pipeline
    assert captured_kwargs.get("profile") is fake_profile


@pytest.mark.asyncio
async def test_run_plan_deletes_branches_after_merge(tmp_path):
    """By default, task branches are deleted after successful merge."""
    plan = _make_plan()
    repo = tmp_path

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch") as mock_delete,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    mock_delete.assert_called_once_with(repo, "wb/task-1-test-task")


@pytest.mark.asyncio
async def test_run_plan_keeps_branches_when_flag_set(tmp_path):
    """With keep_branches=True, task branches are preserved after merge."""
    plan = _make_plan()
    repo = tmp_path

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch") as mock_delete,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False, keep_branches=True)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_run_plan_keeps_branches_on_failed_merge(tmp_path):
    """Failed merges should not delete the task branch regardless of keep_branches."""
    plan = _make_plan()
    repo = tmp_path

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch") as mock_delete,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(
            success=False, message="conflict", conflicts=["file.py"], merge_dir=None
        )

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    mock_delete.assert_not_called()
