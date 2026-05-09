"""CLI entrypoint for workbench."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import click
import yaml
from rich.console import Console

from .directives import (
    FixerDirective,
    ImplementorDirective,
    MergerDirective,
    PlannerDirective,
    ReviewerDirective,
    ReviewerFollowupDirective,
    TddImplementorDirective,
    TddTesterDirective,
    TesterDirective,
)
from .orchestrator import merge_unmerged, run_plan
from .plan_parser import parse_plan
from .profile import ModeConfig, Profile, RoleConfig
from .tmux import check_tmux_available

# Maps role name to the Directive class whose DEFAULT_TEXT seeds the role.
_ROLE_DIRECTIVE_CLASSES = {
    "implementor": ImplementorDirective,
    "tester": TesterDirective,
    "reviewer": ReviewerDirective,
    "fixer": FixerDirective,
    "merger": MergerDirective,
    "planner": PlannerDirective,
}

# Maps (role, sub_mode) to the Directive class whose DEFAULT_TEXT seeds the sub-mode.
_SUBMODE_DIRECTIVE_CLASSES = {
    ("implementor", "tdd"): TddImplementorDirective,
    ("tester", "tdd"): TddTesterDirective,
    ("reviewer", "followup"): ReviewerFollowupDirective,
}


def _default_directive_text(role: str, sub_mode: str | None = None) -> str:
    """Return the built-in DEFAULT_TEXT for a role (or role.sub_mode)."""
    if sub_mode is not None:
        cls = _SUBMODE_DIRECTIVE_CLASSES.get((role, sub_mode))
    else:
        cls = _ROLE_DIRECTIVE_CLASSES.get(role)
    return cls.DEFAULT_TEXT if cls else ""


console = Console()

# ---------------------------------------------------------------------------
# Frontmatter → CLI parameter merging
# ---------------------------------------------------------------------------

_FRONTMATTER_TO_PARAM: dict[str, str] = {
    "session_branch": "session_branch",
    "name": "session_name",
    "base": "base",
    "local": "local",
    "agent": "agent",
    "profile": "profile_path",
    "profile_name": "profile_name",
    "max_concurrent": "max_concurrent",
    "max_retries": "max_retries",
    "tdd": "tdd",
    "skip_test": "skip_test",
    "skip_review": "skip_review",
    "retry_failed": "retry_failed",
    "fail_fast": "fail_fast",
    "cleanup": "cleanup",
    "keep_branches": "keep_branches",
    "push": "push",
}

# Click's --profile option converts the string to a Path via
# type=click.Path(path_type=Path); frontmatter values bypass that, so we
# coerce here to keep downstream code (Profile.resolve calls .exists()) happy.
_FRONTMATTER_PARAM_COERCIONS: dict[str, type] = {
    "profile_path": Path,
}


def _apply_plan_run_config(
    ctx: click.Context,
    plan_run_config: dict[str, object],
    flag_values: dict[str, object],
) -> dict[str, object]:
    """For each plan-shaped flag, fall back to the frontmatter value when
    the user did not pass an explicit CLI flag.  CLI explicit > frontmatter
    > built-in default.  The returned dict has only the keys passed in
    *flag_values*; other frontmatter keys are silently ignored at this layer
    (the parser already validated the schema).
    """
    out = dict(flag_values)
    for name in flag_values:
        if name not in plan_run_config:
            continue
        source = ctx.get_parameter_source(name)
        if source is None or source == click.core.ParameterSource.DEFAULT:
            value = plan_run_config[name]
            coerce = _FRONTMATTER_PARAM_COERCIONS.get(name)
            out[name] = coerce(value) if coerce else value
    return out


def _find_repo_root(start: Path = None) -> Path:
    """Find the git repo root from the current directory."""
    start = start or Path.cwd()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException("Not in a git repository.")
    return Path(result.stdout.strip())


def _ensure_workbench_dir(repo: Path) -> Path:
    """Ensure .workbench/ exists in the repo root. Returns the path."""
    wb_dir = repo / ".workbench"
    wb_dir.mkdir(exist_ok=True)
    return wb_dir


def _get_skills_dir() -> Path:
    """Return the path to the bundled skills directory."""
    return Path(resources.files("workbench.skills"))


def _discover_skills(skills_dir: Path) -> list[tuple[str, Path]]:
    """Return list of (skill_name, skill_md_path) for all bundled skills."""
    skills = []
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").exists():
            skills.append((entry.name, entry / "SKILL.md"))
    return skills


def _detect_agent() -> str:
    """Auto-detect the agent platform from PATH."""
    found = []
    for name in ("claude", "gemini", "codex", "cursor", "copilot"):
        if shutil.which(name):
            found.append(name)

    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        return click.prompt(
            "Multiple agent platforms found. Choose one",
            type=click.Choice(found),
        )
    return "manual"


_PLATFORM_LABEL = {
    "claude": "command",
    "gemini": "skill",
    "cursor": "rule",
    "codex": "instruction",
    "copilot": "skill",
    "manual": "skill file",
}


def _install_to_agents_skills(skills: list[tuple[str, Path]], repo: Path, symlink: bool) -> None:
    """Install skills to <repo>/.agents/skills/ for cross-client discoverability."""
    target_dir = repo / ".agents" / "skills"
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, src in skills:
        src_dir = src.parent
        dest_dir = target_dir / name
        if symlink:
            if dest_dir.is_symlink():
                dest_dir.unlink()
            elif dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.symlink_to(src_dir.resolve())
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
    console.print(f"  Also installed to {target_dir} for cross-client discoverability.")


def _install_skills(
    agent: str | None,
    symlink: bool,
    local: bool = False,
    repo: Path | None = None,
    update: bool = False,
) -> None:
    """Install bundled skill files for the given agent platform."""
    agent = agent or _detect_agent()
    skills_dir = _get_skills_dir()
    skills = _discover_skills(skills_dir)
    label = _PLATFORM_LABEL.get(agent, "skill file")

    if local and repo is None:
        repo = _find_repo_root()

    if not skills:
        console.print("[yellow]No bundled skill files found.[/yellow]")
        return

    # Let user select which skills to install (skip for --update or non-interactive)
    ctx = click.get_current_context(silent=True)
    is_interactive = ctx is None or ctx.color is not False
    if not update and len(skills) > 1 and is_interactive:
        console.print(f"[bold]Available {label}(s) for {agent}:[/bold]")
        for name, src in skills:
            console.print(f"  • {name}")
        console.print()

        try:
            if not click.confirm("Install all skills?", default=True):
                selected = []
                for name, src in skills:
                    if click.confirm(f"  Install {name}?", default=True):
                        selected.append((name, src))
                skills = selected
                if not skills:
                    console.print("[yellow]No skills selected.[/yellow]")
                    return
        except (click.Abort, EOFError):
            pass  # non-interactive — install all

    console.print(f"\n[bold]Installing {len(skills)} {label}(s) for {agent}...[/bold]\n")

    if agent == "claude":
        if local:
            target_dir = repo / ".claude" / "skills"
        else:
            target_dir = Path.home() / ".claude" / "skills"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, src in skills:
            src_dir = src.parent
            dest_dir = target_dir / name
            dest = dest_dir / "SKILL.md"
            if dest.exists() and not symlink and not update:
                if dest.read_text() == src.read_text():
                    console.print(f"  [dim]Skipping /{name} (already up to date)[/dim]")
                    continue
                if not click.confirm(f"  Overwrite existing /{name}?", default=True):
                    console.print(f"  [yellow]Skipped /{name}[/yellow]")
                    continue
            if symlink:
                if dest_dir.is_symlink():
                    dest_dir.unlink()
                elif dest_dir.exists():
                    shutil.rmtree(dest_dir)
                dest_dir.symlink_to(src_dir.resolve())
                console.print(f"  Linked /{name} → {dest_dir}")
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
                console.print(f"  Copied /{name} → {dest_dir}")
        console.print(f"\n  Use in Claude Code: [bold]/{skills[0][0]}[/bold]")
        if local:
            _install_to_agents_skills(skills, repo, symlink)

    elif agent == "gemini":
        if local:
            target_dir = repo / ".agents" / "skills"
        else:
            target_dir = Path.home() / ".agents" / "skills"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, src in skills:
            src_dir = src.parent
            dest_dir = target_dir / name
            dest = dest_dir / "SKILL.md"
            if dest.exists() and not symlink and not update:
                if dest.read_text() == src.read_text():
                    console.print(f"  [dim]Skipping {name} (already up to date)[/dim]")
                    continue
                if not click.confirm(f"  Overwrite existing {name}?", default=True):
                    console.print(f"  [yellow]Skipped {name}[/yellow]")
                    continue
            if symlink:
                if dest_dir.is_symlink():
                    dest_dir.unlink()
                elif dest_dir.exists():
                    shutil.rmtree(dest_dir)
                dest_dir.symlink_to(src_dir.resolve())
                console.print(f"  Linked {name} → {dest_dir}")
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
                console.print(f"  Copied {name} → {dest_dir}")

    elif agent == "copilot":
        if local:
            target_dir = repo / ".github" / "skills"
        else:
            target_dir = Path.home() / ".copilot" / "skills"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, src in skills:
            src_dir = src.parent
            dest_dir = target_dir / name
            dest = dest_dir / "SKILL.md"
            if dest.exists() and not symlink and not update:
                if dest.read_text() == src.read_text():
                    console.print(f"  [dim]Skipping {name} (already up to date)[/dim]")
                    continue
                if not click.confirm(f"  Overwrite existing {name}?", default=True):
                    console.print(f"  [yellow]Skipped {name}[/yellow]")
                    continue
            if symlink:
                if dest_dir.is_symlink():
                    dest_dir.unlink()
                elif dest_dir.exists():
                    shutil.rmtree(dest_dir)
                dest_dir.symlink_to(src_dir.resolve())
                console.print(f"  Linked {name} → {dest_dir}")
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
                console.print(f"  Copied {name} → {dest_dir}")
        if local:
            _install_to_agents_skills(skills, repo, symlink)

    elif agent == "cursor":
        if local:
            console.print("  [dim]Note: cursor skills are always project-level.[/dim]")
        target_dir = Path.cwd() / ".cursor" / "rules"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, src in skills:
            dest = target_dir / f"{name}.md"
            if dest.exists() and not symlink and not update:
                if dest.read_text() == src.read_text():
                    console.print(f"  [dim]Skipping {name} (already up to date)[/dim]")
                    continue
                if not click.confirm(f"  Overwrite existing {name}?", default=True):
                    console.print(f"  [yellow]Skipped {name}[/yellow]")
                    continue
            if symlink:
                dest.unlink(missing_ok=True)
                dest.symlink_to(src.resolve())
                console.print(f"  Linked {name} → {dest}")
            else:
                dest.write_text(src.read_text())
                console.print(f"  Copied {name} → {dest}")
        if local:
            _install_to_agents_skills(skills, repo, symlink)

    elif agent == "codex":
        if local:
            console.print("  [dim]Note: codex skills are always project-level.[/dim]")
        if symlink:
            console.print(
                "  [yellow]Note: --symlink is not supported for codex (content is appended to a single file). Using copy.[/yellow]"
            )
        target_dir = Path.cwd() / ".codex"
        target_dir.mkdir(parents=True, exist_ok=True)
        instructions_path = target_dir / "instructions.md"
        existing = instructions_path.read_text() if instructions_path.exists() else ""

        marker_prefix = "<!-- workbench-skill:"
        for name, src in skills:
            marker = f"{marker_prefix}{name} -->"
            if marker in existing:
                console.print(f"  [yellow]Skipping {name} (already in instructions.md)[/yellow]")
                continue
            content = src.read_text()
            separator = "\n\n---\n\n" if existing.strip() else ""
            existing += f"{separator}{marker}\n{content}"
            console.print(f"  Appended {name} → {instructions_path}")

        instructions_path.write_text(existing)
        if local:
            _install_to_agents_skills(skills, repo, symlink)

    elif agent == "manual":
        console.print(f"  Skill files directory: {skills_dir}\n")
        for name, src in skills:
            console.print(f"  • {name}: {src}")

    console.print(f"\n[green]Done. Installed {len(skills)} {label}(s) for {agent}.[/green]")


@click.group()
@click.version_option(package_name="wbcli")
def main():
    """Workbench - lightweight multi-agent orchestrator.

    Point it at a plan, it dispatches parallel agents to implement, test, and review.
    """
    pass


@main.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
@click.option("--max-concurrent", "-j", default=4, help="Max parallel agents.")
@click.option("--skip-test", is_flag=True, help="Skip the testing phase.")
@click.option("--skip-review", is_flag=True, help="Skip the review phase.")
@click.option("--max-retries", "-r", default=2, help="Max fix attempts per failed stage.")
@click.option(
    "--agent",
    default="claude",
    help="Agent CLI command (claude, gemini, codex, cursor, copilot, or custom).",
)
@click.option("--cleanup", is_flag=True, help="Remove worktrees after completion.")
@click.option(
    "--keep-branches",
    is_flag=True,
    help="Keep task branches after merging (default: auto-delete on success).",
)
@click.option(
    "--repo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Repo path (default: auto-detect).",
)
@click.option(
    "--session-branch",
    "-b",
    default=None,
    help=(
        "Session branch name to use for this run. Created from --base if it "
        "doesn't exist; reused if it does. Alias for --name."
    ),
)
@click.option(
    "--wave",
    "-w",
    default=None,
    type=int,
    help="Run only this wave number (1-indexed). Without this, runs all waves.",
)
@click.option(
    "--start-wave",
    default=1,
    type=int,
    help="Start from this wave number (1-indexed, default: 1). Ignored if --wave is set.",
)
@click.option(
    "--end-wave",
    default=None,
    type=int,
    help="Stop after this wave number (1-indexed). Ignored if --wave is set.",
)
@click.option(
    "--no-tmux", is_flag=True, help="Run agents as raw subprocesses instead of tmux sessions."
)
@click.option(
    "--tdd", is_flag=True, help="Test-driven development mode: write tests first, then implement."
)
@click.option(
    "--local",
    is_flag=True,
    help="Branch from local ref instead of fetching origin. Use to build on local work.",
)
@click.option(
    "--base",
    default=None,
    type=str,
    help="Base branch to start from (default: main). Works with --local.",
)
@click.option(
    "--profile",
    "profile_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a profile.yaml to use.",
)
@click.option(
    "--profile-name",
    default=None,
    help="Named profile to use (resolves profile.<name>.yaml).",
)
@click.option(
    "--name",
    "session_name",
    default=None,
    help=(
        "Session branch name to use for this run. Created from --base if it "
        "doesn't exist; reused if it does. Alias for --session-branch (-b). "
        "Without either, the session is auto-numbered as workbench-<N>."
    ),
)
@click.option(
    "--retry-failed",
    is_flag=True,
    help="Automatically retry tasks that failed due to transient errors (not exhausted retries).",
)
@click.option(
    "--fail-fast",
    is_flag=True,
    help="Stop after the first wave that has any failed tasks.",
)
@click.option(
    "--only-incomplete",
    "only_incomplete",
    is_flag=True,
    help=(
        "Re-run every task in the session that is not yet done — both prior "
        "failures and tasks that never started (e.g. seeded as pending). Skips "
        "tasks already merged into the session branch. Requires --session-branch."
    ),
)
@click.option(
    "--task",
    "task_ids",
    multiple=True,
    help="Run only specific tasks (by ID or slug). Repeatable: --task task-1 --task task-2.",
)
@click.option(
    "--implementor-directive",
    default=None,
    type=str,
    help="Override the implementor agent's instructions.",
)
@click.option(
    "--tester-directive", default=None, type=str, help="Override the tester agent's instructions."
)
@click.option(
    "--reviewer-directive",
    default=None,
    type=str,
    help="Override the reviewer agent's instructions.",
)
@click.option(
    "--fixer-directive", default=None, type=str, help="Override the fixer agent's instructions."
)
@click.option(
    "--push",
    is_flag=True,
    help="Push the session branch to origin after merging (sets upstream tracking).",
)
def run(
    plan_path: Path,
    max_concurrent: int,
    skip_test: bool,
    skip_review: bool,
    max_retries: int,
    agent: str,
    cleanup: bool,
    keep_branches: bool,
    repo: Path | None,
    session_branch: str | None,
    wave: int | None,
    start_wave: int,
    end_wave: int | None,
    no_tmux: bool,
    tdd: bool,
    local: bool,
    base: str | None,
    profile_path: Path | None,
    profile_name: str | None,
    session_name: str | None,
    retry_failed: bool,
    fail_fast: bool,
    only_incomplete: bool,
    task_ids: tuple[str, ...],
    implementor_directive: str | None,
    tester_directive: str | None,
    reviewer_directive: str | None,
    fixer_directive: str | None,
    push: bool,
):
    """Run a plan with parallel agents.

    \b
    Example:
      wb run plan.md
      wb run plan.md -j 6 --skip-review
      wb run plan.md --agent gemini
      wb run plan.md --no-tmux
    """
    if not no_tmux and not check_tmux_available():
        raise click.ClickException(
            "tmux is required but not found on PATH. "
            "Install with: brew install tmux (macOS) or apt install tmux (Linux). "
            "Or use --no-tmux to run without it."
        )

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)

    try:
        plan = parse_plan(plan_path.resolve())
    except ValueError as e:
        raise click.ClickException(str(e))

    # Merge frontmatter run_config into effective flag values
    ctx = click.get_current_context()
    mapped_run_config = {_FRONTMATTER_TO_PARAM[k]: v for k, v in plan.run_config.items()}
    flag_values = {
        "session_branch": session_branch,
        "session_name": session_name,
        "base": base,
        "local": local,
        "agent": agent,
        "profile_path": profile_path,
        "profile_name": profile_name,
        "max_concurrent": max_concurrent,
        "max_retries": max_retries,
        "tdd": tdd,
        "skip_test": skip_test,
        "skip_review": skip_review,
        "retry_failed": retry_failed,
        "fail_fast": fail_fast,
        "cleanup": cleanup,
        "keep_branches": keep_branches,
        "push": push,
    }
    effective = _apply_plan_run_config(ctx, mapped_run_config, flag_values)
    session_branch = effective["session_branch"]
    session_name = effective["session_name"]
    base = effective["base"]
    local = effective["local"]
    agent = effective["agent"]
    profile_path = effective["profile_path"]
    profile_name = effective["profile_name"]
    max_concurrent = effective["max_concurrent"]
    max_retries = effective["max_retries"]
    tdd = effective["tdd"]
    skip_test = effective["skip_test"]
    skip_review = effective["skip_review"]
    retry_failed = effective["retry_failed"]
    fail_fast = effective["fail_fast"]
    cleanup = effective["cleanup"]
    keep_branches = effective["keep_branches"]
    push = effective["push"]

    if tdd and skip_test:
        raise click.ClickException("--tdd and --skip-test are mutually exclusive.")

    if only_incomplete and not session_branch:
        raise click.ClickException("--only-incomplete requires --session-branch (-b).")

    if not plan.tasks:
        raise click.ClickException("No tasks found in plan. Use '## Task: <title>' sections.")

    # Resolve and clamp wave range
    num_waves = len(plan.independent_groups)
    effective_start_wave = wave if wave is not None else start_wave
    effective_end_wave = wave if wave is not None else end_wave

    # Clamp start_wave to [1, num_waves]
    if effective_start_wave < 1 or effective_start_wave > num_waves:
        console.print(
            f"[yellow]--start-wave {effective_start_wave} out of range [1, {num_waves}], defaulting to 1[/yellow]"
        )
        effective_start_wave = 1

    # Clamp end_wave to [1, num_waves] and ensure >= start_wave
    if effective_end_wave is not None:
        if effective_end_wave < 1 or effective_end_wave > num_waves:
            console.print(
                f"[yellow]--end-wave {effective_end_wave} out of range [1, {num_waves}], defaulting to {num_waves}[/yellow]"
            )
            effective_end_wave = num_waves
        if effective_end_wave < effective_start_wave:
            console.print(
                f"[yellow]--end-wave {effective_end_wave} < --start-wave {effective_start_wave}, defaulting to {num_waves}[/yellow]"
            )
            effective_end_wave = num_waves

    from .agents import Role

    directives = {}
    if implementor_directive:
        directives[Role.IMPLEMENTOR] = implementor_directive
    if tester_directive:
        directives[Role.TESTER] = tester_directive
    if reviewer_directive:
        directives[Role.REVIEWER] = reviewer_directive
    if fixer_directive:
        directives[Role.FIXER] = fixer_directive

    console.print(f"\n[bold]Parsed {len(plan.tasks)} task(s) from[/bold] {plan_path}\n")
    for i, task in enumerate(plan.tasks, 1):
        files = f" ({', '.join(task.files)})" if task.files else ""
        deps = f" [after: {', '.join(task.depends_on)}]" if task.depends_on else ""
        console.print(f"  {i}. {task.title}{files}{deps}")

    console.print()
    asyncio.run(
        run_plan(
            plan=plan,
            repo=repo,
            max_concurrent=max_concurrent,
            skip_test=skip_test,
            skip_review=skip_review,
            max_retries=max_retries,
            agent_cmd=agent,
            cleanup_on_done=cleanup,
            session_branch=session_branch,
            start_wave=effective_start_wave,
            end_wave=effective_end_wave,
            use_tmux=not no_tmux,
            directives=directives or None,
            tdd=tdd,
            local=local,
            base_branch=base,
            profile_path=profile_path,
            profile_name=profile_name,
            session_name=session_name,
            keep_branches=keep_branches,
            retry_failed=retry_failed,
            fail_fast=fail_fast,
            only_incomplete=only_incomplete,
            task_filter=set(task_ids) if task_ids else None,
            push=push,
        )
    )


@main.command()
@click.argument("session_branch")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--no-tmux", is_flag=True, help="Run without tmux.")
@click.option("--agent", default="claude", show_default=True, help="Agent CLI to dispatch.")
@click.option(
    "--max-concurrent",
    "-j",
    type=int,
    default=4,
    show_default=True,
    help="Max parallel tasks per wave.",
)
@click.option(
    "--max-retries",
    type=int,
    default=2,
    show_default=True,
    help="Max fix attempts after a failed test or review.",
)
@click.option("--tdd", is_flag=True, help="Run pending tasks in TDD mode.")
@click.option(
    "--profile",
    "profile_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Explicit profile path.",
)
@click.option("--name", "profile_name", default=None, help="Named profile to resolve.")
@click.pass_context
def resume(
    ctx: click.Context,
    session_branch: str,
    repo: Path | None,
    no_tmux: bool,
    agent: str,
    max_concurrent: int,
    max_retries: int,
    tdd: bool,
    profile_path: Path | None,
    profile_name: str | None,
):
    """Resume a session by re-running every task that isn't done + merged.

    Looks up the session in .workbench/status-*.yaml, finds the plan via the
    recorded plan_source, and forwards to:

        wb run <plan> -b <session> --only-incomplete

    For finer-grained control (waves, directive overrides, selective tasks),
    use ``wb run`` directly.
    """
    from .session_status import SessionStatus

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)

    found = SessionStatus.find_by_session(repo, session_branch)
    if found is None:
        raise click.ClickException(
            f"No status file found for session '{session_branch}'. "
            f"Use 'wb run <plan> -b {session_branch} --only-incomplete' instead."
        )
    if not found.plan_source:
        raise click.ClickException(
            f"Session '{session_branch}' has no plan_source recorded. "
            f"Use 'wb run <plan> -b {session_branch} --only-incomplete' instead."
        )

    plan_path = Path(found.plan_source)
    if not plan_path.exists():
        raise click.ClickException(
            f"Plan source not found: {plan_path}\n"
            f"Locate the plan and use 'wb run <plan> -b {session_branch} --only-incomplete'."
        )

    console.print(f"[dim]Resuming session {session_branch} from {plan_path}[/dim]\n")

    ctx.invoke(
        run,
        plan_path=plan_path,
        max_concurrent=max_concurrent,
        skip_test=False,
        skip_review=False,
        max_retries=max_retries,
        agent=agent,
        cleanup=False,
        keep_branches=False,
        repo=repo,
        session_branch=session_branch,
        wave=None,
        start_wave=1,
        end_wave=None,
        no_tmux=no_tmux,
        tdd=tdd,
        local=False,
        base=None,
        profile_path=profile_path,
        profile_name=profile_name,
        session_name=None,
        retry_failed=False,
        fail_fast=False,
        only_incomplete=True,
        task_ids=(),
        implementor_directive=None,
        tester_directive=None,
        reviewer_directive=None,
        fixer_directive=None,
        push=False,
    )


@main.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
def preview(plan_path: Path):
    """Preview tasks parsed from a plan (dry run)."""
    try:
        plan = parse_plan(plan_path.resolve())
    except ValueError as e:
        raise click.ClickException(str(e))

    if not plan.tasks:
        raise click.ClickException("No tasks found in plan.")

    console.print(f"\n[bold]{plan.title}[/bold]")
    console.print(f"Source: {plan_path}\n")

    waves = plan.independent_groups
    for wave_idx, wave in enumerate(waves):
        console.print(
            f"[bold cyan]Wave {wave_idx + 1}[/bold cyan] ({len(wave)} tasks, run in parallel)"
        )
        for task in wave:
            console.print(f"  • [bold]{task.title}[/bold]")
            if task.files:
                console.print(f"    Files: {', '.join(task.files)}")
            if task.depends_on:
                console.print(f"    After: {', '.join(task.depends_on)}")
        console.print()


@main.command()
@click.argument("prompt", type=str, required=False, default="")
@click.option(
    "--from",
    "from_file",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Transform an existing document into workbench plan format.",
)
@click.option(
    "--name",
    "-n",
    default="plan",
    help="Name for the plan file (default: plan). Produces .workbench/plans/<name>.md.",
)
@click.option(
    "--agent",
    default="claude",
    help="Agent CLI command (claude, gemini, codex, cursor, copilot, or custom).",
)
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--no-tmux", is_flag=True, help="Run without tmux.")
def plan(
    prompt: str,
    from_file: Path | None,
    name: str,
    agent: str,
    repo: Path | None,
    no_tmux: bool,
):
    """Generate a workbench plan from a description or existing document.

    \b
    The planner agent explores the codebase, then writes a detailed plan
    to .workbench/plans/<name>.md that can be executed with `wb run`.

    \b
    Example:
      wb plan "Add JWT authentication to the FastAPI app"
      wb plan --from claude-plan.md
      wb plan "Focus on security" --from existing-spec.md
      wb plan "Refactor the database layer" --name db-refactor
    """
    if not prompt and not from_file:
        raise click.ClickException(
            "Provide a prompt, --from <file>, or both.\n"
            '  wb plan "Add JWT auth"\n'
            "  wb plan --from existing-plan.md\n"
            '  wb plan "Focus on security" --from existing-plan.md'
        )

    if not no_tmux and not check_tmux_available():
        raise click.ClickException(
            "tmux is required but not found on PATH. "
            "Install with: brew install tmux (macOS) or apt install tmux (Linux). "
            "Or use --no-tmux to run without it."
        )

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)

    source_content = ""
    if from_file:
        source_content = from_file.read_text()

    output_path = repo / ".workbench" / "plans" / f"{name}.md"
    if from_file and prompt:
        console.print(f"\n[bold]Transforming:[/bold] {from_file}")
        console.print(f"[bold]Guidance:[/bold]   {prompt}")
    elif from_file:
        console.print(f"\n[bold]Transforming:[/bold] {from_file}")
    else:
        console.print(f"\n[bold]Planning:[/bold] {prompt}")
    console.print(f"[bold]Output:[/bold]  {output_path}\n")

    from .agents import run_planner

    result = asyncio.run(
        run_planner(
            repo=repo,
            user_prompt=prompt,
            source_content=source_content,
            plan_name=name,
            agent_cmd=agent,
            use_tmux=not no_tmux,
        )
    )

    if result.status.value == "failed":
        console.print(f"\n[red]Planning failed:[/red] {result.output}")
        raise SystemExit(1)

    if output_path.exists():
        console.print(f"\n[green]Plan written to:[/green] {output_path}\n")
        console.print("[bold]Next steps:[/bold]\n")
        console.print(f"  [dim]# Review the plan[/dim]")
        console.print(f"  cat {output_path}\n")
        console.print(f"  [dim]# Preview task waves (dry run)[/dim]")
        console.print(f"  wb preview {output_path}\n")
        console.print(f"  [dim]# Run the plan[/dim]")
        console.print(f"  wb run {output_path}\n")
        console.print(f"  [dim]# Run with options[/dim]")
        console.print(f"  wb run {output_path} --tdd")
        console.print(f"  wb run {output_path} --skip-review")
        console.print(f"  wb run {output_path} -j 6")
    else:
        console.print(
            f"\n[yellow]Agent completed but plan file not found at {output_path}.[/yellow]"
        )
        console.print(
            "[dim]The agent may have written it elsewhere. Check its output above.[/dim]"
        )


@main.command()
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def status(repo: Path | None):
    """Show active worktrees from workbench."""
    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)
    result = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    wb_trees = [line for line in result.stdout.splitlines() if "wb/" in line]

    if not wb_trees:
        console.print("[dim]No active workbench worktrees.[/dim]")
        return

    console.print(f"[bold]Active workbench worktrees ({len(wb_trees)}):[/bold]\n")
    for line in wb_trees:
        console.print(f"  {line}")


def _list_workbench_worktrees(repo: Path) -> list[tuple[str, str | None]]:
    """Return [(path, branch_or_none), ...] for every workbench worktree.

    Parses ``git worktree list --porcelain``. A worktree is "workbench" if its
    path contains ``.workbench`` (the convention for ``wb run``-created trees).
    Branch is recovered from the porcelain ``branch refs/heads/<name>`` line.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    worktrees: list[tuple[str, str | None]] = []
    current_path: str | None = None
    current_branch: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current_path is not None and ".workbench" in current_path:
                worktrees.append((current_path, current_branch))
            current_path = line.split("worktree ", 1)[1]
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line.split("branch refs/heads/", 1)[1]
    if current_path is not None and ".workbench" in current_path:
        worktrees.append((current_path, current_branch))
    return worktrees


def _list_wb_branches(repo: Path) -> list[str]:
    """Return every ``wb/*`` branch name."""
    result = subprocess.run(
        ["git", "branch", "--list", "wb/*"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return [line.strip().lstrip("* ") for line in result.stdout.splitlines() if line.strip()]


def _resolve_plan_source(plan_source: str, repo: Path) -> Path | None:
    """Resolve a plan_source string to a Path inside the repo, or return None.

    Returns the resolved Path if it exists and is inside the repo. Returns None
    in all other cases (missing field, file does not exist, outside the repo).
    """
    if not plan_source:
        return None
    try:
        resolved = Path(plan_source).expanduser().resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    try:
        if not resolved.is_relative_to(repo.resolve()):
            return None
    except ValueError:
        return None
    return resolved


@main.command()
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.option(
    "--force",
    is_flag=True,
    help="Remove all worktrees and wb/* branches, including in-flight ones.",
)
@click.option(
    "--completed",
    is_flag=True,
    help="Only remove artifacts for completed plans; skip in-flight ones silently.",
)
@click.option(
    "--remove-plans",
    is_flag=True,
    help="Also delete plan source markdown for completed plans.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be removed without removing anything.",
)
def clean(
    repo: Path | None,
    force: bool,
    completed: bool,
    remove_plans: bool,
    dry_run: bool,
):
    """Remove workbench worktrees, branches, and completed-plan status files.

    Default mode removes only artifacts belonging to completed plans (every
    task done + merged). Refuses if any in-flight worktrees, ``wb/*`` branches,
    or incomplete status files exist; pass --completed to skip them silently
    or --force to wipe everything regardless.
    """
    from .session_status import iter_status_files, status_file_branches, status_file_is_complete

    if force and completed:
        raise click.ClickException("--force and --completed are mutually exclusive")

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)

    # 1. Partition status files into completed vs incomplete.
    files = iter_status_files(repo)
    completed_files: list[tuple[Path, dict]] = []
    incomplete_files: list[tuple[Path, dict]] = []
    for path, data in files:
        if status_file_is_complete(data):
            completed_files.append((path, data))
        else:
            incomplete_files.append((path, data))

    completed_branches: set[str] = set()
    for _, data in completed_files:
        completed_branches |= status_file_branches(data)

    # 2. Enumerate live worktrees and branches.
    worktrees = _list_workbench_worktrees(repo)
    branches = _list_wb_branches(repo)

    # 3. Classify each.
    completed_worktrees = [(p, b) for p, b in worktrees if b in completed_branches]
    inflight_worktrees = [(p, b) for p, b in worktrees if b not in completed_branches]
    completed_wb_branches = [b for b in branches if b in completed_branches]
    inflight_wb_branches = [b for b in branches if b not in completed_branches]

    # 4. Default mode: refuse if anything is in-flight.
    if not force and not completed:
        blockers: list[str] = []
        for path, _ in incomplete_files:
            blockers.append(f"  status file: {path.relative_to(repo)}")
        for path, branch in inflight_worktrees:
            label = branch or "(detached)"
            blockers.append(f"  worktree:    {path} [{label}]")
        for branch in inflight_wb_branches:
            blockers.append(f"  branch:      {branch}")
        if blockers and not dry_run:
            raise click.ClickException(
                "In-flight workbench artifacts present:\n"
                + "\n".join(blockers)
                + "\n\nRun with --completed to skip these, or --force to remove everything."
            )

    # 5. Decide the removal set.
    if force:
        wt_to_remove = worktrees
        br_to_remove = branches
        sf_to_remove = [p for p, _ in completed_files]
        plan_files: list[Path] = []  # --remove-plans is ignored under --force
    else:
        wt_to_remove = completed_worktrees
        br_to_remove = completed_wb_branches
        sf_to_remove = [p for p, _ in completed_files]
        plan_files = []
        if remove_plans:
            for _, data in completed_files:
                src = data.get("plan_source", "") if isinstance(data, dict) else ""
                resolved = _resolve_plan_source(src, repo)
                if resolved is not None:
                    plan_files.append(resolved)
                elif src and not Path(src).expanduser().resolve().exists():
                    pass  # silently skip missing
                elif src:
                    console.print(f"[yellow]⚠[/yellow] skipping plan source outside repo: {src}")

    # 6. Execute (or preview).
    counts = {"worktrees": 0, "branches": 0, "status": 0, "plans": 0}
    verb = "would remove" if dry_run else "removed"
    style = "[dim]would remove[/dim]" if dry_run else "[green]✓[/green] removed"

    for path, branch in wt_to_remove:
        if dry_run:
            console.print(f"{style} worktree {path}")
        else:
            r = subprocess.run(
                ["git", "worktree", "remove", path, "--force"],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                console.print(f"{style} worktree {path}")
            else:
                console.print(f"[red]✗[/red] failed to remove worktree {path}: {r.stderr.strip()}")
                continue
        counts["worktrees"] += 1

    for branch in br_to_remove:
        if dry_run:
            console.print(f"{style} branch {branch}")
        else:
            r = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                console.print(f"{style} branch {branch}")
            else:
                console.print(f"[red]✗[/red] failed to remove branch {branch}: {r.stderr.strip()}")
                continue
        counts["branches"] += 1

    for path in sf_to_remove:
        rel = path.relative_to(repo)
        if dry_run:
            console.print(f"{style} status file {rel}")
        else:
            path.unlink()
            console.print(f"{style} status file {rel}")
        counts["status"] += 1

    for path in plan_files:
        try:
            rel = path.relative_to(repo)
        except ValueError:
            rel = path
        if dry_run:
            console.print(f"{style} plan {rel}")
        else:
            path.unlink()
            console.print(f"{style} plan {rel}")
        counts["plans"] += 1

    # 7. Summary
    summary = (
        f"{counts['worktrees']} worktree(s), "
        f"{counts['branches']} branch(es), "
        f"{counts['status']} status file(s), "
        f"{counts['plans']} plan(s)"
    )
    if dry_run:
        console.print(f"\n[bold]Would remove:[/bold] {summary}")
    else:
        console.print(f"\n[green]Cleaned up:[/green] {summary}")


@main.command()
@click.option("--cleanup", is_flag=True, help="Also remove worktrees and branches.")
@click.option(
    "--repo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Repo path (default: auto-detect).",
)
def stop(cleanup: bool, repo: Path | None):
    """Stop all running workbench agents."""
    # Find tmux sessions with wb- prefix
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )

    wb_sessions = []
    if result.returncode == 0:
        wb_sessions = [line for line in result.stdout.splitlines() if line.startswith("wb-")]

    if wb_sessions:
        for session_name in wb_sessions:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
        console.print(f"Stopped {len(wb_sessions)} agent session(s).")
    else:
        console.print("No active agent sessions.")

    if cleanup:
        repo = repo or _find_repo_root()
        _ensure_workbench_dir(repo)

        # Remove worktrees
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        removed = 0
        for line in result.stdout.splitlines():
            if line.startswith("worktree ") and ".workbench" in line:
                path = line.split("worktree ", 1)[1]
                subprocess.run(
                    ["git", "worktree", "remove", path, "--force"], cwd=repo, capture_output=True
                )
                removed += 1

        # Clean up wb/ branches
        result = subprocess.run(
            ["git", "branch", "--list", "wb/*"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            branch = line.strip()
            if branch:
                subprocess.run(["git", "branch", "-D", branch], cwd=repo, capture_output=True)

        console.print(f"[green]Cleaned up {removed} worktree(s).[/green]")


@main.command()
@click.option(
    "--session-branch",
    "-b",
    required=True,
    help="Session branch to merge into (e.g. workbench-1).",
)
@click.option(
    "--plan",
    "plan_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Plan file (auto-detected from status if omitted).",
)
@click.option(
    "--agent",
    default="claude",
    help="Agent CLI for merge conflict resolution (claude, gemini, codex, cursor, or custom).",
)
@click.option(
    "--repo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Repo path (default: auto-detect).",
)
@click.option(
    "--no-tmux", is_flag=True, help="Run agents as raw subprocesses instead of tmux sessions."
)
@click.option(
    "--keep-branches",
    is_flag=True,
    help="Keep task branches after merging (default: auto-delete on success).",
)
@click.option(
    "--push",
    is_flag=True,
    help="Push the session branch to origin after merging (sets upstream tracking).",
)
def merge(
    session_branch: str,
    plan_path: Path | None,
    agent: str,
    repo: Path | None,
    no_tmux: bool,
    keep_branches: bool,
    push: bool,
):
    """Merge completed-but-unmerged task branches into the session branch.

    \b
    Finds tasks that completed their pipeline but haven't been merged yet.
    Attempts each merge, using a resolver agent for conflicts.

    If --plan is provided, uses that plan's status file. Otherwise, scans
    all status files for the session branch.

    \b
    Example:
      wb merge -b workbench-1
      wb merge -b workbench-1 --plan plan.md
      wb merge -b workbench-1 --agent gemini
    """
    if not no_tmux and not check_tmux_available():
        raise click.ClickException(
            "tmux is required but not found on PATH. "
            "Install with: brew install tmux (macOS) or apt install tmux (Linux). "
            "Or use --no-tmux to run without it."
        )

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)

    plan_slug = None
    if plan_path:
        try:
            plan = parse_plan(plan_path.resolve())
        except ValueError as e:
            raise click.ClickException(str(e))
        plan_slug = plan.slug

    asyncio.run(
        merge_unmerged(
            repo=repo,
            session_branch=session_branch,
            plan_slug=plan_slug,
            agent_cmd=agent,
            use_tmux=not no_tmux,
            keep_branches=keep_branches,
            push=push,
        )
    )


@main.command()
@click.option(
    "--agent",
    type=click.Choice(["claude", "gemini", "cursor", "codex", "copilot", "manual"]),
    default=None,
    help="Target agent platform.",
)
@click.option("--symlink", is_flag=True, help="Symlink instead of copy (for development).")
@click.option(
    "--global",
    "use_global",
    is_flag=True,
    help="Install skills to user-level paths only (skip .workbench/ creation).",
)
@click.option(
    "--repo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Repo path (default: auto-detect).",
)
@click.option(
    "--profile",
    "create_profile",
    is_flag=True,
    help="Also create a profile.yaml with the detected agent.",
)
@click.option("--update", is_flag=True, help="Force-update skills to the latest version.")
def setup(
    agent: str | None,
    symlink: bool,
    use_global: bool,
    repo: Path | None,
    create_profile: bool,
    update: bool,
):
    """Set up workbench: create .workbench/, install skills, and optionally create a profile.

    By default, installs skills at both user-level and project-level paths.
    Use --global to only install skills to user-level paths (no .workbench/ creation).
    """
    resolved_agent = agent or _detect_agent()

    if use_global:
        # Global-only: install skills to user-level paths, no .workbench/ creation
        _install_skills(resolved_agent, symlink, local=False, update=update)

        if create_profile:
            target_dir = Path.home() / ".workbench"
            target = target_dir / "profile.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not click.confirm(f"{target} already exists. Overwrite?"):
                    return
            p = Profile.default()
            if resolved_agent != "claude":
                for role_name in Profile._ROLE_NAMES:
                    getattr(p, role_name).agent = resolved_agent
            p.save(target)
            console.print(f"Created profile: {target}")
    else:
        # Local setup: create .workbench/ and install skills at project level
        repo = repo or _find_repo_root()
        wb_dir = repo / ".workbench"
        if wb_dir.exists():
            console.print(f"Already exists: {wb_dir}/")
        else:
            wb_dir.mkdir(exist_ok=True)
            console.print(f"Created {wb_dir}/")

        _install_skills(resolved_agent, symlink, local=True, repo=repo, update=update)

        if create_profile:
            target = wb_dir / "profile.yaml"
            if target.exists():
                if not click.confirm(f"{target} already exists. Overwrite?"):
                    console.print(f"\n[bold green]Repo is ready for workbench.[/bold green]")
                    return
            p = Profile.default()
            if resolved_agent != "claude":
                for role_name in Profile._ROLE_NAMES:
                    getattr(p, role_name).agent = resolved_agent
            p.save(target)
            console.print(f"Created profile: {target}")

        console.print(f"\n[bold green]Repo is ready for workbench.[/bold green]")


@main.command(deprecated=True, hidden=True)
@click.option(
    "--agent", type=click.Choice(["claude", "gemini", "cursor", "codex", "manual"]), default=None
)
@click.option("--symlink", is_flag=True)
@click.option("--local", is_flag=True)
@click.option("--profile", "create_profile", is_flag=True)
@click.option("--update", is_flag=True)
def init(agent: str | None, symlink: bool, local: bool, create_profile: bool, update: bool):
    """Deprecated: use 'wb setup' instead."""
    console.print("[yellow]'wb init' is deprecated. Use 'wb setup' instead.[/yellow]\n")
    # Delegate to setup logic
    ctx = click.get_current_context()
    ctx.invoke(
        setup,
        agent=agent,
        symlink=symlink,
        use_global=not local,
        repo=None,
        create_profile=create_profile,
        update=update,
    )


_VALID_ROLES = Profile._ROLE_NAMES
_VALID_FIELDS = ("agent", "directive", "directive_extend")
_VALID_SUB_MODE_FIELDS = ("directive", "directive_extend")
_VALID_SUB_MODES = {"tdd": {"implementor", "tester"}, "followup": {"reviewer"}}


@main.group()
def profile():
    """Manage agent profiles."""
    pass


@profile.command("init")
@click.option(
    "--global", "use_global", is_flag=True, help="Create in ~/.workbench/ instead of .workbench/."
)
@click.option("--name", default=None, help="Named profile (creates profile.<name>.yaml).")
@click.option(
    "--set",
    "overrides",
    multiple=True,
    help="Set role fields inline (e.g. --set reviewer.agent=gemini).",
)
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def profile_init(
    use_global: bool, name: str | None, overrides: tuple[str, ...], repo: Path | None
):
    """Create a profile.yaml from defaults with optional inline overrides."""
    filename = Profile._profile_filename(name)
    if use_global:
        target = Path.home() / ".workbench" / filename
    else:
        repo = repo or Path.cwd()
        target = repo / ".workbench" / filename

    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        if not click.confirm(f"{target} already exists. Overwrite?"):
            return

    p = Profile.default()

    # Apply --set overrides
    for override in overrides:
        if "=" not in override:
            raise click.ClickException(
                f"Invalid --set format: {override}. Use <role>.<field>=<value>"
            )
        key, value = override.split("=", 1)
        parts = key.split(".")
        if len(parts) == 2:
            role_name, field_name = parts
            sub_mode = None
        elif len(parts) == 3:
            role_name, sub_mode, field_name = parts
        else:
            raise click.ClickException(
                f"Key must be <role>.<field> or <role>.<sub_mode>.<field>, got: {key}"
            )
        if role_name not in _VALID_ROLES:
            raise click.ClickException(f"Unknown role: {role_name}")
        cfg: RoleConfig = getattr(p, role_name)

        if sub_mode is not None:
            if sub_mode not in _VALID_SUB_MODES:
                raise click.ClickException(f"Unknown sub-mode: {sub_mode}")
            if role_name not in _VALID_SUB_MODES[sub_mode]:
                raise click.ClickException(f"{role_name} does not support a '{sub_mode}' sub-mode")
            if field_name not in _VALID_SUB_MODE_FIELDS:
                raise click.ClickException(f"Unknown sub-mode field: {field_name}")
            mode_cfg = getattr(cfg, sub_mode)
            if mode_cfg is None:
                mode_cfg = ModeConfig(directive="")
                setattr(cfg, sub_mode, mode_cfg)
            if field_name == "directive_extend":
                mode_cfg.directive = mode_cfg.directive + "\n\n" + value
            else:
                setattr(mode_cfg, field_name, value)
        else:
            if field_name not in _VALID_FIELDS:
                raise click.ClickException(f"Unknown field: {field_name}")
            if field_name == "directive_extend":
                cfg.directive = cfg.directive + "\n\n" + value
            else:
                setattr(cfg, field_name, value)

    p.save(target)
    console.print(f"Created {target}")


@profile.command("show")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--name", default=None, help="Named profile to resolve.")
@click.option(
    "--profile",
    "profile_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--full",
    is_flag=True,
    help="Show the full directive text (including built-in defaults).",
)
def profile_show(repo: Path | None, name: str | None, profile_path: Path | None, full: bool):
    """Show the resolved profile for each role."""
    repo = repo or Path.cwd()
    resolved = Profile.resolve(
        repo,
        profile_path=Path(profile_path) if profile_path else None,
        profile_name=name,
    )

    if full:
        _profile_show_full(resolved)
        return

    console.print(f"{'Role':<15} {'Agent':<12} {'Directive'}")
    console.print("-" * 60)
    for role_name in _VALID_ROLES:
        cfg: RoleConfig = getattr(resolved, role_name)
        directive_preview = cfg.directive.split("\n")[0][:60] if cfg.directive else "(default)"
        console.print(f"{role_name:<15} {cfg.agent:<12} {directive_preview}")
        if cfg.tdd is not None:
            tdd_preview = (
                cfg.tdd.directive.split("\n")[0][:60] if cfg.tdd.directive else "(default)"
            )
            tdd_key = f"  {role_name}.tdd.directive"
            console.print(f"{tdd_key:<28} {tdd_preview}")
        if cfg.followup is not None:
            fu_preview = (
                cfg.followup.directive.split("\n")[0][:60]
                if cfg.followup.directive
                else "(default)"
            )
            fu_key = f"  {role_name}.followup.directive"
            console.print(f"{fu_key:<28} {fu_preview}")


def _profile_show_full(resolved: Profile) -> None:
    """Print every role with its full directive text, using built-in defaults when unset."""
    for role_name in _VALID_ROLES:
        cfg: RoleConfig = getattr(resolved, role_name)
        console.print(f"[bold]{role_name}[/bold] (agent: {cfg.agent})")
        if cfg.directive:
            console.print("  directive:")
            source = "set"
        else:
            console.print("  directive (default):")
            source = "default"
        text = cfg.directive if source == "set" else _default_directive_text(role_name)
        console.print(_indent(text, "    "))

        for sub_mode, mode_cfg in (("tdd", cfg.tdd), ("followup", cfg.followup)):
            if mode_cfg is None:
                continue
            label = f"{role_name}.{sub_mode}.directive"
            if mode_cfg.directive:
                console.print(f"  {label}:")
                text = mode_cfg.directive
            else:
                console.print(f"  {label} (default):")
                text = _default_directive_text(role_name, sub_mode)
            console.print(_indent(text, "    "))
        console.print()


def _indent(text: str, prefix: str) -> str:
    """Indent every line of `text` by `prefix`."""
    if not text:
        return f"{prefix}(empty)"
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


@profile.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--global", "use_global", is_flag=True)
@click.option("--name", default=None, help="Named profile to update.")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def profile_set(key: str, value: str, use_global: bool, name: str | None, repo: Path | None):
    """Set a profile field (e.g. reviewer.agent gemini, tester.tdd.directive 'text')."""
    parts = key.split(".")
    if len(parts) == 2:
        role_name, field_name = parts
        sub_mode = None
    elif len(parts) == 3:
        role_name, sub_mode, field_name = parts
    else:
        raise click.ClickException(
            f"Key must be <role>.<field> or <role>.<sub_mode>.<field>, got: {key}"
        )

    if role_name not in _VALID_ROLES:
        raise click.ClickException(
            f"Unknown role: {role_name}. Valid roles: {', '.join(_VALID_ROLES)}"
        )

    if sub_mode is not None:
        if sub_mode not in _VALID_SUB_MODES:
            raise click.ClickException(
                f"Unknown sub-mode: {sub_mode}. Valid sub-modes: {', '.join(_VALID_SUB_MODES)}"
            )
        if role_name not in _VALID_SUB_MODES[sub_mode]:
            raise click.ClickException(f"{role_name} does not support a '{sub_mode}' sub-mode")
        if field_name not in _VALID_SUB_MODE_FIELDS:
            raise click.ClickException(
                f"Unknown sub-mode field: {field_name}. "
                f"Valid fields: {', '.join(_VALID_SUB_MODE_FIELDS)}"
            )
    else:
        if field_name not in _VALID_FIELDS:
            raise click.ClickException(
                f"Unknown field: {field_name}. Valid fields: {', '.join(_VALID_FIELDS)}"
            )

    filename = Profile._profile_filename(name)
    if use_global:
        target = Path.home() / ".workbench" / filename
    else:
        repo = repo or Path.cwd()
        target = repo / ".workbench" / filename

    target.parent.mkdir(parents=True, exist_ok=True)

    # Load existing YAML or start empty
    if target.exists():
        data = yaml.safe_load(target.read_text()) or {}
    else:
        data = {}

    if "roles" not in data:
        data["roles"] = {}
    if role_name not in data["roles"]:
        data["roles"][role_name] = {}

    if sub_mode is not None:
        if sub_mode not in data["roles"][role_name]:
            data["roles"][role_name][sub_mode] = {}
        data["roles"][role_name][sub_mode][field_name] = value
    else:
        data["roles"][role_name][field_name] = value

    target.write_text(yaml.dump(data, default_flow_style=False))
    console.print(f"Set {key} = {value} in {target}")


@profile.command("diff")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--name", default=None, help="Named profile to compare.")
@click.option(
    "--profile",
    "profile_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
)
def profile_diff(repo: Path | None, name: str | None, profile_path: Path | None):
    """Compare resolved profile against defaults."""
    repo = repo or Path.cwd()
    resolved = Profile.resolve(
        repo,
        profile_path=Path(profile_path) if profile_path else None,
        profile_name=name,
    )
    default = Profile.default()

    diffs = []
    for role_name in _VALID_ROLES:
        resolved_cfg: RoleConfig = getattr(resolved, role_name)
        default_cfg: RoleConfig = getattr(default, role_name)

        if resolved_cfg.agent != default_cfg.agent:
            diffs.append(f"  {role_name}.agent: {default_cfg.agent} → {resolved_cfg.agent}")
        if resolved_cfg.directive != default_cfg.directive:
            diffs.append(f"  {role_name}.directive: \\[changed]")
        if resolved_cfg.tdd is not None and default_cfg.tdd is None:
            diffs.append(f"  {role_name}.tdd.directive: \\[changed]")
        elif resolved_cfg.tdd is not None and default_cfg.tdd is not None:
            if resolved_cfg.tdd.directive != default_cfg.tdd.directive:
                diffs.append(f"  {role_name}.tdd.directive: \\[changed]")
        if resolved_cfg.followup is not None and default_cfg.followup is None:
            diffs.append(f"  {role_name}.followup.directive: \\[changed]")
        elif resolved_cfg.followup is not None and default_cfg.followup is not None:
            if resolved_cfg.followup.directive != default_cfg.followup.directive:
                diffs.append(f"  {role_name}.followup.directive: \\[changed]")

    if not diffs:
        console.print("Profile matches defaults.")
    else:
        console.print("Differences from defaults:")
        for line in diffs:
            console.print(line)


BUILTIN_AGENTS = {
    "claude": "Claude Code CLI (default)",
    "gemini": "Gemini CLI",
    "codex": "Codex CLI (OpenAI)",
    "cursor": "Cursor CLI (agent command)",
}


def _agents_yaml_path(repo: Path) -> Path:
    return repo / ".workbench" / "agents.yaml"


def _load_agents_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _save_agents_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False))


@main.group()
def agents():
    """Manage agent adapters (.workbench/agents.yaml)."""
    pass


@agents.command("init")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def agents_init(repo: Path | None):
    """Create agents.yaml with the default built-in agent configs."""
    from .adapters import default_agents_config

    repo = repo or _find_repo_root()
    config_path = _agents_yaml_path(repo)

    if config_path.exists():
        if not click.confirm(f"{config_path} already exists. Overwrite?"):
            return

    data = {"agents": default_agents_config()}
    _save_agents_yaml(config_path, data)
    console.print(f"Created {config_path} with {len(data['agents'])} agent(s)")


@agents.command("list")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def agents_list(repo: Path | None):
    """List built-in and configured agents."""
    repo = repo or _find_repo_root()
    config_path = _agents_yaml_path(repo)
    custom = _load_agents_yaml(config_path).get("agents", {})

    console.print("[bold]Built-in agents:[/bold]")
    for name, desc in BUILTIN_AGENTS.items():
        console.print(f"  {name:<12} {desc}")

    if custom:
        console.print(f"\n[bold]Custom agents[/bold] ({config_path}):")
        for name, entry in custom.items():
            cmd = entry.get("command", name)
            fmt = entry.get("output_format", "text")
            console.print(f"  {name:<12} command={cmd}  format={fmt}")
    else:
        console.print(f"\n[dim]No custom agents configured.[/dim]")


@agents.command("show")
@click.argument("name")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def agents_show(name: str, repo: Path | None):
    """Show details for an agent adapter."""
    repo = repo or _find_repo_root()

    if name in BUILTIN_AGENTS:
        console.print(f"[bold]{name}[/bold] (built-in)")
        console.print(f"  Description: {BUILTIN_AGENTS[name]}")
        console.print(f"  Type: built-in adapter")
        return

    config_path = _agents_yaml_path(repo)
    custom = _load_agents_yaml(config_path).get("agents", {})

    if name not in custom:
        raise click.ClickException(
            f"Agent '{name}' not found. Use 'wb agents list' to see available agents."
        )

    entry = custom[name]
    console.print(f"[bold]{name}[/bold] (custom)")
    console.print(f"  command:         {entry.get('command', name)}")
    console.print(f"  args:            {entry.get('args', ['{prompt}'])}")
    console.print(f"  output_format:   {entry.get('output_format', 'text')}")
    console.print(f"  json_result_key: {entry.get('json_result_key', 'result')}")
    console.print(f"  json_cost_key:   {entry.get('json_cost_key', 'cost_usd')}")


@agents.command("add")
@click.argument("name")
@click.option("--command", "cmd", required=True, help="CLI command to invoke.")
@click.option(
    "--args",
    default="{prompt}",
    help='Argument template (default: "{prompt}"). Use {prompt} as placeholder.',
)
@click.option(
    "--output-format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text).",
)
@click.option("--json-result-key", default="result", help="JSON key for result (default: result).")
@click.option("--json-cost-key", default="cost_usd", help="JSON key for cost (default: cost_usd).")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def agents_add(
    name: str,
    cmd: str,
    args: str,
    output_format: str,
    json_result_key: str,
    json_cost_key: str,
    repo: Path | None,
):
    """Add or update a custom agent adapter."""
    repo = repo or _find_repo_root()
    config_path = _agents_yaml_path(repo)
    data = _load_agents_yaml(config_path)

    if "agents" not in data:
        data["agents"] = {}

    # Parse args — support comma-separated or single string
    args_list = [a.strip() for a in args.split(",") if a.strip()]

    entry = {
        "command": cmd,
        "args": args_list,
        "output_format": output_format,
    }
    if output_format == "json":
        entry["json_result_key"] = json_result_key
        entry["json_cost_key"] = json_cost_key

    action = "Updated" if name in data["agents"] else "Added"
    data["agents"][name] = entry
    _save_agents_yaml(config_path, data)
    console.print(f"{action} agent '{name}' in {config_path}")


@agents.command("remove")
@click.argument("name")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def agents_remove(name: str, repo: Path | None):
    """Remove a custom agent adapter."""
    repo = repo or _find_repo_root()
    config_path = _agents_yaml_path(repo)
    data = _load_agents_yaml(config_path)

    agents_cfg = data.get("agents", {})
    if name not in agents_cfg:
        raise click.ClickException(f"Agent '{name}' not found in {config_path}.")

    del agents_cfg[name]
    data["agents"] = agents_cfg
    _save_agents_yaml(config_path, data)
    console.print(f"Removed agent '{name}' from {config_path}")


if __name__ == "__main__":
    main()
