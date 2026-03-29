"""Tests for run_pipeline — the implement → test → review loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, call

import pytest

from workbench.agents import (
    AgentResult,
    Role,
    TaskStatus,
    run_merge_resolver,
    run_pipeline,
)
from workbench.plan_parser import Task
from workbench.worktree import Worktree


@pytest.fixture
def task():
    return Task(id="task-1", title="Build it", description="Build the thing", files=["a.py"])


@pytest.fixture
def worktree(tmp_path):
    return Worktree(path=tmp_path, branch="wb/build-it", task_id="task-1")


def _ok(role, output="ok\nVERDICT: PASS"):
    return AgentResult(task_id="task-1", role=role, status=TaskStatus.DONE, output=output)


def _fail(role, output="bad\nVERDICT: FAIL"):
    return AgentResult(task_id="task-1", role=role, status=TaskStatus.DONE, output=output)


def _crash(role):
    return AgentResult(task_id="task-1", role=role, status=TaskStatus.FAILED, output="crashed")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_pipeline_full_pass(task, worktree, tmp_path):
    """All stages pass on first try."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _ok(Role.TESTER),
            _ok(Role.REVIEWER),
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path))

    assert len(results) == 3
    roles = [r.role for r in results]
    assert roles == [Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER]


def test_pipeline_skip_test(task, worktree, tmp_path):
    """skip_test=True skips the test stage."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _ok(Role.REVIEWER),
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path, skip_test=True))

    roles = [r.role for r in results]
    assert Role.TESTER not in roles
    assert roles == [Role.IMPLEMENTOR, Role.REVIEWER]


def test_pipeline_skip_review(task, worktree, tmp_path):
    """skip_review=True skips the review stage."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _ok(Role.TESTER),
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path, skip_review=True))

    roles = [r.role for r in results]
    assert Role.REVIEWER not in roles


def test_pipeline_skip_both(task, worktree, tmp_path):
    """skip_test + skip_review = only implement."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [_ok(Role.IMPLEMENTOR, "done")]
        results = asyncio.run(run_pipeline(
            task, worktree, tmp_path, skip_test=True, skip_review=True,
        ))

    assert len(results) == 1
    assert results[0].role == Role.IMPLEMENTOR


# ---------------------------------------------------------------------------
# Implementation failure
# ---------------------------------------------------------------------------


def test_pipeline_impl_crash_aborts(task, worktree, tmp_path):
    """If implementation crashes, pipeline stops immediately."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [_crash(Role.IMPLEMENTOR)]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path))

    assert len(results) == 1
    assert results[0].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Test failure → fix → retry
# ---------------------------------------------------------------------------


def test_pipeline_test_fail_then_fix_pass(task, worktree, tmp_path):
    """Test fails, fixer runs, test passes on retry."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _fail(Role.TESTER),           # test fails
            _ok(Role.FIXER, "fixed"),      # fixer succeeds
            _ok(Role.TESTER),              # test passes on retry
            _ok(Role.REVIEWER),            # review passes
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path))

    roles = [r.role for r in results]
    assert roles == [Role.IMPLEMENTOR, Role.TESTER, Role.FIXER, Role.TESTER, Role.REVIEWER]


def test_pipeline_test_fails_exhausts_retries(task, worktree, tmp_path):
    """Test keeps failing until retries exhausted."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _fail(Role.TESTER),            # attempt 1
            _ok(Role.FIXER, "fix1"),
            _fail(Role.TESTER),            # attempt 2
            _ok(Role.FIXER, "fix2"),
            _fail(Role.TESTER),            # attempt 3 — out of retries
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path, max_retries=2))

    # Should have: impl, test, fix, test, fix, test (6 results)
    assert len(results) == 6
    assert results[-1].role == Role.TESTER


def test_pipeline_test_crash_no_retry(task, worktree, tmp_path):
    """If the tester agent itself crashes, don't retry."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _crash(Role.TESTER),
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path))

    assert len(results) == 2
    assert results[-1].status == TaskStatus.FAILED


def test_pipeline_fixer_crash_aborts(task, worktree, tmp_path):
    """If the fixer crashes, pipeline stops."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _fail(Role.TESTER),
            _crash(Role.FIXER),
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path))

    assert len(results) == 3
    assert results[-1].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Review failure → fix → retry
# ---------------------------------------------------------------------------


def test_pipeline_review_fail_then_fix_pass(task, worktree, tmp_path):
    """Review fails, fixer runs, review passes on retry."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _ok(Role.TESTER),
            _fail(Role.REVIEWER),          # review fails
            _ok(Role.FIXER, "fixed"),
            _ok(Role.REVIEWER),            # review passes
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path))

    roles = [r.role for r in results]
    assert roles == [Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER, Role.FIXER, Role.REVIEWER]


def test_pipeline_review_crash_no_retry(task, worktree, tmp_path):
    """Reviewer crash stops the pipeline."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _ok(Role.TESTER),
            _crash(Role.REVIEWER),
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path))

    assert len(results) == 3
    assert results[-1].status == TaskStatus.FAILED


def test_pipeline_review_exhausts_retries(task, worktree, tmp_path):
    """Review keeps failing until retries exhausted."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _ok(Role.TESTER),
            _fail(Role.REVIEWER),
            _ok(Role.FIXER, "fix1"),
            _fail(Role.REVIEWER),           # out of retries
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path, max_retries=1))

    assert len(results) == 5


def test_pipeline_review_fixer_crash_aborts(task, worktree, tmp_path):
    """Fixer crash during review loop stops pipeline."""
    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _ok(Role.TESTER),
            _fail(Role.REVIEWER),
            _crash(Role.FIXER),
        ]
        results = asyncio.run(run_pipeline(task, worktree, tmp_path))

    assert len(results) == 4
    assert results[-1].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Status change callback
# ---------------------------------------------------------------------------


def test_pipeline_notifies_status_changes(task, worktree, tmp_path):
    """on_status_change is called at each phase transition."""
    statuses = []

    def track(task_id, status):
        statuses.append(status)

    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [
            _ok(Role.IMPLEMENTOR, "done"),
            _ok(Role.TESTER),
            _ok(Role.REVIEWER),
        ]
        asyncio.run(run_pipeline(
            task, worktree, tmp_path, on_status_change=track,
        ))

    assert TaskStatus.IMPLEMENTING in statuses
    assert TaskStatus.TESTING in statuses
    assert TaskStatus.REVIEWING in statuses
    assert TaskStatus.DONE in statuses


# ---------------------------------------------------------------------------
# Directives threading
# ---------------------------------------------------------------------------


def test_pipeline_passes_directives(task, worktree, tmp_path):
    """Custom directives are forwarded to run_agent."""
    directives = {Role.IMPLEMENTOR: "custom impl directive"}

    with patch("workbench.agents.run_agent", new_callable=AsyncMock) as mock:
        mock.side_effect = [_ok(Role.IMPLEMENTOR, "done")]
        asyncio.run(run_pipeline(
            task, worktree, tmp_path,
            skip_test=True, skip_review=True,
            directives=directives,
        ))

    # Check the directive kwarg was passed
    _, kwargs = mock.call_args
    assert kwargs["directive"] == "custom impl directive"


# ---------------------------------------------------------------------------
# run_merge_resolver
# ---------------------------------------------------------------------------


def test_merge_resolver_success(tmp_path):
    """Merge resolver returns PASS verdict."""
    with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock,
               return_value=(0, "resolved\nVERDICT: PASS")):
        result = asyncio.run(run_merge_resolver(
            task_branch="wb/feature",
            session_branch="workbench-1",
            merge_dir=tmp_path,
            conflicts=["a.py", "b.py"],
            repo=tmp_path,
        ))

    assert result.role == Role.MERGER
    assert result.passed
    assert result.task_id == "wb/feature"


def test_merge_resolver_failure(tmp_path):
    """Merge resolver returns non-zero exit code."""
    with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock,
               return_value=(1, "could not resolve\nVERDICT: FAIL")):
        result = asyncio.run(run_merge_resolver(
            task_branch="wb/feature",
            session_branch="workbench-1",
            merge_dir=tmp_path,
            conflicts=["a.py"],
            repo=tmp_path,
        ))

    assert result.status == TaskStatus.FAILED


def test_merge_resolver_no_tmux(tmp_path):
    """Merge resolver with use_tmux=False uses subprocess."""
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"resolved\nVERDICT: PASS", b"")
    mock_proc.returncode = 0

    with patch("workbench.agents.asyncio.create_subprocess_exec",
               new_callable=AsyncMock, return_value=mock_proc):
        result = asyncio.run(run_merge_resolver(
            task_branch="wb/feature",
            session_branch="workbench-1",
            merge_dir=tmp_path,
            conflicts=["a.py"],
            repo=tmp_path,
            use_tmux=False,
        ))

    assert result.status == TaskStatus.DONE


def test_merge_resolver_exception(tmp_path):
    """Exception in merge resolver returns FAILED result."""
    with patch("workbench.agents.run_in_tmux", new_callable=AsyncMock,
               side_effect=RuntimeError("boom")):
        result = asyncio.run(run_merge_resolver(
            task_branch="wb/feature",
            session_branch="workbench-1",
            merge_dir=tmp_path,
            conflicts=["a.py"],
            repo=tmp_path,
        ))

    assert result.status == TaskStatus.FAILED
    assert "boom" in result.output
