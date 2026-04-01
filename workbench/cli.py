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

from .orchestrator import run_plan
from .plan_parser import parse_plan
from .profile import Profile, RoleConfig
from .tmux import check_tmux_available

console = Console()


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
    for name in ("claude", "gemini", "codex", "cursor"):
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

    console.print(f"[bold]Installing {len(skills)} {label}(s) for {agent}...[/bold]\n")

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
@click.version_option()
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
@click.option("--agent", default="claude", help="Agent CLI command (claude, gemini, etc).")
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
    help="Resume from an existing session branch (e.g. workbench-1).",
)
@click.option(
    "--start-wave",
    "-w",
    default=1,
    type=int,
    help="Start from this wave number (1-indexed, default: 1).",
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
    help="Name the session branch (creates workbench-<name> instead of workbench-<N>).",
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
    start_wave: int,
    no_tmux: bool,
    tdd: bool,
    local: bool,
    base: str | None,
    profile_path: Path | None,
    profile_name: str | None,
    session_name: str | None,
    implementor_directive: str | None,
    tester_directive: str | None,
    reviewer_directive: str | None,
    fixer_directive: str | None,
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

    if tdd and skip_test:
        raise click.ClickException("--tdd and --skip-test are mutually exclusive.")

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)
    plan = parse_plan(plan_path.resolve())

    if not plan.tasks:
        raise click.ClickException("No tasks found in plan. Use '## Task: <title>' sections.")

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
            start_wave=start_wave,
            use_tmux=not no_tmux,
            directives=directives or None,
            tdd=tdd,
            local=local,
            base_branch=base,
            profile_path=profile_path,
            profile_name=profile_name,
            session_name=session_name,
            keep_branches=keep_branches,
        )
    )


@main.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
def preview(plan_path: Path):
    """Preview tasks parsed from a plan (dry run)."""
    plan = parse_plan(plan_path.resolve())

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
            desc_preview = task.description.strip()[:120]
            if desc_preview:
                console.print(f"    {desc_preview}")
        console.print()


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


@main.command()
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.confirmation_option(prompt="Remove all workbench worktrees?")
def clean(repo: Path | None):
    """Remove all workbench worktrees and branches."""
    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    # Phase 1: Remove worktrees
    removed = 0
    for line in result.stdout.splitlines():
        if line.startswith("worktree ") and ".workbench" in line:
            path = line.split("worktree ", 1)[1]
            subprocess.run(
                ["git", "worktree", "remove", path, "--force"], cwd=repo, capture_output=True
            )
            removed += 1

    # Prune stale worktree references before touching branches
    subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)

    # Phase 2: Collect branches to delete, then delete in one pass
    result = subprocess.run(
        ["git", "branch", "--list", "wb/*"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    branches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for branch in branches:
        subprocess.run(["git", "branch", "-D", branch], cwd=repo, capture_output=True)

    console.print(f"[green]Cleaned up {removed} worktree(s) and {len(branches)} branch(es).[/green]")


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

        subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)

        # Clean up wb/ branches
        result = subprocess.run(
            ["git", "branch", "--list", "wb/*"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        branches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for branch in branches:
            subprocess.run(["git", "branch", "-D", branch], cwd=repo, capture_output=True)

        console.print(f"[green]Cleaned up {removed} worktree(s) and {len(branches)} branch(es).[/green]")


@main.command()
@click.option(
    "--agent",
    type=click.Choice(["claude", "gemini", "cursor", "codex", "manual"]),
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
        if len(parts) != 2:
            raise click.ClickException(f"Key must be <role>.<field>, got: {key}")
        role_name, field_name = parts
        if role_name not in _VALID_ROLES:
            raise click.ClickException(f"Unknown role: {role_name}")
        if field_name not in _VALID_FIELDS:
            raise click.ClickException(f"Unknown field: {field_name}")
        cfg: RoleConfig = getattr(p, role_name)
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
def profile_show(repo: Path | None, name: str | None, profile_path: Path | None):
    """Show the resolved profile for each role."""
    repo = repo or Path.cwd()
    resolved = Profile.resolve(
        repo,
        profile_path=Path(profile_path) if profile_path else None,
        profile_name=name,
    )

    console.print(f"{'Role':<15} {'Agent':<12} {'Directive'}")
    console.print("-" * 60)
    for role_name in _VALID_ROLES:
        cfg: RoleConfig = getattr(resolved, role_name)
        directive_preview = cfg.directive.split("\n")[0][:60] if cfg.directive else ""
        console.print(f"{role_name:<15} {cfg.agent:<12} {directive_preview}")


@profile.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--global", "use_global", is_flag=True)
@click.option("--name", default=None, help="Named profile to update.")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def profile_set(key: str, value: str, use_global: bool, name: str | None, repo: Path | None):
    """Set a profile field (e.g. reviewer.agent gemini)."""
    parts = key.split(".")
    if len(parts) != 2:
        raise click.ClickException(f"Key must be in <role>.<field> format, got: {key}")

    role_name, field_name = parts

    if role_name not in _VALID_ROLES:
        raise click.ClickException(
            f"Unknown role: {role_name}. Valid roles: {', '.join(_VALID_ROLES)}"
        )
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

    if not diffs:
        console.print("Profile matches defaults.")
    else:
        console.print("Differences from defaults:")
        for line in diffs:
            console.print(line)


if __name__ == "__main__":
    main()
