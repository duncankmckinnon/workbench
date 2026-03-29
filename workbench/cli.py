"""CLI entrypoint for workbench."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import click
from rich.console import Console

from .orchestrator import run_plan
from .plan_parser import parse_plan
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
    for name in ("claude", "codex", "cursor"):
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
    "cursor": "rule",
    "codex": "instruction",
    "manual": "skill file",
}


def _install_skills(agent: str | None, symlink: bool) -> None:
    """Install bundled skill files for the given agent platform."""
    agent = agent or _detect_agent()
    skills_dir = _get_skills_dir()
    skills = _discover_skills(skills_dir)
    label = _PLATFORM_LABEL.get(agent, "skill file")

    if not skills:
        console.print("[yellow]No bundled skill files found.[/yellow]")
        return

    console.print(f"[bold]Installing {len(skills)} {label}(s) for {agent}...[/bold]\n")

    if agent == "claude":
        target_dir = Path.home() / ".claude" / "skills"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, src in skills:
            src_dir = src.parent
            dest_dir = target_dir / name
            dest = dest_dir / "SKILL.md"
            if dest.exists() and not symlink:
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

    elif agent == "cursor":
        target_dir = Path.cwd() / ".cursor" / "rules"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, src in skills:
            dest = target_dir / f"{name}.md"
            if dest.exists() and not symlink:
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

    elif agent == "codex":
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
    repo: Path | None,
    session_branch: str | None,
    start_wave: int,
    no_tmux: bool,
    tdd: bool,
    local: bool,
    base: str | None,
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
    "--agent",
    type=click.Choice(["claude", "cursor", "codex", "manual"]),
    default=None,
    help="Target agent platform.",
)
@click.option("--symlink", is_flag=True, help="Symlink instead of copy (for development).")
def init(agent: str | None, symlink: bool):
    """Install workbench skills for your agent platform."""
    _install_skills(agent, symlink)


@main.command()
@click.option(
    "--agent",
    type=click.Choice(["claude", "cursor", "codex", "manual"]),
    default=None,
    help="Target agent platform.",
)
@click.option("--symlink", is_flag=True, help="Symlink skills instead of copy (for development).")
@click.option(
    "--repo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Repo path (default: auto-detect).",
)
def setup(agent: str | None, symlink: bool, repo: Path | None):
    """Set up a repo for workbench: create .workbench/ and install skills."""
    repo = repo or _find_repo_root()
    wb_dir = repo / ".workbench"
    if wb_dir.exists():
        console.print(f"Already exists: {wb_dir}/")
    else:
        wb_dir.mkdir(exist_ok=True)
        console.print(f"Created {wb_dir}/")

    _install_skills(agent, symlink)
    console.print(f"\n[bold green]Repo is ready for workbench.[/bold green]")


if __name__ == "__main__":
    main()
