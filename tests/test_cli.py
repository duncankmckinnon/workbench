"""Tests for CLI flags and commands."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from workbench.cli import _discover_skills, _ensure_workbench_dir, _get_skills_dir, main

# ---------------------------------------------------------------------------
# _ensure_workbench_dir
# ---------------------------------------------------------------------------


def test_ensure_workbench_dir_creates(tmp_path):
    wb = _ensure_workbench_dir(tmp_path)
    assert wb == tmp_path / ".workbench"
    assert wb.is_dir()


def test_ensure_workbench_dir_idempotent(tmp_path):
    (tmp_path / ".workbench").mkdir()
    wb = _ensure_workbench_dir(tmp_path)
    assert wb.is_dir()


# ---------------------------------------------------------------------------
# skill discovery helpers
# ---------------------------------------------------------------------------


def test_get_skills_dir_exists():
    skills_dir = _get_skills_dir()
    assert skills_dir.is_dir()


def test_discover_skills_finds_bundled():
    skills_dir = _get_skills_dir()
    skills = _discover_skills(skills_dir)
    assert len(skills) >= 1
    for name, path in skills:
        assert path.name == "SKILL.md"
        assert path.exists()


def test_discover_skills_ignores_non_skill_dirs(tmp_path):
    # A directory without SKILL.md should be ignored
    (tmp_path / "not-a-skill").mkdir()
    (tmp_path / "real-skill").mkdir()
    (tmp_path / "real-skill" / "SKILL.md").write_text("# Skill")
    skills = _discover_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0][0] == "real-skill"


# ---------------------------------------------------------------------------
# wb run --no-tmux flag
# ---------------------------------------------------------------------------


def test_run_no_tmux_skips_check(git_repo, tmp_path):
    """--no-tmux should skip the tmux availability check entirely."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with (
        patch("workbench.cli.run_plan") as mock_run_plan,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        # Make run_plan a coroutine that returns immediately
        import asyncio

        async def fake_run_plan(**kwargs):
            return []

        mock_run_plan.side_effect = lambda **kwargs: fake_run_plan(**kwargs)

        # Patch asyncio.run to actually run our coroutine
        with patch("workbench.cli.asyncio") as mock_asyncio:
            mock_asyncio.run = lambda coro: (
                asyncio.get_event_loop().run_until_complete(coro)
                if asyncio.get_event_loop().is_running()
                else asyncio.new_event_loop().run_until_complete(coro)
            )
            result = runner.invoke(main, ["run", str(plan), "--no-tmux"])

    # Should not fail with tmux error
    assert "tmux is required" not in (result.output or "")


def test_run_without_tmux_shows_error(git_repo, tmp_path):
    """Without --no-tmux and without tmux installed, should show error."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=False),
    ):
        result = runner.invoke(main, ["run", str(plan)])

    assert result.exit_code != 0
    assert "tmux is required" in result.output


# ---------------------------------------------------------------------------
# wb init --agent manual
# ---------------------------------------------------------------------------


def test_init_manual_prints_skill_paths():
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--agent", "manual"])
    assert result.exit_code == 0
    assert "Skill files directory:" in result.output
    assert "use-workbench" in result.output


def test_init_claude_copies_skills(tmp_path):
    """wb init --agent claude should copy skill folders to ~/.claude/skills/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["init", "--agent", "claude"])

    assert result.exit_code == 0
    skills_dir = fake_home / ".claude" / "skills"
    assert skills_dir.is_dir()
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()
    assert "Copied" in result.output


def test_init_claude_symlinks(tmp_path):
    """wb init --agent claude --symlink should symlink skill dirs."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["init", "--agent", "claude", "--symlink"])

    assert result.exit_code == 0
    dest = fake_home / ".claude" / "skills" / "use-workbench"
    assert dest.is_symlink()
    assert "Linked" in result.output


def test_init_codex_appends_to_instructions(tmp_path, monkeypatch):
    """wb init --agent codex should append skills to .codex/instructions.md."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--agent", "codex"])

    assert result.exit_code == 0
    instructions = tmp_path / ".codex" / "instructions.md"
    assert instructions.exists()
    content = instructions.read_text()
    assert len(content) > 0
    assert "Appended" in result.output


def test_init_codex_skips_duplicates(tmp_path, monkeypatch):
    """wb init --agent codex should skip skills whose name appears in instructions.md."""
    monkeypatch.chdir(tmp_path)
    # Pre-populate instructions.md with the marker so the check triggers
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "instructions.md").write_text(
        "<!-- workbench-skill:use-workbench -->\n# Existing skill\n"
    )

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--agent", "codex"])

    assert result.exit_code == 0
    assert "Skipping" in result.output


def test_init_cursor_copies_to_project(tmp_path, monkeypatch):
    """wb init --agent cursor should copy to .cursor/rules/ in cwd."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--agent", "cursor"])

    assert result.exit_code == 0
    rules_dir = tmp_path / ".cursor" / "rules"
    assert rules_dir.is_dir()
    assert (rules_dir / "use-workbench.md").exists()


# ---------------------------------------------------------------------------
# wb setup
# ---------------------------------------------------------------------------


def test_setup_creates_workbench_dir_and_installs(git_repo):
    """wb setup should create .workbench/ and install skills."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "manual"])

    assert result.exit_code == 0
    assert (git_repo / ".workbench").is_dir()
    assert "Skill files directory:" in result.output
    assert "Repo is ready" in result.output


def test_setup_existing_workbench_dir(git_repo):
    """wb setup with existing .workbench/ should say 'Already exists'."""
    (git_repo / ".workbench").mkdir()
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "manual"])

    assert result.exit_code == 0
    assert "Already exists" in result.output


# ---------------------------------------------------------------------------
# wb run directive options
# ---------------------------------------------------------------------------


def test_run_directive_options_accepted(git_repo, tmp_path):
    """Directive options should be accepted without error."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    # Just test that the options are parsed - use --no-tmux to skip tmux check
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.asyncio") as mock_asyncio,
    ):
        mock_asyncio.run = lambda coro: None
        result = runner.invoke(
            main,
            [
                "run",
                str(plan),
                "--no-tmux",
                "--implementor-directive",
                "Be concise",
                "--tester-directive",
                "Focus on edge cases",
            ],
        )

    # Should parse without error (may fail later due to mock, but options are valid)
    assert "no such option" not in (result.output or "").lower()


# ---------------------------------------------------------------------------
# wb status and wb clean call _ensure_workbench_dir
# ---------------------------------------------------------------------------


def test_status_creates_workbench_dir(git_repo):
    """wb status should ensure .workbench/ exists."""
    runner = CliRunner()
    assert not (git_repo / ".workbench").exists()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["status"])

    assert (git_repo / ".workbench").is_dir()


def test_clean_creates_workbench_dir(git_repo):
    """wb clean should ensure .workbench/ exists."""
    runner = CliRunner()
    assert not (git_repo / ".workbench").exists()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["clean", "--yes"])

    assert (git_repo / ".workbench").is_dir()


# ---------------------------------------------------------------------------
# wb run --tdd flag
# ---------------------------------------------------------------------------


def test_tdd_flag_accepted(git_repo, tmp_path):
    """wb run plan.md --tdd --no-tmux should be accepted without error."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.asyncio") as mock_asyncio,
    ):
        mock_asyncio.run = lambda coro: None
        result = runner.invoke(main, ["run", str(plan), "--tdd", "--no-tmux"])

    assert "no such option" not in (result.output or "").lower()
    assert "mutually exclusive" not in (result.output or "").lower()


def test_tdd_skip_test_mutually_exclusive(git_repo, tmp_path):
    """wb run plan.md --tdd --skip-test should produce an error."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
    ):
        result = runner.invoke(main, ["run", str(plan), "--tdd", "--skip-test"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# wb stop command
# ---------------------------------------------------------------------------


def test_stop_no_sessions():
    """When tmux has no server, should print 'No active agent sessions'."""
    runner = CliRunner()
    with patch("workbench.cli.subprocess.run") as mock_run:
        # tmux list-sessions returns non-zero when no server
        mock_run.return_value = subprocess.CompletedProcess(
            args=["tmux", "list-sessions", "-F", "#{session_name}"],
            returncode=1,
            stdout="",
            stderr="no server running",
        )
        result = runner.invoke(main, ["stop"])

    assert result.exit_code == 0
    assert "No active agent sessions" in result.output


def test_stop_kills_sessions():
    """Should kill only wb- prefixed sessions and report count."""
    runner = CliRunner()
    with patch("workbench.cli.subprocess.run") as mock_run:

        def side_effect(cmd, **kwargs):
            if cmd[0] == "tmux" and cmd[1] == "list-sessions":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="wb-task-1-implementor\nwb-task-2-tester\nother-session\n",
                    stderr="",
                )
            # kill-session calls
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        result = runner.invoke(main, ["stop"])

    assert result.exit_code == 0
    assert "Stopped 2 agent session(s)" in result.output

    # Verify kill-session was called for each wb- session
    kill_calls = [
        c for c in mock_run.call_args_list if len(c[0][0]) >= 2 and c[0][0][1] == "kill-session"
    ]
    assert len(kill_calls) == 2
    killed_names = [c[0][0][3] for c in kill_calls]
    assert "wb-task-1-implementor" in killed_names
    assert "wb-task-2-tester" in killed_names
    assert "other-session" not in killed_names


def test_stop_with_cleanup(git_repo):
    """wb stop --cleanup should kill sessions and clean up worktrees."""
    runner = CliRunner()
    with (
        patch("workbench.cli.subprocess.run") as mock_run,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):

        def side_effect(cmd, **kwargs):
            if cmd[0] == "tmux" and cmd[1] == "list-sessions":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="no server",
                )
            if cmd[0] == "git" and "worktree" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="worktree /repo/.workbench/task-1\nbranch refs/heads/wb/task-1\n\n",
                    stderr="",
                )
            if cmd[0] == "git" and "branch" in cmd and "--list" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="  wb/task-1\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        result = runner.invoke(main, ["stop", "--cleanup"])

    assert result.exit_code == 0
    assert "No active agent sessions" in result.output
    assert "Cleaned up" in result.output
