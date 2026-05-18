"""Tests for workbench.final_review — the two-agent final review orchestration."""

from __future__ import annotations

import asyncio
import io
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from workbench.final_review import (
    PostTaskAgentState,
    PostTaskAgentStatus,
    _post_task_table,
    run_final_review,
)
from workbench.pr_writer import PrWriterAgentError, PrWriterParseError
from workbench.session_status import FinalReviewRecord, SessionStatus

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a minimal git repo with a plan file and session branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    # Initial commit
    (repo / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    # Create session branch
    subprocess.run(["git", "branch", "workbench-1"], cwd=repo, capture_output=True)

    # Plan file
    plan_dir = repo / ".workbench" / "my-plan"
    plan_dir.mkdir(parents=True)
    plan_source = plan_dir / "plan.md"
    plan_source.write_text(
        "# My Plan\n\n## Context\n\nSome context.\n\n## Task: Do stuff\n\nDetails."
    )

    return repo


@pytest.fixture
def plan_source(tmp_repo):
    return tmp_repo / ".workbench" / "my-plan" / "plan.md"


@pytest.fixture
def base_kwargs(tmp_repo, plan_source):
    """Standard kwargs for run_final_review."""
    return dict(
        repo=tmp_repo,
        session_branch="workbench-1",
        plan_slug="my-plan",
        base_branch="main",
        plan_source=plan_source,
        merged_task_titles=["Do stuff"],
        agent_cmd="claude",
        use_tmux=False,
        skip_pr=True,
    )


def _make_mock_adapter(write_file: Path | None = None, content: str = "", exit_code: int = 0):
    """Create a mock adapter that optionally writes a file when 'run'."""
    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.05})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        if write_file:
            write_file.parent.mkdir(parents=True, exist_ok=True)
            write_file.write_text(content, encoding="utf-8")
        proc = MagicMock()
        proc.returncode = exit_code
        proc.communicate = AsyncMock(return_value=(b"output", b""))
        return proc

    return adapter, fake_exec


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_plan_source_raises(tmp_repo):
    """plan_source does not exist → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        await run_final_review(
            repo=tmp_repo,
            session_branch="workbench-1",
            plan_slug="my-plan",
            base_branch="main",
            plan_source=tmp_repo / "nonexistent.md",
            merged_task_titles=["Task 1"],
            use_tmux=False,
        )


@pytest.mark.asyncio
async def test_empty_merged_tasks_raises(tmp_repo, plan_source):
    """merged_task_titles=[] → ValueError."""
    with pytest.raises(ValueError, match="at least one merged task"):
        await run_final_review(
            repo=tmp_repo,
            session_branch="workbench-1",
            plan_slug="my-plan",
            base_branch="main",
            plan_source=plan_source,
            merged_task_titles=[],
            use_tmux=False,
        )


@pytest.mark.asyncio
async def test_summarizer_failure_returns_error_verdict(base_kwargs):
    """Summarizer exits nonzero → record has verdict='error', pr_url is None."""
    adapter = MagicMock()
    adapter.build_command.return_value = ["false"]
    adapter.parse_output.return_value = ("error output", {})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"fail", b"error"))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        record = await run_final_review(**base_kwargs)

    assert record.verdict == "error"
    assert record.pr_url is None


@pytest.mark.asyncio
async def test_summarizer_writes_empty_file_returns_error(base_kwargs, tmp_repo):
    """Adapter exits zero but doesn't write requirements → verdict='error'."""
    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("ok", {"cost_usd": 0.01})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        # Don't write the requirements file
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        record = await run_final_review(**base_kwargs)

    assert record.verdict == "error"
    assert record.pr_url is None


@pytest.mark.asyncio
async def test_reviewer_fail_no_pr_opened(base_kwargs, tmp_repo):
    """Report ends in VERDICT: FAIL → verdict='fail', create_pr never called."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    call_count = {"n": 0}

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.02})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Summarizer: write requirements
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(
                "## Requirements\n- Stuff\n## Non-goals\n- None\n## Acceptance criteria\n- Works"
            )
        else:
            # Reviewer: write report with FAIL verdict
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text("Review report.\n\nVERDICT: FAIL\n")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch("workbench.final_review.create_pr", new_callable=AsyncMock) as mock_pr,
    ):
        # Don't skip PR — but verdict is FAIL so it shouldn't be called
        kwargs = {**base_kwargs, "skip_pr": False}
        record = await run_final_review(**kwargs)

    assert record.verdict == "fail"
    assert record.pr_url is None
    mock_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_reviewer_pass_opens_pr(base_kwargs, tmp_repo):
    """Report ends in VERDICT: PASS, mocked create_pr returns URL."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    call_count = {"n": 0}

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.03})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(
                "## Requirements\n- Feature\n## Non-goals\n- None\n## Acceptance criteria\n- Pass"
            )
        else:
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text("All good.\n\nVERDICT: PASS\n")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch(
            "workbench.final_review.create_pr",
            new_callable=AsyncMock,
            return_value=(True, "https://github.com/org/repo/pull/42"),
        ) as mock_pr,
    ):
        kwargs = {**base_kwargs, "skip_pr": False}
        record = await run_final_review(**kwargs)

    assert record.verdict == "pass"
    assert record.pr_url == "https://github.com/org/repo/pull/42"
    mock_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_reviewer_pass_gh_missing_records_null_url(base_kwargs, tmp_repo):
    """Mocked create_pr returns (False, ...) → verdict='pass', pr_url is None."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    call_count = {"n": 0}

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(
                "## Requirements\n- A\n## Non-goals\n- B\n## Acceptance criteria\n- C"
            )
        else:
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text("Fine.\n\nVERDICT: PASS\n")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch(
            "workbench.final_review.create_pr",
            new_callable=AsyncMock,
            return_value=(False, "gh missing"),
        ),
    ):
        kwargs = {**base_kwargs, "skip_pr": False}
        record = await run_final_review(**kwargs)

    assert record.verdict == "pass"
    assert record.pr_url is None


@pytest.mark.asyncio
async def test_skip_pr_does_not_open_pr_on_pass(base_kwargs, tmp_repo):
    """With skip_pr=True and pass verdict, create_pr is never awaited."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    call_count = {"n": 0}

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(
                "## Requirements\n- X\n## Non-goals\n- Y\n## Acceptance criteria\n- Z"
            )
        else:
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text("LGTM.\n\nVERDICT: PASS\n")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch("workbench.final_review.create_pr", new_callable=AsyncMock) as mock_pr,
    ):
        record = await run_final_review(**base_kwargs)  # skip_pr=True in base_kwargs

    assert record.verdict == "pass"
    assert record.pr_url is None
    mock_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_appended_to_status_yaml(base_kwargs, tmp_repo):
    """After run, SessionStatus.load(...) includes the new record."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    call_count = {"n": 0}

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.05})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(
                "## Requirements\n- R\n## Non-goals\n- N\n## Acceptance criteria\n- A"
            )
        else:
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text("Report.\n\nVERDICT: PASS\n")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
    ):
        await run_final_review(**base_kwargs)

    status = SessionStatus.load(tmp_repo, "my-plan", "workbench-1")
    assert status is not None
    assert len(status.final_reviews) == 1
    assert status.final_reviews[0].verdict == "pass"


@pytest.mark.asyncio
async def test_worktree_creation_failure_persists_record_and_raises(base_kwargs, tmp_repo):
    """Worktree creation failure persists an error record AND propagates the exception."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.02})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        req_path.parent.mkdir(parents=True, exist_ok=True)
        req_path.write_text(
            "## Requirements\n- Stuff\n## Non-goals\n- None\n## Acceptance criteria\n- Works"
        )
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    def fail_worktree(*args, **kwargs):
        raise RuntimeError(
            "git worktree add (exit 128): fatal: 'feature-x' is already checked out"
        )

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree", side_effect=fail_worktree),
        patch("workbench.final_review._cleanup_review_worktree"),
    ):
        with pytest.raises(RuntimeError, match="already checked out"):
            await run_final_review(**base_kwargs)

    status = SessionStatus.load(tmp_repo, "my-plan", "workbench-1")
    assert status is not None
    assert len(status.final_reviews) == 1
    assert status.final_reviews[0].verdict == "error"


def _make_pass_fake_exec(req_path: Path, review_path: Path):
    """Return a fake_exec helper that writes requirements then a PASS report."""
    call_count = {"n": 0}

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(
                "## Requirements\n- R\n## Non-goals\n- N\n## Acceptance criteria\n- A"
            )
        elif call_count["n"] == 2:
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text("Great work.\n\nVERDICT: PASS\n")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    return fake_exec


@pytest.mark.asyncio
async def test_pr_writer_invoked_on_pass(base_kwargs, tmp_repo):
    """On PASS verdict with no pr_body_file, the writer's output drives the PR."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.03})

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_pass_fake_exec(req_path, review_path),
        ),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch(
            "workbench.final_review.run_pr_writer",
            new_callable=AsyncMock,
            return_value=("Custom title", "Custom body"),
        ) as mock_writer,
        patch(
            "workbench.final_review.create_pr",
            new_callable=AsyncMock,
            return_value=(True, "https://github.com/org/repo/pull/7"),
        ) as mock_pr,
    ):
        kwargs = {**base_kwargs, "skip_pr": False}
        record = await run_final_review(**kwargs)

    assert record.verdict == "pass"
    assert record.pr_url == "https://github.com/org/repo/pull/7"
    mock_writer.assert_awaited_once()
    mock_pr.assert_awaited_once()
    _repo, _branch, _base, title_arg, body_arg = mock_pr.await_args.args
    assert title_arg == "Custom title"
    assert body_arg == "Custom body"


@pytest.mark.asyncio
async def test_pr_writer_skipped_when_pr_body_file_provided(base_kwargs, tmp_repo, tmp_path):
    """A user-supplied pr_body_file bypasses the writer entirely."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    body_file = tmp_path / "body.md"
    body_file.write_text("User-supplied body content")

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_pass_fake_exec(req_path, review_path),
        ),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch(
            "workbench.final_review.run_pr_writer",
            new_callable=AsyncMock,
        ) as mock_writer,
        patch(
            "workbench.final_review.create_pr",
            new_callable=AsyncMock,
            return_value=(True, "https://github.com/org/repo/pull/8"),
        ) as mock_pr,
    ):
        kwargs = {**base_kwargs, "skip_pr": False, "pr_body_file": body_file}
        await run_final_review(**kwargs)

    mock_writer.assert_not_awaited()
    mock_pr.assert_awaited_once()
    _repo, _branch, _base, _title, body_arg = mock_pr.await_args.args
    assert body_arg == "User-supplied body content"


@pytest.mark.asyncio
async def test_pr_writer_error_falls_back_to_plan_body(base_kwargs, tmp_repo):
    """PrWriterAgentError → fallback body is plan-derived (contains Context text)."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_pass_fake_exec(req_path, review_path),
        ),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch(
            "workbench.final_review.run_pr_writer",
            new_callable=AsyncMock,
            side_effect=PrWriterAgentError("agent crashed"),
        ),
        patch(
            "workbench.final_review.create_pr",
            new_callable=AsyncMock,
            return_value=(True, "https://github.com/org/repo/pull/9"),
        ) as mock_pr,
    ):
        kwargs = {**base_kwargs, "skip_pr": False}
        record = await run_final_review(**kwargs)

    assert record.verdict == "pass"
    mock_pr.assert_awaited_once()
    _repo, _branch, _base, _title, body_arg = mock_pr.await_args.args
    assert "Some context." in body_arg


@pytest.mark.asyncio
async def test_pr_writer_parse_error_falls_back_to_plan_body(base_kwargs, tmp_repo):
    """PrWriterParseError → fallback body is plan-derived (contains Context text)."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_pass_fake_exec(req_path, review_path),
        ),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch(
            "workbench.final_review.run_pr_writer",
            new_callable=AsyncMock,
            side_effect=PrWriterParseError("bad format"),
        ),
        patch(
            "workbench.final_review.create_pr",
            new_callable=AsyncMock,
            return_value=(True, "https://github.com/org/repo/pull/10"),
        ) as mock_pr,
    ):
        kwargs = {**base_kwargs, "skip_pr": False}
        record = await run_final_review(**kwargs)

    assert record.verdict == "pass"
    mock_pr.assert_awaited_once()
    _repo, _branch, _base, _title, body_arg = mock_pr.await_args.args
    assert "Some context." in body_arg


@pytest.mark.asyncio
async def test_pr_writer_not_called_on_fail_verdict(base_kwargs, tmp_repo):
    """A FAIL verdict skips both the PR writer and create_pr."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    call_count = {"n": 0}

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(
                "## Requirements\n- R\n## Non-goals\n- N\n## Acceptance criteria\n- A"
            )
        else:
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text("Nope.\n\nVERDICT: FAIL\n")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch("workbench.final_review.run_pr_writer", new_callable=AsyncMock) as mock_writer,
        patch("workbench.final_review.create_pr", new_callable=AsyncMock) as mock_pr,
    ):
        kwargs = {**base_kwargs, "skip_pr": False}
        record = await run_final_review(**kwargs)

    assert record.verdict == "fail"
    mock_writer.assert_not_awaited()
    mock_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_writer_not_called_when_skip_pr_true(base_kwargs, tmp_repo):
    """PASS + skip_pr=True still skips the PR writer."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_pass_fake_exec(req_path, review_path),
        ),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch("workbench.final_review.run_pr_writer", new_callable=AsyncMock) as mock_writer,
        patch("workbench.final_review.create_pr", new_callable=AsyncMock) as mock_pr,
    ):
        # skip_pr=True (default in base_kwargs)
        record = await run_final_review(**base_kwargs)

    assert record.verdict == "pass"
    mock_writer.assert_not_awaited()
    mock_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_title_cli_override_wins_over_agent_title(base_kwargs, tmp_repo):
    """An explicit pr_title overrides the agent-authored title."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_pass_fake_exec(req_path, review_path),
        ),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch(
            "workbench.final_review.run_pr_writer",
            new_callable=AsyncMock,
            return_value=("Agent title", "Agent body"),
        ),
        patch(
            "workbench.final_review.create_pr",
            new_callable=AsyncMock,
            return_value=(True, "https://github.com/org/repo/pull/11"),
        ) as mock_pr,
    ):
        kwargs = {**base_kwargs, "skip_pr": False, "pr_title": "CLI title"}
        await run_final_review(**kwargs)

    mock_pr.assert_awaited_once()
    _repo, _branch, _base, title_arg, body_arg = mock_pr.await_args.args
    assert title_arg == "CLI title"
    assert body_arg == "Agent body"


@pytest.mark.asyncio
async def test_review_artifacts_land_in_plan_folder(base_kwargs, tmp_repo):
    """Regression: requirements.md and review.md must live under .workbench/<plan>/wrap-up/<session>/."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    call_count = {"n": 0}

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(
                "## Requirements\n- R\n## Non-goals\n- N\n## Acceptance criteria\n- A"
            )
        else:
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text("All good.\n\nVERDICT: PASS\n")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch("workbench.final_review.create_pr", new_callable=AsyncMock),
    ):
        await run_final_review(**base_kwargs)

    expected_requirements = (
        tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "requirements.md"
    )
    expected_report = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "review.md"
    assert expected_requirements.exists()
    assert expected_report.exists()
    assert not (tmp_repo / ".workbench" / "reviews").exists()
    assert not (tmp_repo / ".workbench" / "reviews-workbench-1").exists()


@pytest.mark.asyncio
async def test_concurrent_runs_rejected_by_lock(base_kwargs, tmp_repo):
    """Two concurrent invocations → the second raises RuntimeError."""
    # Pre-create the lock file
    lock_path = tmp_repo / ".workbench" / "my-plan" / ".review.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("")

    with pytest.raises(RuntimeError, match="Another final-review is running"):
        await run_final_review(**base_kwargs)


# ── _create_review_worktree unit tests ───────────────────────────────


def test_create_review_worktree_uses_detach():
    from workbench.final_review import _create_review_worktree

    with patch("workbench.final_review.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        with patch.object(Path, "exists", return_value=False):
            _create_review_worktree(
                repo=Path("/repo"), wt_path=Path("/wt"), session_branch="feature-x"
            )

    add_call = mock_run.call_args_list[-1]
    cmd = add_call.args[0]
    assert cmd[:3] == ["git", "worktree", "add"]
    assert "--detach" in cmd
    detach_idx = cmd.index("--detach")
    add_idx = cmd.index("add")
    path_idx = cmd.index("/wt")
    assert add_idx < detach_idx < path_idx


def test_create_review_worktree_removes_stale_path_first():
    from workbench.final_review import _create_review_worktree

    with patch("workbench.final_review.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        with patch.object(Path, "exists", return_value=True):
            _create_review_worktree(
                repo=Path("/repo"), wt_path=Path("/wt"), session_branch="feature-x"
            )

    assert len(mock_run.call_args_list) == 2
    first_cmd = mock_run.call_args_list[0].args[0]
    second_cmd = mock_run.call_args_list[1].args[0]
    assert first_cmd[:3] == ["git", "worktree", "remove"]
    assert second_cmd[:3] == ["git", "worktree", "add"]


def test_create_review_worktree_raises_runtime_error_with_stderr_on_failure():
    from workbench.final_review import _create_review_worktree

    def fake_run(cmd, **kwargs):
        if "add" in cmd:
            return MagicMock(
                returncode=128,
                stderr="fatal: 'feature-x' is already checked out at '/some/path'",
                stdout="",
            )
        return MagicMock(returncode=0, stderr="", stdout="")

    with patch("workbench.final_review.subprocess.run", side_effect=fake_run):
        with patch.object(Path, "exists", return_value=False):
            with pytest.raises(RuntimeError) as exc_info:
                _create_review_worktree(
                    repo=Path("/repo"),
                    wt_path=Path("/wt"),
                    session_branch="feature-x",
                )

    msg = str(exc_info.value)
    assert "exit 128" in msg
    assert "already checked out" in msg


def test_create_review_worktree_falls_back_to_stdout_when_stderr_empty():
    from workbench.final_review import _create_review_worktree

    def fake_run(cmd, **kwargs):
        if "add" in cmd:
            return MagicMock(returncode=128, stderr="", stdout="some stdout info")
        return MagicMock(returncode=0, stderr="", stdout="")

    with patch("workbench.final_review.subprocess.run", side_effect=fake_run):
        with patch.object(Path, "exists", return_value=False):
            with pytest.raises(RuntimeError) as exc_info:
                _create_review_worktree(
                    repo=Path("/repo"),
                    wt_path=Path("/wt"),
                    session_branch="feature-x",
                )

    assert "some stdout info" in str(exc_info.value)


def test_create_review_worktree_no_remove_when_path_absent():
    from workbench.final_review import _create_review_worktree

    with patch("workbench.final_review.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        with patch.object(Path, "exists", return_value=False):
            _create_review_worktree(
                repo=Path("/repo"), wt_path=Path("/wt"), session_branch="feature-x"
            )

    assert mock_run.call_count == 1
    assert mock_run.call_args_list[0].args[0][:3] == ["git", "worktree", "add"]


# ── Post-task progress table tests ───────────────────────────────────


def _table_text(table) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=200).print(table)
    return buf.getvalue()


def test_post_task_table_renders_pending_initial_state():
    states = [
        PostTaskAgentState(name="Summarizer"),
        PostTaskAgentState(name="Branch reviewer"),
        PostTaskAgentState(name="PR writer"),
    ]
    table = _post_task_table(states)
    text = _table_text(table)
    assert text.count("pending") == 3
    assert "Summarizer" in text
    assert "Branch reviewer" in text
    assert "PR writer" in text


def test_post_task_table_renders_mixed_statuses():
    states = [
        PostTaskAgentState(name="Summarizer", status=PostTaskAgentStatus.DONE),
        PostTaskAgentState(name="Branch reviewer", status=PostTaskAgentStatus.RUNNING),
        PostTaskAgentState(name="PR writer", status=PostTaskAgentStatus.SKIPPED),
    ]
    text = _table_text(_post_task_table(states))
    assert "done" in text
    assert "running" in text
    assert "skipped" in text

    states_failed = [PostTaskAgentState(name="X", status=PostTaskAgentStatus.FAILED)]
    text2 = _table_text(_post_task_table(states_failed))
    assert "failed" in text2


def test_post_task_agent_state_elapsed_returns_dash_when_not_started():
    s = PostTaskAgentState(name="x")
    assert s.elapsed == "-"


def test_post_task_agent_state_elapsed_formats_running_time():
    s = PostTaskAgentState(name="x", started_at=time.time() - 65)
    assert s.elapsed in {"1m04s", "1m05s", "1m06s"}


def test_post_task_agent_state_elapsed_formats_finished_time():
    s = PostTaskAgentState(name="x", started_at=100.0, finished_at=225.0)
    assert s.elapsed == "2m05s"


@pytest.mark.asyncio
async def test_run_final_review_transitions_states(base_kwargs, tmp_repo):
    """States progress from pending → running → done across all three agents."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    states_ref: list = []
    real_renderer = _post_task_table

    def spy(states):
        if not states_ref:
            states_ref.append(states)
        return real_renderer(states)

    snapshots: list[list[PostTaskAgentStatus]] = []

    def snap():
        if states_ref:
            snapshots.append([s.status for s in states_ref[0]])

    def at_worktree(*args, **kwargs):
        snap()

    async def at_pr_writer(*args, **kwargs):
        snap()
        return ("t", "b")

    async def at_create_pr(*args, **kwargs):
        snap()
        return (True, "https://example/pr/1")

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_pass_fake_exec(req_path, review_path),
        ),
        patch("workbench.final_review._create_review_worktree", side_effect=at_worktree),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch(
            "workbench.final_review.run_pr_writer",
            new_callable=AsyncMock,
            side_effect=at_pr_writer,
        ),
        patch(
            "workbench.final_review.create_pr",
            new_callable=AsyncMock,
            side_effect=at_create_pr,
        ),
        patch(
            "workbench.final_review.push_session_branch",
            return_value=(True, ""),
        ),
        patch("workbench.final_review._post_task_table", side_effect=spy),
    ):
        kwargs = {**base_kwargs, "skip_pr": False}
        await run_final_review(**kwargs)

    final = states_ref[0]
    assert all(s.status == PostTaskAgentStatus.DONE for s in final)

    assert snapshots[0] == [
        PostTaskAgentStatus.DONE,
        PostTaskAgentStatus.RUNNING,
        PostTaskAgentStatus.PENDING,
    ]

    assert snapshots[1] == [
        PostTaskAgentStatus.DONE,
        PostTaskAgentStatus.DONE,
        PostTaskAgentStatus.RUNNING,
    ]


@pytest.mark.asyncio
async def test_run_final_review_marks_pr_writer_skipped_when_skip_pr(base_kwargs, tmp_repo):
    """skip_pr=True → states[2] ends in SKIPPED with note containing 'skip_pr'."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    captured: dict = {}
    real_renderer = _post_task_table

    def spy(states):
        captured["last"] = states
        return real_renderer(states)

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_pass_fake_exec(req_path, review_path),
        ),
        patch("workbench.final_review._create_review_worktree"),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch("workbench.final_review._post_task_table", side_effect=spy),
    ):
        await run_final_review(**base_kwargs)

    states = captured["last"]
    assert states[2].status == PostTaskAgentStatus.SKIPPED
    assert "skip_pr" in states[2].note


@pytest.mark.asyncio
async def test_run_final_review_marks_failed_state_on_agent_failure(base_kwargs, tmp_repo):
    """Branch reviewer raising → states[1] FAILED with note containing exception text."""
    wrap_up_dir = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1"
    req_path = wrap_up_dir / "requirements.md"

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"cost_usd": 0.01})

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        req_path.parent.mkdir(parents=True, exist_ok=True)
        req_path.write_text("## Requirements\n- R\n## Non-goals\n- N\n## Acceptance criteria\n- A")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        return proc

    def boom(*args, **kwargs):
        raise RuntimeError("worktree explosion")

    captured: dict = {}
    real_renderer = _post_task_table

    def spy(states):
        captured["last"] = states
        return real_renderer(states)

    with (
        patch("workbench.final_review.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("workbench.final_review._create_review_worktree", side_effect=boom),
        patch("workbench.final_review._cleanup_review_worktree"),
        patch("workbench.final_review._post_task_table", side_effect=spy),
    ):
        with pytest.raises(RuntimeError, match="worktree explosion"):
            await run_final_review(**base_kwargs)

    states = captured["last"]
    assert states[1].status == PostTaskAgentStatus.FAILED
    assert "worktree explosion" in states[1].note
    assert len(states[1].note) <= 120
