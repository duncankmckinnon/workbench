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
# wb plan
# ---------------------------------------------------------------------------


def test_plan_invokes_planner(git_repo):
    """wb plan should invoke run_planner with the prompt and name."""
    from workbench.agents import AgentResult, Role, TaskStatus

    runner = CliRunner()

    fake_result = AgentResult(
        task_id="planner-myplan",
        role=Role.IMPLEMENTOR,
        status=TaskStatus.DONE,
        output="Plan written.",
    )

    async def fake_run_planner(**kwargs):
        output_path = git_repo / ".workbench" / "plans" / f"{kwargs['plan_name']}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# Generated Plan\n## Task: Foo\nDo the thing.\n")
        return fake_result

    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
        patch("workbench.agents.run_planner", side_effect=fake_run_planner),
    ):
        result = runner.invoke(main, ["plan", "Add JWT auth", "--name", "myplan", "--no-tmux"])

    assert result.exit_code == 0, result.output
    # Rich may wrap long paths across lines, so check without newlines
    flat = result.output.replace("\n", "")
    assert "myplan.md" in flat
    assert "wb preview" in flat
    assert "wb run" in flat
    assert "Next steps" in flat


def test_plan_from_file(git_repo, tmp_path):
    """wb plan --from should read the source file and pass content to planner."""
    from workbench.agents import AgentResult, Role, TaskStatus

    runner = CliRunner()

    source = tmp_path / "claude-plan.md"
    source.write_text("# Claude Plan\n\n## Step 1\nDo something\n## Step 2\nDo another thing\n")

    fake_result = AgentResult(
        task_id="planner-converted",
        role=Role.IMPLEMENTOR,
        status=TaskStatus.DONE,
        output="Plan written.",
    )

    captured_kwargs = {}

    async def fake_run_planner(**kwargs):
        captured_kwargs.update(kwargs)
        output_path = git_repo / ".workbench" / "plans" / f"{kwargs['plan_name']}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# Converted Plan\n## Task: Step one\nDo something.\n")
        return fake_result

    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
        patch("workbench.agents.run_planner", side_effect=fake_run_planner),
    ):
        result = runner.invoke(
            main,
            ["plan", "--from", str(source), "--name", "converted", "--no-tmux"],
        )

    assert result.exit_code == 0, result.output
    assert "Transforming" in result.output
    assert "Claude Plan" in captured_kwargs["source_content"]


def test_plan_from_file_with_prompt(git_repo, tmp_path):
    """wb plan PROMPT --from FILE should pass both."""
    from workbench.agents import AgentResult, Role, TaskStatus

    runner = CliRunner()

    source = tmp_path / "spec.md"
    source.write_text("# Spec\nBuild a widget.\n")

    fake_result = AgentResult(
        task_id="planner-guided",
        role=Role.IMPLEMENTOR,
        status=TaskStatus.DONE,
        output="Plan written.",
    )

    captured_kwargs = {}

    async def fake_run_planner(**kwargs):
        captured_kwargs.update(kwargs)
        output_path = git_repo / ".workbench" / "plans" / f"{kwargs['plan_name']}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# Plan\n## Task: Widget\nBuild it.\n")
        return fake_result

    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
        patch("workbench.agents.run_planner", side_effect=fake_run_planner),
    ):
        result = runner.invoke(
            main,
            ["plan", "Focus on security", "--from", str(source), "--no-tmux"],
        )

    assert result.exit_code == 0, result.output
    assert "Transforming" in result.output
    assert "Guidance" in result.output
    assert captured_kwargs["user_prompt"] == "Focus on security"
    assert "Build a widget" in captured_kwargs["source_content"]


def test_plan_no_prompt_no_from_errors(git_repo):
    """wb plan with neither prompt nor --from should error."""
    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
    ):
        result = runner.invoke(main, ["plan", "--no-tmux"])

    assert result.exit_code != 0
    assert "Provide a prompt" in result.output


def test_plan_without_tmux_shows_error(git_repo):
    """wb plan without tmux and without --no-tmux should error."""
    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=False),
    ):
        result = runner.invoke(main, ["plan", "Do something"])

    assert result.exit_code != 0
    assert "tmux is required" in result.output


# ---------------------------------------------------------------------------
# wb init --agent manual
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wb setup — global (user-level skill install)
# ---------------------------------------------------------------------------


def test_setup_global_manual_prints_skill_paths():
    runner = CliRunner()
    result = runner.invoke(main, ["setup", "--global", "--agent", "manual"])
    assert result.exit_code == 0
    assert "Skill files directory:" in result.output
    assert "use-workbench" in result.output


def test_setup_global_claude_copies_skills(tmp_path):
    """wb setup --global --agent claude should copy skill folders to ~/.claude/skills/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "claude"])

    assert result.exit_code == 0
    skills_dir = fake_home / ".claude" / "skills"
    assert skills_dir.is_dir()
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()
    assert (skills_dir / "configure-workbench" / "SKILL.md").exists()
    assert "Copied" in result.output


def test_setup_global_claude_symlinks(tmp_path):
    """wb setup --global --agent claude --symlink should symlink skill dirs."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "claude", "--symlink"])

    assert result.exit_code == 0
    dest = fake_home / ".claude" / "skills" / "use-workbench"
    assert dest.is_symlink()
    assert "Linked" in result.output


def test_setup_global_codex_appends_to_instructions(tmp_path, monkeypatch):
    """wb setup --global --agent codex should append skills to .codex/instructions.md."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["setup", "--global", "--agent", "codex"])

    assert result.exit_code == 0
    instructions = tmp_path / ".codex" / "instructions.md"
    assert instructions.exists()
    content = instructions.read_text()
    assert len(content) > 0
    assert "Appended" in result.output


def test_setup_global_codex_skips_duplicates(tmp_path, monkeypatch):
    """wb setup --global --agent codex should skip duplicate skills."""
    monkeypatch.chdir(tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "instructions.md").write_text(
        "<!-- workbench-skill:use-workbench -->\n# Existing skill\n"
    )

    runner = CliRunner()
    result = runner.invoke(main, ["setup", "--global", "--agent", "codex"])

    assert result.exit_code == 0
    assert "Skipping" in result.output


def test_setup_global_cursor_copies_to_project(tmp_path, monkeypatch):
    """wb setup --global --agent cursor should copy to .cursor/rules/ in cwd."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["setup", "--global", "--agent", "cursor"])

    assert result.exit_code == 0
    rules_dir = tmp_path / ".cursor" / "rules"
    assert rules_dir.is_dir()
    assert (rules_dir / "use-workbench.md").exists()


def test_setup_global_gemini_copies_skills(tmp_path):
    """wb setup --global --agent gemini should copy skill folders to ~/.agents/skills/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "gemini"])

    assert result.exit_code == 0
    skills_dir = fake_home / ".agents" / "skills"
    assert skills_dir.is_dir()
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()
    assert "Copied" in result.output


def test_setup_global_gemini_symlinks(tmp_path):
    """wb setup --global --agent gemini --symlink should create symlink at ~/.agents/skills/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "gemini", "--symlink"])

    assert result.exit_code == 0
    dest = fake_home / ".agents" / "skills" / "use-workbench"
    assert dest.is_symlink()
    assert "Linked" in result.output


def test_setup_agent_choice_includes_gemini():
    """The --agent option should accept 'gemini' as a valid choice."""
    runner = CliRunner()
    fake_home = Path("/tmp/test_gemini_choice")
    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "gemini"])

    assert "Invalid value" not in (result.output or "")


# ---------------------------------------------------------------------------
# wb setup — local (project-level, default)
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


def test_setup_claude_local(tmp_path, git_repo):
    """wb setup --agent claude installs to <repo>/.claude/skills/ and <repo>/.agents/skills/."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "claude"])

    assert result.exit_code == 0
    claude_skill = git_repo / ".claude" / "skills" / "use-workbench" / "SKILL.md"
    assert claude_skill.exists()
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()
    assert "cross-client" in result.output.lower()


def test_setup_gemini_local(tmp_path, git_repo):
    """wb setup --agent gemini installs to <repo>/.agents/skills/."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "gemini"])

    assert result.exit_code == 0
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()


def test_setup_cursor_local(tmp_path, monkeypatch, git_repo):
    """wb setup --agent cursor installs to .cursor/rules/ and .agents/skills/."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "cursor"])

    assert result.exit_code == 0
    rules_dir = tmp_path / ".cursor" / "rules"
    assert rules_dir.is_dir()
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()


def test_setup_codex_local(tmp_path, monkeypatch, git_repo):
    """wb setup --agent codex installs to .codex/instructions.md and .agents/skills/."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "codex"])

    assert result.exit_code == 0
    instructions = tmp_path / ".codex" / "instructions.md"
    assert instructions.exists()
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()


def test_setup_claude_local_symlinks(git_repo):
    """wb setup --agent claude --symlink should symlink at repo level."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "claude", "--symlink"])

    assert result.exit_code == 0
    dest = git_repo / ".claude" / "skills" / "use-workbench"
    assert dest.is_symlink()
    agents_dest = git_repo / ".agents" / "skills" / "use-workbench"
    assert agents_dest.exists()


def test_setup_gemini_local_symlinks(git_repo):
    """wb setup --agent gemini --symlink should symlink at repo level."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "gemini", "--symlink"])

    assert result.exit_code == 0
    dest = git_repo / ".agents" / "skills" / "use-workbench"
    assert dest.is_symlink()


def test_setup_installs_cross_client_skills(git_repo):
    """wb setup should install to .agents/skills/ for cross-client discoverability."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "claude"])

    assert result.exit_code == 0
    agents_skill = git_repo / ".agents" / "skills" / "use-workbench" / "SKILL.md"
    assert agents_skill.exists()


# ---------------------------------------------------------------------------
# wb setup — skill selection
# ---------------------------------------------------------------------------


def test_setup_install_all_on_confirm(tmp_path):
    """Answering 'y' to 'Install all?' should install all skills."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "claude"], input="y\n")

    assert result.exit_code == 0
    skills_dir = fake_home / ".claude" / "skills"
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()
    assert (skills_dir / "configure-workbench" / "SKILL.md").exists()
    assert (skills_dir / "install-workbench" / "SKILL.md").exists()


def test_setup_select_individual_skills(tmp_path):
    """Declining 'Install all?' should prompt per skill, installing only selected."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    # "n" for install all, then "y", "n", "y" for 3 skills
    # Skills are sorted: configure-workbench, install-workbench, use-workbench
    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(
            main, ["setup", "--global", "--agent", "claude"], input="n\ny\nn\ny\n"
        )

    assert result.exit_code == 0
    skills_dir = fake_home / ".claude" / "skills"
    assert (skills_dir / "configure-workbench" / "SKILL.md").exists()
    assert not (skills_dir / "install-workbench" / "SKILL.md").exists()
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()


def test_setup_select_none_skips(tmp_path):
    """Declining all individual skills should skip installation."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    # "n" for install all, then "n" for each skill
    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(
            main, ["setup", "--global", "--agent", "claude"], input="n\nn\nn\nn\n"
        )

    assert result.exit_code == 0
    assert "No skills selected" in result.output


def test_setup_update_skips_selection(tmp_path):
    """--update should skip the selection prompt entirely."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "claude", "--update"])

    assert result.exit_code == 0
    # All skills should be installed without prompting
    skills_dir = fake_home / ".claude" / "skills"
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()
    assert (skills_dir / "configure-workbench" / "SKILL.md").exists()
    assert "Install all" not in result.output


# ---------------------------------------------------------------------------
# wb init (deprecated, delegates to setup)
# ---------------------------------------------------------------------------


def test_init_deprecated_shows_warning():
    """wb init should show deprecation warning."""
    runner = CliRunner()
    fake_home = Path("/tmp/test_init_deprecated")
    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["init", "--agent", "manual"])
    assert "deprecated" in result.output.lower()


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


# ---------------------------------------------------------------------------
# wb run --retry-failed / --fail-fast / --only-failed flags
# ---------------------------------------------------------------------------


def _run_cli_with_capture(git_repo, tmp_path, extra_args):
    """Helper to invoke `wb run` with a fake run_plan that captures kwargs.

    Returns (CliRunner result, kwargs dict passed to run_plan).
    """
    import asyncio

    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()

    with (
        patch("workbench.cli.run_plan") as mock_run_plan,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.asyncio") as mock_asyncio,
    ):

        async def fake_run_plan(**kwargs):
            return []

        mock_run_plan.side_effect = lambda **kwargs: fake_run_plan(**kwargs)
        mock_asyncio.run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)

        result = runner.invoke(main, ["run", str(plan), "--no-tmux"] + extra_args)

        # Extract kwargs from the mock's call record
        captured = dict(mock_run_plan.call_args.kwargs) if mock_run_plan.called else {}

    return result, captured


def test_run_retry_failed_flag(git_repo, tmp_path):
    """--retry-failed should pass retry_failed=True to run_plan."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, ["--retry-failed"])
    assert result.exit_code == 0, result.output
    assert captured.get("retry_failed") is True


def test_run_fail_fast_flag(git_repo, tmp_path):
    """--fail-fast should pass fail_fast=True to run_plan."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, ["--fail-fast"])
    assert result.exit_code == 0, result.output
    assert captured.get("fail_fast") is True


def test_run_only_failed_requires_session_branch(git_repo, tmp_path):
    """--only-failed without --session-branch should error."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["run", str(plan), "--no-tmux", "--only-failed"])

    assert result.exit_code != 0
    assert "--only-failed requires --session-branch" in result.output


def test_run_only_failed_with_session_branch(git_repo, tmp_path):
    """--only-failed with --session-branch should pass both to run_plan."""
    result, captured = _run_cli_with_capture(
        git_repo, tmp_path, ["--only-failed", "-b", "workbench-1"]
    )
    assert result.exit_code == 0, result.output
    assert captured.get("only_failed") is True
    assert captured.get("session_branch") == "workbench-1"


def test_run_flags_default_to_false(git_repo, tmp_path):
    """Without flags, retry_failed, fail_fast, and only_failed default to False."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, [])
    assert result.exit_code == 0, result.output
    assert captured.get("retry_failed") is False
    assert captured.get("fail_fast") is False
    assert captured.get("only_failed") is False


# ---------------------------------------------------------------------------
# Wave range validation
# ---------------------------------------------------------------------------


def _run_cli_with_capture_multi_wave(git_repo, tmp_path, extra_args, num_waves=3):
    """Like _run_cli_with_capture but with chained dependent tasks (one per wave)."""
    import asyncio

    # Each task depends on the previous → sequential waves
    lines = ["# Plan"]
    for i in range(1, num_waves + 1):
        lines.append(f"## Task: step {i}")
        if i > 1:
            lines.append(f"Depends: step-{i - 1}")
        lines.append(f"Do thing {i}")
        lines.append("")
    plan = tmp_path / "plan.md"
    plan.write_text("\n".join(lines))

    runner = CliRunner()

    with (
        patch("workbench.cli.run_plan") as mock_run_plan,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.asyncio") as mock_asyncio,
    ):

        async def fake_run_plan(**kwargs):
            return []

        mock_run_plan.side_effect = lambda **kwargs: fake_run_plan(**kwargs)
        mock_asyncio.run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)

        result = runner.invoke(main, ["run", str(plan), "--no-tmux"] + extra_args)

        captured = dict(mock_run_plan.call_args.kwargs) if mock_run_plan.called else {}

    return result, captured


def test_wave_flag_sets_start_and_end(git_repo, tmp_path):
    """-w 2 should set start_wave=2 and end_wave=2."""
    result, captured = _run_cli_with_capture_multi_wave(git_repo, tmp_path, ["-w", "2"])
    assert result.exit_code == 0, result.output
    assert captured.get("start_wave") == 2
    assert captured.get("end_wave") == 2


def test_start_wave_and_end_wave_passthrough(git_repo, tmp_path):
    """--start-wave 1 --end-wave 2 passes through correctly."""
    result, captured = _run_cli_with_capture_multi_wave(
        git_repo, tmp_path, ["--start-wave", "1", "--end-wave", "2"]
    )
    assert result.exit_code == 0, result.output
    assert captured.get("start_wave") == 1
    assert captured.get("end_wave") == 2


def test_start_wave_defaults_no_end(git_repo, tmp_path):
    """Without --wave or --end-wave, start_wave defaults to 1 and end_wave is None."""
    result, captured = _run_cli_with_capture_multi_wave(git_repo, tmp_path, [])
    assert result.exit_code == 0, result.output
    assert captured.get("start_wave") == 1
    assert captured.get("end_wave") is None


def test_start_wave_too_low_clamps_to_1(git_repo, tmp_path):
    """--start-wave 0 clamps to 1 with a warning."""
    result, captured = _run_cli_with_capture_multi_wave(git_repo, tmp_path, ["--start-wave", "0"])
    assert result.exit_code == 0, result.output
    assert captured.get("start_wave") == 1
    assert "defaulting to 1" in result.output


def test_start_wave_too_high_clamps_to_1(git_repo, tmp_path):
    """--start-wave beyond num_waves clamps to 1."""
    result, captured = _run_cli_with_capture_multi_wave(git_repo, tmp_path, ["--start-wave", "99"])
    assert result.exit_code == 0, result.output
    assert captured.get("start_wave") == 1
    assert "defaulting to 1" in result.output


def test_end_wave_too_high_clamps_to_num_waves(git_repo, tmp_path):
    """--end-wave beyond num_waves clamps to N."""
    result, captured = _run_cli_with_capture_multi_wave(git_repo, tmp_path, ["--end-wave", "99"])
    assert result.exit_code == 0, result.output
    assert captured.get("end_wave") == 3  # 3 tasks = 3 waves
    assert "defaulting to 3" in result.output


def test_end_wave_too_low_clamps_to_num_waves(git_repo, tmp_path):
    """--end-wave 0 clamps to N."""
    result, captured = _run_cli_with_capture_multi_wave(git_repo, tmp_path, ["--end-wave", "0"])
    assert result.exit_code == 0, result.output
    assert captured.get("end_wave") == 3
    assert "defaulting to 3" in result.output


def test_end_wave_less_than_start_wave_clamps(git_repo, tmp_path):
    """--end-wave < --start-wave clamps end_wave to N."""
    result, captured = _run_cli_with_capture_multi_wave(
        git_repo, tmp_path, ["--start-wave", "2", "--end-wave", "1"]
    )
    assert result.exit_code == 0, result.output
    assert captured.get("start_wave") == 2
    assert captured.get("end_wave") == 3
    assert "defaulting to 3" in result.output


def test_wave_flag_out_of_range_clamps(git_repo, tmp_path):
    """-w 99 clamps to valid range (start=1 since out of range)."""
    result, captured = _run_cli_with_capture_multi_wave(git_repo, tmp_path, ["-w", "99"])
    assert result.exit_code == 0, result.output
    # --wave sets both start and end to 99; start clamps to 1, end clamps to N
    assert captured.get("start_wave") == 1
    assert captured.get("end_wave") == 3


# ---------------------------------------------------------------------------
# wb merge
# ---------------------------------------------------------------------------


def test_merge_requires_session_branch(git_repo):
    """wb merge without -b should error."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["merge"])

    assert result.exit_code != 0


def test_merge_passes_args_without_plan(git_repo):
    """wb merge -b workbench-1 should pass plan_slug=None to merge_unmerged."""
    import asyncio

    runner = CliRunner()

    with (
        patch("workbench.cli.merge_unmerged") as mock_merge,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.asyncio") as mock_asyncio,
    ):

        async def fake_merge(**kwargs):
            pass

        mock_merge.side_effect = lambda **kwargs: fake_merge(**kwargs)
        mock_asyncio.run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)

        result = runner.invoke(main, ["merge", "-b", "workbench-1", "--no-tmux"])

        captured = dict(mock_merge.call_args.kwargs) if mock_merge.called else {}

    assert result.exit_code == 0, result.output
    assert captured.get("session_branch") == "workbench-1"
    assert captured.get("plan_slug") is None
    assert captured.get("use_tmux") is False


def test_merge_passes_args_with_plan(git_repo, tmp_path):
    """wb merge -b workbench-1 --plan plan.md should pass plan_slug."""
    import asyncio

    plan = tmp_path / "plan.md"
    plan.write_text("# My Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()

    with (
        patch("workbench.cli.merge_unmerged") as mock_merge,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.asyncio") as mock_asyncio,
    ):

        async def fake_merge(**kwargs):
            pass

        mock_merge.side_effect = lambda **kwargs: fake_merge(**kwargs)
        mock_asyncio.run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)

        result = runner.invoke(
            main, ["merge", "-b", "workbench-1", "--plan", str(plan), "--no-tmux"]
        )

        captured = dict(mock_merge.call_args.kwargs) if mock_merge.called else {}

    assert result.exit_code == 0, result.output
    assert captured.get("session_branch") == "workbench-1"
    assert captured.get("plan_slug") == "my-plan"


# ---------------------------------------------------------------------------
# wb run --task
# ---------------------------------------------------------------------------


def test_run_task_filter_single(git_repo, tmp_path):
    """--task should pass task_filter to run_plan."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, ["--task", "task-2"])
    assert result.exit_code == 0, result.output
    assert captured.get("task_filter") == {"task-2"}


def test_run_task_filter_multiple(git_repo, tmp_path):
    """Multiple --task flags should pass all values."""
    result, captured = _run_cli_with_capture(
        git_repo, tmp_path, ["--task", "task-1", "--task", "task-3"]
    )
    assert result.exit_code == 0, result.output
    assert captured.get("task_filter") == {"task-1", "task-3"}


def test_run_task_filter_none_by_default(git_repo, tmp_path):
    """Without --task, task_filter should be None."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, [])
    assert result.exit_code == 0, result.output
    assert captured.get("task_filter") is None


# ---------------------------------------------------------------------------
# wb agents
# ---------------------------------------------------------------------------


def test_agents_init_creates_yaml(git_repo):
    """wb agents init should create agents.yaml with all built-in configs."""
    (git_repo / ".workbench").mkdir(exist_ok=True)

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "init"])

    assert result.exit_code == 0
    assert "Created" in result.output

    config = yaml.safe_load((git_repo / ".workbench" / "agents.yaml").read_text())
    assert "claude" in config["agents"]
    assert "gemini" in config["agents"]
    assert "codex" in config["agents"]
    assert "cursor" in config["agents"]
    # Verify structure of one entry
    assert config["agents"]["claude"]["command"] == "claude"
    assert "{prompt}" in config["agents"]["claude"]["args"]


def test_agents_init_prompts_on_overwrite(git_repo):
    """wb agents init should prompt before overwriting existing file."""
    wb_dir = git_repo / ".workbench"
    wb_dir.mkdir(exist_ok=True)
    (wb_dir / "agents.yaml").write_text("agents:\n  old: {}\n")

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        # Decline overwrite
        result = runner.invoke(main, ["agents", "init"], input="n\n")

    assert result.exit_code == 0
    # Original file should be unchanged
    config = yaml.safe_load((wb_dir / "agents.yaml").read_text())
    assert "old" in config["agents"]


def test_agents_init_overwrites_on_confirm(git_repo):
    """wb agents init should overwrite when user confirms."""
    wb_dir = git_repo / ".workbench"
    wb_dir.mkdir(exist_ok=True)
    (wb_dir / "agents.yaml").write_text("agents:\n  old: {}\n")

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "init"], input="y\n")

    assert result.exit_code == 0
    config = yaml.safe_load((wb_dir / "agents.yaml").read_text())
    assert "old" not in config["agents"]
    assert "claude" in config["agents"]


def test_agents_list_shows_builtins(git_repo):
    """wb agents list should show built-in agents."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "list"])

    assert result.exit_code == 0
    assert "claude" in result.output
    assert "gemini" in result.output
    assert "codex" in result.output


def test_agents_list_shows_custom(git_repo):
    """wb agents list should show custom agents from agents.yaml."""
    wb_dir = git_repo / ".workbench"
    wb_dir.mkdir(exist_ok=True)
    (wb_dir / "agents.yaml").write_text(
        "agents:\n  my-agent:\n    command: my-cli\n    output_format: json\n"
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "list"])

    assert result.exit_code == 0
    assert "my-agent" in result.output
    assert "my-cli" in result.output


def test_agents_show_builtin(git_repo):
    """wb agents show claude should show built-in details."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "show", "claude"])

    assert result.exit_code == 0
    assert "built-in" in result.output


def test_agents_show_custom(git_repo):
    """wb agents show should show custom agent details."""
    wb_dir = git_repo / ".workbench"
    wb_dir.mkdir(exist_ok=True)
    (wb_dir / "agents.yaml").write_text(
        "agents:\n  my-agent:\n    command: my-cli\n    args: ['--headless', '{prompt}']\n    output_format: json\n"
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "show", "my-agent"])

    assert result.exit_code == 0
    assert "my-cli" in result.output
    assert "json" in result.output


def test_agents_show_not_found(git_repo):
    """wb agents show for unknown agent should error."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "show", "nonexistent"])

    assert result.exit_code != 0
    assert "not found" in result.output


def test_agents_add_creates_yaml(git_repo):
    """wb agents add should create agents.yaml with the new agent."""
    (git_repo / ".workbench").mkdir(exist_ok=True)

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(
            main,
            ["agents", "add", "my-agent", "--command", "my-cli", "--args", "--headless,{prompt}"],
        )

    assert result.exit_code == 0
    assert "Added" in result.output

    config = yaml.safe_load((git_repo / ".workbench" / "agents.yaml").read_text())
    assert "my-agent" in config["agents"]
    assert config["agents"]["my-agent"]["command"] == "my-cli"
    assert config["agents"]["my-agent"]["args"] == ["--headless", "{prompt}"]


def test_agents_add_with_json_format(git_repo):
    """wb agents add with --output-format json should include json keys."""
    (git_repo / ".workbench").mkdir(exist_ok=True)

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(
            main,
            [
                "agents",
                "add",
                "my-agent",
                "--command",
                "my-cli",
                "--output-format",
                "json",
                "--json-result-key",
                "output",
                "--json-cost-key",
                "cost",
            ],
        )

    assert result.exit_code == 0
    config = yaml.safe_load((git_repo / ".workbench" / "agents.yaml").read_text())
    entry = config["agents"]["my-agent"]
    assert entry["output_format"] == "json"
    assert entry["json_result_key"] == "output"
    assert entry["json_cost_key"] == "cost"


def test_agents_add_updates_existing(git_repo):
    """wb agents add for an existing agent should update it."""
    wb_dir = git_repo / ".workbench"
    wb_dir.mkdir(exist_ok=True)
    (wb_dir / "agents.yaml").write_text("agents:\n  my-agent:\n    command: old-cli\n")

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "add", "my-agent", "--command", "new-cli"])

    assert result.exit_code == 0
    assert "Updated" in result.output
    config = yaml.safe_load((wb_dir / "agents.yaml").read_text())
    assert config["agents"]["my-agent"]["command"] == "new-cli"


def test_agents_remove(git_repo):
    """wb agents remove should delete the agent from agents.yaml."""
    wb_dir = git_repo / ".workbench"
    wb_dir.mkdir(exist_ok=True)
    (wb_dir / "agents.yaml").write_text(
        "agents:\n  my-agent:\n    command: my-cli\n  other:\n    command: other-cli\n"
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "remove", "my-agent"])

    assert result.exit_code == 0
    assert "Removed" in result.output
    config = yaml.safe_load((wb_dir / "agents.yaml").read_text())
    assert "my-agent" not in config["agents"]
    assert "other" in config["agents"]


def test_agents_remove_not_found(git_repo):
    """wb agents remove for unknown agent should error."""
    (git_repo / ".workbench").mkdir(exist_ok=True)

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "remove", "nonexistent"])

    assert result.exit_code != 0
    assert "not found" in result.output
