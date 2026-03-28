"""CLI entrypoint for workbench."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import click
from rich.console import Console

from .orchestrator import run_plan
from .plan_parser import parse_plan


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
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None, help="Repo path (default: auto-detect).")
@click.option("--session-branch", "-b", default=None, help="Resume from an existing session branch (e.g. workbench-1).")
@click.option("--start-wave", "-w", default=1, type=int, help="Start from this wave number (1-indexed, default: 1).")
def run(plan_path: Path, max_concurrent: int, skip_test: bool, skip_review: bool, max_retries: int, agent: str, cleanup: bool, repo: Path | None, session_branch: str | None, start_wave: int):
    """Run a plan with parallel agents.

    \b
    Example:
      wb run plan.md
      wb run plan.md -j 6 --skip-review
      wb run plan.md --agent gemini
    """
    repo = repo or _find_repo_root()
    plan = parse_plan(plan_path.resolve())

    if not plan.tasks:
        raise click.ClickException("No tasks found in plan. Use '## Task: <title>' sections.")

    console.print(f"\n[bold]Parsed {len(plan.tasks)} task(s) from[/bold] {plan_path}\n")
    for i, task in enumerate(plan.tasks, 1):
        files = f" ({', '.join(task.files)})" if task.files else ""
        deps = f" [after: {', '.join(task.depends_on)}]" if task.depends_on else ""
        console.print(f"  {i}. {task.title}{files}{deps}")

    console.print()
    asyncio.run(run_plan(
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
    ))


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
        console.print(f"[bold cyan]Wave {wave_idx + 1}[/bold cyan] ({len(wave)} tasks, run in parallel)")
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
            subprocess.run(["git", "worktree", "remove", path, "--force"], cwd=repo, capture_output=True)
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


if __name__ == "__main__":
    main()
