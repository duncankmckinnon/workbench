"""Tests for the orchestrator — TaskState, status table, and run_plan."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from workbench.agents import AgentResult, Role, TaskStatus
from workbench.orchestrator import TaskState, _status_table, run_plan
from workbench.plan_parser import Plan, Task
from workbench.worktree import Worktree, MergeResult


# ---------------------------------------------------------------------------
# TaskState properties
# ---------------------------------------------------------------------------


@pytest.fixture
def task():
    return Task(id="task-1", title="Build it", description="Build the thing")


class TestTaskStateElapsed:
    def test_no_start(self, task):
        state = TaskState(task=task)
        assert state.elapsed == "-"

    def test_running(self, task):
        state = TaskState(task=task, started_at=time.time() - 65)
        elapsed = state.elapsed
        assert "m" in elapsed
        assert "s" in elapsed

    def test_finished(self, task):
        now = time.time()
        state = TaskState(task=task, started_at=now - 125, finished_at=now)
        assert state.elapsed == "2m05s"


class TestTaskStateFixCount:
    def test_no_fixes(self, task):
        state = TaskState(task=task)
        assert state.fix_count == 0

    def test_with_fixes(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output=""),
            AgentResult(task_id="task-1", role=Role.TESTER, status=TaskStatus.DONE, output="VERDICT: FAIL"),
            AgentResult(task_id="task-1", role=Role.FIXER, status=TaskStatus.DONE, output="fixed"),
            AgentResult(task_id="task-1", role=Role.TESTER, status=TaskStatus.DONE, output="VERDICT: PASS"),
        ])
        assert state.fix_count == 1


class TestTaskStatePhaseSummary:
    def test_empty(self, task):
        state = TaskState(task=task)
        assert state.phase_summary == ""

    def test_impl_ok(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output=""),
        ])
        assert state.phase_summary == "impl:ok"

    def test_impl_fail(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.FAILED, output=""),
        ])
        assert state.phase_summary == "impl:fail"

    def test_test_pass(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output=""),
            AgentResult(task_id="task-1", role=Role.TESTER, status=TaskStatus.DONE, output="VERDICT: PASS"),
        ])
        assert "test:pass" in state.phase_summary

    def test_test_crash(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output=""),
            AgentResult(task_id="task-1", role=Role.TESTER, status=TaskStatus.FAILED, output="crashed"),
        ])
        assert "test:crash" in state.phase_summary

    def test_test_fail(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output=""),
            AgentResult(task_id="task-1", role=Role.TESTER, status=TaskStatus.DONE, output="bad\nVERDICT: FAIL"),
        ])
        assert "test:fail" in state.phase_summary

    def test_review_pass(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.REVIEWER, status=TaskStatus.DONE, output="VERDICT: PASS"),
        ])
        assert "review:pass" in state.phase_summary

    def test_review_crash(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.REVIEWER, status=TaskStatus.FAILED, output=""),
        ])
        assert "review:crash" in state.phase_summary

    def test_review_fail(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.REVIEWER, status=TaskStatus.DONE, output="bad\nVERDICT: FAIL"),
        ])
        assert "review:fail" in state.phase_summary

    def test_fix_ok(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.FIXER, status=TaskStatus.DONE, output=""),
        ])
        assert "fix" in state.phase_summary

    def test_fix_fail(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.FIXER, status=TaskStatus.FAILED, output=""),
        ])
        assert "fix:fail" in state.phase_summary

    def test_full_pipeline(self, task):
        state = TaskState(task=task, results=[
            AgentResult(task_id="task-1", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output=""),
            AgentResult(task_id="task-1", role=Role.TESTER, status=TaskStatus.DONE, output="VERDICT: PASS"),
            AgentResult(task_id="task-1", role=Role.REVIEWER, status=TaskStatus.DONE, output="VERDICT: PASS"),
        ])
        assert state.phase_summary == "impl:ok → test:pass → review:pass"


# ---------------------------------------------------------------------------
# _status_table
# ---------------------------------------------------------------------------


class TestStatusTable:
    def test_renders_without_error(self, task):
        """_status_table should return a Rich Table without crashing."""
        states = [
            TaskState(task=task, status=TaskStatus.PENDING),
            TaskState(
                task=Task(id="task-2", title="Other", description=""),
                status=TaskStatus.DONE,
                worktree=Worktree(path="/tmp/x", branch="wb/other", task_id="task-2"),
                results=[
                    AgentResult(task_id="task-2", role=Role.IMPLEMENTOR, status=TaskStatus.DONE, output=""),
                ],
            ),
        ]
        table = _status_table(states)
        assert table.title == "Workbench"
        assert table.row_count == 2

    def test_all_status_styles(self, task):
        """Every TaskStatus value should render without error."""
        for status in TaskStatus:
            state = TaskState(task=task, status=status)
            table = _status_table([state])
            assert table.row_count == 1


# ---------------------------------------------------------------------------
# run_plan — integration-style tests with mocked agents
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_plan(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Test\n\n## Context\nA test.\n\n## Task: One\nFiles: a.py\n\nDo it.\n")
    from workbench.plan_parser import parse_plan
    return parse_plan(plan_file)


def test_run_plan_single_task(simple_plan, tmp_path):
    """run_plan with one task, skip test and review, mocked worktree and agents."""

    async def mock_pipeline(**kwargs):
        return [AgentResult(
            task_id="task-1", role=Role.IMPLEMENTOR,
            status=TaskStatus.DONE, output="done",
        )]

    mock_wt = Worktree(path=tmp_path, branch="wb/one", task_id="task-1")

    with patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"), \
         patch("workbench.orchestrator.create_worktree", return_value=mock_wt), \
         patch("workbench.orchestrator.run_pipeline", new_callable=AsyncMock, side_effect=mock_pipeline), \
         patch("workbench.orchestrator.merge_into_session", return_value=MergeResult(
             branch="wb/one", success=True, message="Merged cleanly.",
         )):
        states = asyncio.run(run_plan(
            plan=simple_plan,
            repo=tmp_path,
            skip_test=True,
            skip_review=True,
        ))

    assert len(states) == 1
    assert states[0].status == TaskStatus.DONE


def test_run_plan_merge_conflict_resolved(simple_plan, tmp_path):
    """Merge conflict triggers resolver, which succeeds."""

    async def mock_pipeline(**kwargs):
        return [AgentResult(
            task_id="task-1", role=Role.IMPLEMENTOR,
            status=TaskStatus.DONE, output="done",
        )]

    mock_wt = Worktree(path=tmp_path, branch="wb/one", task_id="task-1")
    conflict_result = MergeResult(
        branch="wb/one", success=False,
        message="Merge conflict in 1 file(s).",
        conflicts=["a.py"],
        merge_dir=tmp_path / "_merge",
    )
    resolve_ok = AgentResult(
        task_id="wb/one", role=Role.MERGER,
        status=TaskStatus.DONE, output="VERDICT: PASS",
    )
    merge_complete = MergeResult(branch="wb/one", success=True, message="Merged after resolution.")

    with patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"), \
         patch("workbench.orchestrator.create_worktree", return_value=mock_wt), \
         patch("workbench.orchestrator.run_pipeline", new_callable=AsyncMock, side_effect=mock_pipeline), \
         patch("workbench.orchestrator.merge_into_session", return_value=conflict_result), \
         patch("workbench.orchestrator.run_merge_resolver", new_callable=AsyncMock, return_value=resolve_ok), \
         patch("workbench.orchestrator.complete_merge", return_value=merge_complete):
        states = asyncio.run(run_plan(
            plan=simple_plan, repo=tmp_path,
            skip_test=True, skip_review=True,
        ))

    assert states[0].status == TaskStatus.DONE


def test_run_plan_merge_conflict_unresolved(simple_plan, tmp_path):
    """Merge conflict resolver fails → task marked FAILED."""

    async def mock_pipeline(**kwargs):
        return [AgentResult(
            task_id="task-1", role=Role.IMPLEMENTOR,
            status=TaskStatus.DONE, output="done",
        )]

    mock_wt = Worktree(path=tmp_path, branch="wb/one", task_id="task-1")
    conflict_result = MergeResult(
        branch="wb/one", success=False,
        message="Merge conflict in 1 file(s).",
        conflicts=["a.py"],
        merge_dir=tmp_path / "_merge",
    )
    resolve_fail = AgentResult(
        task_id="wb/one", role=Role.MERGER,
        status=TaskStatus.DONE, output="VERDICT: FAIL",
    )

    with patch("workbench.orchestrator.create_session_branch", return_value="workbench-1"), \
         patch("workbench.orchestrator.create_worktree", return_value=mock_wt), \
         patch("workbench.orchestrator.run_pipeline", new_callable=AsyncMock, side_effect=mock_pipeline), \
         patch("workbench.orchestrator.merge_into_session", return_value=conflict_result), \
         patch("workbench.orchestrator.run_merge_resolver", new_callable=AsyncMock, return_value=resolve_fail), \
         patch("workbench.orchestrator.cleanup_merge_worktree"):
        states = asyncio.run(run_plan(
            plan=simple_plan, repo=tmp_path,
            skip_test=True, skip_review=True,
        ))

    assert states[0].status == TaskStatus.FAILED
