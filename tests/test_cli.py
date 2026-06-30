"""Tests for CLI flags and commands."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from workbench.cli import (
    _detect_agent,
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
# _detect_agent
# ---------------------------------------------------------------------------


def test_detect_agent_finds_agy_as_antigravity():
    """_detect_agent returns 'antigravity' when agy is on PATH."""
    with patch(
        "workbench.cli.shutil.which", side_effect=lambda b: "/usr/bin/agy" if b == "agy" else None
    ):
        result = _detect_agent()
    assert result == "antigravity"


def test_detect_agent_ignores_unregistered_google_cli():
    """_detect_agent returns 'manual' when no registered agent binary is on PATH."""
    with patch("workbench.cli.shutil.which", return_value=None):
        result = _detect_agent()
    assert result == "manual"


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
        plan_name = kwargs["plan_name"]
        output_path = git_repo / ".workbench" / plan_name / "plan.md"
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
    assert "myplan/plan.md" in flat
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


def test_plan_failure_shows_error(git_repo):
    """wb plan shows error when planner agent fails."""
    from workbench.agents import AgentResult, Role, TaskStatus

    runner = CliRunner()

    async def fake_run_planner(**kwargs):
        return AgentResult(
            task_id="planner-plan",
            role=Role.IMPLEMENTOR,
            status=TaskStatus.FAILED,
            output="Agent crashed.",
        )

    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
        patch("workbench.agents.run_planner", side_effect=fake_run_planner),
    ):
        result = runner.invoke(main, ["plan", "Do something", "--no-tmux"])

    assert result.exit_code != 0
    flat = result.output.replace("\n", "")
    assert "Planning failed" in flat


def test_plan_file_not_found_shows_warning(git_repo):
    """wb plan warns when agent succeeds but doesn't write the plan file."""
    from workbench.agents import AgentResult, Role, TaskStatus

    runner = CliRunner()

    async def fake_run_planner(**kwargs):
        return AgentResult(
            task_id="planner-plan",
            role=Role.IMPLEMENTOR,
            status=TaskStatus.DONE,
            output="Done.",
        )

    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
        patch("workbench.agents.run_planner", side_effect=fake_run_planner),
    ):
        result = runner.invoke(main, ["plan", "Do something", "--no-tmux"])

    assert result.exit_code == 0
    flat = result.output.replace("\n", "")
    assert "plan file not found" in flat


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


def test_setup_global_antigravity_copies_skills(tmp_path):
    """wb setup --global --agent antigravity should copy skill folders to ~/.agents/skills/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "antigravity"])

    assert result.exit_code == 0
    skills_dir = fake_home / ".agents" / "skills"
    assert skills_dir.is_dir()
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()
    assert "Copied" in result.output


def test_setup_global_antigravity_symlinks(tmp_path):
    """wb setup --global --agent antigravity --symlink should create symlink at ~/.agents/skills/."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "antigravity", "--symlink"])

    assert result.exit_code == 0
    dest = fake_home / ".agents" / "skills" / "use-workbench"
    assert dest.is_symlink()
    assert "Linked" in result.output


def test_setup_agent_choice_includes_antigravity():
    """The --agent option should accept 'antigravity' as a valid choice."""
    runner = CliRunner()
    fake_home = Path("/tmp/test_antigravity_choice")
    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(main, ["setup", "--global", "--agent", "antigravity"])

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


def test_setup_antigravity_local(tmp_path, git_repo):
    """wb setup --agent antigravity installs to <repo>/.agents/skills/."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "antigravity"])

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


def test_setup_antigravity_local_symlinks(git_repo):
    """wb setup --agent antigravity --symlink should symlink at repo level."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["setup", "--agent", "antigravity", "--symlink"])

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

    # "n" for install all, then per-skill answers.
    # Skills are sorted alphabetically:
    #   configure-workbench (y), generate-conventions (n),
    #   install-workbench (n), use-workbench (y)
    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(
            main, ["setup", "--global", "--agent", "claude"], input="n\ny\nn\nn\ny\n"
        )

    assert result.exit_code == 0
    skills_dir = fake_home / ".claude" / "skills"
    assert (skills_dir / "configure-workbench" / "SKILL.md").exists()
    assert not (skills_dir / "generate-conventions" / "SKILL.md").exists()
    assert not (skills_dir / "install-workbench" / "SKILL.md").exists()
    assert (skills_dir / "use-workbench" / "SKILL.md").exists()


def test_setup_select_none_skips(tmp_path):
    """Declining all individual skills should skip installation."""
    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    # "n" for install all, then "n" for each of the 4 bundled skills
    with patch("workbench.cli.Path.home", return_value=fake_home):
        result = runner.invoke(
            main, ["setup", "--global", "--agent", "claude"], input="n\nn\nn\nn\nn\n"
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
        result = runner.invoke(main, ["clean", "--force"])

    assert result.exit_code == 0
    assert (git_repo / ".workbench").is_dir()


class TestClean:
    """Tests for the status-aware wb clean command."""

    def _write_status(self, repo: Path, slug: str, sessions: dict, plan_source: str = "") -> Path:
        """Write a .workbench/status-<slug>.yaml with the given sessions block."""
        wb = repo / ".workbench"
        wb.mkdir(exist_ok=True)
        path = wb / f"status-{slug}.yaml"
        data: dict = {"sessions": sessions}
        if plan_source:
            data["plan_source"] = plan_source
        path.write_text(yaml.dump(data))
        return path

    def _completed_session(self, branch: str = "wb/feat") -> dict:
        return {
            "workbench-1": {
                "tasks": {
                    "task-1": {
                        "status": "done",
                        "merged": True,
                        "branch": branch,
                        "last_agent": "reviewer",
                    }
                }
            }
        }

    def _incomplete_session(self, branch: str = "wb/inflight") -> dict:
        return {
            "workbench-1": {
                "tasks": {
                    "task-1": {
                        "status": "done",
                        "merged": False,
                        "branch": branch,
                        "last_agent": "implementor",
                    }
                }
            }
        }

    def test_default_empty_repo_succeeds(self, git_repo):
        """wb clean with no status files and no worktrees: zero summary, no errors."""
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean"])

        assert result.exit_code == 0
        assert "Cleaned up:" in result.output

    def test_default_completed_only_removes_status_file(self, git_repo):
        """One completed status file with no live worktree: status file removed."""
        path = self._write_status(git_repo, "p1", self._completed_session())

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean"])

        assert result.exit_code == 0
        assert not path.exists()
        assert "1 status file" in result.output

    def test_default_blocks_on_incomplete_status_file(self, git_repo):
        """Incomplete status file present: error, list it, remove nothing."""
        path = self._write_status(git_repo, "p1", self._incomplete_session())

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean"])

        assert result.exit_code != 0
        assert "In-flight workbench artifacts present" in result.output
        assert "status-p1.yaml" in result.output
        assert path.exists()  # not removed

    def test_default_blocks_on_freshly_seeded_pending_plan(self, git_repo):
        """A run that wrote pending records up-front but never finished must
        be classified as in-flight, not silently cleaned."""
        seeded = {
            "workbench-1": {
                "tasks": {
                    "task-1": {
                        "status": "pending",
                        "merged": False,
                        "branch": None,
                        "last_agent": "",
                    },
                    "task-2": {
                        "status": "pending",
                        "merged": False,
                        "branch": None,
                        "last_agent": "",
                    },
                }
            }
        }
        path = self._write_status(git_repo, "seeded", seeded)

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean"])

        assert result.exit_code != 0
        assert "status-seeded.yaml" in result.output
        assert path.exists()

    def test_completed_skips_incomplete_silently(self, git_repo):
        """--completed: removes completed pieces, leaves incomplete ones alone."""
        completed_path = self._write_status(git_repo, "done-plan", self._completed_session())
        incomplete_path = self._write_status(git_repo, "wip-plan", self._incomplete_session())

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--completed"])

        assert result.exit_code == 0
        assert not completed_path.exists()
        assert incomplete_path.exists()

    def test_force_removes_completed_status_files(self, git_repo):
        """--force: removes completed status files; incomplete left as run history."""
        completed_path = self._write_status(git_repo, "done-plan", self._completed_session())
        incomplete_path = self._write_status(git_repo, "wip-plan", self._incomplete_session())

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--force"])

        assert result.exit_code == 0
        assert not completed_path.exists()
        assert incomplete_path.exists()

    def test_force_and_completed_mutually_exclusive(self, git_repo):
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--force", "--completed"])

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_remove_plans_deletes_existing_plan_source(self, git_repo):
        """--completed --remove-plans: deletes the plan_source file when it exists."""
        plans_dir = git_repo / ".workbench" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "my-plan.md"
        plan_file.write_text("# Plan\n## Task: x\nDo it.\n")

        self._write_status(
            git_repo, "my-plan", self._completed_session(), plan_source=str(plan_file)
        )

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--completed", "--remove-plans"])

        assert result.exit_code == 0
        assert not plan_file.exists()
        assert "1 plan(s)" in result.output

    def test_remove_plans_skips_missing_plan_source(self, git_repo):
        """--remove-plans with a plan_source that does not exist: no error."""
        self._write_status(
            git_repo,
            "my-plan",
            self._completed_session(),
            plan_source=str(git_repo / "does-not-exist.md"),
        )

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--completed", "--remove-plans"])

        assert result.exit_code == 0
        assert "0 plan(s)" in result.output

    def test_remove_plans_refuses_outside_repo(self, git_repo, tmp_path_factory):
        """--remove-plans with a plan_source resolving outside the repo: warn + skip.

        The git_repo fixture aliases tmp_path, so we need a sibling tmp path
        for a file genuinely outside the repo.
        """
        outside_dir = tmp_path_factory.mktemp("outside")
        outside_plan = outside_dir / "outside.md"
        outside_plan.write_text("# Plan outside repo")

        self._write_status(
            git_repo,
            "my-plan",
            self._completed_session(),
            plan_source=str(outside_plan),
        )

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--completed", "--remove-plans"])

        assert result.exit_code == 0
        assert outside_plan.exists()  # not removed
        assert "outside repo" in result.output

    def test_force_ignores_remove_plans(self, git_repo):
        """Under --force, --remove-plans is ignored; plan source survives."""
        plans_dir = git_repo / ".workbench" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "kept.md"
        plan_file.write_text("# kept")

        self._write_status(git_repo, "kept", self._completed_session(), plan_source=str(plan_file))

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--force", "--remove-plans"])

        assert result.exit_code == 0
        assert plan_file.exists()  # --force does NOT touch plan source

    def test_dry_run_changes_nothing(self, git_repo):
        """--dry-run prints would-remove lines; filesystem stays untouched."""
        path = self._write_status(git_repo, "p1", self._completed_session())

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--dry-run"])

        assert result.exit_code == 0
        assert path.exists()
        assert "would remove" in result.output.lower()
        assert "Would remove:" in result.output

    def test_dry_run_with_incomplete_does_not_error(self, git_repo):
        """--dry-run avoids the in-flight error so the user can preview the plan."""
        self._write_status(git_repo, "wip", self._incomplete_session())

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "--dry-run"])

        assert result.exit_code == 0
        assert "Would remove:" in result.output


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

    def test_profile_init_set_two_part_agent(self, tmp_path):
        """wb profile init --set reviewer.agent=antigravity writes that agent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "init", "--repo", str(repo), "--set", "reviewer.agent=antigravity"],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["reviewer"]["agent"] == "antigravity"

    def test_profile_init_set_two_part_directive(self, tmp_path):
        """wb profile init --set reviewer.directive=Custom replaces the directive."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "init", "--repo", str(repo), "--set", "reviewer.directive=Custom"],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["reviewer"]["directive"].strip() == "Custom"

    def test_profile_init_set_two_part_directive_extend(self, tmp_path):
        """wb profile init --set reviewer.directive_extend=Extra writes the extended directive."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "profile",
                "init",
                "--repo",
                str(repo),
                "--set",
                "reviewer.directive_extend=Extra",
            ],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        directive = data["roles"]["reviewer"]["directive"]
        assert directive.endswith("Extra")

    def test_profile_init_set_three_part_tdd_directive(self, tmp_path):
        """wb profile init --set tester.tdd.directive=Custom writes the sub-mode."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "profile",
                "init",
                "--repo",
                str(repo),
                "--set",
                "tester.tdd.directive=Custom TDD",
            ],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["tester"]["tdd"]["directive"].strip() == "Custom TDD"

    def test_profile_init_set_three_part_tdd_directive_extend(self, tmp_path):
        """wb profile init --set tester.tdd.directive_extend=Extra appends."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "profile",
                "init",
                "--repo",
                str(repo),
                "--set",
                "tester.tdd.directive_extend=Extra",
            ],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        directive = data["roles"]["tester"]["tdd"]["directive"]
        assert directive.endswith("Extra")

    def test_profile_init_set_three_part_followup_directive(self, tmp_path):
        """wb profile init --set reviewer.followup.directive=Custom writes the sub-mode."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "profile",
                "init",
                "--repo",
                str(repo),
                "--set",
                "reviewer.followup.directive=Custom FU",
            ],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["reviewer"]["followup"]["directive"].strip() == "Custom FU"

    def test_profile_init_set_invalid_format_no_equals(self, tmp_path):
        """wb profile init --set foo (no `=`) errors."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["profile", "init", "--repo", str(repo), "--set", "foo"])

        assert result.exit_code != 0
        assert "Invalid --set format" in result.output

    def test_profile_init_set_invalid_format_one_part(self, tmp_path):
        """wb profile init --set foo=bar errors: key needs at least one dot."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["profile", "init", "--repo", str(repo), "--set", "foo=bar"])

        assert result.exit_code != 0
        assert "<role>.<field>" in result.output

    def test_profile_init_set_invalid_format_four_part(self, tmp_path):
        """wb profile init --set a.b.c.d=x errors with format message."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "profile",
                "init",
                "--repo",
                str(repo),
                "--set",
                "tester.tdd.directive.extra=x",
            ],
        )

        assert result.exit_code != 0
        assert "<role>.<field> or <role>.<sub_mode>.<field>" in result.output

    def test_profile_init_set_invalid_role(self, tmp_path):
        """wb profile init --set bogus.agent=claude errors: unknown role."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "init", "--repo", str(repo), "--set", "bogus.agent=claude"]
        )

        assert result.exit_code != 0
        assert "Unknown role" in result.output

    def test_profile_init_set_invalid_field(self, tmp_path):
        """wb profile init --set reviewer.bogus=x errors: unknown field."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "init", "--repo", str(repo), "--set", "reviewer.bogus=x"]
        )

        assert result.exit_code != 0
        assert "Unknown field" in result.output

    def test_profile_init_set_invalid_submode(self, tmp_path):
        """wb profile init --set tester.bogus.directive=x errors: unknown sub-mode."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "init", "--repo", str(repo), "--set", "tester.bogus.directive=x"],
        )

        assert result.exit_code != 0
        assert "Unknown sub-mode" in result.output

    def test_profile_init_set_role_does_not_support_submode(self, tmp_path):
        """wb profile init --set merger.tdd.directive=x errors: merger has no tdd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "init", "--repo", str(repo), "--set", "merger.tdd.directive=x"],
        )

        assert result.exit_code != 0
        assert "merger does not support a 'tdd' sub-mode" in result.output

    def test_profile_init_set_invalid_submode_field(self, tmp_path):
        """wb profile init --set tester.tdd.agent=antigravity errors: sub-modes have no agent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "init", "--repo", str(repo), "--set", "tester.tdd.agent=antigravity"],
        )

        assert result.exit_code != 0
        assert "Unknown sub-mode field" in result.output

    def test_profile_init_set_multiple_overrides(self, tmp_path):
        """Multiple --set flags all apply, mixing two-part and three-part keys."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "profile",
                "init",
                "--repo",
                str(repo),
                "--set",
                "reviewer.agent=antigravity",
                "--set",
                "tester.tdd.directive=Custom TDD",
                "--set",
                "reviewer.followup.directive=Custom FU",
            ],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["reviewer"]["agent"] == "antigravity"
        assert data["roles"]["tester"]["tdd"]["directive"].strip() == "Custom TDD"
        assert data["roles"]["reviewer"]["followup"]["directive"].strip() == "Custom FU"


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

    def test_profile_show_marks_unset_directive_as_default(self, tmp_path):
        """When a role's directive is empty, profile show prints '(default)'."""
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(main, ["profile", "show", "--repo", str(repo)])

        assert result.exit_code == 0
        assert "(default)" in result.output

    def test_profile_show_does_not_mark_set_directive_as_default(self, tmp_path):
        """When a directive is overridden, '(default)' is not shown for that role.

        Derives the role list from ``_ALL_ROLE_NAMES`` so adding a new role to
        the profile schema does not require updating this test.
        """
        from workbench.profile import _ALL_ROLE_NAMES

        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(
            yaml.dump(
                {
                    "roles": {
                        role: {"directive": f"Custom {role} directive"} for role in _ALL_ROLE_NAMES
                    }
                }
            )
        )
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main,
                ["profile", "show", "--repo", str(repo), "--profile", str(profile_path)],
            )

        assert result.exit_code == 0
        assert "(default)" not in result.output
        assert "Custom implementor directive" in result.output

    def test_profile_show_marks_unset_submode_directive_as_default(self, tmp_path):
        """Empty sub-mode directive lines also display '(default)'."""
        profile_path = tmp_path / "custom.yaml"
        # Write a sub-mode block but without a directive value, forcing the empty
        # sub-mode directive path.
        profile_path.write_text(yaml.dump({"roles": {"tester": {"tdd": {"directive": ""}}}}))
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main,
                ["profile", "show", "--repo", str(repo), "--profile", str(profile_path)],
            )

        assert result.exit_code == 0
        assert "tester.tdd.directive" in result.output
        assert "(default)" in result.output

    def test_profile_show_full_prints_default_text(self, tmp_path):
        """wb profile show --full prints the full default directive for unset roles."""
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(main, ["profile", "show", "--repo", str(repo), "--full"])

        assert result.exit_code == 0
        # All roles labelled as using defaults
        for role in ("implementor", "tester", "reviewer", "fixer", "merger", "planner"):
            assert role in result.output
        assert "(default)" in result.output
        # Full default text must come through. Rich wraps to terminal width, so
        # we check for a short, distinctive sentinel substring rather than a full line.
        from workbench.directives import ImplementorDirective

        sentinel = ImplementorDirective.DEFAULT_TEXT.strip().split()[0:4]
        # First few words should appear contiguously somewhere in output.
        assert " ".join(sentinel) in result.output

    def test_profile_show_full_prints_set_directive_without_default_marker(self, tmp_path):
        """wb profile show --full uses overridden text and omits the '(default)' marker for it."""
        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(
            yaml.dump({"roles": {"reviewer": {"directive": "Reviewer override line."}}})
        )
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main,
                [
                    "profile",
                    "show",
                    "--repo",
                    str(repo),
                    "--profile",
                    str(profile_path),
                    "--full",
                ],
            )

        assert result.exit_code == 0
        assert "Reviewer override line." in result.output
        # The reviewer block should not be flagged as default
        reviewer_section = result.output.split("reviewer", 1)[1].split("fixer", 1)[0]
        assert "(default)" not in reviewer_section

    def test_profile_show_full_prints_full_submode_default(self, tmp_path):
        """wb profile show --full prints the full sub-mode default text when sub-mode is unset."""
        # Trigger a sub-mode block by writing it with an empty directive
        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(yaml.dump({"roles": {"tester": {"tdd": {"directive": ""}}}}))
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main,
                [
                    "profile",
                    "show",
                    "--repo",
                    str(repo),
                    "--profile",
                    str(profile_path),
                    "--full",
                ],
            )

        assert result.exit_code == 0
        assert "tester.tdd.directive (default)" in result.output
        from workbench.directives import TddTesterDirective

        sentinel = TddTesterDirective.DEFAULT_TEXT.strip().split()[0:4]
        assert " ".join(sentinel) in result.output

    def test_profile_show_with_explicit_path(self, tmp_path):
        """wb profile show --profile <path> uses the explicit profile."""

        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(yaml.dump({"roles": {"reviewer": {"agent": "antigravity"}}}))
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main, ["profile", "show", "--repo", str(repo), "--profile", str(profile_path)]
            )

        assert result.exit_code == 0
        assert "antigravity" in result.output

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
        """wb profile set reviewer.agent antigravity updates the YAML."""

        repo = tmp_path / "repo"
        repo.mkdir()
        wb_dir = repo / ".workbench"
        wb_dir.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "reviewer.agent", "antigravity", "--repo", str(repo)]
        )

        assert result.exit_code == 0
        profile_path = wb_dir / "profile.yaml"
        assert profile_path.exists()
        data = yaml.safe_load(profile_path.read_text())
        assert data["roles"]["reviewer"]["agent"] == "antigravity"

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
            main, ["profile", "set", "nonexistent.agent", "antigravity", "--repo", str(repo)]
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
            yaml.dump(
                {"roles": {"reviewer": {"agent": "codex"}, "tester": {"agent": "antigravity"}}}
            )
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "reviewer.agent", "antigravity", "--repo", str(repo)]
        )

        assert result.exit_code == 0
        data = yaml.safe_load(profile_path.read_text())
        assert data["roles"]["reviewer"]["agent"] == "antigravity"
        # tester should be unchanged
        assert data["roles"]["tester"]["agent"] == "antigravity"

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
                        "reviewer": {"agent": "antigravity"},
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
        assert "antigravity" in result.output
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


class TestProfileSetDottedPaths:
    """Tests for wb profile set with dotted sub-mode paths."""

    def test_profile_set_tdd_directive(self, tmp_path):
        """wb profile set tester.tdd.directive 'Custom' writes to YAML."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "tester.tdd.directive", "Custom", "--repo", str(repo)]
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["tester"]["tdd"]["directive"] == "Custom"

    def test_profile_set_tdd_directive_extend(self, tmp_path):
        """wb profile set tester.tdd.directive_extend 'Extra' sets the extend field."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "set", "tester.tdd.directive_extend", "Extra", "--repo", str(repo)],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["tester"]["tdd"]["directive_extend"] == "Extra"

    def test_profile_set_followup_directive(self, tmp_path):
        """wb profile set reviewer.followup.directive 'Custom' writes to YAML."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "set", "reviewer.followup.directive", "Custom", "--repo", str(repo)],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["reviewer"]["followup"]["directive"] == "Custom"

    def test_profile_set_planner_directive(self, tmp_path):
        """wb profile set planner.directive 'Custom' writes to YAML."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "planner.directive", "Custom", "--repo", str(repo)]
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["planner"]["directive"] == "Custom"

    def test_profile_set_planner_agent(self, tmp_path):
        """wb profile set planner.agent antigravity writes to YAML."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "planner.agent", "antigravity", "--repo", str(repo)]
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["planner"]["agent"] == "antigravity"

    def test_profile_set_invalid_submode(self, tmp_path):
        """wb profile set merger.tdd.directive 'X' errors: merger doesn't support tdd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "merger.tdd.directive", "X", "--repo", str(repo)]
        )

        assert result.exit_code != 0
        assert "merger does not support a 'tdd' sub-mode" in result.output

    def test_profile_set_invalid_submode_field(self, tmp_path):
        """wb profile set tester.tdd.agent antigravity errors: sub-modes don't have agent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "tester.tdd.agent", "antigravity", "--repo", str(repo)]
        )

        assert result.exit_code != 0
        assert "sub-mode field" in result.output.lower() or "Unknown" in result.output

    def test_profile_show_includes_submodes(self, tmp_path):
        """wb profile show includes tester.tdd.directive when set."""
        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(
            yaml.dump({"roles": {"tester": {"tdd": {"directive": "TDD custom directive"}}}})
        )
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main,
                ["profile", "show", "--repo", str(repo), "--profile", str(profile_path)],
            )

        assert result.exit_code == 0
        assert "tester.tdd.directive" in result.output

    def test_profile_show_omits_unset_submodes(self, tmp_path):
        """Default profile show does not emit any tdd or followup lines."""
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(main, ["profile", "show", "--repo", str(repo)])

        assert result.exit_code == 0
        assert "tdd" not in result.output
        assert "followup" not in result.output

    def test_profile_set_implementor_tdd_directive(self, tmp_path):
        """wb profile set implementor.tdd.directive works (implementor supports tdd)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "set", "implementor.tdd.directive", "TDD impl", "--repo", str(repo)],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["implementor"]["tdd"]["directive"] == "TDD impl"

    def test_profile_set_fixer_tdd_invalid(self, tmp_path):
        """wb profile set fixer.tdd.directive errors: fixer doesn't support tdd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "fixer.tdd.directive", "X", "--repo", str(repo)]
        )

        assert result.exit_code != 0
        assert "fixer does not support a 'tdd' sub-mode" in result.output

    def test_profile_set_planner_tdd_invalid(self, tmp_path):
        """wb profile set planner.tdd.directive errors: planner doesn't support tdd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "planner.tdd.directive", "X", "--repo", str(repo)]
        )

        assert result.exit_code != 0
        assert "planner does not support a 'tdd' sub-mode" in result.output

    def test_profile_set_reviewer_tdd_invalid(self, tmp_path):
        """wb profile set reviewer.tdd.directive errors: reviewer doesn't support tdd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "reviewer.tdd.directive", "X", "--repo", str(repo)]
        )

        assert result.exit_code != 0
        assert "reviewer does not support a 'tdd' sub-mode" in result.output

    def test_profile_set_implementor_followup_invalid(self, tmp_path):
        """wb profile set implementor.followup.directive errors."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "set", "implementor.followup.directive", "X", "--repo", str(repo)],
        )

        assert result.exit_code != 0
        assert "implementor does not support a 'followup' sub-mode" in result.output

    def test_profile_set_tester_followup_invalid(self, tmp_path):
        """wb profile set tester.followup.directive errors."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "tester.followup.directive", "X", "--repo", str(repo)]
        )

        assert result.exit_code != 0
        assert "tester does not support a 'followup' sub-mode" in result.output

    def test_profile_set_four_part_key_errors(self, tmp_path):
        """wb profile set a.b.c.d errors with invalid format."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "tester.tdd.directive.extra", "X", "--repo", str(repo)]
        )

        assert result.exit_code != 0
        assert "<role>.<field> or <role>.<sub_mode>.<field>" in result.output

    def test_profile_set_unknown_submode_errors(self, tmp_path):
        """wb profile set tester.bogus.directive errors with unknown sub-mode."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["profile", "set", "tester.bogus.directive", "X", "--repo", str(repo)]
        )

        assert result.exit_code != 0
        assert "Unknown sub-mode" in result.output

    def test_profile_diff_shows_tdd_submode(self, tmp_path):
        """wb profile diff includes tdd sub-mode when configured."""
        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(
            yaml.dump({"roles": {"tester": {"tdd": {"directive": "TDD custom"}}}})
        )
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main,
                ["profile", "diff", "--repo", str(repo), "--profile", str(profile_path)],
            )

        assert result.exit_code == 0
        assert "tester.tdd.directive" in result.output
        assert "changed" in result.output

    def test_profile_diff_shows_followup_submode(self, tmp_path):
        """wb profile diff includes followup sub-mode when configured."""
        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(
            yaml.dump({"roles": {"reviewer": {"followup": {"directive": "Followup custom"}}}})
        )
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(
                main,
                ["profile", "diff", "--repo", str(repo), "--profile", str(profile_path)],
            )

        assert result.exit_code == 0
        assert "reviewer.followup.directive" in result.output
        assert "changed" in result.output

    def test_profile_diff_no_submode_changes_when_default(self, tmp_path):
        """wb profile diff does not show sub-mode lines for default profile."""
        repo = tmp_path / "repo"
        repo.mkdir()

        runner = CliRunner()
        with patch("workbench.cli.Path.home", return_value=tmp_path / "fakehome"):
            result = runner.invoke(main, ["profile", "diff", "--repo", str(repo)])

        assert result.exit_code == 0
        assert "tdd" not in result.output
        assert "followup" not in result.output

    def test_profile_set_submode_updates_existing_file(self, tmp_path):
        """Setting a sub-mode field on an existing profile preserves other fields."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".workbench").mkdir()
        existing = {"roles": {"tester": {"agent": "antigravity", "directive": "existing"}}}
        (repo / ".workbench" / "profile.yaml").write_text(yaml.dump(existing))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["profile", "set", "tester.tdd.directive", "New TDD", "--repo", str(repo)],
        )

        assert result.exit_code == 0
        data = yaml.safe_load((repo / ".workbench" / "profile.yaml").read_text())
        assert data["roles"]["tester"]["agent"] == "antigravity"
        assert data["roles"]["tester"]["directive"] == "existing"
        assert data["roles"]["tester"]["tdd"]["directive"] == "New TDD"


# ---------------------------------------------------------------------------
# wb run --profile flag
# ---------------------------------------------------------------------------


class TestRunWithProfile:
    def test_run_with_profile_flag(self, git_repo, tmp_path):
        """wb run plan.md --profile custom.yaml passes profile_path to run_plan."""

        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n## Task: hello\nDo something\n")
        profile_path = tmp_path / "custom.yaml"
        profile_path.write_text(yaml.dump({"roles": {"implementor": {"agent": "antigravity"}}}))

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
# wb run --retry-failed / --fail-fast / --only-incomplete flags
# ---------------------------------------------------------------------------


def _run_cli_with_capture(git_repo, tmp_path, extra_args, plan_text=None):
    """Helper to invoke `wb run` with a fake run_plan that captures kwargs.

    Returns (CliRunner result, kwargs dict passed to run_plan).
    """
    import asyncio

    plan = tmp_path / "plan.md"
    plan.write_text(plan_text or "# Plan\n## Task: hello\nDo something\n")

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


def test_run_no_fail_fast_flag(git_repo, tmp_path):
    """--no-fail-fast should pass fail_fast=False to run_plan."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, ["--no-fail-fast"])
    assert result.exit_code == 0, result.output
    assert captured.get("fail_fast") is False


def test_run_headroom_flag(git_repo, tmp_path):
    """--headroom should pass headroom=True to run_plan."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, ["--headroom"])
    assert result.exit_code == 0, result.output
    assert captured.get("headroom") is True


def test_run_no_headroom_flag(git_repo, tmp_path):
    """--no-headroom should pass headroom=False to run_plan."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, ["--no-headroom"])
    assert result.exit_code == 0, result.output
    assert captured.get("headroom") is False


def test_run_headroom_omitted_passes_none(git_repo, tmp_path):
    """Omitting headroom flags should let run_plan use config."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, [])
    assert result.exit_code == 0, result.output
    assert captured.get("headroom") is None


def test_run_fail_fast_frontmatter_precedence(git_repo, tmp_path):
    """Frontmatter should override default, and CLI should override frontmatter."""
    # 1. Frontmatter 'fail_fast: false' overrides the default True -> False
    plan_text = "---\nfail_fast: false\n---\n# Plan\n## Task: hello\nDo something\n"
    result, captured = _run_cli_with_capture(git_repo, tmp_path, [], plan_text=plan_text)
    assert result.exit_code == 0, result.output
    assert captured.get("fail_fast") is False

    # 2. CLI --fail-fast overrides frontmatter 'fail_fast: false' -> True
    result, captured = _run_cli_with_capture(
        git_repo, tmp_path, ["--fail-fast"], plan_text=plan_text
    )
    assert result.exit_code == 0, result.output
    assert captured.get("fail_fast") is True

    # 3. CLI --no-fail-fast overrides frontmatter 'fail_fast: true' -> False
    plan_text_true = "---\nfail_fast: true\n---\n# Plan\n## Task: hello\nDo something\n"
    result, captured = _run_cli_with_capture(
        git_repo, tmp_path, ["--no-fail-fast"], plan_text=plan_text_true
    )
    assert result.exit_code == 0, result.output
    assert captured.get("fail_fast") is False


def test_run_only_incomplete_requires_session_branch(git_repo, tmp_path):
    """--only-incomplete without --session-branch should error."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["run", str(plan), "--no-tmux", "--only-incomplete"])

    assert result.exit_code != 0
    assert "--only-incomplete requires --session-branch" in result.output


def test_run_only_incomplete_with_session_branch(git_repo, tmp_path):
    """--only-incomplete with --session-branch should pass through to run_plan."""
    result, captured = _run_cli_with_capture(
        git_repo, tmp_path, ["--only-incomplete", "-b", "workbench-1"]
    )
    assert result.exit_code == 0, result.output
    assert captured.get("only_incomplete") is True
    assert captured.get("session_branch") == "workbench-1"


def test_run_flags_defaults(git_repo, tmp_path):
    """Without flags, retry_failed and only_incomplete default to False; fail_fast defaults to True."""
    result, captured = _run_cli_with_capture(git_repo, tmp_path, [])
    assert result.exit_code == 0, result.output
    assert captured.get("retry_failed") is False
    assert captured.get("fail_fast") is True
    assert captured.get("only_incomplete") is False


# ---------------------------------------------------------------------------
# wb resume
# ---------------------------------------------------------------------------


class TestResume:
    """Tests for wb resume <session-branch>."""

    def _write_status(self, repo: Path, slug: str, session_branch: str, plan_source: str) -> Path:
        from workbench.session_status import SessionStatus

        ss = SessionStatus(plan_slug=slug, session_branch=session_branch, plan_source=plan_source)
        ss.record_task("task-1", status="pending")
        ss.save(repo)
        return repo / ".workbench" / f"status-{slug}.yaml"

    def test_resume_unknown_session_errors(self, git_repo):
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["resume", "workbench-doesnotexist"])

        assert result.exit_code != 0
        assert "No status file found" in result.output
        assert "workbench-doesnotexist" in result.output

    def test_resume_missing_plan_source_errors(self, git_repo):
        from workbench.session_status import SessionStatus

        ss = SessionStatus(plan_slug="orphan", session_branch="workbench-1", plan_source="")
        ss.record_task("task-1", status="pending")
        ss.save(git_repo)

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["resume", "workbench-1"])

        assert result.exit_code != 0
        assert "no plan_source recorded" in result.output

    def test_resume_plan_source_does_not_exist_errors(self, git_repo):
        self._write_status(git_repo, "gone", "workbench-1", plan_source="/nonexistent/plan.md")

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["resume", "workbench-1"])

        assert result.exit_code != 0
        assert "Plan source not found" in result.output

    def test_resume_forwards_to_run_with_only_incomplete(self, git_repo):
        plans_dir = git_repo / ".workbench" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plans_dir / "demo.md"
        plan_path.write_text("# Plan\n## Task: hello\nDo it.\n")
        self._write_status(git_repo, "demo", "workbench-1", plan_source=str(plan_path))

        runner = CliRunner()
        with (
            patch("workbench.cli.run_plan") as mock_run_plan,
            patch("workbench.cli._find_repo_root", return_value=git_repo),
            patch("workbench.cli.asyncio") as mock_asyncio,
            patch("workbench.cli.check_tmux_available", return_value=True),
        ):
            import asyncio

            async def fake_run_plan(**kwargs):
                return []

            mock_run_plan.side_effect = lambda **kwargs: fake_run_plan(**kwargs)
            mock_asyncio.run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)

            result = runner.invoke(main, ["resume", "workbench-1", "--no-tmux"])

        assert result.exit_code == 0, result.output
        kwargs = mock_run_plan.call_args.kwargs
        assert kwargs["session_branch"] == "workbench-1"
        assert kwargs["only_incomplete"] is True
        assert kwargs["retry_failed"] is True
        assert kwargs["fail_fast"] is True
        # Plan path passed through
        assert Path(str(kwargs["plan"].source)).name == "demo.md"

    def test_resume_no_fail_fast_flag(self, git_repo):
        """wb resume --no-fail-fast should pass fail_fast=False to run_plan."""
        plans_dir = git_repo / ".workbench" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plans_dir / "demo.md"
        plan_path.write_text("# Plan\n## Task: hello\nDo it.\n")
        self._write_status(git_repo, "demo", "workbench-1", plan_source=str(plan_path))

        runner = CliRunner()
        with (
            patch("workbench.cli.run_plan") as mock_run_plan,
            patch("workbench.cli._find_repo_root", return_value=git_repo),
            patch("workbench.cli.asyncio") as mock_asyncio,
            patch("workbench.cli.check_tmux_available", return_value=True),
        ):
            import asyncio

            async def fake_run_plan(**kwargs):
                return []

            mock_run_plan.side_effect = lambda **kwargs: fake_run_plan(**kwargs)
            mock_asyncio.run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)

            result = runner.invoke(main, ["resume", "workbench-1", "--no-fail-fast", "--no-tmux"])

        assert result.exit_code == 0, result.output
        kwargs = mock_run_plan.call_args.kwargs
        assert kwargs["fail_fast"] is False

    def test_resume_passes_through_tdd(self, git_repo):
        plans_dir = git_repo / ".workbench" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plans_dir / "demo.md"
        plan_path.write_text("# Plan\n## Task: hello\nDo it.\n")
        self._write_status(git_repo, "demo", "workbench-1", plan_source=str(plan_path))

        runner = CliRunner()
        with (
            patch("workbench.cli.run_plan") as mock_run_plan,
            patch("workbench.cli._find_repo_root", return_value=git_repo),
            patch("workbench.cli.asyncio") as mock_asyncio,
            patch("workbench.cli.check_tmux_available", return_value=True),
        ):
            import asyncio

            async def fake_run_plan(**kwargs):
                return []

            mock_run_plan.side_effect = lambda **kwargs: fake_run_plan(**kwargs)
            mock_asyncio.run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)

            result = runner.invoke(main, ["resume", "workbench-1", "--no-tmux", "--tdd"])

        assert result.exit_code == 0, result.output
        kwargs = mock_run_plan.call_args.kwargs
        assert kwargs["tdd"] is True
        assert kwargs["only_incomplete"] is True


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

    plan_dir = tmp_path / "my-plan"
    plan_dir.mkdir()
    plan = plan_dir / "plan.md"
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
    assert "antigravity" in config["agents"]
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
    assert "antigravity" in result.output
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


# ---------------------------------------------------------------------------
# wb agents --plan
# ---------------------------------------------------------------------------


def test_agents_init_plan_creates_plan_level_yaml(git_repo):
    """wb agents --plan init creates agents.yaml inside the plan dir."""
    plan_dir = git_repo / ".workbench" / "my-plan"
    plan_dir.mkdir(parents=True)

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "--plan", "my-plan", "init"])

    assert result.exit_code == 0
    config_path = plan_dir / "agents.yaml"
    assert config_path.exists()
    config = yaml.safe_load(config_path.read_text())
    assert "claude" in config["agents"]


def test_agents_init_plan_errors_if_plan_dir_missing(git_repo):
    """wb agents --plan init should error when plan dir does not exist."""
    (git_repo / ".workbench").mkdir(exist_ok=True)

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "--plan", "nonexistent", "init"])

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_agents_list_plan_shows_plan_agents(git_repo):
    """wb agents --plan list shows agents from the plan-level file."""
    plan_dir = git_repo / ".workbench" / "my-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "agents.yaml").write_text(
        "agents:\n  plan-agent:\n    command: plan-cli\n    output_format: text\n"
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "--plan", "my-plan", "list"])

    assert result.exit_code == 0
    assert "plan-agent" in result.output
    assert "plan-cli" in result.output
    assert "my-plan" in result.output


def test_agents_add_plan_writes_to_plan_level_yaml(git_repo):
    """wb agents --plan add writes to the plan-level agents.yaml."""
    plan_dir = git_repo / ".workbench" / "my-plan"
    plan_dir.mkdir(parents=True)

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(
            main,
            ["agents", "--plan", "my-plan", "add", "new-agent", "--command", "new-cli"],
        )

    assert result.exit_code == 0
    config = yaml.safe_load((plan_dir / "agents.yaml").read_text())
    assert "new-agent" in config["agents"]
    assert config["agents"]["new-agent"]["command"] == "new-cli"
    # Project-level file should NOT be created
    assert not (git_repo / ".workbench" / "agents.yaml").exists()


def test_agents_remove_plan_removes_from_plan_level_yaml(git_repo):
    """wb agents --plan remove removes from the plan-level agents.yaml."""
    plan_dir = git_repo / ".workbench" / "my-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "agents.yaml").write_text(
        "agents:\n  plan-agent:\n    command: plan-cli\n  keep:\n    command: keep-cli\n"
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "--plan", "my-plan", "remove", "plan-agent"])

    assert result.exit_code == 0
    config = yaml.safe_load((plan_dir / "agents.yaml").read_text())
    assert "plan-agent" not in config["agents"]
    assert "keep" in config["agents"]


def test_agents_show_plan_shows_plan_level_agent(git_repo):
    """wb agents --plan show reads from the plan-level agents.yaml."""
    plan_dir = git_repo / ".workbench" / "my-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "agents.yaml").write_text(
        "agents:\n  plan-agent:\n    command: plan-cli\n    args: ['{prompt}']\n    output_format: text\n"
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["agents", "--plan", "my-plan", "show", "plan-agent"])

    assert result.exit_code == 0
    assert "plan-cli" in result.output


def test_agents_plan_does_not_affect_project_level(git_repo):
    """Writing to plan-level agents.yaml leaves the project-level file untouched."""
    wb_dir = git_repo / ".workbench"
    wb_dir.mkdir(exist_ok=True)
    (wb_dir / "agents.yaml").write_text("agents:\n  project-agent:\n    command: proj-cli\n")

    plan_dir = wb_dir / "my-plan"
    plan_dir.mkdir()

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        runner.invoke(
            main,
            ["agents", "--plan", "my-plan", "add", "new-agent", "--command", "new-cli"],
        )

    # Project-level file must be unchanged
    config = yaml.safe_load((wb_dir / "agents.yaml").read_text())
    assert list(config["agents"].keys()) == ["project-agent"]


# ---------------------------------------------------------------------------
# Frontmatter run_config → CLI merge
# ---------------------------------------------------------------------------


def _run_cli_with_frontmatter(git_repo, tmp_path, plan_text, extra_args=None):
    """Invoke `wb run` with a plan containing frontmatter and capture run_plan kwargs.

    Thin wrapper around ``_run_cli_with_capture`` that accepts custom plan text.
    """
    return _run_cli_with_capture(git_repo, tmp_path, extra_args or [], plan_text=plan_text)


class TestRunConfigFrontmatter:
    """Tests for frontmatter run_config merging into CLI flags."""

    def test_frontmatter_tdd_no_cli_flag(self, git_repo, tmp_path):
        """Plan with tdd: true, no CLI --tdd → tdd is True."""
        plan_text = "---\ntdd: true\n---\n# Plan\n## Task: hello\nDo something\n"
        result, captured = _run_cli_with_frontmatter(
            git_repo, tmp_path, plan_text, ["-b", "workbench-1"]
        )
        assert result.exit_code == 0, result.output
        assert captured.get("tdd") is True

    def test_frontmatter_tdd_with_cli_skip_test_errors(self, git_repo, tmp_path):
        """Plan with tdd: true, CLI --skip-test → mutual-exclusivity error."""
        plan_text = "---\ntdd: true\n---\n# Plan\n## Task: hello\nDo something\n"
        result, _ = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text, ["--skip-test"])
        assert result.exit_code != 0
        assert "--tdd and --skip-test are mutually exclusive" in result.output

    def test_cli_overrides_frontmatter_string(self, git_repo, tmp_path):
        """Plan with agent: antigravity, CLI --agent claude → agent == 'claude'."""
        plan_text = "---\nagent: antigravity\n---\n# Plan\n## Task: hello\nDo something\n"
        result, captured = _run_cli_with_frontmatter(
            git_repo, tmp_path, plan_text, ["--agent", "claude"]
        )
        assert result.exit_code == 0, result.output
        assert captured.get("agent_cmd") == "claude"

    def test_cli_overrides_frontmatter_int(self, git_repo, tmp_path):
        """Plan with max_concurrent: 8, CLI -j 2 → max_concurrent == 2."""
        plan_text = "---\nmax_concurrent: 8\n---\n# Plan\n## Task: hello\nDo something\n"
        result, captured = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text, ["-j", "2"])
        assert result.exit_code == 0, result.output
        assert captured.get("max_concurrent") == 2

    def test_frontmatter_session_branch_used_when_no_b(self, git_repo, tmp_path):
        """Plan with session_branch: workbench-feat, no CLI -b → session_branch used."""
        plan_text = (
            "---\nsession_branch: workbench-feat\n---\n# Plan\n## Task: hello\nDo something\n"
        )
        result, captured = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text)
        assert result.exit_code == 0, result.output
        assert captured.get("session_branch") == "workbench-feat"

    def test_frontmatter_session_branch_overridden_by_b(self, git_repo, tmp_path):
        """Plan with session_branch: workbench-feat, CLI -b workbench-other → overridden."""
        plan_text = (
            "---\nsession_branch: workbench-feat\n---\n# Plan\n## Task: hello\nDo something\n"
        )
        result, captured = _run_cli_with_frontmatter(
            git_repo, tmp_path, plan_text, ["-b", "workbench-other"]
        )
        assert result.exit_code == 0, result.output
        assert captured.get("session_branch") == "workbench-other"

    def test_frontmatter_max_concurrent_used(self, git_repo, tmp_path):
        """Plan with max_concurrent: 7 → max_concurrent == 7."""
        plan_text = "---\nmax_concurrent: 7\n---\n# Plan\n## Task: hello\nDo something\n"
        result, captured = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text)
        assert result.exit_code == 0, result.output
        assert captured.get("max_concurrent") == 7

    def test_frontmatter_profile_path_resolved(self, git_repo, tmp_path):
        """Plan with profile: /abs/path/profile.yaml → profile_path set as Path.

        Must be a Path (not str) so downstream code (Profile.resolve) can call
        Path methods like .exists() on it — matching what click's --profile
        option produces via type=click.Path(path_type=Path).
        """
        profile = tmp_path / "profile.yaml"
        profile.write_text("roles: {}\n")
        plan_text = f"---\nprofile: {profile}\n---\n# Plan\n## Task: hello\nDo something\n"
        result, captured = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text)
        assert result.exit_code == 0, result.output
        assert captured.get("profile_path") == profile
        assert isinstance(captured.get("profile_path"), Path)

    def test_frontmatter_unknown_key_surfaces_error(self, git_repo, tmp_path):
        """Plan with bogus: 1 → exits non-zero with key name in message."""
        plan_text = "---\nbogus: 1\n---\n# Plan\n## Task: hello\nDo something\n"
        result, _ = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text)
        assert result.exit_code != 0
        assert "bogus" in result.output

    def test_no_frontmatter_unchanged(self, git_repo, tmp_path):
        """Plan without frontmatter → all defaults unchanged."""
        plan_text = "# Plan\n## Task: hello\nDo something\n"
        result, captured = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text)
        assert result.exit_code == 0, result.output
        assert captured.get("tdd") is False
        assert captured.get("max_concurrent") == 4
        assert captured.get("agent_cmd") == "claude"
        assert captured.get("session_branch") is None

    def test_frontmatter_only_incomplete_not_allowed(self, git_repo, tmp_path):
        """only_incomplete in frontmatter → parser rejects it as unknown key."""
        plan_text = "---\nonly_incomplete: true\n---\n# Plan\n## Task: hello\nDo something\n"
        result, _ = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text)
        assert result.exit_code != 0
        assert "only_incomplete" in result.output

    def test_frontmatter_tdd_explicit_cli_tdd_passes(self, git_repo, tmp_path):
        """Plan with tdd: true, CLI --tdd → tdd is True (both agree)."""
        plan_text = "---\ntdd: true\n---\n# Plan\n## Task: hello\nDo something\n"
        result, captured = _run_cli_with_frontmatter(git_repo, tmp_path, plan_text, ["--tdd"])
        assert result.exit_code == 0, result.output
        assert captured.get("tdd") is True


# ---------------------------------------------------------------------------
# wb run --final-review
# ---------------------------------------------------------------------------


def _write_status_file(repo, plan_slug, session_branch, tasks, final_reviews=None, plan_source=""):
    """Write a status.yaml for testing."""
    wb = repo / ".workbench" / plan_slug
    wb.mkdir(parents=True, exist_ok=True)
    session_data = {"tasks": tasks}
    if final_reviews:
        session_data["final_reviews"] = final_reviews
    data = {"sessions": {session_branch: session_data}}
    if plan_source:
        data["plan_source"] = plan_source
    (wb / "status.yaml").write_text(yaml.dump(data))


def _make_plan_in_dir(base_dir, plan_slug="my-plan"):
    """Create a plan.md inside a named directory, returning the plan path."""
    plan_dir = base_dir / plan_slug
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_dir / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")
    return plan


def test_run_final_review_flag_invokes_orchestration(git_repo, tmp_path):
    """--final-review should invoke run_final_review after run_plan."""
    from workbench.session_status import FinalReviewRecord

    plan = _make_plan_in_dir(tmp_path, "my-plan")

    # Pre-create a status file so the post-run lookup finds merged tasks
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    mock_record = FinalReviewRecord(
        timestamp="2026-05-10T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
        pr_url="https://github.com/test/test/pull/1",
    )

    async def fake_run_plan(**kwargs):
        return []

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan) as mock_run_plan,
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["run", str(plan), "--no-tmux", "--final-review", "-b", "workbench-1"],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.called
    assert "PASS" in result.output


def test_run_final_review_skipped_on_failed_wave(git_repo, tmp_path):
    """--final-review must not run if any task in the run failed."""
    plan = _make_plan_in_dir(tmp_path, "my-plan")

    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "failed",
                "branch": "wb/feat",
                "merged": False,
                "last_agent": "tester",
            }
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    async def fake_run_plan(**kwargs):
        return []

    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch("workbench.cli.run_final_review") as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["run", str(plan), "--no-tmux", "--final-review", "-b", "workbench-1"],
        )

    assert result.exit_code == 0, result.output
    assert not mock_final_review.called
    assert "Skipping final review" in result.output


def test_run_final_review_not_suppressed_by_carryforward_failure(git_repo, tmp_path):
    """--final-review must run when a --task-filtered run succeeds even if prior runs
    left a failed record for a non-targeted task in the status file."""
    from workbench.session_status import FinalReviewRecord

    # Two-task plan so task-2 can be targeted while task-1 is carry-forward.
    plan_dir = tmp_path / "my-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_dir / "plan.md"
    plan.write_text("# Plan\n## Task: Task One\nDo one\n\n## Task: Task Two\nDo two\n")

    # Status: task-1 failed in a prior run (carry-forward), task-2 merged this run.
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "failed",
                "branch": "wb/task-one",
                "merged": False,
                "last_agent": "tester",
            },
            "task-2": {
                "status": "done",
                "branch": "wb/task-two",
                "merged": True,
                "last_agent": "reviewer",
            },
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    mock_record = FinalReviewRecord(
        timestamp="2026-05-10T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    async def fake_run_plan(**kwargs):
        return []

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch("workbench.cli.run_final_review", side_effect=fake_final_review) as mock_fr,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            [
                "run",
                str(plan),
                "--no-tmux",
                "--final-review",
                "-b",
                "workbench-1",
                "--task",
                "task-2",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_fr.called, "final review must run when targeted task succeeded"
    assert "Skipping final review" not in result.output


def test_run_final_review_skipped_when_no_tasks_merge(git_repo, tmp_path):
    """--final-review with 0 merged tasks should not invoke run_final_review."""
    plan = _make_plan_in_dir(tmp_path, "my-plan")

    # Status with no merged tasks
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "failed",
                "branch": "wb/feat",
                "merged": False,
                "last_agent": "implementor",
            }
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    async def fake_run_plan(**kwargs):
        return []

    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch("workbench.cli.run_final_review") as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["run", str(plan), "--no-tmux", "--final-review", "-b", "workbench-1"],
        )

    assert result.exit_code == 0, result.output
    assert not mock_final_review.called


def test_merge_review_flag_invokes_orchestration(git_repo, tmp_path):
    """wb merge --review should invoke run_final_review after merging."""
    from workbench.session_status import FinalReviewRecord, SessionStatus

    plan = _make_plan_in_dir(tmp_path, "my-plan")

    # Create a SessionStatus with merged tasks and plan_source
    status = SessionStatus(
        plan_slug="my-plan",
        session_branch="workbench-1",
        plan_source=str(plan),
    )
    status.record_task("task-1", "done", "wb/feat", merged=True, last_agent="reviewer")

    mock_record = FinalReviewRecord(
        timestamp="2026-05-10T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_merge(**kwargs):
        return status

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch("workbench.cli.merge_unmerged", side_effect=fake_merge),
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["merge", "-b", "workbench-1", "--review", "--no-tmux"],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.called


def test_final_review_standalone_command(git_repo, tmp_path):
    """wb final-review session-branch should invoke run_final_review."""
    from workbench.session_status import FinalReviewRecord

    plan = _make_plan_in_dir(tmp_path, "my-plan")

    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    mock_record = FinalReviewRecord(
        timestamp="2026-05-10T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["final-review", "workbench-1", "--no-tmux"],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.called
    assert "PASS" in result.output


def test_review_is_alias_for_final_review(git_repo, tmp_path):
    """wb review should be an alias for wb final-review."""
    from workbench.session_status import FinalReviewRecord

    plan = _make_plan_in_dir(tmp_path, "my-plan")

    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    mock_record = FinalReviewRecord(
        timestamp="2026-05-11T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(main, ["review", "workbench-1", "--no-tmux"])

    assert result.exit_code == 0, result.output
    assert mock_final_review.called
    assert "PASS" in result.output


def test_final_review_standalone_no_session_error(git_repo):
    """wb final-review with nonexistent session should error."""
    runner = CliRunner()

    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["final-review", "nonexistent-session", "--no-tmux"])

    assert result.exit_code != 0
    assert "No status file found" in result.output


def test_status_shows_final_review_summary(git_repo):
    """wb status should show final review summary when reviews exist."""
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        final_reviews=[
            {
                "timestamp": "2026-05-10T00:00:00Z",
                "verdict": "pass",
                "review_path": ".workbench/my-plan/wrap-up/workbench-1/review.md",
                "requirements_path": ".workbench/my-plan/wrap-up/workbench-1/requirements.md",
                "summarizer_agent": "claude",
                "reviewer_agent": "claude",
                "pr_url": "https://github.com/test/test/pull/1",
            }
        ],
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["status"])

    assert result.exit_code == 0, result.output
    assert "Final review" in result.output
    assert "PASS" in result.output
    assert "1 run(s)" in result.output


def test_status_shows_not_run_when_no_reviews(git_repo):
    """wb status should show 'not run' when no final reviews exist."""
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["status"])

    assert result.exit_code == 0, result.output
    assert "not run" in result.output


def test_pr_title_override_passed_through(git_repo, tmp_path):
    """--pr-title should propagate to run_final_review kwargs."""
    from workbench.session_status import FinalReviewRecord

    plan = _make_plan_in_dir(tmp_path, "my-plan")

    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    mock_record = FinalReviewRecord(
        timestamp="2026-05-10T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["final-review", "workbench-1", "--no-tmux", "--pr-title", "My title"],
        )

    assert result.exit_code == 0, result.output
    captured = dict(mock_final_review.call_args.kwargs)
    assert captured.get("pr_title") == "My title"


def test_skip_pr_flag_propagates(git_repo, tmp_path):
    """--skip-pr should pass skip_pr=True to run_final_review."""
    from workbench.session_status import FinalReviewRecord

    plan = _make_plan_in_dir(tmp_path, "my-plan")

    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    mock_record = FinalReviewRecord(
        timestamp="2026-05-10T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["final-review", "workbench-1", "--no-tmux", "--skip-pr"],
        )

    assert result.exit_code == 0, result.output
    captured = dict(mock_final_review.call_args.kwargs)
    assert captured.get("skip_pr") is True


def test_frontmatter_final_review_true_runs_review_without_flag(git_repo, tmp_path):
    """Plan frontmatter final_review: true should trigger review without --final-review flag."""
    from workbench.session_status import FinalReviewRecord

    plan_text = "---\nfinal_review: true\n---\n# Plan\n## Task: hello\nDo something\n"
    plan_dir = tmp_path / "my-plan"
    plan_dir.mkdir()
    plan = plan_dir / "plan.md"
    plan.write_text(plan_text)

    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    mock_record = FinalReviewRecord(
        timestamp="2026-05-10T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_run_plan(**kwargs):
        return []

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["run", str(plan), "--no-tmux", "-b", "workbench-1"],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.called


def test_cli_flag_overrides_frontmatter(git_repo, tmp_path):
    """--final-review flag should override frontmatter final_review: false."""
    from workbench.session_status import FinalReviewRecord

    plan_text = "---\nfinal_review: false\n---\n# Plan\n## Task: hello\nDo something\n"
    plan_dir = tmp_path / "my-plan"
    plan_dir.mkdir()
    plan = plan_dir / "plan.md"
    plan.write_text(plan_text)

    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    mock_record = FinalReviewRecord(
        timestamp="2026-05-10T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_run_plan(**kwargs):
        return []

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["run", str(plan), "--no-tmux", "--final-review", "-b", "workbench-1"],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.called


# ---------------------------------------------------------------------------
# Plan-name resolution in wb run
# ---------------------------------------------------------------------------


def _capture_run_plan(git_repo, args):
    """Invoke `wb run` with `run_plan` mocked, returning (result, captured kwargs)."""
    import asyncio as _asyncio

    runner = CliRunner()
    with (
        patch("workbench.cli.run_plan") as mock_run_plan,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.asyncio") as mock_asyncio,
    ):

        async def fake_run_plan(**kwargs):
            return []

        mock_run_plan.side_effect = lambda **kwargs: fake_run_plan(**kwargs)
        mock_asyncio.run = lambda coro: _asyncio.new_event_loop().run_until_complete(coro)

        result = runner.invoke(main, ["run"] + args + ["--no-tmux"])

    captured = dict(mock_run_plan.call_args.kwargs) if mock_run_plan.called else {}
    return result, captured


def test_run_resolves_plan_name(git_repo):
    """`wb run myfeature` should resolve to .workbench/myfeature/plan.md."""
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n## Task: hello\nDo something\n")

    result, captured = _capture_run_plan(git_repo, ["myfeature"])

    assert result.exit_code == 0, result.output
    assert captured.get("plan") is not None
    assert captured["plan"].source == (plan_dir / "plan.md").resolve()


def test_run_legacy_plan_path_still_works(git_repo):
    """`wb run foo` should resolve to legacy .workbench/plans/foo.md."""
    plans_dir = git_repo / ".workbench" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "foo.md").write_text("# Plan\n## Task: hello\nDo something\n")

    result, captured = _capture_run_plan(git_repo, ["foo"])

    assert result.exit_code == 0, result.output
    assert captured["plan"].source == (plans_dir / "foo.md").resolve()


def test_run_full_path_passthrough(git_repo, tmp_path_factory):
    """A full path argument should bypass name resolution, including paths outside the repo."""
    external = tmp_path_factory.mktemp("external")
    plan = external / "somewhere.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    result, captured = _capture_run_plan(git_repo, [str(plan)])

    assert result.exit_code == 0, result.output
    assert captured["plan"].source == plan.resolve()
    # Ensure the path was treated as a path (not a name lookup); the plan
    # really does live outside .workbench/<name>/plan.md.
    assert ".workbench" not in str(captured["plan"].source)


def test_run_prefers_new_layout_over_legacy(git_repo):
    """When both .workbench/<name>/plan.md and .workbench/plans/<name>.md exist,
    new layout wins."""
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n## Task: new\nNew layout.\n")

    legacy_dir = git_repo / ".workbench" / "plans"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "myfeature.md").write_text("# Plan\n## Task: legacy\nLegacy layout.\n")

    result, captured = _capture_run_plan(git_repo, ["myfeature"])

    assert result.exit_code == 0, result.output
    assert captured["plan"].source == (plan_dir / "plan.md").resolve()


def test_run_unknown_name_errors(git_repo):
    """`wb run nonexistent` should fail with a clear message."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["run", "nonexistent", "--no-tmux"])

    assert result.exit_code != 0
    assert "nonexistent" in result.output


# ---------------------------------------------------------------------------
# wb pull-request
# ---------------------------------------------------------------------------


def _write_status_file_multi(repo, plan_slug, sessions_data, plan_source=""):
    """Write a status.yaml with multiple sessions for testing."""
    wb = repo / ".workbench" / plan_slug
    wb.mkdir(parents=True, exist_ok=True)
    data = {"sessions": sessions_data}
    if plan_source:
        data["plan_source"] = plan_source
    (wb / "status.yaml").write_text(yaml.dump(data))


def test_pull_request_with_one_session_succeeds(git_repo, tmp_path):
    """One session in status.yaml → command auto-discovers and opens PR."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    _write_status_file(
        git_repo,
        "myfeature",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    async def fake_pr_writer(**kwargs):
        return "Agent title", "Body content from agent"

    async def fake_create_pr(*args, **kwargs):
        return True, "https://github.com/test/test/pull/42"

    with (
        patch("workbench.cli.run_pr_writer", side_effect=fake_pr_writer) as mock_writer,
        patch("workbench.cli.create_pr", side_effect=fake_create_pr) as mock_create,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(main, ["pull-request", "myfeature", "--no-tmux"])

    assert result.exit_code == 0, result.output
    assert mock_writer.called
    assert mock_create.called
    assert "https://github.com/test/test/pull/42" in result.output


def test_pull_request_with_multiple_sessions_requires_b(git_repo, tmp_path):
    """Multiple sessions, no -b → error mentions both session names and -b."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    _write_status_file_multi(
        git_repo,
        "myfeature",
        {
            "workbench-1": {
                "tasks": {
                    "task-1": {
                        "status": "done",
                        "branch": "wb/a",
                        "merged": True,
                        "last_agent": "reviewer",
                    }
                }
            },
            "workbench-2": {
                "tasks": {
                    "task-1": {
                        "status": "done",
                        "branch": "wb/b",
                        "merged": True,
                        "last_agent": "reviewer",
                    }
                }
            },
        },
        plan_source=str(plan),
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["pull-request", "myfeature", "--no-tmux"])

    assert result.exit_code != 0
    assert "workbench-1" in result.output
    assert "workbench-2" in result.output
    assert "-b" in result.output


def test_pull_request_with_explicit_b_picks_session(git_repo, tmp_path):
    """Two sessions, -b workbench-2 → writer called with session_branch='workbench-2'."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    _write_status_file_multi(
        git_repo,
        "myfeature",
        {
            "workbench-1": {
                "tasks": {
                    "task-1": {
                        "status": "done",
                        "branch": "wb/a",
                        "merged": True,
                        "last_agent": "reviewer",
                    }
                }
            },
            "workbench-2": {
                "tasks": {
                    "task-1": {
                        "status": "done",
                        "branch": "wb/b",
                        "merged": True,
                        "last_agent": "reviewer",
                    }
                }
            },
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    async def fake_pr_writer(**kwargs):
        return "Agent title", "Body content from agent"

    async def fake_create_pr(*args, **kwargs):
        return True, "https://github.com/test/test/pull/2"

    with (
        patch("workbench.cli.run_pr_writer", side_effect=fake_pr_writer) as mock_writer,
        patch("workbench.cli.create_pr", side_effect=fake_create_pr),
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main, ["pull-request", "myfeature", "-b", "workbench-2", "--no-tmux"]
        )

    assert result.exit_code == 0, result.output
    assert mock_writer.call_args.kwargs.get("session_branch") == "workbench-2"


def test_pull_request_no_sessions_errors(git_repo, tmp_path):
    """Missing status file → clear error."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["pull-request", "nonexistent", "--no-tmux"])

    assert result.exit_code != 0
    assert "nonexistent" in result.output


def test_preview_resolves_plan_name(git_repo):
    """`wb preview myfeature` should resolve to .workbench/myfeature/plan.md."""
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["preview", "myfeature"])

    assert result.exit_code == 0, result.output
    assert str(plan_dir / "plan.md") in result.output or "hello" in result.output


def test_run_loads_per_plan_agents_yaml(git_repo):
    """When `.workbench/myfeature/agents.yaml` exists, it is passed in agents_config_paths."""
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n## Task: hello\nDo something\n")
    (plan_dir / "agents.yaml").write_text("agents: {}\n")

    result, captured = _capture_run_plan(git_repo, ["myfeature"])

    assert result.exit_code == 0, result.output
    paths = captured.get("agents_config_paths") or []
    assert (plan_dir / "agents.yaml") in paths
    assert paths[0] == plan_dir / "agents.yaml"


def test_run_loads_per_plan_profile(git_repo):
    """Plan-name resolution must yield a Plan whose folder_id matches the per-plan
    folder, so run_plan layers the per-plan profile.yaml.
    """
    from workbench.path_resolver import resolve_profile_paths
    from workbench.profile import Profile

    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n## Task: hello\nDo something\n")
    (plan_dir / "profile.yaml").write_text(
        "roles:\n  implementor:\n    directive: per-plan-impl\n"
    )

    # Sanity-check the resolution helper itself.
    paths = resolve_profile_paths(git_repo, plan_slug="myfeature")
    assert paths and paths[0] == plan_dir / "profile.yaml"
    merged = Profile.from_layered_yaml(list(reversed(paths)))
    assert merged.implementor.directive == "per-plan-impl"

    # The CLI-layer assertion: the parsed plan's folder_id is the plan slug,
    # which is what run_plan uses to look up the per-plan profile.
    result, captured = _capture_run_plan(git_repo, ["myfeature"])
    assert result.exit_code == 0, result.output
    assert captured["plan"].source == (plan_dir / "plan.md").resolve()
    assert captured["plan"].folder_id == "myfeature"


# ---------------------------------------------------------------------------
# wb plan --copy-settings and wb plan copy-settings
# ---------------------------------------------------------------------------


def test_plan_copy_settings_creates_profile_yaml(git_repo):
    """`wb plan copy-settings myfeature` materializes profile.yaml into the plan folder."""
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n## Task: hello\nDo something\n")

    project_profile = git_repo / ".workbench" / "profile.yaml"
    project_profile.write_text(
        "roles:\n  implementor:\n    agent: antigravity\n    directive: project-impl\n"
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["plan", "copy-settings", "myfeature"])

    assert result.exit_code == 0, result.output
    target = plan_dir / "profile.yaml"
    assert target.exists()
    text = target.read_text()
    assert text.startswith("# Frozen by `wb plan --copy-settings` on ")
    data = yaml.safe_load(text)
    assert data["roles"]["implementor"]["agent"] == "antigravity"
    assert data["roles"]["implementor"]["directive"] == "project-impl"


def test_plan_copy_settings_creates_agents_yaml(git_repo):
    """`wb plan copy-settings myfeature` materializes agents.yaml into the plan folder."""
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n## Task: hello\nDo something\n")

    project_agents = git_repo / ".workbench" / "agents.yaml"
    project_agents.write_text(
        "agents:\n  my-custom:\n    command: foo\n    args: ['{prompt}']\n"
        "    output_format: text\n"
    )

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["plan", "copy-settings", "myfeature"])

    assert result.exit_code == 0, result.output
    target = plan_dir / "agents.yaml"
    assert target.exists()
    text = target.read_text()
    assert text.startswith("# Frozen by `wb plan --copy-settings` on ")
    assert "my-custom" in text


def test_plan_copy_settings_skips_missing_sources(git_repo):
    """When no source files exist, copy-settings prints messages and doesn't error."""
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n## Task: hello\nDo something\n")

    # Guard against a stray project profile.yaml/agents.yaml leaking from
    # other tests or future fixture changes.
    assert not (git_repo / ".workbench" / "profile.yaml").exists()
    assert not (git_repo / ".workbench" / "agents.yaml").exists()

    runner = CliRunner()
    fake_home = git_repo / "fake-home"
    fake_home.mkdir()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.path_resolver.Path.home", return_value=fake_home),
    ):
        result = runner.invoke(main, ["plan", "copy-settings", "myfeature"])

    assert result.exit_code == 0, result.output
    assert not (plan_dir / "profile.yaml").exists()
    assert not (plan_dir / "agents.yaml").exists()
    assert "skipping" in result.output


def test_plan_copy_settings_errors_on_missing_plan_folder(git_repo):
    """copy-settings should error if the plan folder doesn't exist."""
    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["plan", "copy-settings", "nonexistent"])

    assert result.exit_code != 0
    assert "nonexistent" in result.output or "not found" in result.output.lower()


def test_plan_with_copy_settings_flag(git_repo):
    """`wb plan "prompt" --name myfeature --copy-settings` copies settings AND runs planner."""
    from workbench.agents import AgentResult, Role, TaskStatus

    project_profile = git_repo / ".workbench" / "profile.yaml"
    project_profile.parent.mkdir(parents=True, exist_ok=True)
    project_profile.write_text("roles:\n  implementor:\n    agent: antigravity\n")

    fake_result = AgentResult(
        task_id="planner",
        role=Role.IMPLEMENTOR,
        status=TaskStatus.DONE,
        output="Plan written.",
    )

    call_log: list[dict] = []

    async def fake_run_planner(**kwargs):
        call_log.append(dict(kwargs))
        plan_name = kwargs["plan_name"]
        output_path = git_repo / ".workbench" / plan_name / "plan.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# Plan\n## Task: Hello\nDo something.\n")
        return fake_result

    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
        patch("workbench.agents.run_planner", side_effect=fake_run_planner) as mock_planner,
    ):
        result = runner.invoke(
            main,
            ["plan", "Add feature X", "--name", "myfeature", "--copy-settings", "--no-tmux"],
        )

    assert result.exit_code == 0, result.output
    assert (git_repo / ".workbench" / "myfeature" / "profile.yaml").exists()
    # The planner must actually have been invoked with the right plan name —
    # otherwise this would pass even if the planner step were deleted.
    assert mock_planner.call_count == 1
    assert call_log and call_log[0]["plan_name"] == "myfeature"


# ---------------------------------------------------------------------------
# wb clean removes worktrees in both layouts
# ---------------------------------------------------------------------------


def _make_real_worktree(repo: Path, rel_path: str, branch: str) -> Path:
    """Create a real git worktree at `repo/rel_path` on a fresh branch."""
    worktree_dir = repo / rel_path
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "branch", "--no-track", branch, "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", str(worktree_dir), branch],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return worktree_dir


def test_clean_removes_new_layout_worktrees(git_repo):
    """`wb clean --force` removes worktrees nested under .workbench/<plan>/."""
    wt = _make_real_worktree(git_repo, ".workbench/myfeature/task-1", "wb/myfeature-task-1")
    assert wt.exists()

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["clean", "--force"])

    assert result.exit_code == 0, result.output
    assert not wt.exists()


def test_clean_removes_legacy_layout_worktrees(git_repo):
    """`wb clean --force` removes legacy top-level worktrees under .workbench/."""
    wt = _make_real_worktree(git_repo, ".workbench/task-1", "wb/task-1")
    assert wt.exists()

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["clean", "--force"])

    assert result.exit_code == 0, result.output
    assert not wt.exists()


class TestCleanProjectScope:
    """Tests for `wb clean PROJECT` (project-scoped cleanup)."""

    def test_errors_when_project_folder_missing(self, git_repo):
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "nonexistent"])
        assert result.exit_code != 0
        assert "No project folder" in result.output

    def test_accepts_plan_path_form(self, git_repo):
        """`wb clean .workbench/<slug>/plan.md` resolves to the same slug."""
        _write_status_file(
            git_repo,
            "alpha",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/a", "merged": True}},
        )
        _write_status_file(
            git_repo,
            "beta",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/b", "merged": True}},
        )
        (git_repo / ".workbench" / "alpha" / "plan.md").write_text("# Plan\n")

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", ".workbench/alpha/plan.md"])

        assert result.exit_code == 0, result.output
        assert not (git_repo / ".workbench" / "alpha" / "status.yaml").exists()
        assert (git_repo / ".workbench" / "beta" / "status.yaml").exists()

    def test_errors_when_plan_path_points_outside_workbench(self, git_repo):
        """A path whose parent dir doesn't exist under .workbench/ errors clearly."""
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "some/random/plan.md"])
        assert result.exit_code != 0
        assert "No project folder" in result.output

    def test_only_removes_named_project_status_file(self, git_repo):
        _write_status_file(
            git_repo,
            "alpha",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/a", "merged": True}},
        )
        _write_status_file(
            git_repo,
            "beta",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/b", "merged": True}},
        )

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "alpha"])

        assert result.exit_code == 0, result.output
        assert not (git_repo / ".workbench" / "alpha").exists()  # folder removed when empty
        assert (git_repo / ".workbench" / "beta" / "status.yaml").exists()

    def test_removes_empty_project_folder_after_cleanup(self, git_repo):
        _write_status_file(
            git_repo,
            "alpha",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/a", "merged": True}},
        )

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "alpha"])

        assert result.exit_code == 0, result.output
        assert not (git_repo / ".workbench" / "alpha").exists()
        assert "project folder" in result.output

    def test_keeps_project_folder_when_remnants_exist(self, git_repo):
        """Plan.md or other untouched files keep the folder around."""
        _write_status_file(
            git_repo,
            "alpha",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/a", "merged": True}},
        )
        (git_repo / ".workbench" / "alpha" / "plan.md").write_text("# Plan\n")

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "alpha"])

        assert result.exit_code == 0, result.output
        assert (git_repo / ".workbench" / "alpha" / "plan.md").exists()
        assert not (git_repo / ".workbench" / "alpha" / "status.yaml").exists()

    def test_dry_run_does_not_remove_project_folder(self, git_repo):
        _write_status_file(
            git_repo,
            "alpha",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/a", "merged": True}},
        )

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "alpha", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert (git_repo / ".workbench" / "alpha" / "status.yaml").exists()
        assert (git_repo / ".workbench" / "alpha").is_dir()

    def test_only_removes_named_project_worktrees(self, git_repo):
        _write_status_file(
            git_repo,
            "alpha",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/alpha-t1", "merged": True}},
        )
        _write_status_file(
            git_repo,
            "beta",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/beta-t1", "merged": True}},
        )
        wt_alpha = _make_real_worktree(git_repo, ".workbench/alpha/t1", "wb/alpha-t1")
        wt_beta = _make_real_worktree(git_repo, ".workbench/beta/t1", "wb/beta-t1")

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "alpha"])

        assert result.exit_code == 0, result.output
        assert not wt_alpha.exists()
        assert wt_beta.exists()

    def test_only_removes_named_project_branches(self, git_repo):
        _write_status_file(
            git_repo,
            "alpha",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/alpha-t1", "merged": True}},
        )
        _write_status_file(
            git_repo,
            "beta",
            "workbench-1",
            {"t1": {"status": "done", "branch": "wb/beta-t1", "merged": True}},
        )
        subprocess.run(
            ["git", "branch", "--no-track", "wb/alpha-t1", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "--no-track", "wb/beta-t1", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "alpha"])

        assert result.exit_code == 0, result.output
        branches = subprocess.run(
            ["git", "branch", "--list", "wb/*"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        ).stdout
        assert "wb/alpha-t1" not in branches
        assert "wb/beta-t1" in branches

    def test_only_removes_named_project_ephemeral_worktrees(self, git_repo):
        # Create ephemeral wt dirs without making them real git worktrees;
        # _list_ephemeral_worktrees uses filesystem-scan, and the cleanup
        # falls back to rmtree when git worktree remove fails on a fake wt.
        alpha = git_repo / ".workbench" / "alpha" / "wrap-up" / "wb-1" / ".review-wt"
        beta = git_repo / ".workbench" / "beta" / "wrap-up" / "wb-1" / ".review-wt"
        alpha.mkdir(parents=True)
        beta.mkdir(parents=True)

        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["clean", "alpha"])

        assert result.exit_code == 0, result.output
        assert not alpha.exists()
        assert beta.exists()


def test_pull_request_plan_as_path(git_repo, tmp_path):
    """Passing path to plan.md should resolve to slug from parent directory."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    _write_status_file(
        git_repo,
        "myfeature",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    async def fake_pr_writer(**kwargs):
        return "Agent title", "Body content"

    async def fake_create_pr(*args, **kwargs):
        return True, "https://github.com/test/test/pull/1"

    with (
        patch("workbench.cli.run_pr_writer", side_effect=fake_pr_writer) as mock_writer,
        patch("workbench.cli.create_pr", side_effect=fake_create_pr),
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(main, ["pull-request", str(plan), "--no-tmux"])

    assert result.exit_code == 0, result.output
    assert mock_writer.call_args.kwargs.get("plan_slug") == "myfeature"


def test_pull_request_pr_body_file_skips_writer(git_repo, tmp_path):
    """--pr-body-file → writer not invoked; create_pr called with that body."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    _write_status_file(
        git_repo,
        "myfeature",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    body_file = tmp_path / "body.md"
    body_file.write_text("Custom body content from file")

    runner = CliRunner()

    async def fake_create_pr(repo, session, base, title, body):
        return True, "https://github.com/test/test/pull/3"

    with (
        patch("workbench.cli.run_pr_writer") as mock_writer,
        patch("workbench.cli.create_pr", side_effect=fake_create_pr) as mock_create,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            [
                "pull-request",
                "myfeature",
                "--no-tmux",
                "--pr-body-file",
                str(body_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert not mock_writer.called
    assert mock_create.call_args.args[4] == "Custom body content from file"


def test_pull_request_pr_title_override_wins(git_repo, tmp_path):
    """--pr-title overrides writer-returned title."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    _write_status_file(
        git_repo,
        "myfeature",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    async def fake_pr_writer(**kwargs):
        return "Agent title", "Body content"

    async def fake_create_pr(repo, session, base, title, body):
        return True, "https://github.com/test/test/pull/1"

    with (
        patch("workbench.cli.run_pr_writer", side_effect=fake_pr_writer),
        patch("workbench.cli.create_pr", side_effect=fake_create_pr) as mock_create,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["pull-request", "myfeature", "--no-tmux", "--pr-title", "CLI title"],
        )

    assert result.exit_code == 0, result.output
    assert mock_create.call_args.args[3] == "CLI title"


def test_pull_request_writer_error_falls_back(git_repo, tmp_path):
    """Writer raises PrWriterAgentError → create_pr still called with fallback body."""
    from workbench.pr_writer import PrWriterAgentError

    plan = _make_plan_in_dir(tmp_path, "myfeature")
    _write_status_file(
        git_repo,
        "myfeature",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    async def fake_pr_writer(**kwargs):
        raise PrWriterAgentError("agent crashed")

    async def fake_create_pr(*args, **kwargs):
        return True, "https://github.com/test/test/pull/5"

    with (
        patch("workbench.cli.run_pr_writer", side_effect=fake_pr_writer),
        patch("workbench.cli.create_pr", side_effect=fake_create_pr) as mock_create,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(main, ["pull-request", "myfeature", "--no-tmux"])

    assert result.exit_code == 0, result.output
    assert mock_create.called
    assert "PR writer failed" in result.output or "fallback" in result.output.lower()


def test_pull_request_pr_base_override(git_repo, tmp_path):
    """--pr-base develop → create_pr called with base='develop'."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    _write_status_file(
        git_repo,
        "myfeature",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    runner = CliRunner()

    async def fake_pr_writer(**kwargs):
        return "Agent title", "Body content"

    async def fake_create_pr(repo, session, base, title, body):
        return True, "https://github.com/test/test/pull/6"

    with (
        patch("workbench.cli.run_pr_writer", side_effect=fake_pr_writer),
        patch("workbench.cli.create_pr", side_effect=fake_create_pr) as mock_create,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main, ["pull-request", "myfeature", "--no-tmux", "--pr-base", "develop"]
        )

    assert result.exit_code == 0, result.output
    assert mock_create.call_args.args[2] == "develop"


def test_pull_request_threads_per_plan_agents_yaml(git_repo, tmp_path):
    """A `.workbench/<plan>/agents.yaml` is forwarded to run_pr_writer."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "agents.yaml").write_text("agents: {}\n")
    _write_status_file(
        git_repo,
        "myfeature",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    captured: dict = {}

    async def fake_pr_writer(**kwargs):
        captured.update(kwargs)
        return "Agent title", "Body content from agent"

    async def fake_create_pr(*args, **kwargs):
        return True, "https://github.com/test/test/pull/1"

    runner = CliRunner()
    with (
        patch("workbench.cli.run_pr_writer", side_effect=fake_pr_writer),
        patch("workbench.cli.create_pr", side_effect=fake_create_pr),
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(main, ["pull-request", "myfeature", "--no-tmux"])

    assert result.exit_code == 0, result.output
    paths = captured.get("agents_config_paths") or []
    assert (plan_dir / "agents.yaml") in paths
    assert paths[0] == plan_dir / "agents.yaml"


def test_pull_request_threads_per_plan_profile(git_repo, tmp_path):
    """A `.workbench/<plan>/profile.yaml` directive reaches run_pr_writer."""
    plan = _make_plan_in_dir(tmp_path, "myfeature")
    plan_dir = git_repo / ".workbench" / "myfeature"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "profile.yaml").write_text("roles:\n  pr_writer:\n    directive: per-plan-pr\n")
    _write_status_file(
        git_repo,
        "myfeature",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    captured: dict = {}

    async def fake_pr_writer(**kwargs):
        captured.update(kwargs)
        return "Agent title", "Body content from agent"

    async def fake_create_pr(*args, **kwargs):
        return True, "https://github.com/test/test/pull/1"

    runner = CliRunner()
    with (
        patch("workbench.cli.run_pr_writer", side_effect=fake_pr_writer),
        patch("workbench.cli.create_pr", side_effect=fake_create_pr),
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(main, ["pull-request", "myfeature", "--no-tmux"])

    assert result.exit_code == 0, result.output
    profile_obj = captured.get("profile")
    assert profile_obj is not None
    assert profile_obj.pr_writer.directive == "per-plan-pr"


# ---------------------------------------------------------------------------
# --pr-writer-directive threading
# ---------------------------------------------------------------------------


def test_run_final_review_passes_pr_writer_directive(git_repo, tmp_path):
    """wb run --final-review --pr-writer-directive should propagate to run_final_review."""
    from workbench.session_status import FinalReviewRecord

    plan = _make_plan_in_dir(tmp_path, "my-plan")
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    mock_record = FinalReviewRecord(
        timestamp="2026-05-11T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_run_plan(**kwargs):
        return []

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            [
                "run",
                str(plan),
                "--no-tmux",
                "--final-review",
                "-b",
                "workbench-1",
                "--pr-writer-directive",
                "Custom",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.call_args.kwargs.get("pr_writer_directive") == "Custom"


def test_merge_review_passes_pr_writer_directive(git_repo, tmp_path):
    """wb merge --review --pr-writer-directive should propagate to run_final_review."""
    from workbench.session_status import FinalReviewRecord, SessionStatus

    plan = _make_plan_in_dir(tmp_path, "my-plan")
    status = SessionStatus(
        plan_slug="my-plan",
        session_branch="workbench-1",
        plan_source=str(plan),
    )
    status.record_task("task-1", "done", "wb/feat", merged=True, last_agent="reviewer")

    mock_record = FinalReviewRecord(
        timestamp="2026-05-11T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_merge(**kwargs):
        return status

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch("workbench.cli.merge_unmerged", side_effect=fake_merge),
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            [
                "merge",
                "-b",
                "workbench-1",
                "--review",
                "--no-tmux",
                "--pr-writer-directive",
                "MergeCustom",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.call_args.kwargs.get("pr_writer_directive") == "MergeCustom"


def test_final_review_command_passes_pr_writer_directive(git_repo, tmp_path):
    """wb final-review <session> --pr-writer-directive should propagate."""
    from workbench.session_status import FinalReviewRecord

    plan = _make_plan_in_dir(tmp_path, "my-plan")
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    mock_record = FinalReviewRecord(
        timestamp="2026-05-11T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )

    runner = CliRunner()

    async def fake_final_review(**kwargs):
        return mock_record

    with (
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            [
                "final-review",
                "workbench-1",
                "--no-tmux",
                "--pr-writer-directive",
                "FinalCustom",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.call_args.kwargs.get("pr_writer_directive") == "FinalCustom"


# ---------------------------------------------------------------------------
# Ephemeral worktree enumeration and cleanup
# ---------------------------------------------------------------------------


def test_list_ephemeral_worktrees_finds_review_wt(tmp_path):
    from workbench.cli import _list_ephemeral_worktrees

    wt = tmp_path / ".workbench" / "my-plan" / "wrap-up" / "feature-1" / ".review-wt"
    wt.mkdir(parents=True)

    paths = _list_ephemeral_worktrees(tmp_path)
    assert wt in paths


def test_list_ephemeral_worktrees_finds_pr_writer_wt(tmp_path):
    from workbench.cli import _list_ephemeral_worktrees

    wt = tmp_path / ".workbench" / "my-plan" / "wrap-up" / "feature-1" / ".pr-writer-wt"
    wt.mkdir(parents=True)

    paths = _list_ephemeral_worktrees(tmp_path)
    assert wt in paths


def test_list_ephemeral_worktrees_finds_legacy_review_wt(tmp_path):
    """Pre-wrap-up layout: .workbench/<plan>/.review-wt/<session>/ still cleaned up."""
    from workbench.cli import _list_ephemeral_worktrees

    wt = tmp_path / ".workbench" / "my-plan" / ".review-wt" / "feature-1"
    wt.mkdir(parents=True)

    paths = _list_ephemeral_worktrees(tmp_path)
    assert wt in paths


def test_list_ephemeral_worktrees_finds_legacy_pr_writer_wt(tmp_path):
    """Pre-wrap-up layout: .workbench/<plan>/.pr-writer-wt/<session>/ still cleaned up."""
    from workbench.cli import _list_ephemeral_worktrees

    wt = tmp_path / ".workbench" / "my-plan" / ".pr-writer-wt" / "feature-1"
    wt.mkdir(parents=True)

    paths = _list_ephemeral_worktrees(tmp_path)
    assert wt in paths


def test_list_ephemeral_worktrees_finds_merge_dir(tmp_path):
    from workbench.cli import _list_ephemeral_worktrees

    merge = tmp_path / ".workbench" / "_merge"
    merge.mkdir(parents=True)

    paths = _list_ephemeral_worktrees(tmp_path)
    assert merge in paths


def test_list_ephemeral_worktrees_returns_empty_when_none(tmp_path):
    from workbench.cli import _list_ephemeral_worktrees

    task = tmp_path / ".workbench" / "my-plan" / "task-1"
    task.mkdir(parents=True)

    paths = _list_ephemeral_worktrees(tmp_path)
    assert paths == []


def test_list_ephemeral_worktrees_sorted_deterministic(tmp_path):
    from workbench.cli import _list_ephemeral_worktrees

    base = tmp_path / ".workbench" / "my-plan" / "wrap-up"
    for name in ("zebra", "alpha", "mango"):
        (base / name / ".review-wt").mkdir(parents=True)

    paths = _list_ephemeral_worktrees(tmp_path)
    assert [p.parent.name for p in paths] == ["alpha", "mango", "zebra"]
    assert all(p.name == ".review-wt" for p in paths)


def test_clean_removes_ephemeral_worktrees(git_repo):
    from unittest.mock import MagicMock

    review_wt = git_repo / ".workbench" / "my-plan" / "wrap-up" / "feature" / ".review-wt"
    review_wt.mkdir(parents=True)
    merge_wt = git_repo / ".workbench" / "_merge"
    merge_wt.mkdir(parents=True)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, list)
            and len(cmd) >= 3
            and cmd[0] == "git"
            and cmd[1] == "worktree"
            and cmd[2] == "remove"
        ):
            return MagicMock(returncode=1, stderr="mock failure", stdout="")
        return real_run(cmd, *args, **kwargs)

    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.subprocess.run", side_effect=fake_run),
    ):
        result = runner.invoke(main, ["clean"])

    assert result.exit_code == 0, result.output
    assert not review_wt.exists()
    assert not merge_wt.exists()


def test_clean_dry_run_lists_but_does_not_remove_ephemeral(git_repo):
    review_wt = git_repo / ".workbench" / "my-plan" / "wrap-up" / "feature" / ".review-wt"
    review_wt.mkdir(parents=True)
    merge_wt = git_repo / ".workbench" / "_merge"
    merge_wt.mkdir(parents=True)

    runner = CliRunner()
    with patch("workbench.cli._find_repo_root", return_value=git_repo):
        result = runner.invoke(main, ["clean", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert review_wt.exists()
    assert merge_wt.exists()
    assert "feature/.review-wt" in result.output
    assert "_merge" in result.output


def test_clean_reports_per_path_failure_without_aborting(git_repo):
    import os
    import stat
    from unittest.mock import MagicMock

    wt_bad = git_repo / ".workbench" / "plan-a" / ".review-wt" / "bad"
    wt_bad.mkdir(parents=True)
    wt_good = git_repo / ".workbench" / "plan-b" / ".review-wt" / "good"
    wt_good.mkdir(parents=True)

    parent_of_bad = wt_bad.parent
    original_mode = parent_of_bad.stat().st_mode
    os.chmod(parent_of_bad, stat.S_IRUSR | stat.S_IXUSR)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, list)
            and len(cmd) >= 3
            and cmd[0] == "git"
            and cmd[1] == "worktree"
            and cmd[2] == "remove"
        ):
            return MagicMock(returncode=1, stderr="mock failure", stdout="")
        return real_run(cmd, *args, **kwargs)

    runner = CliRunner()
    try:
        with (
            patch("workbench.cli._find_repo_root", return_value=git_repo),
            patch("workbench.cli.subprocess.run", side_effect=fake_run),
        ):
            result = runner.invoke(main, ["clean"])
    finally:
        os.chmod(parent_of_bad, original_mode)

    assert result.exit_code == 0, result.output
    assert "✓" in result.output
    assert "✗" in result.output
    assert not wt_good.exists()
    assert wt_bad.exists()


class TestConventionsCli:
    def test_init_writes_template(self, git_repo):
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["conventions", "init"])
        assert result.exit_code == 0, result.output
        path = git_repo / ".workbench" / "conventions.md"
        assert path.exists()
        assert "# Project conventions" in path.read_text()

    def test_init_refuses_if_file_exists(self, git_repo):
        (git_repo / ".workbench").mkdir(exist_ok=True)
        (git_repo / ".workbench" / "conventions.md").write_text("existing\n")
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["conventions", "init"])
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_show_prints_file_contents(self, git_repo):
        (git_repo / ".workbench").mkdir(exist_ok=True)
        (git_repo / ".workbench" / "conventions.md").write_text("# Hi\n- rule\n")
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["conventions", "show"])
        assert result.exit_code == 0
        assert "# Hi" in result.output
        assert "- rule" in result.output

    def test_show_errors_when_missing(self, git_repo):
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["conventions", "show"])
        assert result.exit_code != 0
        assert "does not exist" in result.output

    def test_delete_with_yes_removes_file(self, git_repo):
        (git_repo / ".workbench").mkdir(exist_ok=True)
        path = git_repo / ".workbench" / "conventions.md"
        path.write_text("x\n")
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["conventions", "delete", "--yes"])
        assert result.exit_code == 0
        assert not path.exists()

    def test_delete_prompts_without_yes_and_aborts_on_no(self, git_repo):
        (git_repo / ".workbench").mkdir(exist_ok=True)
        path = git_repo / ".workbench" / "conventions.md"
        path.write_text("x\n")
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["conventions", "delete"], input="n\n")
        assert result.exit_code == 0
        assert path.exists()
        assert "Aborted" in result.output

    def test_delete_noop_when_missing(self, git_repo):
        runner = CliRunner()
        with patch("workbench.cli._find_repo_root", return_value=git_repo):
            result = runner.invoke(main, ["conventions", "delete", "--yes"])
        assert result.exit_code == 0
        assert "does not exist" in result.output

    def test_edit_creates_then_opens(self, git_repo, monkeypatch):
        called: list[list[str]] = []
        monkeypatch.setenv("EDITOR", "true")

        def fake_call(cmd):
            called.append(cmd)
            return 0

        with (
            patch("workbench.cli._find_repo_root", return_value=git_repo),
            patch("workbench.cli.subprocess.call", side_effect=fake_call),
        ):
            result = CliRunner().invoke(main, ["conventions", "edit"])
        assert result.exit_code == 0
        path = git_repo / ".workbench" / "conventions.md"
        assert path.exists()
        assert called and called[0][0] == "true" and str(path) in called[0]


class TestPlanGenerateLoadsConventions:
    def test_plan_generate_passes_conventions_text_to_run_planner(self, git_repo, tmp_path):
        """When .workbench/conventions.md exists, wb plan generate forwards its content."""
        (git_repo / ".workbench").mkdir(exist_ok=True)
        (git_repo / ".workbench" / "conventions.md").write_text("- conv rule Z\n")

        captured: dict = {}

        async def fake_run_planner(**kwargs):
            captured.update(kwargs)
            from workbench.agents import TaskStatus

            class R:
                status = TaskStatus.DONE
                output = ""
                error = ""
                cost_usd = 0.0

            return R()

        runner = CliRunner()
        with (
            patch("workbench.cli._find_repo_root", return_value=git_repo),
            patch("workbench.cli.check_tmux_available", return_value=True),
            patch("workbench.agents.run_planner", side_effect=fake_run_planner),
        ):
            result = runner.invoke(main, ["plan", "generate", "test prompt", "--name", "p"])

        assert result.exit_code == 0, result.output
        assert "- conv rule Z" in captured.get("conventions_text", "")


# ---------------------------------------------------------------------------
# --max-retries threading into run_final_review
# ---------------------------------------------------------------------------


def _final_review_mock_record():
    from workbench.session_status import FinalReviewRecord

    return FinalReviewRecord(
        timestamp="2026-05-11T00:00:00Z",
        verdict="pass",
        review_path=".workbench/my-plan/wrap-up/workbench-1/review.md",
        requirements_path=".workbench/my-plan/wrap-up/workbench-1/requirements.md",
        summarizer_agent="claude",
        reviewer_agent="claude",
    )


def test_run_final_review_forwards_max_retries(git_repo, tmp_path):
    """wb run --final-review --max-retries 5 should forward max_retries=5."""
    plan = _make_plan_in_dir(tmp_path, "my-plan")
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    record = _final_review_mock_record()

    async def fake_run_plan(**kwargs):
        return []

    async def fake_final_review(**kwargs):
        return record

    runner = CliRunner()
    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            [
                "run",
                str(plan),
                "--no-tmux",
                "--final-review",
                "-b",
                "workbench-1",
                "--max-retries",
                "5",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.call_args.kwargs.get("max_retries") == 5


def test_run_final_review_default_max_retries(git_repo, tmp_path):
    """wb run --final-review without --max-retries should forward the default (2)."""
    plan = _make_plan_in_dir(tmp_path, "my-plan")
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    record = _final_review_mock_record()

    async def fake_run_plan(**kwargs):
        return []

    async def fake_final_review(**kwargs):
        return record

    runner = CliRunner()
    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["run", str(plan), "--no-tmux", "--final-review", "-b", "workbench-1"],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.call_args.kwargs.get("max_retries") == 2


def test_merge_review_forwards_max_retries(git_repo, tmp_path):
    """wb merge --review --max-retries 4 should forward max_retries=4."""
    from workbench.session_status import SessionStatus

    plan = _make_plan_in_dir(tmp_path, "my-plan")
    status = SessionStatus(
        plan_slug="my-plan",
        session_branch="workbench-1",
        plan_source=str(plan),
    )
    status.record_task("task-1", "done", "wb/feat", merged=True, last_agent="reviewer")

    record = _final_review_mock_record()

    async def fake_merge(**kwargs):
        return status

    async def fake_final_review(**kwargs):
        return record

    runner = CliRunner()
    with (
        patch("workbench.cli.merge_unmerged", side_effect=fake_merge),
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            [
                "merge",
                "-b",
                "workbench-1",
                "--review",
                "--no-tmux",
                "--max-retries",
                "4",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.call_args.kwargs.get("max_retries") == 4


def test_final_review_command_forwards_max_retries(git_repo, tmp_path):
    """wb final-review --max-retries 7 should forward max_retries=7."""
    plan = _make_plan_in_dir(tmp_path, "my-plan")
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    record = _final_review_mock_record()

    async def fake_final_review(**kwargs):
        return record

    runner = CliRunner()
    with (
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            [
                "final-review",
                "workbench-1",
                "--no-tmux",
                "--max-retries",
                "7",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.call_args.kwargs.get("max_retries") == 7


# ---------------------------------------------------------------------------
# wb run trace metadata flags
# ---------------------------------------------------------------------------


def _invoke_run_with_trace_flags(git_repo, tmp_path, extra_flags: list[str]):
    """Invoke `wb run` with the given extra flags and capture run_plan kwargs."""
    plan = tmp_path / "trace-plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    runner = CliRunner()
    with (
        patch("workbench.cli.run_plan") as mock_run_plan,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.asyncio") as mock_asyncio,
    ):
        mock_asyncio.run = lambda coro: None
        result = runner.invoke(main, ["run", str(plan), "--no-tmux", *extra_flags])

    return result, mock_run_plan


def test_run_trace_prompt_forwards_true(git_repo, tmp_path):
    """--trace-prompt should forward trace_prompt=True (trace_env=True default) to run_plan."""
    result, mock_run_plan = _invoke_run_with_trace_flags(git_repo, tmp_path, ["--trace-prompt"])
    assert "no such option" not in (result.output or "").lower()
    assert mock_run_plan.called
    kwargs = mock_run_plan.call_args.kwargs
    assert kwargs.get("trace_prompt") is True
    assert kwargs.get("trace_env") is True


def test_run_no_trace_disables_both_even_with_trace_prompt(git_repo, tmp_path):
    """--no-trace overrides --trace-prompt: both effective values are False."""
    result, mock_run_plan = _invoke_run_with_trace_flags(
        git_repo, tmp_path, ["--no-trace", "--trace-prompt"]
    )
    assert "no such option" not in (result.output or "").lower()
    assert mock_run_plan.called
    kwargs = mock_run_plan.call_args.kwargs
    assert kwargs.get("trace_env") is False
    assert kwargs.get("trace_prompt") is False


def test_run_no_trace_env_disables_env_only(git_repo, tmp_path):
    """--no-trace-env disables env injection but leaves trace_prompt at the default (False)."""
    result, mock_run_plan = _invoke_run_with_trace_flags(git_repo, tmp_path, ["--no-trace-env"])
    assert "no such option" not in (result.output or "").lower()
    assert mock_run_plan.called
    kwargs = mock_run_plan.call_args.kwargs
    assert kwargs.get("trace_env") is False
    assert kwargs.get("trace_prompt") is False


def test_run_bare_defaults_trace_env_on_trace_prompt_off(git_repo, tmp_path):
    """A bare `run` (no trace flags) forwards trace_env=True, trace_prompt=False."""
    result, mock_run_plan = _invoke_run_with_trace_flags(git_repo, tmp_path, [])
    assert mock_run_plan.called
    kwargs = mock_run_plan.call_args.kwargs
    assert kwargs.get("trace_env") is True
    assert kwargs.get("trace_prompt") is False


def test_final_review_command_default_max_retries(git_repo, tmp_path):
    """wb final-review without --max-retries should forward the default (2)."""
    plan = _make_plan_in_dir(tmp_path, "my-plan")
    _write_status_file(
        git_repo,
        "my-plan",
        "workbench-1",
        {
            "task-1": {
                "status": "done",
                "branch": "wb/feat",
                "merged": True,
                "last_agent": "reviewer",
            }
        },
        plan_source=str(plan),
    )

    record = _final_review_mock_record()

    async def fake_final_review(**kwargs):
        return record

    runner = CliRunner()
    with (
        patch(
            "workbench.cli.run_final_review", side_effect=fake_final_review
        ) as mock_final_review,
        patch("workbench.cli._find_repo_root", return_value=git_repo),
    ):
        result = runner.invoke(
            main,
            ["final-review", "workbench-1", "--no-tmux"],
        )

    assert result.exit_code == 0, result.output
    assert mock_final_review.call_args.kwargs.get("max_retries") == 2


# ---------------------------------------------------------------------------
# --model flag plumbing
# ---------------------------------------------------------------------------


def test_parse_model_overrides_empty():
    from workbench.cli import _parse_model_overrides

    assert _parse_model_overrides(()) == {}


def test_parse_model_overrides_bare_value():
    from workbench.cli import _parse_model_overrides

    assert _parse_model_overrides(("claude-opus-4-8",)) == {"": "claude-opus-4-8"}


def test_parse_model_overrides_scoped():
    from workbench.cli import _parse_model_overrides

    out = _parse_model_overrides(("claude=claude-opus-4-8", "codex=gpt-5-codex"))
    assert out == {"claude": "claude-opus-4-8", "codex": "gpt-5-codex"}


def test_parse_model_overrides_strips_whitespace():
    from workbench.cli import _parse_model_overrides

    out = _parse_model_overrides(("  claude  =  claude-opus-4-8  ",))
    assert out == {"claude": "claude-opus-4-8"}


def test_parse_model_overrides_last_wins():
    from workbench.cli import _parse_model_overrides

    out = _parse_model_overrides(("claude=a", "claude=b"))
    assert out == {"claude": "b"}

    out = _parse_model_overrides(("x", "y"))
    assert out == {"": "y"}


def test_parse_model_overrides_mixed():
    from workbench.cli import _parse_model_overrides

    out = _parse_model_overrides(("shared-model", "codex=gpt-5-codex"))
    assert out == {"": "shared-model", "codex": "gpt-5-codex"}


def test_run_forwards_cli_models_to_run_plan(git_repo, tmp_path):
    """wb run --model forwards a correct cli_models dict to run_plan."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    captured: dict = {}

    async def fake_run_plan(**kwargs):
        captured.update(kwargs)
        return []

    runner = CliRunner()
    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
    ):
        result = runner.invoke(
            main,
            [
                "run",
                str(plan),
                "--no-tmux",
                "--model",
                "claude=claude-opus-4-8",
                "--model",
                "codex=gpt-5-codex",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured.get("cli_models") == {
        "claude": "claude-opus-4-8",
        "codex": "gpt-5-codex",
    }


def test_run_without_model_forwards_empty_cli_models(git_repo, tmp_path):
    """wb run without --model still forwards an empty dict, preserving behavior."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    captured: dict = {}

    async def fake_run_plan(**kwargs):
        captured.update(kwargs)
        return []

    runner = CliRunner()
    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
    ):
        result = runner.invoke(main, ["run", str(plan), "--no-tmux"])

    assert result.exit_code == 0, result.output
    assert captured.get("cli_models") == {}


def test_run_bare_model_applies_to_all_agents(git_repo, tmp_path):
    """A bare --model value uses the "" key meaning "all agents"."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Task: hello\nDo something\n")

    captured: dict = {}

    async def fake_run_plan(**kwargs):
        captured.update(kwargs)
        return []

    runner = CliRunner()
    with (
        patch("workbench.cli.run_plan", side_effect=fake_run_plan),
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
    ):
        result = runner.invoke(
            main,
            ["run", str(plan), "--no-tmux", "--model", "claude-opus-4-8"],
        )

    assert result.exit_code == 0, result.output
    assert captured.get("cli_models") == {"": "claude-opus-4-8"}


def test_plan_forwards_resolved_model_to_run_planner(git_repo):
    """wb plan --model resolves the planner model and forwards it to run_planner."""
    from workbench.agents import AgentResult, Role, TaskStatus

    fake_result = AgentResult(
        task_id="planner-myplan",
        role=Role.IMPLEMENTOR,
        status=TaskStatus.DONE,
        output="ok",
    )

    captured: dict = {}

    async def fake_run_planner(**kwargs):
        captured.update(kwargs)
        plan_name = kwargs["plan_name"]
        output_path = git_repo / ".workbench" / plan_name / "plan.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# Generated Plan\n## Task: Foo\nDo it.\n")
        return fake_result

    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
        patch("workbench.agents.run_planner", side_effect=fake_run_planner),
    ):
        result = runner.invoke(
            main,
            [
                "plan",
                "Add JWT auth",
                "--name",
                "myplan",
                "--no-tmux",
                "--agent",
                "claude",
                "--model",
                "claude=claude-opus-4-8",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured.get("model") == "claude-opus-4-8"


def test_plan_without_model_forwards_none(git_repo):
    """wb plan without --model forwards model=None to run_planner."""
    from workbench.agents import AgentResult, Role, TaskStatus

    fake_result = AgentResult(
        task_id="planner-x",
        role=Role.IMPLEMENTOR,
        status=TaskStatus.DONE,
        output="ok",
    )

    captured: dict = {}

    async def fake_run_planner(**kwargs):
        captured.update(kwargs)
        plan_name = kwargs["plan_name"]
        output_path = git_repo / ".workbench" / plan_name / "plan.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# Generated Plan\n## Task: Foo\nDo it.\n")
        return fake_result

    runner = CliRunner()
    with (
        patch("workbench.cli._find_repo_root", return_value=git_repo),
        patch("workbench.cli.check_tmux_available", return_value=True),
        patch("workbench.agents.run_planner", side_effect=fake_run_planner),
    ):
        result = runner.invoke(
            main,
            ["plan", "Add JWT auth", "--name", "x", "--no-tmux"],
        )

    assert result.exit_code == 0, result.output
    assert captured.get("model") is None
