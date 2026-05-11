"""Tests for workbench.github_pr module."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workbench.github_pr import create_pr


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal repo directory with .workbench/tmp."""
    return tmp_path


def _make_process(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """Build a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# -------------------------------------------------------------------
# gh missing
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gh_missing_returns_suggested_command(tmp_repo: Path) -> None:
    with patch("workbench.github_pr.shutil.which", return_value=None):
        success, msg = await create_pr(
            tmp_repo,
            "wb/session-1",
            "main",
            "Add feature X",
            "body text",
        )

    assert success is False
    assert "gh pr create" in msg
    assert "--base main" in msg
    assert "--head wb/session-1" in msg
    assert "Add feature X" in msg


# -------------------------------------------------------------------
# gh present but not authenticated
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gh_unauthed_returns_suggested_command(tmp_repo: Path) -> None:
    auth_proc = _make_process(returncode=1)
    with (
        patch("workbench.github_pr.shutil.which", return_value="/usr/bin/gh"),
        patch(
            "workbench.github_pr.asyncio.create_subprocess_exec",
            return_value=auth_proc,
        ),
    ):
        success, msg = await create_pr(
            tmp_repo,
            "wb/session-2",
            "develop",
            "Fix bug Y",
            "body text",
        )

    assert success is False
    assert "gh pr create" in msg
    assert "--base develop" in msg
    assert "--head wb/session-2" in msg
    assert "Fix bug Y" in msg


# -------------------------------------------------------------------
# gh succeeds
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gh_success_parses_url(tmp_repo: Path) -> None:
    auth_proc = _make_process(returncode=0)
    pr_proc = _make_process(
        returncode=0,
        stdout=b"https://github.com/foo/bar/pull/123\n",
    )

    call_count = 0

    async def _fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call is gh auth status, second is gh pr create
        if call_count == 1:
            return auth_proc
        return pr_proc

    with (
        patch("workbench.github_pr.shutil.which", return_value="/usr/bin/gh"),
        patch(
            "workbench.github_pr.asyncio.create_subprocess_exec",
            side_effect=_fake_exec,
        ),
    ):
        success, msg = await create_pr(
            tmp_repo,
            "wb/session-3",
            "main",
            "PR title",
            "PR body",
        )

    assert success is True
    assert msg == "https://github.com/foo/bar/pull/123"


# -------------------------------------------------------------------
# gh runs but fails
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gh_failure_returns_stderr(tmp_repo: Path) -> None:
    auth_proc = _make_process(returncode=0)
    pr_proc = _make_process(
        returncode=1,
        stderr=b"GraphQL: Could not resolve to a Repository",
    )

    call_count = 0

    async def _fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return auth_proc
        return pr_proc

    with (
        patch("workbench.github_pr.shutil.which", return_value="/usr/bin/gh"),
        patch(
            "workbench.github_pr.asyncio.create_subprocess_exec",
            side_effect=_fake_exec,
        ),
    ):
        success, msg = await create_pr(
            tmp_repo,
            "wb/session-4",
            "main",
            "PR title",
            "PR body",
        )

    assert success is False
    assert msg == "GraphQL: Could not resolve to a Repository"


# -------------------------------------------------------------------
# Tempfile cleanup
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tempfile_cleanup_on_success(tmp_repo: Path) -> None:
    auth_proc = _make_process(returncode=0)
    pr_proc = _make_process(returncode=0, stdout=b"https://github.com/x/y/pull/1\n")

    call_count = 0

    async def _fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return auth_proc
        return pr_proc

    with (
        patch("workbench.github_pr.shutil.which", return_value="/usr/bin/gh"),
        patch(
            "workbench.github_pr.asyncio.create_subprocess_exec",
            side_effect=_fake_exec,
        ),
    ):
        await create_pr(tmp_repo, "wb/s", "main", "t", "b")

    body_path = tmp_repo / ".workbench" / "tmp" / "pr-body.md"
    assert not body_path.exists()


@pytest.mark.asyncio
async def test_tempfile_cleanup_on_failure(tmp_repo: Path) -> None:
    auth_proc = _make_process(returncode=0)
    pr_proc = _make_process(returncode=1, stderr=b"error")

    call_count = 0

    async def _fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return auth_proc
        return pr_proc

    with (
        patch("workbench.github_pr.shutil.which", return_value="/usr/bin/gh"),
        patch(
            "workbench.github_pr.asyncio.create_subprocess_exec",
            side_effect=_fake_exec,
        ),
    ):
        await create_pr(tmp_repo, "wb/s", "main", "t", "b")

    body_path = tmp_repo / ".workbench" / "tmp" / "pr-body.md"
    assert not body_path.exists()
