"""Tests for CLI flags and commands."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from workbench.cli import (
    _discover_skills,
    _ensure_workbench_dir,
    _get_skills_dir,
    main,
)


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
    with patch("workbench.cli.run_plan") as mock_run_plan, \
         patch("workbench.cli._find_repo_root", return_value=git_repo):
        # Make run_plan a coroutine that returns immediately
        import asyncio

        async def fake_run_plan(**kwargs):
            return []

        mock_run_plan.side_effect = lambda **kwargs: fake_run_plan(**kwargs)

        # Patch asyncio.run to actually run our coroutine
        with patch("workbench.cli.asyncio") as mock_asyncio:
            mock_asyncio.run = lambda coro: asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop().is_running() else asyncio.new_event_loop().run_until_complete(coro)
            result = runner.invoke(main, ["run", str(plan), "--no-tmux"])

    # Should not fail with tmux error
    assert "tmux is required" not in (result.output or "")


def test_run_without_tmux_shows_error(git_repo, tmp_path):
    """Without --no-tmux and without tmux installed, should show error."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo), \
         patch("workbench.cli.check_tmux_available", return_value=False):
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
    assert "Skills directory:" in result.output
    assert "use-workbench" in result.output


def test_init_claude_copies_skills(tmp_path):
    """wb init --agent claude should copy skill files to ~/.claude/commands/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["init", "--agent", "claude"])

    assert result.exit_code == 0
    commands_dir = fake_home / ".claude" / "commands"
    assert commands_dir.is_dir()
    assert (commands_dir / "use-workbench.md").exists()
    assert "Copied" in result.output


def test_init_claude_symlinks(tmp_path):
    """wb init --agent claude --symlink should create symlinks."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["init", "--agent", "claude", "--symlink"])

    assert result.exit_code == 0
    dest = fake_home / ".claude" / "commands" / "use-workbench.md"
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
    (codex_dir / "instructions.md").write_text("<!-- workbench-skill:use-workbench -->\n# Existing skill\n")

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
    assert "Skills directory:" in result.output
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
    with patch("workbench.cli._find_repo_root", return_value=git_repo), \
         patch("workbench.cli.asyncio") as mock_asyncio:
        mock_asyncio.run = lambda coro: None
        result = runner.invoke(main, [
            "run", str(plan),
            "--no-tmux",
            "--implementor-directive", "Be concise",
            "--tester-directive", "Focus on edge cases",
        ])

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
