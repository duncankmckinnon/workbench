"""Tests for orchestrator module."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workbench.agents import AgentResult, Role, TaskStatus
from workbench.orchestrator import merge_unmerged, run_plan
from workbench.session_status import SessionStatus
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


# ---------------------------------------------------------------------------
# --retry-failed
# ---------------------------------------------------------------------------


def _make_two_task_plan() -> Plan:
    """Plan with two independent tasks (same wave)."""
    return _make_plan(
        title="Two Tasks",
        tasks=[
            Task(id="task-1", title="Good Task", description="succeeds", files=[], depends_on=[]),
            Task(id="task-2", title="Bad Task", description="crashes", files=[], depends_on=[]),
        ],
    )


@pytest.mark.asyncio
async def test_retry_failed_retries_crashed_task(tmp_path):
    """--retry-failed re-runs tasks that crashed (fix_count < max_retries)."""
    plan = _make_two_task_plan()
    repo = tmp_path
    call_count = {"task-2": 0}

    async def fake_pipeline(**kwargs):
        task = kwargs["task"]
        if task.id == "task-2":
            call_count["task-2"] += 1
            if call_count["task-2"] == 1:
                # First call: simulate agent crash (FAILED status, no fix attempts)
                return [
                    AgentResult(
                        task_id="task-2",
                        role=Role.IMPLEMENTOR,
                        status=TaskStatus.FAILED,
                        output="Agent error: connection timeout",
                    )
                ]
            # Second call (retry): succeed
            return []
        return []  # task-1 always succeeds

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan, repo=repo, use_tmux=False, retry_failed=True,
        )

    # task-2's pipeline should have been called twice (initial + retry)
    assert call_count["task-2"] == 2
    # Both tasks should end up DONE
    assert all(s.status == TaskStatus.DONE for s in results)


@pytest.mark.asyncio
async def test_retry_failed_skips_exhausted_retries(tmp_path):
    """--retry-failed does NOT re-run tasks that exhausted fix cycles."""
    plan = _make_plan()
    repo = tmp_path

    call_count = {"task-1": 0}

    async def fake_pipeline(**kwargs):
        call_count["task-1"] += 1
        # Return results with max_retries worth of fix attempts
        # (fix_count == 2 == max_retries, so not retryable)
        return [
            AgentResult(
                task_id="task-1",
                role=Role.IMPLEMENTOR,
                status=TaskStatus.DONE,
                output="implemented",
            ),
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: FAIL\nTests failed",
                attempt=1,
            ),
            AgentResult(
                task_id="task-1",
                role=Role.FIXER,
                status=TaskStatus.DONE,
                output="fixed",
                attempt=1,
            ),
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: FAIL\nStill failing",
                attempt=2,
            ),
            AgentResult(
                task_id="task-1",
                role=Role.FIXER,
                status=TaskStatus.DONE,
                output="fixed again",
                attempt=2,
            ),
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: FAIL\nStill broken",
                attempt=3,
            ),
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan, repo=repo, use_tmux=False, retry_failed=True, max_retries=2,
        )

    # Pipeline should only be called once (no retry — exhausted retries)
    assert call_count["task-1"] == 1
    # Task should remain FAILED
    assert results[0].status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_retry_failed_disabled_by_default(tmp_path):
    """Without --retry-failed, crashed tasks are NOT retried."""
    plan = _make_plan()
    repo = tmp_path
    call_count = {"task-1": 0}

    async def fake_pipeline(**kwargs):
        call_count["task-1"] += 1
        return [
            AgentResult(
                task_id="task-1",
                role=Role.IMPLEMENTOR,
                status=TaskStatus.FAILED,
                output="Agent error: crash",
            )
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert call_count["task-1"] == 1
    assert results[0].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# --fail-fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_fast_stops_after_first_wave_failure(tmp_path):
    """--fail-fast should not proceed to wave 2 if wave 1 has failures."""
    plan = _make_plan(
        title="Multi-wave",
        tasks=[
            Task(id="task-1", title="Wave1 Task", description="fails", files=[], depends_on=[]),
            Task(
                id="task-2",
                title="Wave2 Task",
                description="depends on task-1",
                files=[],
                depends_on=["task-1"],
            ),
        ],
    )
    repo = tmp_path
    pipeline_calls = []

    async def fake_pipeline(**kwargs):
        task = kwargs["task"]
        pipeline_calls.append(task.id)
        if task.id == "task-1":
            return [
                AgentResult(
                    task_id="task-1",
                    role=Role.IMPLEMENTOR,
                    status=TaskStatus.FAILED,
                    output="Agent crash",
                )
            ]
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(plan=plan, repo=repo, use_tmux=False, fail_fast=True)

    # Only wave 1 task should have run
    assert pipeline_calls == ["task-1"]
    # Wave 2 task should not appear in results (wave was never entered)
    wave2_ids = [s.task.id for s in results if s.task.id == "task-2"]
    assert wave2_ids == []
    # Only wave 1 results returned
    assert len(results) == 1
    assert results[0].status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_fail_fast_allows_full_run_on_success(tmp_path):
    """--fail-fast should not interfere when all tasks succeed."""
    plan = _make_plan(
        title="Multi-wave",
        tasks=[
            Task(id="task-1", title="Wave1 Task", description="ok", files=[], depends_on=[]),
            Task(
                id="task-2",
                title="Wave2 Task",
                description="ok too",
                files=[],
                depends_on=["task-1"],
            ),
        ],
    )
    repo = tmp_path
    pipeline_calls = []

    async def fake_pipeline(**kwargs):
        pipeline_calls.append(kwargs["task"].id)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(plan=plan, repo=repo, use_tmux=False, fail_fast=True)

    assert sorted(pipeline_calls) == ["task-1", "task-2"]
    assert all(s.status == TaskStatus.DONE for s in results)


# ---------------------------------------------------------------------------
# --only-failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_failed_skips_completed_tasks(tmp_path):
    """--only-failed skips tasks recorded as 'done' in status.json."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)
    pipeline_calls = []

    # Pre-seed status.json with task-1 completed
    prior = SessionStatus(session_branch="workbench-1")
    prior.record_task("task-1", status="done", branch="wb/good-task", merged=True)
    prior.save(repo)

    async def fake_pipeline(**kwargs):
        pipeline_calls.append(kwargs["task"].id)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/bad-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            only_failed=True,
        )

    # Only task-2 (Bad Task) should have gone through the pipeline
    assert pipeline_calls == ["task-2"]
    # task-1 should be DONE (pre-skipped), task-2 should be DONE (ran successfully)
    assert all(s.status == TaskStatus.DONE for s in results)


@pytest.mark.asyncio
async def test_only_failed_runs_all_when_no_status(tmp_path):
    """--only-failed with no prior status.json runs everything."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)
    pipeline_calls = []

    async def fake_pipeline(**kwargs):
        pipeline_calls.append(kwargs["task"].id)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            only_failed=True,
        )

    assert sorted(pipeline_calls) == ["task-1", "task-2"]


@pytest.mark.asyncio
async def test_only_failed_ignores_different_session(tmp_path):
    """--only-failed ignores status.json from a different session branch."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)
    pipeline_calls = []

    # Status from a different session
    prior = SessionStatus(session_branch="workbench-old")
    prior.record_task("task-1", status="done", branch="wb/good-task", merged=True)
    prior.save(repo)

    async def fake_pipeline(**kwargs):
        pipeline_calls.append(kwargs["task"].id)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            only_failed=True,
        )

    # All tasks should run since the status was from a different session
    assert sorted(pipeline_calls) == ["task-1", "task-2"]


# ---------------------------------------------------------------------------
# Combined flags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_failed_with_fail_fast(tmp_path):
    """--retry-failed + --fail-fast: retry first, then fail-fast if still failing."""
    plan = _make_plan()
    repo = tmp_path
    call_count = {"task-1": 0}

    async def fake_pipeline(**kwargs):
        call_count["task-1"] += 1
        # Always crash (transient-style failure, no fix attempts)
        return [
            AgentResult(
                task_id="task-1",
                role=Role.IMPLEMENTOR,
                status=TaskStatus.FAILED,
                output="Agent error: timeout",
            )
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan, repo=repo, use_tmux=False, retry_failed=True, fail_fast=True,
        )

    # Should have been called twice (initial + one retry)
    assert call_count["task-1"] == 2
    # Still failed after retry
    assert results[0].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Status persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_json_written_after_task(tmp_path):
    """status.json should be written after each task completes."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    # status.json should exist and contain task-1 as done+merged
    status = SessionStatus.load(repo)
    assert status is not None
    assert status.session_branch == "workbench-1"
    assert "task-1" in status.tasks
    assert status.tasks["task-1"].status == "done"
    assert status.tasks["task-1"].merged is True


@pytest.mark.asyncio
async def test_status_json_records_failed_task(tmp_path):
    """Failed tasks should be recorded in status.json."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()

    async def fake_pipeline(**kwargs):
        return [
            AgentResult(
                task_id="task-1",
                role=Role.IMPLEMENTOR,
                status=TaskStatus.FAILED,
                output="Agent crash",
            )
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    status = SessionStatus.load(repo)
    assert status is not None
    assert status.tasks["task-1"].status == "failed"
    assert status.tasks["task-1"].merged is False
    assert status.tasks["task-1"].last_agent == "implementor"


@pytest.mark.asyncio
async def test_status_json_marks_merged_after_merge(tmp_path):
    """Tasks should be marked merged=True after successful merge."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_main_branch", return_value="main"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    status = SessionStatus.load(repo)
    assert status.tasks["task-1"].merged is True
    assert status.tasks["task-2"].merged is True


# ---------------------------------------------------------------------------
# merge_unmerged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_unmerged_merges_done_unmerged(tmp_path):
    """merge_unmerged should merge tasks with status=done, merged=False."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(session_branch="workbench-1")
    status.record_task("task-1", status="done", branch="wb/feat-a")
    status.record_task("task-2", status="done", branch="wb/feat-b")
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_merged_branches", return_value={"main", "workbench-1"}),
    ):
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        result = await merge_unmerged(repo=repo, session_branch="workbench-1", use_tmux=False)

    assert mock_merge.call_count == 2
    assert result.tasks["task-1"].merged is True
    assert result.tasks["task-2"].merged is True


@pytest.mark.asyncio
async def test_merge_unmerged_skips_already_merged(tmp_path):
    """merge_unmerged should skip tasks already merged and update status."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(session_branch="workbench-1")
    status.record_task("task-1", status="done", branch="wb/feat-a")
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch(
            "workbench.orchestrator.get_merged_branches",
            return_value={"main", "workbench-1", "wb/feat-a"},
        ),
    ):
        result = await merge_unmerged(repo=repo, session_branch="workbench-1", use_tmux=False)

    # Should NOT attempt merge — branch was already merged
    mock_merge.assert_not_called()
    # But should update status to merged=True
    assert result.tasks["task-1"].merged is True


@pytest.mark.asyncio
async def test_merge_unmerged_skips_failed_tasks(tmp_path):
    """merge_unmerged should not attempt to merge tasks with status=failed."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(session_branch="workbench-1")
    status.record_task("task-1", status="failed", branch="wb/feat-a")
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_merged_branches", return_value={"main", "workbench-1"}),
    ):
        result = await merge_unmerged(repo=repo, session_branch="workbench-1", use_tmux=False)

    mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_merge_unmerged_skips_already_merged_in_status(tmp_path):
    """merge_unmerged should skip tasks already marked merged=True in status."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(session_branch="workbench-1")
    status.record_task("task-1", status="done", branch="wb/feat-a", merged=True)
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_merged_branches", return_value={"main", "workbench-1"}),
    ):
        result = await merge_unmerged(repo=repo, session_branch="workbench-1", use_tmux=False)

    mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_merge_unmerged_no_status_file(tmp_path):
    """merge_unmerged with no status.json should return empty status."""
    repo = tmp_path
    result = await merge_unmerged(repo=repo, session_branch="workbench-1", use_tmux=False)
    assert len(result.tasks) == 0


@pytest.mark.asyncio
async def test_merge_unmerged_wrong_session(tmp_path):
    """merge_unmerged should reject status from a different session branch."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(session_branch="workbench-old")
    status.record_task("task-1", status="done", branch="wb/feat-a")
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
    ):
        result = await merge_unmerged(repo=repo, session_branch="workbench-1", use_tmux=False)

    mock_merge.assert_not_called()
    assert result.session_branch == "workbench-old"


@pytest.mark.asyncio
async def test_merge_unmerged_persists_status(tmp_path):
    """merge_unmerged should persist merged status to disk."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(session_branch="workbench-1")
    status.record_task("task-1", status="done", branch="wb/feat-a")
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_merged_branches", return_value={"main", "workbench-1"}),
    ):
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)
        await merge_unmerged(repo=repo, session_branch="workbench-1", use_tmux=False)

    # Reload from disk and verify
    reloaded = SessionStatus.load(repo)
    assert reloaded.tasks["task-1"].merged is True
