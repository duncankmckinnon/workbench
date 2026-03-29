"""Tests for the TDD pipeline execution path in run_pipeline()."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, call

import pytest

from workbench.agents import (
    AgentResult,
    Role,
    TaskStatus,
    TDD_DIRECTIVES,
    run_pipeline,
)
from workbench.plan_parser import Task
from workbench.worktree import Worktree


@pytest.fixture
def sample_task():
    return Task(id="task-1", title="Add feature", description="Implement X", files=["src/x.py"])


@pytest.fixture
def sample_worktree(tmp_path):
    return Worktree(path=tmp_path, branch="wb/task-1", task_id="task-1")


def _make_result(role: Role, status: TaskStatus, output: str) -> AgentResult:
    return AgentResult(task_id="task-1", role=role, status=status, output=output)


def _pass_result(role: Role) -> AgentResult:
    return _make_result(role, TaskStatus.DONE, "All good.\nVERDICT: PASS")


def _fail_verdict_result(role: Role) -> AgentResult:
    return _make_result(role, TaskStatus.DONE, "Something wrong.\nVERDICT: FAIL")


def _crash_result(role: Role) -> AgentResult:
    return _make_result(role, TaskStatus.FAILED, "Agent error: crash")


def _done_result(role: Role) -> AgentResult:
    return _make_result(role, TaskStatus.DONE, "Done implementing.")


class TestTDDPipelineFullPass:
    def test_pipeline_tdd_full_pass(self, sample_task, sample_worktree, tmp_path):
        """TDD: tester writes tests (DONE), implementor implements (PASS verdict),
        test verification passes, review passes.
        Verify roles are: TESTER, IMPLEMENTOR, TESTER, REVIEWER."""
        side_effects = [
            _done_result(Role.TESTER),       # TDD Phase 1: write failing tests (no verdict needed)
            _pass_result(Role.IMPLEMENTOR),  # TDD Phase 2: implement (tests pass + comprehensive)
            _pass_result(Role.TESTER),        # Verification: test
            _pass_result(Role.REVIEWER),      # Review
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
            results = asyncio.run(run_pipeline(
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                use_tmux=False,
                tdd=True,
            ))

        assert len(results) == 4
        assert [r.role for r in results] == [
            Role.TESTER, Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER,
        ]


class TestTDDTestWriteFails:
    def test_pipeline_tdd_test_write_fails(self, sample_task, sample_worktree, tmp_path):
        """TDD tester crashes -> pipeline stops after 1 result."""
        side_effects = [
            _crash_result(Role.TESTER),
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
            results = asyncio.run(run_pipeline(
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                use_tmux=False,
                tdd=True,
            ))

        assert len(results) == 1
        assert results[0].role == Role.TESTER
        assert results[0].status == TaskStatus.FAILED


class TestTDDImplVerdictFail:
    def test_pipeline_tdd_impl_verdict_fail(self, sample_task, sample_worktree, tmp_path):
        """TDD implementor returns VERDICT: FAIL (tests fail or not comprehensive) -> pipeline stops."""
        side_effects = [
            _done_result(Role.TESTER),
            _fail_verdict_result(Role.IMPLEMENTOR),
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
            results = asyncio.run(run_pipeline(
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                use_tmux=False,
                tdd=True,
            ))

        assert len(results) == 2
        assert results[0].role == Role.TESTER
        assert results[1].role == Role.IMPLEMENTOR
        assert not results[1].passed


class TestTDDImplFails:
    def test_pipeline_tdd_impl_fails(self, sample_task, sample_worktree, tmp_path):
        """TDD implementation crashes -> pipeline stops after 2 results."""
        side_effects = [
            _done_result(Role.TESTER),
            _crash_result(Role.IMPLEMENTOR),
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
            results = asyncio.run(run_pipeline(
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                use_tmux=False,
                tdd=True,
            ))

        assert len(results) == 2
        assert results[0].role == Role.TESTER
        assert results[1].role == Role.IMPLEMENTOR
        assert results[1].status == TaskStatus.FAILED


class TestTDDDirectives:
    def test_pipeline_tdd_uses_tdd_directives(self, sample_task, sample_worktree, tmp_path):
        """Verify TDD_DIRECTIVES are used (not DEFAULT_DIRECTIVES) when no override provided."""
        captured_directives = []

        async def mock_run_agent(*args, **kwargs):
            captured_directives.append(kwargs.get("directive"))
            role = args[0]
            if role == Role.TESTER and len(captured_directives) == 1:
                return _done_result(role)  # TDD tester: no verdict
            return _pass_result(role)

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
            results = asyncio.run(run_pipeline(
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                use_tmux=False,
                tdd=True,
            ))

        # First call: TDD tester directive
        assert captured_directives[0] == TDD_DIRECTIVES[Role.TESTER]
        # Second call: TDD implementor directive
        assert captured_directives[1] == TDD_DIRECTIVES[Role.IMPLEMENTOR]

    def test_pipeline_tdd_directive_override(self, sample_task, sample_worktree, tmp_path):
        """Custom directives dict overrides TDD defaults."""
        custom_tester = "Custom tester directive"
        custom_impl = "Custom implementor directive"
        captured_directives = []

        async def mock_run_agent(*args, **kwargs):
            captured_directives.append(kwargs.get("directive"))
            role = args[0]
            if role == Role.TESTER and len(captured_directives) == 1:
                return _done_result(role)  # TDD tester: no verdict
            return _pass_result(role)

        custom_directives = {
            Role.TESTER: custom_tester,
            Role.IMPLEMENTOR: custom_impl,
        }

        with patch("workbench.agents.run_agent", side_effect=mock_run_agent):
            results = asyncio.run(run_pipeline(
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                use_tmux=False,
                tdd=True,
                directives=custom_directives,
            ))

        assert captured_directives[0] == custom_tester
        assert captured_directives[1] == custom_impl


class TestTDDVerificationFailsThenFixes:
    def test_pipeline_tdd_verification_fails_then_fixes(self, sample_task, sample_worktree, tmp_path):
        """After TDD impl, verification test fails, fixer runs, test passes on retry."""
        side_effects = [
            _done_result(Role.TESTER),            # TDD Phase 1: write tests (no verdict)
            _pass_result(Role.IMPLEMENTOR),       # TDD Phase 2: implement (PASS verdict)
            _fail_verdict_result(Role.TESTER),     # Verification: test FAILS
            _done_result(Role.FIXER),              # Fixer addresses issues
            _pass_result(Role.TESTER),             # Verification retry: PASS
            _pass_result(Role.REVIEWER),           # Review: PASS
        ]

        with patch("workbench.agents.run_agent", new_callable=AsyncMock, side_effect=side_effects):
            results = asyncio.run(run_pipeline(
                task=sample_task,
                worktree=sample_worktree,
                repo=tmp_path,
                use_tmux=False,
                tdd=True,
            ))

        assert len(results) == 6
        assert [r.role for r in results] == [
            Role.TESTER, Role.IMPLEMENTOR, Role.TESTER, Role.FIXER, Role.TESTER, Role.REVIEWER,
        ]
