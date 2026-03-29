"""Tests for CLI flags and commands."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml
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
# wb init --agent gemini (cross-client skills)
# ---------------------------------------------------------------------------


def test_init_gemini_copies_skills(tmp_path):
    """wb init --agent gemini should copy skill folders to ~/.agents/skills/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["init", "--agent", "gemini"])

    assert result.exit_code == 0
    skills_dir = fake_home / ".agents" / "skills"
    assert skills_dir.is_dir()
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()
    assert "Copied" in result.output


def test_init_gemini_symlinks(tmp_path):
    """wb init --agent gemini --symlink should create symlink at ~/.agents/skills/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["init", "--agent", "gemini", "--symlink"])

    assert result.exit_code == 0
    dest = fake_home / ".agents" / "skills" / "use-workbench"
    assert dest.is_symlink()
    assert "Linked" in result.output


# ---------------------------------------------------------------------------
# wb init --local (cross-client skill install)
# ---------------------------------------------------------------------------


def test_init_claude_local(tmp_path, git_repo):
    """wb init --agent claude --local installs to <repo>/.claude/skills/ and <repo>/.agents/skills/."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["init", "--agent", "claude", "--local"])

    assert result.exit_code == 0
    # Should install to repo-level .claude/skills/
    claude_skill = git_repo / ".claude" / "skills" / "use-workbench" / "SKILL.md"
    assert claude_skill.exists()
    # Should also install to .agents/skills/ for cross-client discoverability
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()
    assert "cross-client" in result.output.lower()


def test_init_gemini_local(tmp_path, git_repo):
    """wb init --agent gemini --local installs to <repo>/.agents/skills/."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["init", "--agent", "gemini", "--local"])

    assert result.exit_code == 0
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()


def test_init_cursor_local_noop(tmp_path, monkeypatch, git_repo):
    """wb init --agent cursor --local is a no-op (cursor is always project-level), prints note."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["init", "--agent", "cursor", "--local"])

    assert result.exit_code == 0
    # Cursor should still install to .cursor/rules/ as normal
    rules_dir = tmp_path / ".cursor" / "rules"
    assert rules_dir.is_dir()
    # Should also install to .agents/skills/ for cross-client discoverability
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()


def test_init_codex_local_noop(tmp_path, monkeypatch, git_repo):
    """wb init --agent codex --local is a no-op (codex is always project-level), prints note."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["init", "--agent", "codex", "--local"])

    assert result.exit_code == 0
    # Codex should still append to .codex/instructions.md as normal
    instructions = tmp_path / ".codex" / "instructions.md"
    assert instructions.exists()
    # Should also install to .agents/skills/ for cross-client discoverability
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()


def test_init_local_requires_repo(tmp_path, monkeypatch):
    """wb init --local should fail when repo root cannot be found."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", side_effect=SystemExit(1)):
        result = runner.invoke(main, ["init", "--agent", "claude", "--local"])

    assert result.exit_code != 0


def test_init_claude_local_symlinks(git_repo):
    """wb init --agent claude --local --symlink should symlink at repo level."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["init", "--agent", "claude", "--local", "--symlink"])

    assert result.exit_code == 0
    dest = git_repo / ".claude" / "skills" / "use-workbench"
    assert dest.is_symlink()
    # Cross-client dir should also be created
    agents_dest = git_repo / ".agents" / "skills" / "use-workbench"
    assert agents_dest.exists()


def test_init_gemini_local_symlinks(git_repo):
    """wb init --agent gemini --local --symlink should symlink at repo level."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["init", "--agent", "gemini", "--local", "--symlink"])

    assert result.exit_code == 0
    dest = git_repo / ".agents" / "skills" / "use-workbench"
    assert dest.is_symlink()


# ---------------------------------------------------------------------------
# wb setup cross-client skill install
# ---------------------------------------------------------------------------


def test_setup_installs_cross_client_skills(git_repo):
    """wb setup should install to .agents/skills/ for cross-client discoverability."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "claude"])

    assert result.exit_code == 0
    # setup is always project-scoped, so should install to .agents/skills/
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()


# ---------------------------------------------------------------------------
# wb init --agent choice includes gemini
# ---------------------------------------------------------------------------


def test_init_agent_choice_includes_gemini():
    """The --agent option should accept 'gemini' as a valid choice."""
    runner = CliRunner()
    # Just verify that gemini is accepted without "invalid choice" error
    fake_home = Path("/tmp/test_gemini_choice")
    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["init", "--agent", "gemini"])

    # Should not fail with "Invalid value for '--agent'"
    assert "Invalid value" not in (result.output or "")


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


# ---------------------------------------------------------------------------
# wb profile subcommand group
# ---------------------------------------------------------------------------


class TestProfileInit:
    def test_profile_init_creates_file(self, tmp_path):
        """wb profile init creates .workbench/profile.yaml with valid YAML."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["profile", "init", "--repo", str(repo)])

        assert result.exit_code == 0
        profile_path = repo / ".workbench" / "profile.yaml"
        assert profile_path.exists()
        # Should be valid YAML with roles

        data = yaml.safe_load(profile_path.read_text())
        assert "roles" in data

    def test_profile_init_global(self, tmp_path):
        """wb profile init --global creates ~/.workbench/profile.yaml."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=fake_home):
            result = runner.invoke(main, ["profile", "init", "--global"])

        assert result.exit_code == 0
        global_path = fake_home / ".workbench" / "profile.yaml"
        assert global_path.exists()

    def test_profile_init_global_creates_directory(self, tmp_path):
        """wb profile init --global creates ~/.workbench/ if it doesn't exist."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=fake_home):
            result = runner.invoke(main, ["profile", "init", "--global"])

        assert result.exit_code == 0
        assert (fake_home / ".workbench").is_dir()

    def test_profile_init_no_overwrite(self, tmp_path):
        """Existing file, user declines overwrite, file unchanged."""
        repo = tmp_path / "repo"
        repo.mkdir()
        wb_dir = repo / ".workbench"
        wb_dir.mkdir()
        profile_path = wb_dir / "profile.yaml"
        original_content = "# original content\nroles: {}\n"
        profile_path.write_text(original_content)

        runner = CliRunner()
        # User inputs 'n' to decline overwrite
        result = runner.invoke(main, ["profile", "init", "--repo", str(repo)], input="n\n")

        assert profile_path.read_text() == original_content

    def test_profile_init_overwrite_accepted(self, tmp_path):
        """Existing file, user accepts overwrite, file is replaced."""
        repo = tmp_path / "repo"
        repo.mkdir()
        wb_dir = repo / ".workbench"
        wb_dir.mkdir()
        profile_path = wb_dir / "profile.yaml"
        profile_path.write_text("# old\nroles: {}\n")

        runner = CliRunner()
        result = runner.invoke(main, ["profile", "init", "--repo", str(repo)], input="y\n")

        assert result.exit_code == 0

        data = yaml.safe_load(profile_path.read_text())
        # Should now have all roles from default profile
        assert "roles" in data
        assert "implementor" in data["roles"]

    def test_profile_init_prints_path(self, tmp_path):
        """wb profile init prints the path of the created file."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["profile", "init", "--repo", str(repo)])

        assert result.exit_code == 0
        # Rich may wrap long paths across lines, so check without newlines
        assert "profile.yaml" in result.output.replace("\n", "")


class TestProfileShow:
    def test_profile_show_output(self, tmp_path):
        """wb profile show displays role table with agents."""
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(main, ["profile", "show", "--repo", str(repo)])

        assert result.exit_code == 0
        # Should show all roles with their agents
        assert "implementor" in result.output
        assert "tester" in result.output
        assert "reviewer" in result.output
        assert "fixer" in result.output
        assert "claude" in result.output

    def test_profile_show_with_explicit_path(self, tmp_path):
        """wb profile show --profile <path> uses the explicit profile."""

        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(yaml.dump({"roles": {"reviewer": {"agent": "gemini"}}}))
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main, ["profile", "show", "--repo", str(repo), "--profile", str(profile_path)]
            )

        assert result.exit_code == 0
        assert "gemini" in result.output

    def test_profile_show_truncates_directive(self, tmp_path):
        """wb profile show truncates directive to first line."""

        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(
            yaml.dump(
                {"roles": {"reviewer": {"directive": "First line.\nSecond line.\nThird line."}}}
            )
        )
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main, ["profile", "show", "--repo", str(repo), "--profile", str(profile_path)]
            )

        assert result.exit_code == 0
        # First line should be visible; second/third should not
        assert "First line." in result.output
        assert "Second line." not in result.output


class TestProfileSet:
    def test_profile_set_agent(self, tmp_path):
        """wb profile set reviewer.agent gemini updates the YAML."""

        repo = tmp_path / "repo"
        repo.mkdir()
        wb_dir = repo / ".workbench"
        wb_dir.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "reviewer.agent", "gemini", "--repo", str(repo)]
        )

        assert result.exit_code == 0
        profile_path = wb_dir / "profile.yaml"
        assert profile_path.exists()
        data = yaml.safe_load(profile_path.read_text())
        assert data["roles"]["reviewer"]["agent"] == "gemini"

    def test_profile_set_directive_extend(self, tmp_path):
        """wb profile set tester.directive_extend 'Extra' sets the extend field."""

        repo = tmp_path / "repo"
        repo.mkdir()
        wb_dir = repo / ".workbench"
        wb_dir.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "profile",
                "set",
                "tester.directive_extend",
                "Extra instructions.",
                "--repo",
                str(repo),
            ],
        )

        assert result.exit_code == 0
        profile_path = wb_dir / "profile.yaml"
        data = yaml.safe_load(profile_path.read_text())
        assert data["roles"]["tester"]["directive_extend"] == "Extra instructions."

    def test_profile_set_invalid_key_format(self, tmp_path):
        """wb profile set with key not in role.field format produces error."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "invalidkey", "value", "--repo", str(repo)]
        )

        assert result.exit_code != 0

    def test_profile_set_invalid_role(self, tmp_path):
        """wb profile set with unknown role produces error."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "nonexistent.agent", "gemini", "--repo", str(repo)]
        )

        assert result.exit_code != 0

    def test_profile_set_invalid_field(self, tmp_path):
        """wb profile set with unknown field produces error."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "reviewer.bogus_field", "x", "--repo", str(repo)]
        )

        assert result.exit_code != 0

    def test_profile_set_updates_existing(self, tmp_path):
        """wb profile set preserves other fields in existing YAML."""

        repo = tmp_path / "repo"
        repo.mkdir()
        wb_dir = repo / ".workbench"
        wb_dir.mkdir()
        profile_path = wb_dir / "profile.yaml"
        profile_path.write_text(
            yaml.dump({"roles": {"reviewer": {"agent": "codex"}, "tester": {"agent": "gemini"}}})
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "reviewer.agent", "gemini", "--repo", str(repo)]
        )

        assert result.exit_code == 0
        data = yaml.safe_load(profile_path.read_text())
        assert data["roles"]["reviewer"]["agent"] == "gemini"
        # tester should be unchanged
        assert data["roles"]["tester"]["agent"] == "gemini"

    def test_profile_set_global(self, tmp_path):
        """wb profile set --global writes to ~/.workbench/profile.yaml."""

        fake_home = tmp_path / "home"
        fake_home.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=fake_home):
            result = runner.invoke(
                main, ["profile", "set", "implementor.agent", "codex", "--global"]
            )

        assert result.exit_code == 0
        global_path = fake_home / ".workbench" / "profile.yaml"
        assert global_path.exists()
        data = yaml.safe_load(global_path.read_text())
        assert data["roles"]["implementor"]["agent"] == "codex"


class TestProfileDiff:
    def test_profile_diff_no_changes(self, tmp_path):
        """Default profile shows 'matches defaults'."""
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(main, ["profile", "diff", "--repo", str(repo)])

        assert result.exit_code == 0
        assert "matches defaults" in result.output.lower()

    def test_profile_diff_with_changes(self, tmp_path):
        """Modified profile shows which roles differ."""

        repo = tmp_path / "repo"
        repo.mkdir()
        wb_dir = repo / ".workbench"
        wb_dir.mkdir()
        (wb_dir / "profile.yaml").write_text(
            yaml.dump(
                {
                    "roles": {
                        "reviewer": {"agent": "gemini"},
                        "tester": {"directive": "Custom directive."},
                    }
                }
            )
        )

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(main, ["profile", "diff", "--repo", str(repo)])

        assert result.exit_code == 0
        assert "reviewer" in result.output
        assert "gemini" in result.output
        # Directive diff should show [changed], not the full text
        assert "tester" in result.output
        assert "[changed]" in result.output.lower() or "changed" in result.output.lower()

    def test_profile_diff_with_explicit_path(self, tmp_path):
        """wb profile diff --profile <path> uses the explicit profile."""

        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(yaml.dump({"roles": {"fixer": {"agent": "codex"}}}))
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main, ["profile", "diff", "--repo", str(repo), "--profile", str(profile_path)]
            )

        assert result.exit_code == 0
        assert "fixer" in result.output
        assert "codex" in result.output


# ---------------------------------------------------------------------------
# wb run --profile flag
# ---------------------------------------------------------------------------


class TestRunWithProfile:
    def test_run_with_profile_flag(self, git_repo, tmp_path):
        """wb run plan.md --profile custom.yaml passes profile_path to run_plan."""

        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n## Task: hello\nDo something\n")
        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(yaml.dump({"roles": {"implementor": {"agent": "gemini"}}}))

        runner = CliRunner()
        with (
            patch("workbench.cli.run_plan") as mock_run_plan,
            patch("workbench.cli._find_repo_root", return_value=git_repo),
            patch("workbench.cli.asyncio") as mock_asyncio,
        ):
            mock_asyncio.run = lambda coro: None
            result = runner.invoke(
                main,
                ["run", str(plan), "--no-tmux", "--profile", str(profile_path)],
            )

        # Verify the option was accepted and run_plan was called
        assert "no such option" not in (result.output or "").lower()
        assert mock_run_plan.called, "run_plan was never called"
        assert mock_run_plan.call_args.kwargs["profile_path"] == profile_path

    def test_run_profile_flag_nonexistent_file(self, git_repo, tmp_path):
        """wb run --profile with nonexistent file produces an error."""
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n## Task: hello\nDo something\n")

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(
                main,
                ["run", str(plan), "--no-tmux", "--profile", str(tmp_path / "missing.yaml")],
            )

        # click.Path(exists=True) should reject nonexistent files
        assert result.exit_code != 0
