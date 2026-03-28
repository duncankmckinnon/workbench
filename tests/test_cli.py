"""Tests for the CLI module."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from workbench.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def plan_file(tmp_path):
    """Write a minimal valid plan to tmp_path."""
    p = tmp_path / "plan.md"
    p.write_text(
        "# Test Plan\n\n"
        "## Task: Build widget\n\n"
        "Implement the widget feature.\n"
    )
    return p


@pytest.fixture
def empty_plan(tmp_path):
    """Write a plan with no tasks."""
    p = tmp_path / "empty.md"
    p.write_text("# Empty Plan\n\nNo tasks here.\n")
    return p


class TestVersion:
    def test_version(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output


class TestPreview:
    def test_preview(self, runner, plan_file):
        result = runner.invoke(main, ["preview", str(plan_file)])
        assert result.exit_code == 0
        assert "Build widget" in result.output

    def test_preview_empty(self, runner, empty_plan):
        result = runner.invoke(main, ["preview", str(empty_plan)])
        assert result.exit_code != 0
        assert "No tasks" in result.output


class TestRun:
    def test_run_no_tmux_available(self, runner, plan_file):
        """Mock check_tmux_available=False, no --no-tmux flag → error with install instructions."""
        with patch("workbench.cli.check_tmux_available", return_value=False):
            result = runner.invoke(main, ["run", str(plan_file)])
        assert result.exit_code != 0
        assert "tmux" in result.output
        assert "install" in result.output.lower()

    def test_run_no_tmux_flag(self, runner, plan_file):
        """--no-tmux skips tmux check, proceeds to run (mock orchestrator)."""
        with patch("workbench.cli.check_tmux_available") as mock_check, \
             patch("workbench.cli.run_plan", return_value=[]) as mock_run:
            result = runner.invoke(main, ["run", str(plan_file), "--no-tmux"])
        # tmux check should NOT have been called
        mock_check.assert_not_called()
        assert result.exit_code == 0

    def test_run_outside_git_repo(self, runner, tmp_path, plan_file):
        """Invoke from non-git dir → 'Not in a git repository'."""
        with patch("workbench.cli.check_tmux_available", return_value=True), \
             runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["run", str(plan_file)])
        assert result.exit_code != 0
        assert "Not in a git repository" in result.output


class TestInit:
    def test_init_manual(self, runner):
        """wb init --agent manual prints skill file paths."""
        result = runner.invoke(main, ["init", "--agent", "manual"])
        assert result.exit_code == 0
        assert "implement.md" in result.output
        assert "review.md" in result.output

    def test_init_claude(self, runner, tmp_path):
        """wb init --agent claude creates symlinks in ~/.claude/commands/."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch("workbench.cli.Path.home", return_value=fake_home):
            result = runner.invoke(main, ["init", "--agent", "claude"])
        assert result.exit_code == 0
        commands_dir = fake_home / ".claude" / "commands"
        assert commands_dir.exists()
        links = list(commands_dir.iterdir())
        assert len(links) >= 2
        names = {l.name for l in links}
        assert "implement.md" in names
        assert "review.md" in names
        # Verify they are symlinks
        for link in links:
            assert link.is_symlink()
