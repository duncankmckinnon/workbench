"""Tests for orchestrator module."""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workbench.agents import AgentResult, Role, TaskStatus
from workbench.headroom import HeadroomConfig
from workbench.orchestrator import TaskState, merge_unmerged, run_plan
from workbench.plan_parser import Plan, Task, parse_plan
from workbench.profile import Profile
from workbench.session_status import SessionStatus


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
    # folder_id derives from parent dir name; match it to the slug
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return Plan(
        title=title, tasks=tasks, source=Path(f"/{slug}/plan.md"), context="", conventions=""
    )


@pytest.mark.asyncio
async def test_orchestrator_passes_plan_slug_to_create_worktree(tmp_path):
    """create_worktree should be called with plan_slug=plan.folder_id."""
    plan = _make_plan(title="My Feature")
    repo = tmp_path

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert mock_wt.called
    kwargs = mock_wt.call_args.kwargs
    assert kwargs.get("plan_slug") == plan.folder_id == "my-feature"


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
    """run_plan with profile_path should layer the explicit profile and pass it to run_pipeline."""
    plan = _make_plan()
    repo = tmp_path

    # Create a dummy profile YAML file
    profile_path = tmp_path / "custom_profile.yaml"
    profile_path.write_text("roles:\n  reviewer:\n    agent: antigravity\n")

    captured_kwargs = {}

    async def fake_pipeline(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            profile_path=profile_path,
        )

    # The explicit profile should be layered into the resolved profile passed to run_pipeline
    resolved_profile = captured_kwargs.get("profile")
    assert resolved_profile is not None
    assert resolved_profile.reviewer.agent == "antigravity"


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
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    # Post-merge delete should have been called with the worktree's branch name
    mock_delete.assert_any_call(repo, "wb/task-1-test-task")


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
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False, keep_branches=True)

    # Post-merge delete should NOT have been called with the worktree branch
    for call in mock_delete.call_args_list:
        assert call.args[1] != "wb/task-1-test-task", "Branch should be kept after merge"


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
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(
            success=False, message="conflict", conflicts=["file.py"], merge_dir=None
        )

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    # Post-merge delete should NOT have been called with the worktree branch
    for call in mock_delete.call_args_list:
        assert call.args[1] != "wb/task-1-test-task", "Branch should be kept on failed merge"


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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            retry_failed=True,
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            retry_failed=True,
            max_retries=2,
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(plan=plan, repo=repo, use_tmux=False, fail_fast=True)

    assert sorted(pipeline_calls) == ["task-1", "task-2"]
    assert all(s.status == TaskStatus.DONE for s in results)


# ---------------------------------------------------------------------------
# --only-incomplete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_incomplete_skips_completed_tasks(tmp_path):
    """--only-incomplete skips tasks recorded as 'done' in status.json."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)
    pipeline_calls = []

    # Pre-seed status.json with task-1 completed
    prior = SessionStatus(plan_slug="two-tasks", session_branch="workbench-1")
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
            only_incomplete=True,
        )

    # Only task-2 (Bad Task) should have gone through the pipeline
    assert pipeline_calls == ["task-2"]
    # task-1 should be DONE (pre-skipped), task-2 should be DONE (ran successfully)
    assert all(s.status == TaskStatus.DONE for s in results)


@pytest.mark.asyncio
async def test_only_incomplete_runs_all_when_no_status(tmp_path):
    """--only-incomplete with no prior status.json runs everything."""
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
            only_incomplete=True,
        )

    assert sorted(pipeline_calls) == ["task-1", "task-2"]


@pytest.mark.asyncio
async def test_only_incomplete_ignores_different_session(tmp_path):
    """--only-incomplete ignores status.json from a different session branch."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)
    pipeline_calls = []

    # Status from a different session
    prior = SessionStatus(plan_slug="two-tasks", session_branch="workbench-old")
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
            only_incomplete=True,
        )

    # All tasks should run since the status was from a different session
    assert sorted(pipeline_calls) == ["task-1", "task-2"]


# ---------------------------------------------------------------------------
# --end-wave
# ---------------------------------------------------------------------------


def _make_three_wave_plan() -> Plan:
    """Plan with 3 sequential waves (each task depends on the previous)."""
    return _make_plan(
        title="Three Waves",
        tasks=[
            Task(id="task-1", title="Wave1", description="first", files=[], depends_on=[]),
            Task(
                id="task-2", title="Wave2", description="second", files=[], depends_on=["task-1"]
            ),
            Task(id="task-3", title="Wave3", description="third", files=[], depends_on=["task-2"]),
        ],
    )


@pytest.mark.asyncio
async def test_end_wave_stops_after_specified_wave(tmp_path):
    """end_wave=1 should only run wave 1 and skip waves 2 and 3."""
    plan = _make_three_wave_plan()
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(plan=plan, repo=repo, use_tmux=False, end_wave=1)

    # Only wave 1 task should have run
    assert pipeline_calls == ["task-1"]
    assert len(results) == 1
    assert results[0].status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_end_wave_none_runs_all_waves(tmp_path):
    """end_wave=None (default) should run all waves."""
    plan = _make_three_wave_plan()
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert sorted(pipeline_calls) == ["task-1", "task-2", "task-3"]
    assert all(s.status == TaskStatus.DONE for s in results)


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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            retry_failed=True,
            fail_fast=True,
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    # status.json should exist and contain task-1 as done+merged
    status = SessionStatus.load(repo, "test-plan", "workbench-1")
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    status = SessionStatus.load(repo, "test-plan", "workbench-1")
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    status = SessionStatus.load(repo, "two-tasks", "workbench-1")
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

    status = SessionStatus(plan_slug="two-tasks", session_branch="workbench-1")
    status.record_task("task-1", status="done", branch="wb/feat-a")
    status.record_task("task-2", status="done", branch="wb/feat-b")
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_merged_branches", return_value={"main", "workbench-1"}),
    ):
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        result = await merge_unmerged(
            repo=repo, session_branch="workbench-1", plan_slug="two-tasks", use_tmux=False
        )

    assert mock_merge.call_count == 2
    assert result.tasks["task-1"].merged is True
    assert result.tasks["task-2"].merged is True


@pytest.mark.asyncio
async def test_merge_unmerged_skips_already_merged(tmp_path):
    """merge_unmerged should skip tasks already merged and update status."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(plan_slug="two-tasks", session_branch="workbench-1")
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
        result = await merge_unmerged(
            repo=repo, session_branch="workbench-1", plan_slug="two-tasks", use_tmux=False
        )

    # Should NOT attempt merge — branch was already merged
    mock_merge.assert_not_called()
    # But should update status to merged=True
    assert result.tasks["task-1"].merged is True


@pytest.mark.asyncio
async def test_merge_unmerged_skips_failed_tasks(tmp_path):
    """merge_unmerged should not attempt to merge tasks with status=failed."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(plan_slug="two-tasks", session_branch="workbench-1")
    status.record_task("task-1", status="failed", branch="wb/feat-a")
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_merged_branches", return_value={"main", "workbench-1"}),
    ):
        result = await merge_unmerged(
            repo=repo, session_branch="workbench-1", plan_slug="two-tasks", use_tmux=False
        )

    mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_merge_unmerged_skips_already_merged_in_status(tmp_path):
    """merge_unmerged should skip tasks already marked merged=True in status."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(plan_slug="two-tasks", session_branch="workbench-1")
    status.record_task("task-1", status="done", branch="wb/feat-a", merged=True)
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.get_merged_branches", return_value={"main", "workbench-1"}),
    ):
        result = await merge_unmerged(
            repo=repo, session_branch="workbench-1", plan_slug="two-tasks", use_tmux=False
        )

    mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_merge_unmerged_no_status_file(tmp_path):
    """merge_unmerged with no status.json should return empty status."""
    repo = tmp_path
    result = await merge_unmerged(
        repo=repo, session_branch="workbench-1", plan_slug="two-tasks", use_tmux=False
    )
    assert len(result.tasks) == 0


@pytest.mark.asyncio
async def test_merge_unmerged_wrong_session(tmp_path):
    """merge_unmerged with no matching session should return empty status."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    # Status exists for workbench-old, but we request workbench-1
    status = SessionStatus(plan_slug="two-tasks", session_branch="workbench-old")
    status.record_task("task-1", status="done", branch="wb/feat-a")
    status.save(repo)

    with (patch("workbench.orchestrator.merge_into_session") as mock_merge,):
        result = await merge_unmerged(
            repo=repo, session_branch="workbench-1", plan_slug="two-tasks", use_tmux=False
        )

    mock_merge.assert_not_called()
    assert result.session_branch == "workbench-1"
    assert len(result.tasks) == 0


@pytest.mark.asyncio
async def test_merge_unmerged_persists_status(tmp_path):
    """merge_unmerged should persist merged status to disk."""
    repo = tmp_path
    (repo / ".workbench").mkdir()

    status = SessionStatus(plan_slug="two-tasks", session_branch="workbench-1")
    status.record_task("task-1", status="done", branch="wb/feat-a")
    status.save(repo)

    with (
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch("workbench.orchestrator.get_merged_branches", return_value={"main", "workbench-1"}),
    ):
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)
        await merge_unmerged(
            repo=repo, session_branch="workbench-1", plan_slug="two-tasks", use_tmux=False
        )

    # Reload from disk and verify
    reloaded = SessionStatus.load(repo, "two-tasks", "workbench-1")
    assert reloaded.tasks["task-1"].merged is True


# ---------------------------------------------------------------------------
# --task filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_filter_runs_only_matching_tasks(tmp_path):
    """--task should run only the specified task."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/bad-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            task_filter={"task-2"},
        )

    # Only task-2 should have run
    assert pipeline_calls == ["task-2"]
    # Only task-2 should be in results
    assert len(results) == 1
    assert results[0].task.id == "task-2"


@pytest.mark.asyncio
async def test_task_filter_by_slug(tmp_path):
    """--task should accept task slugs as well as IDs."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/good-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            task_filter={"good-task"},
        )

    assert pipeline_calls == ["task-1"]
    assert len(results) == 1
    assert results[0].task.id == "task-1"


@pytest.mark.asyncio
async def test_task_filter_preserves_other_status(tmp_path):
    """--task re-run should not modify status of non-filtered tasks."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()

    # Pre-seed status with task-1 done+merged
    prior = SessionStatus(plan_slug="two-tasks", session_branch="workbench-1")
    prior.record_task(
        "task-1", status="done", branch="wb/good-task", merged=True, last_agent="reviewer"
    )
    prior.save(repo)

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/bad-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            task_filter={"task-2"},
        )

    # task-1's prior status should be preserved
    status = SessionStatus.load(repo, "two-tasks", "workbench-1")
    assert status.tasks["task-1"].status == "done"
    assert status.tasks["task-1"].merged is True
    assert status.tasks["task-1"].last_agent == "reviewer"
    # task-2 should be newly recorded
    assert "task-2" in status.tasks


@pytest.mark.asyncio
async def test_task_filter_none_runs_all(tmp_path):
    """task_filter=None should run all tasks (default behavior)."""
    plan = _make_two_task_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert sorted(pipeline_calls) == ["task-1", "task-2"]
    assert len(results) == 2


@pytest.mark.asyncio
async def test_task_filter_multiple(tmp_path):
    """--task with multiple values should run all specified tasks."""
    plan = _make_plan(
        title="Three Tasks",
        tasks=[
            Task(id="task-1", title="Alpha", description="a", files=[], depends_on=[]),
            Task(id="task-2", title="Beta", description="b", files=[], depends_on=[]),
            Task(id="task-3", title="Gamma", description="c", files=[], depends_on=[]),
        ],
    )
    repo = tmp_path
    (repo / ".workbench").mkdir()
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
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            task_filter={"task-1", "task-3"},
        )

    assert sorted(pipeline_calls) == ["task-1", "task-3"]
    assert len(results) == 2


class TestSessionBranchResolution:
    """session_branch and session_name are aliases — both create-on-missing,
    reuse-if-exists. Without either, the orchestrator auto-numbers a session.
    """

    @pytest.mark.asyncio
    async def test_session_branch_missing_is_created_from_base(self, tmp_path):
        """session_branch set to a non-existent branch → create from base."""
        plan = _make_plan()
        captured = {}

        async def fake_pipeline(**kwargs):
            return []

        with (
            patch("workbench.orchestrator.branch_exists", return_value=False),
            patch("workbench.orchestrator.create_session_branch") as mock_create,
            patch("workbench.orchestrator.create_worktree") as mock_wt,
            patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
            patch("workbench.orchestrator.merge_into_session") as mock_merge,
            patch("workbench.orchestrator.delete_branch"),
        ):
            mock_create.return_value = "bootstrap"
            mock_wt.return_value = MagicMock(branch="wb/test", path=tmp_path / "wt")
            mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

            await run_plan(
                plan=plan,
                repo=tmp_path,
                session_branch="bootstrap",
                base_branch="extension",
                use_tmux=False,
            )

        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["session_name"] == "bootstrap"
        assert kwargs["base"] == "extension"

    @pytest.mark.asyncio
    async def test_session_branch_existing_is_reused(self, tmp_path):
        """session_branch that exists → no create_session_branch call."""
        plan = _make_plan()

        async def fake_pipeline(**kwargs):
            return []

        with (
            patch("workbench.orchestrator.branch_exists", return_value=True),
            patch("workbench.orchestrator.create_session_branch") as mock_create,
            patch("workbench.orchestrator.create_worktree") as mock_wt,
            patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
            patch("workbench.orchestrator.merge_into_session") as mock_merge,
            patch("workbench.orchestrator.delete_branch"),
        ):
            mock_wt.return_value = MagicMock(branch="wb/test", path=tmp_path / "wt")
            mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

            await run_plan(
                plan=plan,
                repo=tmp_path,
                session_branch="bootstrap",
                base_branch="extension",
                use_tmux=False,
            )

        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_name_alias_creates_when_missing(self, tmp_path):
        """session_name alone (no session_branch) → same create-on-missing behavior."""
        plan = _make_plan()

        async def fake_pipeline(**kwargs):
            return []

        with (
            patch("workbench.orchestrator.branch_exists", return_value=False),
            patch("workbench.orchestrator.create_session_branch") as mock_create,
            patch("workbench.orchestrator.create_worktree") as mock_wt,
            patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
            patch("workbench.orchestrator.merge_into_session") as mock_merge,
            patch("workbench.orchestrator.delete_branch"),
        ):
            mock_create.return_value = "bootstrap"
            mock_wt.return_value = MagicMock(branch="wb/test", path=tmp_path / "wt")
            mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

            await run_plan(
                plan=plan,
                repo=tmp_path,
                session_name="bootstrap",
                base_branch="extension",
                use_tmux=False,
            )

        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["session_name"] == "bootstrap"
        assert mock_create.call_args.kwargs["base"] == "extension"

    @pytest.mark.asyncio
    async def test_neither_provided_auto_numbers_session(self, tmp_path):
        """No session_branch / session_name → auto-numbered (session_name=None)."""
        plan = _make_plan()

        async def fake_pipeline(**kwargs):
            return []

        with (
            patch("workbench.orchestrator.branch_exists") as mock_exists,
            patch("workbench.orchestrator.create_session_branch") as mock_create,
            patch("workbench.orchestrator.create_worktree") as mock_wt,
            patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
            patch("workbench.orchestrator.merge_into_session") as mock_merge,
            patch("workbench.orchestrator.delete_branch"),
        ):
            mock_create.return_value = "workbench-1"
            mock_wt.return_value = MagicMock(branch="wb/test", path=tmp_path / "wt")
            mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

            await run_plan(plan=plan, repo=tmp_path, use_tmux=False)

        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["session_name"] is None
        # branch_exists not consulted when nothing was declared
        mock_exists.assert_not_called()


# ---------------------------------------------------------------------------
# Status persistence on handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_persisted_on_each_handoff(tmp_path):
    """Mid-pipeline status transitions should hit status.yaml, not just the final write.

    Today the orchestrator only persists DONE/FAILED after the whole pipeline
    completes; a crash mid-pipeline left the YAML stuck at 'pending'. With the
    fix, every IMPLEMENTING/TESTING/REVIEWING/FIXING/MERGING transition writes
    through, so external observers see the live phase.
    """
    plan = _make_plan()
    repo = tmp_path

    captured_calls: list[dict] = []

    async def fake_update_task(self, repo, task_id, **kwargs):
        captured_calls.append({"task_id": task_id, **kwargs})

    async def fake_pipeline(**kwargs):
        # Simulate a normal implement→test→review pipeline by firing
        # the status callback for each phase.
        cb = kwargs["on_status_change"]
        cb(kwargs["task"].id, TaskStatus.IMPLEMENTING)
        cb(kwargs["task"].id, TaskStatus.TESTING)
        cb(kwargs["task"].id, TaskStatus.REVIEWING)
        return [
            AgentResult(
                task_id=kwargs["task"].id,
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            )
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch.object(SessionStatus, "update_task", new=fake_update_task),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/task-1-test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    # Filter to our task's updates
    task_updates = [c for c in captured_calls if c["task_id"] == "task-1"]
    statuses = [c["status"] for c in task_updates]
    last_agents = [c["last_agent"] for c in task_updates]

    # Mid-pipeline transitions should each have been persisted
    assert "implementing" in statuses
    assert "testing" in statuses
    assert "reviewing" in statuses
    # last_agent should track the role behind each transition
    assert "implementor" in last_agents
    assert "tester" in last_agents
    assert "reviewer" in last_agents
    # Final write (done) follows after the pipeline completes
    assert statuses[-1] == "done"


@pytest.mark.asyncio
async def test_status_persist_failure_does_not_crash_pipeline(tmp_path):
    """A persist failure during a handoff is logged but doesn't kill the run."""
    plan = _make_plan()
    repo = tmp_path

    call_count = {"n": 0}

    async def flaky_update_task(self, repo, task_id, **kwargs):
        call_count["n"] += 1
        # Fail mid-pipeline writes; the final 'done' write succeeds. This
        # tests that mid-pipeline persist failures (which run as fire-and-forget
        # asyncio.Tasks) are absorbed by the _persist_status helper without
        # crashing the pipeline.
        if kwargs.get("status") != "done":
            raise RuntimeError("disk full")

    async def fake_pipeline(**kwargs):
        cb = kwargs["on_status_change"]
        cb(kwargs["task"].id, TaskStatus.IMPLEMENTING)
        cb(kwargs["task"].id, TaskStatus.TESTING)
        return [
            AgentResult(
                task_id=kwargs["task"].id,
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            )
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch.object(SessionStatus, "update_task", new=flaky_update_task),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/task-1-test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        # Should not raise
        await run_plan(plan=plan, repo=repo, use_tmux=False)

    # At least one persist was attempted
    assert call_count["n"] > 0


# ---------------------------------------------------------------------------
# TaskState.phase_summary in-progress entries and new fields
# ---------------------------------------------------------------------------


def _dummy_task(task_id: str = "t1") -> Task:
    return Task(id=task_id, title="T", description="d", files=[], depends_on=[])


def test_phase_summary_in_progress_implementing():
    state = TaskState(task=_dummy_task(), status=TaskStatus.IMPLEMENTING)
    assert state.phase_summary == "impl…"


def test_phase_summary_in_progress_appended_to_completed():
    state = TaskState(task=_dummy_task(), status=TaskStatus.TESTING)
    state.results.append(
        AgentResult(task_id="t1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output="")
    )
    assert state.phase_summary == "impl:ok → test…"


def test_phase_summary_terminal_status_no_suffix():
    state = TaskState(task=_dummy_task(), status=TaskStatus.DONE)
    state.results.append(
        AgentResult(task_id="t1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output="")
    )
    assert state.phase_summary == "impl:ok"


def test_phase_summary_pending_is_empty():
    state = TaskState(task=_dummy_task(), status=TaskStatus.PENDING)
    assert state.phase_summary == ""


def test_taskstate_new_fields_have_safe_defaults():
    state = TaskState(task=_dummy_task())
    assert state.wave_num == 0
    assert state.merged is False
    assert state.merge_error is False


# ---------------------------------------------------------------------------
# _status_table columns
# ---------------------------------------------------------------------------


from types import SimpleNamespace

from workbench.orchestrator import _status_table


def _cell_text(table, col_index: int, row_index: int) -> str:
    cell = list(table.columns[col_index].cells)[row_index]
    return cell.plain if hasattr(cell, "plain") else str(cell)


def test_status_table_columns():
    table = _status_table([])
    assert [col.header for col in table.columns] == [
        "Task",
        "Wave",
        "Status",
        "Branch",
        "Time",
        "Pipeline",
        "Merged",
    ]


def test_status_table_renders_merged_row(tmp_path):
    state = TaskState(
        task=SimpleNamespace(title="t1"),
        wave_num=1,
        merged=True,
        worktree=SimpleNamespace(branch="wb/foo", path=tmp_path / "x", task_id="t1"),
    )
    table = _status_table([state])
    # Branch column (index 3)
    assert "wb/foo" in _cell_text(table, 3, 0)
    # Merged column (index 6)
    assert _cell_text(table, 6, 0) == "✓"


def test_status_table_renders_merge_error_row():
    state = TaskState(
        task=SimpleNamespace(title="t2"),
        wave_num=2,
        merge_error=True,
        worktree=None,
    )
    table = _status_table([state])
    assert _cell_text(table, 6, 0) == "✗"
    assert _cell_text(table, 3, 0) == "-"


@pytest.mark.asyncio
async def test_merge_success_sets_state_merged(tmp_path):
    """A successful merge should flip state.merged on the TaskState."""
    plan = _make_plan()
    repo = tmp_path

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert len(results) == 1
    assert results[0].status == TaskStatus.DONE
    assert results[0].merged is True
    assert results[0].merge_error is False
    assert results[0].wave_num == 1


@pytest.mark.asyncio
async def test_merge_failure_sets_state_merge_error(tmp_path):
    """A failed no-conflict merge should flip state.merge_error on the TaskState."""
    plan = _make_plan()
    repo = tmp_path

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(
            success=False, message="merge refused", conflicts=None, merge_dir=None
        )

        results = await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert len(results) == 1
    assert results[0].status == TaskStatus.FAILED
    assert results[0].merged is False
    assert results[0].merge_error is True


# ---------------------------------------------------------------------------
# Resume from completed stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_failed_resumes_from_completed_stages(tmp_path):
    """When prior run failed mid-pipeline, retry attaches and skips completed stages."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    # Pre-seed prior status: task-1 failed after impl+tester succeeded
    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        last_agent="reviewer",
        completed_stages=["implementor", "tester"],
    )
    prior.save(repo)

    captured = {}

    async def fake_pipeline(**kwargs):
        captured["resume_completed_stages"] = kwargs.get("resume_completed_stages")
        captured["prior_results"] = kwargs.get("prior_results")
        return [
            AgentResult(
                task_id="task-1",
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            )
        ]

    # Fake attach_worktree to bypass branch_exists/path checks
    fake_wt = MagicMock(branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock())

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch("workbench.orchestrator.attach_worktree", return_value=fake_wt) as mock_attach,
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_create.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
        )

    assert captured["resume_completed_stages"] == ["implementor", "tester"]
    assert captured["prior_results"] is None
    mock_attach.assert_called_once()
    mock_create.assert_not_called()

    # Persisted completed_stages should include the new reviewer pass
    status = SessionStatus.load(repo, "test-plan", "workbench-1")
    assert status is not None
    assert "reviewer" in status.tasks["task-1"].completed_stages


@pytest.mark.asyncio
async def test_retry_failed_falls_back_when_branch_missing(tmp_path):
    """If the prior branch no longer exists, fall back to create_worktree."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        completed_stages=["implementor", "tester"],
    )
    prior.save(repo)

    captured = {}

    async def fake_pipeline(**kwargs):
        captured["resume_completed_stages"] = kwargs.get("resume_completed_stages")
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=False),
        patch("workbench.orchestrator.attach_worktree") as mock_attach,
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_create.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
        )

    assert captured["resume_completed_stages"] is None
    mock_attach.assert_not_called()
    assert mock_create.called


@pytest.mark.asyncio
async def test_retry_failed_falls_back_when_worktree_dir_missing(tmp_path):
    """When attach_worktree raises (dir wiped), fall back to create_worktree."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        completed_stages=["implementor", "tester"],
    )
    prior.save(repo)

    captured = {}

    async def fake_pipeline(**kwargs):
        captured["resume_completed_stages"] = kwargs.get("resume_completed_stages")
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch(
            "workbench.orchestrator.attach_worktree",
            side_effect=RuntimeError("path or branch missing"),
        ),
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_create.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
        )

    assert captured["resume_completed_stages"] is None
    assert mock_create.called


@pytest.mark.asyncio
async def test_retry_failed_no_resume_when_completed_stages_empty(tmp_path):
    """Prior failed record with empty completed_stages → full rerun (no resume)."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task("task-1", status="failed", branch="wb/test-task", completed_stages=[])
    prior.save(repo)

    captured = {}

    async def fake_pipeline(**kwargs):
        captured["resume_completed_stages"] = kwargs.get("resume_completed_stages")
        captured["prior_results"] = kwargs.get("prior_results")
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch("workbench.orchestrator.attach_worktree") as mock_attach,
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_create.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
        )

    assert captured["resume_completed_stages"] is None
    assert captured["prior_results"] is None
    mock_attach.assert_not_called()
    assert mock_create.called


@pytest.mark.asyncio
async def test_tdd_disables_resume(tmp_path):
    """TDD mode skips the resume path even when prior stages exist."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        completed_stages=["implementor", "tester"],
    )
    prior.save(repo)

    async def fake_pipeline(**kwargs):
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch("workbench.orchestrator.attach_worktree") as mock_attach,
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_create.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
            tdd=True,
        )

    mock_attach.assert_not_called()
    assert mock_create.called


# ---------------------------------------------------------------------------
# _completed_stages derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_stages_persisted_after_full_pipeline_pass(tmp_path):
    """Full impl→test→review pass should persist all three roles as completed."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()

    async def fake_pipeline(**kwargs):
        return [
            AgentResult(
                task_id="task-1",
                role=Role.IMPLEMENTOR,
                status=TaskStatus.DONE,
                output="impl",
            ),
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            ),
            AgentResult(
                task_id="task-1",
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            ),
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    status = SessionStatus.load(repo, "test-plan", "workbench-1")
    assert status.tasks["task-1"].completed_stages == ["implementor", "tester", "reviewer"]


@pytest.mark.asyncio
async def test_completed_stages_drops_pre_fixer_passes(tmp_path):
    """A fixer in the middle of the run invalidates a pre-fixer test pass."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()

    async def fake_pipeline(**kwargs):
        return [
            AgentResult(
                task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output="impl"
            ),
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            ),
            AgentResult(
                task_id="task-1",
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: FAIL",
            ),
            AgentResult(task_id="task-1", role=Role.FIXER, status=TaskStatus.DONE, output="fixed"),
            AgentResult(
                task_id="task-1",
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            ),
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    status = SessionStatus.load(repo, "test-plan", "workbench-1")
    # Tester pass was pre-fixer and not re-run — should be dropped.
    # Reviewer pass came after the fixer — preserved.
    assert status.tasks["task-1"].completed_stages == ["implementor", "reviewer"]


@pytest.mark.asyncio
async def test_completed_stages_only_impl_when_fixer_invalidates_test(tmp_path):
    """Fixer after impl+test pass: tester invalidated, only implementor stays."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir()

    async def fake_pipeline(**kwargs):
        return [
            AgentResult(
                task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output="impl"
            ),
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            ),
            AgentResult(task_id="task-1", role=Role.FIXER, status=TaskStatus.DONE, output="fixed"),
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    status = SessionStatus.load(repo, "test-plan", "workbench-1")
    assert status.tasks["task-1"].completed_stages == ["implementor"]


@pytest.mark.asyncio
async def test_completed_stages_preserved_through_status_callback(tmp_path):
    """Mid-pipeline status callback before any AgentResult should not clobber prior stages."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    # Seed prior status so the carried-forward record has stages
    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        completed_stages=["implementor", "tester"],
    )
    prior.save(repo)

    async def fake_pipeline(**kwargs):
        # Fire a status transition before any AgentResult arrives — the
        # persist call this produces must not zero out the prior stages.
        cb = kwargs["on_status_change"]
        cb(kwargs["task"].id, TaskStatus.REVIEWING)
        # Allow the scheduled persist to actually run
        await asyncio.sleep(0)
        return [
            AgentResult(
                task_id=kwargs["task"].id,
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            )
        ]

    fake_wt = MagicMock(branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock())

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch("workbench.orchestrator.attach_worktree", return_value=fake_wt),
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
        )

    status = SessionStatus.load(repo, "test-plan", "workbench-1")
    stages = status.tasks["task-1"].completed_stages
    # Prior stages should remain (mid-pipeline write preserved them) and
    # reviewer should be there too after the new pass.
    assert "implementor" in stages
    assert "tester" in stages
    assert "reviewer" in stages


# ---------------------------------------------------------------------------
# In-session retry resume path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_session_retry_resumes_via_attach(tmp_path):
    """When the resumed pipeline fails again, in-session retry attaches again."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        completed_stages=["implementor", "tester"],
    )
    prior.save(repo)

    call_count = {"pipeline": 0}

    async def fake_pipeline(**kwargs):
        call_count["pipeline"] += 1
        if call_count["pipeline"] == 1:
            # Resumed run still fails at reviewer — eligible for in-session retry.
            return [
                AgentResult(
                    task_id="task-1",
                    role=Role.REVIEWER,
                    status=TaskStatus.DONE,
                    output="VERDICT: FAIL",
                )
            ]
        # Second invocation: reviewer finally passes.
        return [
            AgentResult(
                task_id="task-1",
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            )
        ]

    fake_wt = MagicMock(branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock())

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch("workbench.orchestrator.attach_worktree", return_value=fake_wt) as mock_attach,
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
        )

    # Wave-setup attach + in-session retry attach = 2.
    assert mock_attach.call_count == 2
    mock_create.assert_not_called()
    assert call_count["pipeline"] == 2


@pytest.mark.asyncio
async def test_in_session_retry_falls_back_to_create_when_attach_raises(tmp_path):
    """If the second attach raises RuntimeError, fall back to create_worktree."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        completed_stages=["implementor", "tester"],
    )
    prior.save(repo)

    fake_wt = MagicMock(branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock())

    # First attach succeeds (wave-setup), second raises (in-session retry).
    attach_results = [fake_wt, RuntimeError("worktree gone")]

    def _attach(*_args, **_kwargs):
        result = attach_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def fake_pipeline(**kwargs):
        return [
            AgentResult(
                task_id="task-1",
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: FAIL",
            )
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch("workbench.orchestrator.attach_worktree", side_effect=_attach) as mock_attach,
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_create.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt2", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
        )

    assert mock_attach.call_count == 2
    # Fallback after the second attach raised.
    assert mock_create.called


@pytest.mark.asyncio
async def test_in_session_retry_marks_failed_when_create_worktree_fails(tmp_path):
    """If the in-session retry fallback's create_worktree raises, task is marked FAILED."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        completed_stages=["implementor", "tester"],
    )
    prior.save(repo)

    fake_wt = MagicMock(branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock())

    attach_results = [fake_wt, RuntimeError("worktree gone")]

    def _attach(*_args, **_kwargs):
        result = attach_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    create_calls = {"n": 0}

    def _create(*args, **kwargs):
        create_calls["n"] += 1
        if create_calls["n"] == 1:
            raise OSError("disk full")
        return fake_wt

    async def fake_pipeline(**kwargs):
        return [
            AgentResult(
                task_id="task-1",
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: FAIL",
            )
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch("workbench.orchestrator.attach_worktree", side_effect=_attach),
        patch("workbench.orchestrator.create_worktree", side_effect=_create),
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        results = await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            retry_failed=True,
        )

    failed = [s for s in results if s.task.id == "task-1"][0]
    assert failed.status == TaskStatus.FAILED
    # The fallback's exception output is appended as an implementor result.
    assert any(
        r.role == Role.IMPLEMENTOR and "Retry worktree creation failed" in r.output
        for r in failed.results
    )


# ---------------------------------------------------------------------------
# Streaming via on_result during run_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_result_streams_state_results_during_pipeline(tmp_path):
    """The orchestrator's _on_result callback should populate state.results live."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    snapshots: list[list[str]] = []

    async def fake_pipeline(**kwargs):
        on_result = kwargs.get("on_result")
        assert on_result is not None, "orchestrator must pass on_result to run_pipeline"

        results = [
            AgentResult(
                task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output="impl"
            ),
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            ),
            AgentResult(
                task_id="task-1",
                role=Role.REVIEWER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            ),
        ]
        for r in results:
            on_result(r)
            await asyncio.sleep(0)
            # Snapshot what status.yaml has right now.
            status = SessionStatus.load(repo, "test-plan", "workbench-1")
            stages = status.tasks["task-1"].completed_stages if status else []
            snapshots.append(list(stages))
        return results

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
        )

    # status.yaml should reflect the pipeline's progress incrementally.
    assert snapshots[0] == ["implementor"]
    assert snapshots[1] == ["implementor", "tester"]
    assert snapshots[2] == ["implementor", "tester", "reviewer"]


@pytest.mark.asyncio
async def test_on_result_persists_completed_stages_mid_pipeline(tmp_path):
    """Each AgentResult streamed through on_result should write completed_stages to disk."""
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    async def fake_pipeline(**kwargs):
        on_result = kwargs["on_result"]
        on_result(
            AgentResult(
                task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output="impl"
            )
        )
        await asyncio.sleep(0)
        on_result(
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            )
        )
        await asyncio.sleep(0)
        # Fixer invalidates tester — the next persist should drop tester.
        on_result(
            AgentResult(task_id="task-1", role=Role.FIXER, status=TaskStatus.DONE, output="fixed")
        )
        await asyncio.sleep(0)
        return [
            AgentResult(
                task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output="impl"
            ),
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            ),
            AgentResult(task_id="task-1", role=Role.FIXER, status=TaskStatus.DONE, output="fixed"),
        ]

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False, session_branch="workbench-1")

    # After the fixer ran, tester must be dropped — only implementor remains trusted.
    status = SessionStatus.load(repo, "test-plan", "workbench-1")
    assert status.tasks["task-1"].completed_stages == ["implementor"]


def test_status_table_renders_pending_row():
    state = TaskState(
        task=SimpleNamespace(title="t3"),
        wave_num=0,
        merged=False,
        merge_error=False,
        worktree=None,
    )
    table = _status_table([state])
    # Wave (1), Branch (3), Merged (6)
    assert _cell_text(table, 1, 0) == "-"
    assert _cell_text(table, 3, 0) == "-"
    assert _cell_text(table, 6, 0) == "-"


@pytest.mark.asyncio
async def test_run_plan_inherits_conventions_from_repo_file(tmp_path):
    """When the plan has no ## Conventions section, .workbench/conventions.md
    populates plan.conventions, which the orchestrator forwards to run_pipeline."""
    repo = tmp_path
    workbench_dir = repo / ".workbench"
    workbench_dir.mkdir()
    (workbench_dir / "conventions.md").write_text("- ORCH-SENTINEL\n")

    plan_path = repo / "plan.md"
    plan_path.write_text(
        "# Test Plan\n" "\n" "## Task: Test Task\n" "Files: test.py\n" "\n" "A test task body.\n"
    )

    plan = parse_plan(plan_path, repo=repo)

    captured_kwargs: dict = {}

    async def fake_pipeline(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert "ORCH-SENTINEL" in captured_kwargs.get("plan_conventions", "")


@pytest.mark.asyncio
async def test_run_plan_plan_conventions_win_over_repo_file(tmp_path):
    """A plan with its own ## Conventions section wins over .workbench/conventions.md."""
    repo = tmp_path
    workbench_dir = repo / ".workbench"
    workbench_dir.mkdir()
    (workbench_dir / "conventions.md").write_text("- ORCH-SENTINEL\n")

    plan_path = repo / "plan.md"
    plan_path.write_text(
        "# Test Plan\n"
        "\n"
        "## Conventions\n"
        "- PLAN-OWN-CONVENTION\n"
        "\n"
        "## Task: Test Task\n"
        "Files: test.py\n"
        "\n"
        "A test task body.\n"
    )

    plan = parse_plan(plan_path, repo=repo)

    captured_kwargs: dict = {}

    async def fake_pipeline(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    plan_conventions = captured_kwargs.get("plan_conventions", "")
    assert "PLAN-OWN-CONVENTION" in plan_conventions
    assert "ORCH-SENTINEL" not in plan_conventions


@pytest.mark.asyncio
async def test_run_plan_passes_trace_metadata_to_pipeline(tmp_path):
    """run_plan should forward plan_name, wave_num, trace_env, trace_prompt to run_pipeline."""
    plan = _make_plan(title="My Feature")
    repo = tmp_path

    captured_kwargs: dict = {}

    async def fake_pipeline(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_wt.return_value = MagicMock(branch="wb/test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            trace_env=True,
            trace_prompt=True,
        )

    assert captured_kwargs.get("plan_name") == plan.folder_id
    assert captured_kwargs.get("wave_num") == 1
    assert captured_kwargs.get("trace_env") is True
    assert captured_kwargs.get("trace_prompt") is True


@pytest.mark.asyncio
async def test_run_plan_constructs_headroom_proxy_and_forwards_config(tmp_path):
    """run_plan should resolve headroom once and forward that config to pipelines."""
    plan = _make_plan(title="My Feature")
    repo = tmp_path
    headroom_config = HeadroomConfig(enabled=True, port=9999)

    captured_kwargs: dict = {}

    async def fake_pipeline(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
        patch(
            "workbench.orchestrator.resolve_headroom_config",
            return_value=headroom_config,
        ) as mock_resolve,
        patch("workbench.orchestrator.HeadroomProxy") as mock_proxy,
    ):
        mock_wt.return_value = MagicMock(branch="wb/test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)
        mock_proxy.return_value.__enter__.return_value = MagicMock(active=True)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            headroom=True,
        )

    mock_resolve.assert_called_once()
    assert mock_resolve.call_args.kwargs["cli_override"] is True
    mock_proxy.assert_called_once_with(headroom_config)
    assert captured_kwargs.get("headroom") == headroom_config


# ---------------------------------------------------------------------------
# Model selection wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plan_forwards_cli_models_and_plan_models(tmp_path):
    """run_plan forwards cli_models verbatim and normalizes plan.run_config[model]."""
    plan = _make_plan()
    plan.run_config = {"model": {"claude": "plan-model"}}
    repo = tmp_path

    captured: dict = {}

    async def fake_pipeline(**kwargs):
        captured.update(kwargs)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            cli_models={"claude": "cli-model"},
        )

    assert captured.get("cli_models") == {"claude": "cli-model"}
    assert captured.get("plan_models") == {"claude": "plan-model"}


@pytest.mark.asyncio
async def test_run_plan_normalizes_scalar_frontmatter_model(tmp_path):
    """A scalar frontmatter model normalizes to the "" key."""
    plan = _make_plan()
    plan.run_config = {"model": "shared-model"}
    repo = tmp_path

    captured: dict = {}

    async def fake_pipeline(**kwargs):
        captured.update(kwargs)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert captured.get("plan_models") == {"": "shared-model"}


@pytest.mark.asyncio
async def test_run_plan_defaults_models_to_empty(tmp_path):
    """No frontmatter model and no --model: plan_models={} and cli_models is None."""
    plan = _make_plan()
    repo = tmp_path

    captured: dict = {}

    async def fake_pipeline(**kwargs):
        captured.update(kwargs)
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_wt,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
    ):
        mock_wt.return_value = MagicMock(branch="wb/task-1-test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert captured.get("plan_models") == {}
    assert captured.get("cli_models") is None


@pytest.mark.asyncio
async def test_resume_reuses_stages_regression(tmp_path):
    """Resume reuses stages (the regression test — must fail before the fix).
    Seed a prior status where a task is status="failed", branch="wb/<slug>",
    completed_stages=["implementor"], with the branch and on-disk worktree dir present.
    Call run_plan(..., only_incomplete=True, retry_failed=False).
    Assert:
    - attach_worktree was called and create_worktree was not called for that task;
    - the task's branch was not deleted;
    - run_pipeline received resume_completed_stages=["implementor"]
    """
    plan = _make_plan()
    repo = tmp_path
    (repo / ".workbench").mkdir(parents=True, exist_ok=True)

    # Seed a prior status: task-1 failed after impl succeeded
    prior = SessionStatus(plan_slug="test-plan", session_branch="workbench-1")
    prior.record_task(
        "task-1",
        status="failed",
        branch="wb/test-task",
        last_agent="tester",
        completed_stages=["implementor"],
    )
    prior.save(repo)

    captured = {}

    async def fake_pipeline(**kwargs):
        captured["resume_completed_stages"] = kwargs.get("resume_completed_stages")
        return [
            AgentResult(
                task_id="task-1",
                role=Role.TESTER,
                status=TaskStatus.DONE,
                output="VERDICT: PASS",
            )
        ]

    fake_wt = MagicMock(branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock())

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.branch_exists", return_value=True),
        patch("workbench.orchestrator.attach_worktree", return_value=fake_wt) as mock_attach,
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch") as mock_delete,
    ):
        mock_create.return_value = MagicMock(
            branch="wb/test-task", path=tmp_path / "wt", cleanup=MagicMock()
        )
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(
            plan=plan,
            repo=repo,
            use_tmux=False,
            session_branch="workbench-1",
            only_incomplete=True,
            retry_failed=False,
            keep_branches=True,
        )

    # This is what we EXPECT after the fix.
    # Before the fix, mock_attach will NOT be called, mock_create WILL be called,
    # and resume_completed_stages will be None.
    assert mock_attach.called, "attach_worktree should be called for resume"
    assert not mock_create.called, "create_worktree should NOT be called for resume"

    # Check that the branch was not deleted
    # The code deletes branches with f"wb/{state.task.slug}" which is "wb/test-task"
    for call in mock_delete.call_args_list:
        assert call.args[1] != "wb/test-task"

    assert captured["resume_completed_stages"] == ["implementor"]


@pytest.mark.asyncio
async def test_fresh_run_unaffected(tmp_path):
    """Fresh run unaffected: with no prior record for the task,
    create_worktree is used and run_pipeline gets resume_completed_stages of None/empty.
    """
    plan = _make_plan()
    repo = tmp_path

    captured = {}

    async def fake_pipeline(**kwargs):
        captured["resume_completed_stages"] = kwargs.get("resume_completed_stages")
        return []

    with (
        patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"),
        patch("workbench.orchestrator.create_worktree") as mock_create,
        patch("workbench.orchestrator.run_pipeline", side_effect=fake_pipeline),
        patch("workbench.orchestrator.merge_into_session") as mock_merge,
        patch("workbench.orchestrator.delete_branch"),
    ):
        mock_create.return_value = MagicMock(branch="wb/test-task", path=tmp_path / "wt")
        mock_merge.return_value = MagicMock(success=True, message="merged", conflicts=None)

        await run_plan(plan=plan, repo=repo, use_tmux=False)

    assert mock_create.called
    assert captured["resume_completed_stages"] is None


def test_run_plan_fail_fast_default():
    """run_plan default: assert the fail_fast parameter default is True."""
    sig = inspect.signature(run_plan)
    assert sig.parameters["fail_fast"].default is True
