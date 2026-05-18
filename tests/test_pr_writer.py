"""Tests for workbench.pr_writer — PR-writer agent orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workbench.pr_writer import (
    PrWriterAgentError,
    PrWriterParseError,
    derive_body_from_plan,
    derive_title_from_plan,
    run_pr_writer,
)
from workbench.profile import Profile, RoleConfig

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_worktree_helpers():
    """Stub the session-branch worktree creation/cleanup.

    The real helpers shell out to `git worktree add <path> <branch>`, which
    requires the branch to exist in the test repo. These tests don't initialize
    a real git repo, so we make the helpers no-ops; tests that need to verify
    cwd behavior can override locally.
    """
    with (
        patch("workbench.pr_writer._create_pr_writer_worktree"),
        patch("workbench.pr_writer._cleanup_pr_writer_worktree"),
    ):
        yield


@pytest.fixture
def tmp_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_dir = repo / ".workbench" / "my-plan"
    plan_dir.mkdir(parents=True)
    plan_source = plan_dir / "plan.md"
    plan_source.write_text(
        "# My Plan\n\n## Context\n\nSome context goes here.\n\n## Task: T\n\nDetails.\n"
    )
    return repo


@pytest.fixture
def plan_source(tmp_repo):
    return tmp_repo / ".workbench" / "my-plan" / "plan.md"


@pytest.fixture
def base_kwargs(tmp_repo, plan_source):
    return dict(
        repo=tmp_repo,
        session_branch="workbench-1",
        plan_slug="my-plan",
        base_branch="main",
        plan_source=plan_source,
        merged_task_titles=["Task one"],
        agent_cmd="claude",
        use_tmux=False,
    )


def _git_proc_factory(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """Build an async-callable that returns a fake git subprocess."""

    async def fake_git(*cmd, cwd=None, stdout=None, stderr=None):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        return proc

    return fake_git


def _patch_git(returncode: int = 0, stdout: bytes = b"file.py | 2 +-\n"):
    """Patch asyncio.create_subprocess_exec to act like git succeeding."""
    return patch(
        "asyncio.create_subprocess_exec",
        side_effect=_git_proc_factory(returncode=returncode, stdout=stdout),
    )


def _make_adapter(
    write_path: Path | None,
    write_content: str,
):
    """Create a mock adapter; calling build_command schedules `write_content`."""
    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("ok", {"cost_usd": 0.0})

    # Patch a side-effect into asyncio.create_subprocess_exec via the adapter:
    # we instead simulate write within the subprocess wrapper below.
    adapter._write_path = write_path
    adapter._write_content = write_content
    return adapter


def _exec_factory(
    pr_write_path: Path | None,
    pr_write_content: str,
    pr_returncode: int = 0,
):
    """Build an async create_subprocess_exec stub.

    git invocations succeed and return a stub diff stat. The third call
    (the agent) optionally writes the pr-body.md and returns `pr_returncode`.
    """
    call_count = {"n": 0}

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        call_count["n"] += 1
        proc = MagicMock()
        if call_count["n"] <= 2:
            # git diff / git log
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"git output\n", b""))
            return proc
        # Agent call
        if pr_write_path is not None and pr_returncode == 0:
            pr_write_path.parent.mkdir(parents=True, exist_ok=True)
            pr_write_path.write_text(pr_write_content, encoding="utf-8")
        proc.returncode = pr_returncode
        proc.communicate = AsyncMock(return_value=(b"agent output", b""))
        return proc

    return fake_exec, call_count


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_writes_pr_body_and_returns_title_body(base_kwargs, tmp_repo):
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    adapter = _make_adapter(output, "# Add feature X\n\nDoes the thing.\n")
    fake_exec, _ = _exec_factory(output, "# Add feature X\n\nDoes the thing.\n")

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        title, body = await run_pr_writer(**base_kwargs)

    assert title == "Add feature X"
    assert body == "Does the thing."
    assert output.exists()


@pytest.mark.asyncio
async def test_existing_pr_body_is_overwritten(base_kwargs, tmp_repo):
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("stale stale stale", encoding="utf-8")

    new_content = "# Fresh title\n\nFresh body content here.\n"
    adapter = _make_adapter(output, new_content)
    fake_exec, _ = _exec_factory(output, new_content)

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        title, body = await run_pr_writer(**base_kwargs)

    assert title == "Fresh title"
    assert "stale" not in output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_missing_context_section_uses_h1_only(base_kwargs, tmp_repo, plan_source):
    plan_source.write_text("# Title Only\n\nNo context section here.\n")
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    rendered_prompts: list[str] = []

    adapter = MagicMock()

    def build_command(prompt, _cwd):
        rendered_prompts.append(prompt)
        return ["echo", "ok"]

    adapter.build_command.side_effect = build_command
    adapter.parse_output.return_value = ("ok", {"cost_usd": 0.0})

    fake_exec, _ = _exec_factory(output, "# T\n\nbody-content-here\n")

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        await run_pr_writer(**base_kwargs)

    assert rendered_prompts, "expected adapter.build_command to be invoked"
    assert "# Title Only" in rendered_prompts[0]


@pytest.mark.asyncio
async def test_agent_nonzero_raises_pr_writer_agent_error(base_kwargs, tmp_repo):
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    adapter = _make_adapter(None, "")
    fake_exec, _ = _exec_factory(output, "", pr_returncode=1)

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        with pytest.raises(PrWriterAgentError):
            await run_pr_writer(**base_kwargs)


@pytest.mark.asyncio
async def test_agent_writes_nothing_raises_pr_writer_agent_error(base_kwargs):
    adapter = _make_adapter(None, "")
    fake_exec, _ = _exec_factory(None, "", pr_returncode=0)

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        with pytest.raises(PrWriterAgentError, match="did not write"):
            await run_pr_writer(**base_kwargs)


@pytest.mark.asyncio
async def test_missing_title_heading_raises_parse_error(base_kwargs, tmp_repo):
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    adapter = _make_adapter(output, "no title heading here, just text\n")
    fake_exec, _ = _exec_factory(output, "no title heading here, just text\n")

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        with pytest.raises(PrWriterParseError):
            await run_pr_writer(**base_kwargs)


@pytest.mark.asyncio
async def test_empty_body_raises_parse_error(base_kwargs, tmp_repo):
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    adapter = _make_adapter(output, "# Only Title\n")
    fake_exec, _ = _exec_factory(output, "# Only Title\n")

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        with pytest.raises(PrWriterParseError):
            await run_pr_writer(**base_kwargs)


@pytest.mark.asyncio
async def test_title_truncated_at_200_chars(base_kwargs, tmp_repo):
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    long_title = "A" * 500
    content = f"# {long_title}\n\nplenty of body content right here.\n"
    adapter = _make_adapter(output, content)
    fake_exec, _ = _exec_factory(output, content)

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        title, _body = await run_pr_writer(**base_kwargs)

    assert len(title) == 200
    assert title.endswith("…")


@pytest.mark.asyncio
async def test_git_diff_failure_raises_agent_error(base_kwargs):
    """A non-zero `git diff` should raise PrWriterAgentError before invoking the agent."""
    adapter = _make_adapter(None, "")

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        proc = MagicMock()
        proc.returncode = 128
        proc.communicate = AsyncMock(return_value=(b"", b"fatal: bad revision\n"))
        return proc

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        with pytest.raises(PrWriterAgentError, match="git command failed"):
            await run_pr_writer(**base_kwargs)


@pytest.mark.asyncio
async def test_profile_pr_writer_agent_used_when_provided(base_kwargs, tmp_repo):
    """profile.pr_writer.agent overrides the default 'claude' agent."""
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    profile = Profile.default()
    profile.pr_writer = RoleConfig(agent="codex", directive="")
    base_kwargs["profile"] = profile

    captured: list[str] = []

    def fake_get_adapter(agent_cmd, _config_path):
        captured.append(agent_cmd)
        adapter = MagicMock()
        adapter.build_command.return_value = ["echo", "ok"]
        adapter.parse_output.return_value = ("ok", {"cost_usd": 0.0})
        return adapter

    fake_exec, _ = _exec_factory(output, "# Title\n\nbody content here.\n")

    with (
        patch("workbench.pr_writer.get_adapter", side_effect=fake_get_adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        await run_pr_writer(**base_kwargs)

    assert captured == ["codex"]


@pytest.mark.asyncio
async def test_directive_override_takes_precedence_over_profile(base_kwargs, tmp_repo):
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    profile = Profile.default()
    profile.pr_writer = RoleConfig(agent="claude", directive="FROM_PROFILE")
    base_kwargs["profile"] = profile
    base_kwargs["directive_override"] = "FROM_OVERRIDE_DIRECTIVE_TEXT"

    rendered: list[str] = []

    adapter = MagicMock()

    def build_command(prompt, _cwd):
        rendered.append(prompt)
        return ["echo", "ok"]

    adapter.build_command.side_effect = build_command
    adapter.parse_output.return_value = ("ok", {"cost_usd": 0.0})

    fake_exec, _ = _exec_factory(output, "# T\n\nbody content here.\n")

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        await run_pr_writer(**base_kwargs)

    assert rendered
    prompt = rendered[0]
    assert "FROM_OVERRIDE_DIRECTIVE_TEXT" in prompt
    assert "FROM_PROFILE" not in prompt


# ── Plan-helper tests ────────────────────────────────────────────────


def test_derive_title_from_plan_uses_h1():
    plan = "# Big Project\n\n## Context\n\nstuff\n"
    assert derive_title_from_plan(plan, "big-project") == "Big Project"


def test_derive_title_from_plan_falls_back_to_slug():
    assert derive_title_from_plan("no heading here\n", "my-slug") == "My Slug"


def test_derive_body_includes_context_and_tasks():
    plan = "# Plan\n\n## Context\n\nWhy we're doing this.\n\n## Task: One\n\nDetails.\n"
    body = derive_body_from_plan(plan, ["One", "Two"])
    assert "## Context" in body
    assert "Why we're doing this." in body
    assert "## Tasks" in body
    assert "- One" in body
    assert "- Two" in body


def test_derive_body_omits_context_when_absent():
    body = derive_body_from_plan("# Plan\n\nNo context.\n", ["Task A"])
    assert "## Context" not in body
    assert "- Task A" in body


def test_derive_body_appends_report_footer():
    body = derive_body_from_plan("# Plan\n", ["T"], review_path=Path("wrap-up/review.md"))
    assert "Reviewed by workbench" in body
    assert "wrap-up/review.md" in body


@pytest.mark.asyncio
async def test_agent_runs_in_session_branch_worktree_not_repo(base_kwargs, tmp_repo):
    """The agent subprocess must be invoked with cwd=<worktree>, not cwd=<repo>.

    Without this, the agent's Read/Glob/Bash tools see whatever branch `repo`
    is currently checked out at (typically main) instead of session_branch,
    and silently describes stale code.
    """
    output = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / "pr-body.md"
    captured_cwds: list[str | None] = []

    async def fake_exec(*cmd, cwd=None, stdout=None, stderr=None):
        captured_cwds.append(cwd)
        proc = MagicMock()
        # First two calls are git diff/log — succeed silently.
        if len(captured_cwds) <= 2:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc
        # Third call is the agent — write the file and succeed.
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# A title\n\nbody body body\n", encoding="utf-8")
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("ok", {"cost_usd": 0.0})

    with (
        patch("workbench.pr_writer.get_adapter", return_value=adapter),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        await run_pr_writer(**base_kwargs)

    # The agent call is the 3rd subprocess (after git diff and git log).
    assert len(captured_cwds) >= 3, "expected at least three subprocess invocations"
    agent_cwd = captured_cwds[2]
    expected_wt = tmp_repo / ".workbench" / "my-plan" / "wrap-up" / "workbench-1" / ".pr-writer-wt"
    assert agent_cwd == str(expected_wt), (
        f"Agent must run with cwd={expected_wt}, got cwd={agent_cwd}. "
        "Running from repo root would expose the wrong branch's files."
    )
