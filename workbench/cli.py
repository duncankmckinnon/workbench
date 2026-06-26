"""CLI entrypoint for workbench."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import click
import yaml
from rich.console import Console

from .adapters import get_adapter
from .conventions import TEMPLATE, conventions_path, load_conventions_text
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
from .final_review import run_final_review
from .github_pr import create_pr
from .orchestrator import merge_unmerged, run_plan
from .path_resolver import (
    plan_slug_from_path,
    resolve_agents_config_paths,
    resolve_plan_path,
    resolve_profile_paths,
)
from .plan_parser import parse_plan
from .pr_writer import PrWriterError, derive_body_from_plan, derive_title_from_plan, run_pr_writer
from .profile import ModeConfig, Profile, RoleConfig
from .session_status import FinalReviewRecord, SessionStatus
from .tmux import check_tmux_available, run_in_tmux
from .worktree import iter_worktree_dirs, push_session_branch

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
    "final_review": "final_review",
    "trace": "trace",
    "trace_env": "trace_env",
    "trace_prompt": "trace_prompt",
}

# Click's --profile option converts the string to a Path via
# type=click.Path(path_type=Path); frontmatter values bypass that, so we
# coerce here to keep downstream code (Profile.resolve calls .exists()) happy.
_FRONTMATTER_PARAM_COERCIONS: dict[str, type] = {
    "profile_path": Path,
}


def _parse_model_overrides(values: tuple[str, ...]) -> dict[str, str]:
    """Parse repeated --model values into {agent: model}.

    'agent=model' → scoped; bare 'model' → {"": model} (all agents).
    Later values override earlier ones for the same key.
    """
    out: dict[str, str] = {}
    for v in values:
        if "=" in v:
            agent, model = v.split("=", 1)
            out[agent.strip()] = model.strip()
        else:
            out[""] = v.strip()
    return out


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


def _resolve_final_review_args(
    repo: Path,
    session_branch: str,
    plan_slug: str,
    base_branch: str,
    plan_source: Path,
    merged_task_titles: list[str],
    agent: str,
    no_tmux: bool,
    profile: Profile | None,
    summarizer_directive: str | None,
    branch_reviewer_directive: str | None,
    pr_writer_directive: str | None,
    pr_title: str | None,
    pr_body_file: Path | None,
    pr_base: str | None,
    skip_pr: bool,
) -> dict:
    """Build kwargs dict for run_final_review from CLI flags + session info."""
    return dict(
        repo=repo,
        session_branch=session_branch,
        plan_slug=plan_slug,
        base_branch=base_branch,
        plan_source=plan_source,
        merged_task_titles=merged_task_titles,
        agent_cmd=agent,
        use_tmux=not no_tmux,
        profile=profile,
        summarizer_directive=summarizer_directive,
        branch_reviewer_directive=branch_reviewer_directive,
        pr_writer_directive=pr_writer_directive,
        pr_title=pr_title,
        pr_body_file=pr_body_file,
        pr_base=pr_base,
        skip_pr=skip_pr,
    )


def _print_final_review_result(record: FinalReviewRecord) -> None:
    """Print a one-line summary of a final review result."""
    verdict_style = {
        "pass": "green",
        "fail": "red",
        "error": "yellow",
    }.get(record.verdict, "dim")
    location = record.pr_url or record.review_path
    console.print(
        f"Final review: [{verdict_style}]{record.verdict.upper()}[/{verdict_style}] — {location}"
    )


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


def _resolve_plan_arg(arg: str, repo: Path) -> tuple[Path, str]:
    """Resolve a plan reference (name or path) and return (plan_path, plan_slug).

    Raises ClickException if the plan cannot be located.
    """
    try:
        plan_path = resolve_plan_path(arg, repo)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    if not plan_path.exists():
        raise click.ClickException(f"Plan path does not exist: {plan_path}")
    return plan_path.resolve(), plan_slug_from_path(plan_path)


def _merge_profile_yaml_dicts(paths: list[Path]) -> dict:
    """Merge raw YAML dicts from profile.yaml files in lowest-first order.

    Only emits role keys that appear in at least one source file, so the
    frozen output is a clean superset of the inputs rather than the full
    default profile.
    """
    roles: dict[str, dict] = {}
    for path in paths:
        data = yaml.safe_load(path.read_text()) or {}
        for role, role_data in (data.get("roles") or {}).items():
            if not isinstance(role_data, dict):
                continue
            target = roles.setdefault(role, {})
            for key, val in role_data.items():
                target[key] = val
    return {"roles": roles}


def _copy_plan_settings(repo: Path, plan_slug: str) -> None:
    """Materialize the current effective profile.yaml and agents.yaml into the plan folder.

    The source for profile is the home + project chain (no per-plan level —
    that is what we are populating). The source for agents.yaml is the
    project-level file. Only keys present in the source files are written;
    the default profile is not expanded into the frozen file.
    """
    from datetime import date as _date

    plan_dir = repo / ".workbench" / plan_slug
    plan_dir.mkdir(parents=True, exist_ok=True)
    today = _date.today().isoformat()
    header = (
        f"# Frozen by `wb plan --copy-settings` on {today}\n"
        f"# Edit this file to customize profile/agents for this plan only.\n"
    )

    profile_paths = resolve_profile_paths(repo, plan_slug=None)
    target_profile = plan_dir / "profile.yaml"
    if profile_paths:
        merged = _merge_profile_yaml_dicts(list(reversed(profile_paths)))
        body = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
        target_profile.write_text(header + body)
        console.print(f"Wrote {target_profile}")
    else:
        console.print(
            f"[dim]No profile.yaml found at project or home level; skipping {target_profile}.[/dim]"
        )

    agents_paths = resolve_agents_config_paths(repo, plan_slug=None)
    target_agents = plan_dir / "agents.yaml"
    if agents_paths:
        source = agents_paths[0]
        target_agents.write_text(header + source.read_text())
        console.print(f"Wrote {target_agents}")
    else:
        console.print(
            f"[dim]No agents.yaml found at project level; skipping {target_agents}.[/dim]"
        )


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
    _BINARY_TO_NAME: dict[str, str] = {
        "claude": "claude",
        "agy": "antigravity",
        "codex": "codex",
        "agent": "cursor",
        "copilot": "copilot",
    }
    found = []
    for binary, name in _BINARY_TO_NAME.items():
        if shutil.which(binary):
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
    "antigravity": "skill",
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

    elif agent == "antigravity":
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
@click.argument("plan_path", type=str)
@click.option("--max-concurrent", "-j", default=4, help="Max parallel agents.")
@click.option("--skip-test", is_flag=True, help="Skip the testing phase.")
@click.option("--skip-review", is_flag=True, help="Skip the review phase.")
@click.option("--max-retries", "-r", default=2, help="Max fix attempts per failed stage.")
@click.option(
    "--agent",
    default="claude",
    help="Agent CLI command (claude, antigravity, codex, cursor, copilot, or custom).",
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
    "--fail-fast/--no-fail-fast",
    "fail_fast",
    default=True,
    help="On the first wave with a failed task, finish the in-flight wave then "
    "stop (default: on; use --no-fail-fast to disable).",
)
@click.option(
    "--headroom/--no-headroom",
    "headroom",
    default=None,
    help="Route agents through a local headroom proxy to cut token costs "
    "(default: use the headroom: config block; --headroom forces on).",
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
@click.option(
    "--final-review",
    "final_review",
    is_flag=True,
    help="Run a final whole-branch review after merges complete.",
)
@click.option("--pr-title", default=None, type=str, help="Override the PR title.")
@click.option(
    "--pr-body-file",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a file whose content becomes the PR body.",
)
@click.option("--pr-base", default=None, type=str, help="Override the PR base branch.")
@click.option("--skip-pr", is_flag=True, help="Skip PR creation even on PASS verdict.")
@click.option(
    "--summarizer-directive",
    default=None,
    type=str,
    help="Override the requirements summarizer agent's instructions.",
)
@click.option(
    "--branch-reviewer-directive",
    default=None,
    type=str,
    help="Override the branch reviewer agent's instructions.",
)
@click.option(
    "--pr-writer-directive",
    default=None,
    type=str,
    help="Override the PR writer agent's instructions.",
)
@click.option(
    "--trace/--no-trace",
    "trace",
    default=True,
    help="Master switch for session trace metadata (env + prompt). Default on.",
)
@click.option(
    "--trace-env/--no-trace-env",
    "trace_env",
    default=True,
    help="Inject WB_*/OTEL trace env vars into agent processes. Default on.",
)
@click.option(
    "--trace-prompt/--no-trace-prompt",
    "trace_prompt",
    default=False,
    help="Prepend a wb-session metadata block to agent prompts. Default off.",
)
@click.option(
    "--model",
    "model_overrides",
    multiple=True,
    help="Model per agent: --model claude=claude-opus-4-8 (repeatable), "
    "or --model claude-opus-4-8 to apply to all agents.",
)
def run(
    plan_path: str,
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
    headroom: bool | None,
    only_incomplete: bool,
    task_ids: tuple[str, ...],
    implementor_directive: str | None,
    tester_directive: str | None,
    reviewer_directive: str | None,
    fixer_directive: str | None,
    push: bool,
    final_review: bool,
    pr_title: str | None,
    pr_body_file: Path | None,
    pr_base: str | None,
    skip_pr: bool,
    summarizer_directive: str | None,
    branch_reviewer_directive: str | None,
    pr_writer_directive: str | None,
    trace: bool,
    trace_env: bool,
    trace_prompt: bool,
    model_overrides: tuple[str, ...],
):
    """Run a plan with parallel agents.

    \b
    Example:
      wb run plan.md
      wb run plan.md -j 6 --skip-review
      wb run plan.md --agent antigravity
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

    resolved_plan_path, plan_slug = _resolve_plan_arg(plan_path, repo)

    try:
        plan = parse_plan(resolved_plan_path, repo=repo)
    except ValueError as e:
        raise click.ClickException(str(e))

    # Merge frontmatter run_config into effective flag values
    ctx = click.get_current_context()
    mapped_run_config = {
        _FRONTMATTER_TO_PARAM[k]: v
        for k, v in plan.run_config.items()
        if k in _FRONTMATTER_TO_PARAM
    }
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
        "final_review": final_review,
        "trace": trace,
        "trace_env": trace_env,
        "trace_prompt": trace_prompt,
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
    final_review = effective["final_review"]
    trace = effective["trace"]
    trace_env = effective["trace_env"]
    trace_prompt = effective["trace_prompt"]

    effective_trace_env = trace and trace_env
    effective_trace_prompt = trace and trace_prompt

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

    console.print(f"\n[bold]Parsed {len(plan.tasks)} task(s) from[/bold] {resolved_plan_path}\n")
    for i, task in enumerate(plan.tasks, 1):
        files = f" ({', '.join(task.files)})" if task.files else ""
        deps = f" [after: {', '.join(task.depends_on)}]" if task.depends_on else ""
        console.print(f"  {i}. {task.title}{files}{deps}")

    agents_config_paths = resolve_agents_config_paths(repo, plan_slug=plan_slug)

    cli_models = _parse_model_overrides(model_overrides)

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
            agents_config_paths=agents_config_paths,
            trace_env=effective_trace_env,
            trace_prompt=effective_trace_prompt,
            cli_models=cli_models,
            headroom=headroom,
        )
    )

    # Final review after run completes
    if final_review:
        status_plan_slug = plan.folder_id
        status = (
            SessionStatus.load(repo, status_plan_slug, session_branch) if session_branch else None
        )
        if status is None:
            # session_branch was auto-generated; scan for it
            for path in sorted((repo / ".workbench").glob("*/status.yaml")):
                if path.parent.name == status_plan_slug:
                    data = yaml.safe_load(path.read_text()) or {}
                    for sb in data.get("sessions") or {}:
                        status = SessionStatus.load(repo, status_plan_slug, sb)
                        session_branch = sb
                        break
                    break

        if status:
            merged_tasks = {tid for tid, rec in status.tasks.items() if rec.merged}
            merged_titles = [t.title for t in plan.tasks if t.id in merged_tasks]
            if merged_titles:
                fr_profile_paths = resolve_profile_paths(
                    repo,
                    plan_slug=plan_slug,
                    explicit_path=profile_path,
                    name=profile_name,
                )
                profile_obj = Profile.from_layered_yaml(list(reversed(fr_profile_paths)))
                review_args = _resolve_final_review_args(
                    repo=repo,
                    session_branch=session_branch or status.session_branch,
                    plan_slug=status_plan_slug,
                    base_branch=base or "main",
                    plan_source=resolved_plan_path,
                    merged_task_titles=merged_titles,
                    agent=agent,
                    no_tmux=no_tmux,
                    profile=profile_obj,
                    summarizer_directive=summarizer_directive,
                    branch_reviewer_directive=branch_reviewer_directive,
                    pr_writer_directive=pr_writer_directive,
                    pr_title=pr_title,
                    pr_body_file=pr_body_file,
                    pr_base=pr_base,
                    skip_pr=skip_pr,
                )
                review_args["agents_config_paths"] = agents_config_paths
                review_args["max_retries"] = max_retries
                review_args["trace_env"] = effective_trace_env
                review_args["trace_prompt"] = effective_trace_prompt
                record = asyncio.run(run_final_review(**review_args))
                _print_final_review_result(record)


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
    "--fail-fast/--no-fail-fast",
    "fail_fast",
    default=True,
    help="On the first wave with a failed task, finish the in-flight wave then "
    "stop (default: on; use --no-fail-fast to disable).",
)
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
    fail_fast: bool,
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
        plan_path=str(plan_path),
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
        fail_fast=fail_fast,
        headroom=None,
        only_incomplete=True,
        task_ids=(),
        implementor_directive=None,
        tester_directive=None,
        reviewer_directive=None,
        fixer_directive=None,
        push=False,
        final_review=False,
        pr_title=None,
        pr_body_file=None,
        pr_base=None,
        skip_pr=False,
        summarizer_directive=None,
        branch_reviewer_directive=None,
        pr_writer_directive=None,
        trace=True,
        trace_env=True,
        trace_prompt=False,
    )


@main.command()
@click.argument("plan_path", type=str)
@click.option(
    "--repo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Repo path (default: auto-detect).",
)
def preview(plan_path: str, repo: Path | None):
    """Preview tasks parsed from a plan (dry run)."""
    repo = repo or _find_repo_root()
    resolved_plan_path, _plan_slug = _resolve_plan_arg(plan_path, repo)
    try:
        plan = parse_plan(resolved_plan_path)
    except ValueError as e:
        raise click.ClickException(str(e))

    if not plan.tasks:
        raise click.ClickException("No tasks found in plan.")

    console.print(f"\n[bold]{plan.title}[/bold]")
    console.print(f"Source: {resolved_plan_path}\n")

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


class _PlanGroup(click.Group):
    """Click group for ``wb plan`` that falls through to ``generate`` for
    unknown first positional args, so ``wb plan "prompt"`` keeps working.

    Injects ``generate`` at the head of the arg list when the first
    positional arg is not a registered subcommand (e.g. when the user is
    passing a prompt or only options).
    """

    def parse_args(self, ctx, args):
        # Let --help/-h bubble up to the group so users can discover
        # subcommands (otherwise --help shows generate's help instead).
        if any(a in ("--help", "-h") for a in args):
            return super().parse_args(ctx, args)
        first_positional = next((a for a in args if not a.startswith("-")), None)
        if first_positional is None or first_positional not in self.commands:
            args = ["generate"] + list(args)
        return super().parse_args(ctx, args)


@main.group(cls=_PlanGroup)
def plan():
    """Generate workbench plans or manage per-plan settings."""
    pass


@plan.command("generate")
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
    help="Name for the plan file (default: plan). Produces .workbench/<name>/plan.md.",
)
@click.option(
    "--agent",
    default="claude",
    help="Agent CLI command (claude, antigravity, codex, cursor, copilot, or custom).",
)
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--no-tmux", is_flag=True, help="Run without tmux.")
@click.option(
    "--copy-settings",
    is_flag=True,
    help="Copy the current effective profile.yaml and agents.yaml into the plan folder.",
)
@click.option(
    "--trace/--no-trace",
    "trace",
    default=True,
    help="Master switch for session trace metadata (env + prompt). Default on.",
)
@click.option(
    "--trace-env/--no-trace-env",
    "trace_env",
    default=True,
    help="Inject WB_*/OTEL trace env vars into agent processes. Default on.",
)
@click.option(
    "--trace-prompt/--no-trace-prompt",
    "trace_prompt",
    default=False,
    help="Prepend a wb-session metadata block to agent prompts. Default off.",
)
@click.option(
    "--model",
    "model_overrides",
    multiple=True,
    help="Model per agent: --model claude=claude-opus-4-8 (repeatable), "
    "or --model claude-opus-4-8 to apply to all agents.",
)
def plan_generate(
    prompt: str,
    from_file: Path | None,
    name: str,
    agent: str,
    repo: Path | None,
    no_tmux: bool,
    copy_settings: bool,
    trace: bool,
    trace_env: bool,
    trace_prompt: bool,
    model_overrides: tuple[str, ...],
):
    """Generate a workbench plan from a description or existing document.

    \b
    The planner agent explores the codebase, then writes a detailed plan
    to .workbench/<name>/plan.md that can be executed with `wb run`.

    \b
    Example:
      wb plan "Add JWT authentication to the FastAPI app"
      wb plan --from claude-plan.md
      wb plan "Focus on security" --from existing-spec.md
      wb plan "Refactor the database layer" --name db-refactor
      wb plan "Add feature X" --name foo --copy-settings
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

    if copy_settings:
        _copy_plan_settings(repo, name)

    source_content = ""
    if from_file:
        source_content = from_file.read_text()

    output_path = repo / ".workbench" / name / "plan.md"
    if from_file and prompt:
        console.print(f"\n[bold]Transforming:[/bold] {from_file}")
        console.print(f"[bold]Guidance:[/bold]   {prompt}")
    elif from_file:
        console.print(f"\n[bold]Transforming:[/bold] {from_file}")
    else:
        console.print(f"\n[bold]Planning:[/bold] {prompt}")
    console.print(f"[bold]Output:[/bold]  {output_path}\n")

    from .agents import resolve_model, run_planner

    conventions_text = load_conventions_text(repo)

    agents_paths = resolve_agents_config_paths(repo, plan_slug=name)
    effective_trace_env = trace and trace_env
    effective_trace_prompt = trace and trace_prompt

    cli_models = _parse_model_overrides(model_overrides)
    profile = None
    planner_agent = profile.planner.agent if profile else agent
    planner_model = resolve_model(
        role="planner",
        agent=planner_agent,
        cli_models=cli_models,
        profile=profile,
        plan_models={},
    )

    result = asyncio.run(
        run_planner(
            repo=repo,
            user_prompt=prompt,
            source_content=source_content,
            plan_name=name,
            agent_cmd=agent,
            use_tmux=not no_tmux,
            agents_config_paths=agents_paths,
            conventions_text=conventions_text,
            trace_env=effective_trace_env,
            trace_prompt=effective_trace_prompt,
            model=planner_model,
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


@plan.command("copy-settings")
@click.argument("name", type=str)
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def plan_copy_settings(name: str, repo: Path | None):
    """Copy the current effective profile and agents config into an existing plan folder."""
    repo = repo or _find_repo_root()
    plan_dir = repo / ".workbench" / name
    if not plan_dir.exists():
        raise click.ClickException(f"Plan folder not found: {plan_dir}")
    _copy_plan_settings(repo, name)


@main.group()
def conventions():
    """Manage the project-wide .workbench/conventions.md file."""
    pass


@conventions.command("init")
@click.option(
    "--generate",
    is_flag=True,
    help="Dispatch an agent (via the configured adapter) with the generate-conventions skill "
    "to draft the file from a codebase scan. Without --generate, writes a static template.",
)
@click.option(
    "--agent",
    default="claude",
    help="Agent CLI command for --generate (default: claude).",
)
@click.option("--no-tmux", is_flag=True, help="Run --generate without tmux.")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def conventions_init(generate: bool, agent: str, no_tmux: bool, repo: Path | None):
    """Create .workbench/conventions.md from a template (or via an agent with --generate)."""
    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)
    path = conventions_path(repo)
    if path.exists():
        raise click.ClickException(
            f"{path.relative_to(repo)} already exists. "
            "Run `wb conventions delete` first to start over."
        )

    if not generate:
        path.write_text(TEMPLATE, encoding="utf-8")
        console.print(f"[green]Wrote[/green] {path.relative_to(repo)}")
        return

    if not no_tmux and not check_tmux_available():
        raise click.ClickException(
            "tmux is required but not found on PATH. Install with `brew install tmux` or pass --no-tmux."
        )

    agents_paths = resolve_agents_config_paths(repo)
    adapter = get_adapter(agent, agents_paths)
    prompt = (
        "Use the generate-conventions skill to draft .workbench/conventions.md "
        "for this project. Survey the codebase, then write the file."
    )
    cmd = adapter.build_command(prompt, repo)
    if no_tmux:
        result = subprocess.run(cmd, cwd=str(repo))
        returncode = result.returncode
    else:
        returncode, _ = asyncio.run(run_in_tmux("wb-conventions-generate", cmd, repo))

    if returncode != 0:
        raise click.ClickException(f"Agent exited {returncode}; file was not written.")
    if not path.exists():
        raise click.ClickException("Agent exited 0 but did not write .workbench/conventions.md.")
    console.print(f"[green]Wrote[/green] {path.relative_to(repo)}")


@conventions.command("edit")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def conventions_edit(repo: Path | None):
    """Open .workbench/conventions.md in $EDITOR (creates from template if missing)."""
    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)
    path = conventions_path(repo)
    if not path.exists():
        path.write_text(TEMPLATE, encoding="utf-8")
    editor = os.environ.get("EDITOR", "vi")
    subprocess.call([editor, str(path)])


@conventions.command("show")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def conventions_show(repo: Path | None):
    """Print .workbench/conventions.md to stdout."""
    repo = repo or _find_repo_root()
    path = conventions_path(repo)
    if not path.exists():
        raise click.ClickException(
            f"{path.relative_to(repo)} does not exist. " "Run `wb conventions init` to create it."
        )
    click.echo(path.read_text(encoding="utf-8"), nl=False)


@conventions.command("delete")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def conventions_delete(yes: bool, repo: Path | None):
    """Remove .workbench/conventions.md (prompts unless --yes)."""
    repo = repo or _find_repo_root()
    path = conventions_path(repo)
    if not path.exists():
        console.print(f"{path.relative_to(repo)} does not exist; nothing to do.")
        return
    if not yes and not click.confirm(f"Delete {path.relative_to(repo)}?", default=False):
        console.print("Aborted.")
        return
    path.unlink()
    console.print(f"[green]Deleted[/green] {path.relative_to(repo)}")


@main.command()
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
def status(repo: Path | None):
    """Show active worktrees from workbench."""
    from .session_status import iter_status_files

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
    else:
        console.print(f"[bold]Active workbench worktrees ({len(wb_trees)}):[/bold]\n")
        for line in wb_trees:
            console.print(f"  {line}")

    # Show final review status per session
    for _path, data in iter_status_files(repo):
        sessions = data.get("sessions", {})
        for sb, session_data in sessions.items():
            if not isinstance(session_data, dict):
                continue
            reviews = session_data.get("final_reviews", [])
            if not reviews:
                console.print(f"\n  Final review ({sb}): [dim]not run[/dim]")
            else:
                latest = reviews[-1]
                verdict = latest.get("verdict", "unknown")
                verdict_style = {"pass": "green", "fail": "red", "error": "yellow"}.get(
                    verdict, "dim"
                )
                location = latest.get("pr_url") or latest.get(
                    "review_path", latest.get("report_path", "")
                )
                n = len(reviews)
                console.print(
                    f"\n  Final review ({sb}, {n} run(s), "
                    f"latest [{verdict_style}]{verdict.upper()}[/{verdict_style}]): "
                    f"{location}"
                )


def _list_workbench_worktrees(repo: Path) -> list[tuple[str, str | None]]:
    """Return [(path, branch_or_none), ...] for every workbench worktree.

    Uses ``iter_worktree_dirs`` (filesystem scan that covers both new
    ``.workbench/<plan>/<task>`` and legacy ``.workbench/<task>`` layouts)
    as the source of truth. Branch names are looked up via ``git worktree
    list --porcelain``.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    branch_map: dict[Path, str | None] = {}
    current_path: Path | None = None
    current_branch: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current_path is not None:
                try:
                    branch_map[current_path.resolve()] = current_branch
                except OSError:
                    branch_map[current_path] = current_branch
            current_path = Path(line.split("worktree ", 1)[1])
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line.split("branch refs/heads/", 1)[1]
    if current_path is not None:
        try:
            branch_map[current_path.resolve()] = current_branch
        except OSError:
            branch_map[current_path] = current_branch

    out: list[tuple[str, str | None]] = []
    for d in iter_worktree_dirs(repo):
        try:
            resolved = d.resolve()
        except OSError:
            resolved = d
        out.append((str(d), branch_map.get(resolved)))
    return out


def _list_ephemeral_worktrees(repo: Path) -> list[Path]:
    """Enumerate ephemeral agent worktrees that should never outlive a run.

    Includes:
      - .workbench/<plan>/wrap-up/<session_branch>/.review-wt    (branch reviewer)
      - .workbench/<plan>/wrap-up/<session_branch>/.pr-writer-wt (PR writer)
      - .workbench/<plan>/.review-wt/<session_branch>            (legacy reviewer)
      - .workbench/<plan>/.pr-writer-wt/<session_branch>         (legacy writer)
      - .workbench/_merge  (merge resolver, shared path)

    Returns absolute paths in deterministic (sorted) order.
    """
    wb = repo / ".workbench"
    if not wb.exists():
        return []
    paths: list[Path] = []
    for plan_dir in sorted(wb.iterdir()):
        if not plan_dir.is_dir():
            continue
        wrap_up = plan_dir / "wrap-up"
        if wrap_up.is_dir():
            for session_dir in sorted(wrap_up.iterdir()):
                if not session_dir.is_dir():
                    continue
                for sub in (".review-wt", ".pr-writer-wt"):
                    wt = session_dir / sub
                    if wt.is_dir():
                        paths.append(wt)
        for sub in (".review-wt", ".pr-writer-wt"):
            sub_dir = plan_dir / sub
            if sub_dir.is_dir():
                for child in sorted(sub_dir.iterdir()):
                    if child.is_dir():
                        paths.append(child)
    merge_dir = wb / "_merge"
    if merge_dir.exists():
        paths.append(merge_dir)
    return paths


def _remove_ephemeral_worktree(repo: Path, wt_path: Path) -> tuple[bool, str]:
    """Remove an ephemeral worktree. Returns (ok, message)."""
    result = subprocess.run(
        ["git", "worktree", "remove", str(wt_path), "--force"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, "removed"
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)
        if not wt_path.exists():
            return True, "removed (filesystem)"
    return False, result.stderr.strip() or "unknown error"


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
@click.argument("project", required=False, default=None)
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
    project: str | None,
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

    Pass PROJECT (the plan folder name under .workbench/) to scope cleanup to
    a single plan. The plan's worktrees, wb/* branches, status file, ephemeral
    agent worktrees, and (when empty after cleanup) the .workbench/<project>/
    directory itself are removed. The same --force / --completed / --remove-plans
    / --dry-run semantics apply, scoped to the project.
    """
    from .session_status import iter_status_files, status_file_branches, status_file_is_complete

    if force and completed:
        raise click.ClickException("--force and --completed are mutually exclusive")

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)

    if project is not None:
        from .path_resolver import is_plan_reference_path, plan_slug_from_path

        if is_plan_reference_path(project):
            project = plan_slug_from_path(Path(project))
        project_dir = repo / ".workbench" / project
        if not project_dir.is_dir():
            raise click.ClickException(
                f"No project folder at {project_dir.relative_to(repo)}. "
                "Pass the plan folder name (e.g. `wb clean my-plan`) "
                "or a path to the plan file (e.g. `wb clean .workbench/my-plan/plan.md`)."
            )

    # 1. Partition status files into completed vs incomplete.
    files = iter_status_files(repo)
    if project is not None:
        files = [(p, d) for p, d in files if _status_file_slug(p) == project]
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
    if project is not None:
        project_root = (repo / ".workbench" / project).resolve()
        worktrees = [(p, b) for p, b in worktrees if _path_inside(Path(p), project_root)]
        project_branches: set[str] = set()
        for _, data in files:
            project_branches |= status_file_branches(data)
        branches = [b for b in branches if b in project_branches]

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

    # 7. Ephemeral agent worktrees (branch reviewer / PR writer / merge resolver).
    ephemeral = _list_ephemeral_worktrees(repo)
    if project is not None:
        project_root = (repo / ".workbench" / project).resolve()
        ephemeral = [p for p in ephemeral if _path_inside(p, project_root)]
    if ephemeral:
        console.print("\n[bold]Ephemeral agent worktrees:[/bold]")
        for p in ephemeral:
            try:
                rel = p.relative_to(repo)
            except ValueError:
                rel = p
            console.print(f"  {rel}")
        if dry_run:
            console.print(f"[dim]Would remove {len(ephemeral)} ephemeral worktree(s).[/dim]")
        else:
            removed = 0
            for p in ephemeral:
                ok, msg = _remove_ephemeral_worktree(repo, p)
                mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
                try:
                    rel = p.relative_to(repo)
                except ValueError:
                    rel = p
                console.print(f"  {mark} {rel} — {msg}")
                if ok:
                    removed += 1
            console.print(f"[green]Removed {removed} ephemeral worktree(s).[/green]")

    # 8. Summary
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

    # 9. When scoped to a single project, drop the now-empty plan folder.
    if project is not None and not dry_run:
        project_dir = repo / ".workbench" / project
        if project_dir.is_dir() and not any(project_dir.iterdir()):
            project_dir.rmdir()
            console.print(f"{style} project folder {project_dir.relative_to(repo)}")


def _status_file_slug(path: Path) -> str:
    """Plan slug from a status file path (new or legacy layout)."""
    if path.name == "status.yaml":
        return path.parent.name
    return path.stem.removeprefix("status-")


def _path_inside(child: Path, parent: Path) -> bool:
    """True if `child` (resolved) is at or under `parent` (already resolved)."""
    try:
        return child.resolve().is_relative_to(parent)
    except (OSError, ValueError):
        return False


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

        removed = 0
        for d in iter_worktree_dirs(repo):
            subprocess.run(
                ["git", "worktree", "remove", str(d), "--force"],
                cwd=repo,
                capture_output=True,
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
    help="Agent CLI for merge conflict resolution (claude, antigravity, codex, cursor, or custom).",
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
@click.option(
    "--review",
    "run_review",
    is_flag=True,
    help="Run a final review after merging.",
)
@click.option(
    "--max-retries",
    type=int,
    default=2,
    show_default=True,
    help="Max fix attempts after a failed review.",
)
@click.option("--pr-title", default=None, type=str, help="Override the PR title.")
@click.option(
    "--pr-body-file",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a file whose content becomes the PR body.",
)
@click.option("--pr-base", default=None, type=str, help="Override the PR base branch.")
@click.option("--skip-pr", is_flag=True, help="Skip PR creation even on PASS verdict.")
@click.option(
    "--summarizer-directive",
    default=None,
    type=str,
    help="Override the requirements summarizer agent's instructions.",
)
@click.option(
    "--branch-reviewer-directive",
    default=None,
    type=str,
    help="Override the branch reviewer agent's instructions.",
)
@click.option(
    "--pr-writer-directive",
    default=None,
    type=str,
    help="Override the PR writer agent's instructions.",
)
@click.option(
    "--trace/--no-trace",
    "trace",
    default=True,
    help="Master switch for session trace metadata (env + prompt). Default on.",
)
@click.option(
    "--trace-env/--no-trace-env",
    "trace_env",
    default=True,
    help="Inject WB_*/OTEL trace env vars into agent processes. Default on.",
)
@click.option(
    "--trace-prompt/--no-trace-prompt",
    "trace_prompt",
    default=False,
    help="Prepend a wb-session metadata block to agent prompts. Default off.",
)
def merge(
    session_branch: str,
    plan_path: Path | None,
    agent: str,
    repo: Path | None,
    no_tmux: bool,
    keep_branches: bool,
    push: bool,
    run_review: bool,
    max_retries: int,
    pr_title: str | None,
    pr_body_file: Path | None,
    pr_base: str | None,
    skip_pr: bool,
    summarizer_directive: str | None,
    branch_reviewer_directive: str | None,
    pr_writer_directive: str | None,
    trace: bool,
    trace_env: bool,
    trace_prompt: bool,
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
      wb merge -b workbench-1 --agent antigravity
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
    plan = None
    if plan_path:
        try:
            plan = parse_plan(plan_path.resolve())
        except ValueError as e:
            raise click.ClickException(str(e))
        plan_slug = plan.folder_id

    # Pass `None` so merge_unmerged resolves agents_config_paths once it has
    # the canonical plan slug (when --plan is omitted, that slug is only
    # known after the status file is loaded).
    status = asyncio.run(
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

    # Final review after merge
    if run_review:
        merged_count = sum(1 for rec in status.tasks.values() if rec.merged)
        if merged_count > 0:
            # Load plan for task titles
            plan_source_path = Path(status.plan_source) if status.plan_source else None
            if plan is None and plan_source_path and plan_source_path.exists():
                try:
                    plan = parse_plan(plan_source_path.resolve())
                except ValueError:
                    plan = None

            if plan is None:
                console.print(
                    "[yellow]Cannot run final review: plan source not available.[/yellow]"
                )
            else:
                merged_tasks = {tid for tid, rec in status.tasks.items() if rec.merged}
                merged_titles = [t.title for t in plan.tasks if t.id in merged_tasks]
                if merged_titles:
                    # Derive base_branch from plan frontmatter or default
                    base_branch = plan.run_config.get("base", "main")
                    review_args = _resolve_final_review_args(
                        repo=repo,
                        session_branch=session_branch,
                        plan_slug=status.plan_slug,
                        base_branch=base_branch,
                        plan_source=plan.source,
                        merged_task_titles=merged_titles,
                        agent=agent,
                        no_tmux=no_tmux,
                        profile=None,
                        summarizer_directive=summarizer_directive,
                        branch_reviewer_directive=branch_reviewer_directive,
                        pr_writer_directive=pr_writer_directive,
                        pr_title=pr_title,
                        pr_body_file=pr_body_file,
                        pr_base=pr_base,
                        skip_pr=skip_pr,
                    )
                    review_args["agents_config_paths"] = resolve_agents_config_paths(
                        repo, plan_slug=status.plan_slug
                    )
                    review_args["max_retries"] = max_retries
                    review_args["trace_env"] = trace and trace_env
                    review_args["trace_prompt"] = trace and trace_prompt
                    record = asyncio.run(run_final_review(**review_args))
                    _print_final_review_result(record)


@main.command("final-review")
@click.argument("session_branch")
@click.option(
    "--repo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Repo path (default: auto-detect).",
)
@click.option(
    "--agent",
    default="claude",
    help="Agent CLI to dispatch.",
)
@click.option("--no-tmux", is_flag=True, help="Run without tmux.")
@click.option(
    "--max-retries",
    type=int,
    default=2,
    show_default=True,
    help="Max fix attempts after a failed review.",
)
@click.option("--pr-title", default=None, type=str, help="Override the PR title.")
@click.option(
    "--pr-body-file",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a file whose content becomes the PR body.",
)
@click.option("--pr-base", default=None, type=str, help="Override the PR base branch.")
@click.option("--skip-pr", is_flag=True, help="Skip PR creation even on PASS verdict.")
@click.option(
    "--summarizer-directive",
    default=None,
    type=str,
    help="Override the requirements summarizer agent's instructions.",
)
@click.option(
    "--branch-reviewer-directive",
    default=None,
    type=str,
    help="Override the branch reviewer agent's instructions.",
)
@click.option(
    "--pr-writer-directive",
    default=None,
    type=str,
    help="Override the PR writer agent's instructions.",
)
@click.option(
    "--profile",
    "profile_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a profile.yaml to use.",
)
@click.option("--profile-name", default=None, help="Named profile to resolve.")
@click.option(
    "--trace/--no-trace",
    "trace",
    default=True,
    help="Master switch for session trace metadata (env + prompt). Default on.",
)
@click.option(
    "--trace-env/--no-trace-env",
    "trace_env",
    default=True,
    help="Inject WB_*/OTEL trace env vars into agent processes. Default on.",
)
@click.option(
    "--trace-prompt/--no-trace-prompt",
    "trace_prompt",
    default=False,
    help="Prepend a wb-session metadata block to agent prompts. Default off.",
)
def final_review_cmd(
    session_branch: str,
    repo: Path | None,
    agent: str,
    no_tmux: bool,
    max_retries: int,
    pr_title: str | None,
    pr_body_file: Path | None,
    pr_base: str | None,
    skip_pr: bool,
    summarizer_directive: str | None,
    branch_reviewer_directive: str | None,
    pr_writer_directive: str | None,
    profile_path: Path | None,
    profile_name: str | None,
    trace: bool,
    trace_env: bool,
    trace_prompt: bool,
):
    """Run a final whole-branch review for a session.

    \b
    Looks up the session status, loads the plan, and runs the two-agent
    review sequence (requirements summarizer + branch reviewer).

    \b
    Example:
      wb final-review workbench-1
      wb final-review workbench-1 --skip-pr
      wb final-review workbench-1 --pr-title "My feature"
    """
    if not no_tmux and not check_tmux_available():
        raise click.ClickException(
            "tmux is required but not found on PATH. "
            "Install with: brew install tmux (macOS) or apt install tmux (Linux). "
            "Or use --no-tmux to run without it."
        )

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)

    found = SessionStatus.find_by_session(repo, session_branch)
    if found is None:
        raise click.ClickException(
            f"No status file found for session '{session_branch}'. "
            f"Run 'wb run' first to create a session."
        )

    if not found.plan_source:
        raise click.ClickException(
            f"Session '{session_branch}' has no plan_source recorded. "
            f"Cannot determine the plan to review."
        )

    plan_source = Path(found.plan_source)
    if not plan_source.exists():
        raise click.ClickException(
            f"Plan source not found: {plan_source}\n"
            f"The plan file may have been moved or deleted."
        )

    try:
        plan = parse_plan(plan_source.resolve())
    except ValueError as e:
        raise click.ClickException(f"Failed to parse plan: {e}")

    # Compute merged task titles
    merged_tasks = {tid for tid, rec in found.tasks.items() if rec.merged}
    merged_titles = [t.title for t in plan.tasks if t.id in merged_tasks]

    if not merged_titles:
        raise click.ClickException(
            f"No merged tasks found for session '{session_branch}'. "
            f"Merge tasks first with 'wb merge -b {session_branch}'."
        )

    # Derive base_branch from plan frontmatter or default
    base_branch = plan.run_config.get("base", "main")

    profile_paths_for_review = resolve_profile_paths(
        repo,
        plan_slug=found.plan_slug,
        explicit_path=profile_path,
        name=profile_name,
    )
    profile_obj = Profile.from_layered_yaml(list(reversed(profile_paths_for_review)))
    agents_paths_for_review = resolve_agents_config_paths(repo, plan_slug=found.plan_slug)

    review_args = _resolve_final_review_args(
        repo=repo,
        session_branch=session_branch,
        plan_slug=found.plan_slug,
        base_branch=base_branch,
        plan_source=plan_source.resolve(),
        merged_task_titles=merged_titles,
        agent=agent,
        no_tmux=no_tmux,
        profile=profile_obj,
        summarizer_directive=summarizer_directive,
        branch_reviewer_directive=branch_reviewer_directive,
        pr_writer_directive=pr_writer_directive,
        pr_title=pr_title,
        pr_body_file=pr_body_file,
        pr_base=pr_base,
        skip_pr=skip_pr,
    )
    review_args["agents_config_paths"] = agents_paths_for_review
    review_args["max_retries"] = max_retries
    review_args["trace_env"] = trace and trace_env
    review_args["trace_prompt"] = trace and trace_prompt
    record = asyncio.run(run_final_review(**review_args))
    _print_final_review_result(record)


main.add_command(final_review_cmd, name="review")


# ---------------------------------------------------------------------------
# wb pull-request
# ---------------------------------------------------------------------------


def _resolve_plan_slug(plan: str) -> str:
    """Normalize a plan argument to a plan slug.

    Accepts a slug, a path to a plan file, or a path to a plan folder.
    For a file: returns the parent directory name.
    For a folder: returns the folder name.
    For a non-path string: returns it unchanged.
    """
    p = Path(plan)
    if p.exists():
        if p.is_file():
            return p.parent.name
        return p.name
    return plan


def _load_session_for_plan(
    repo: Path, plan_slug: str, session_branch: str | None
) -> SessionStatus:
    """Load the SessionStatus for the given plan_slug + optional session.

    Parses the status YAML once (avoiding a redundant second read inside
    SessionStatus.load) and raises click.ClickException for ambiguous /
    missing sessions.
    """
    status_path = SessionStatus.path_for(repo, plan_slug)
    legacy_path = SessionStatus.legacy_path_for(repo, plan_slug)
    raw_path = status_path if status_path.exists() else legacy_path
    if not raw_path.exists():
        raise click.ClickException(
            f"No status file found for plan '{plan_slug}'. Run 'wb run' first."
        )

    data = yaml.safe_load(raw_path.read_text()) or {}
    sessions = data.get("sessions") or {}
    session_keys = list(sessions.keys())

    if not session_keys:
        raise click.ClickException(
            f"No sessions found for plan '{plan_slug}'. Run 'wb run' first."
        )

    if session_branch is not None:
        if session_branch not in session_keys:
            raise click.ClickException(
                f"Session '{session_branch}' not found in plan '{plan_slug}'. "
                f"Available sessions: {', '.join(session_keys)}."
            )
        chosen = session_branch
    elif len(session_keys) == 1:
        chosen = session_keys[0]
    else:
        raise click.ClickException(
            f"Plan '{plan_slug}' has multiple sessions: {', '.join(session_keys)}. "
            f"Use -b <session> to choose."
        )

    # Build the SessionStatus from the already-parsed YAML rather than
    # re-reading the file via SessionStatus.load().
    session_data = sessions.get(chosen) or {}
    from .session_status import FinalReviewRecord, TaskRecord

    tasks = {tid: TaskRecord.from_dict(rec) for tid, rec in session_data.get("tasks", {}).items()}
    final_reviews = [FinalReviewRecord.from_dict(r) for r in session_data.get("final_reviews", [])]
    return SessionStatus(
        plan_slug=plan_slug,
        session_branch=chosen,
        plan_source=data.get("plan_source", ""),
        tasks=tasks,
        final_reviews=final_reviews,
    )


def _collect_merged_titles(status: SessionStatus, plan) -> list[str]:
    """Return titles of merged tasks by intersecting status.tasks with plan.tasks.

    Accepts a parsed Plan object (so callers can parse once).
    """
    merged_ids = {tid for tid, rec in status.tasks.items() if rec.merged}
    return [t.title for t in plan.tasks if t.id in merged_ids]


def _resolve_base_from_plan(plan) -> str:
    """Return the plan's frontmatter ``base`` or ``main``."""
    return plan.run_config.get("base", "main") or "main"


@main.command("pull-request")
@click.argument("plan", type=str)
@click.option(
    "-b",
    "--session-branch",
    default=None,
    help="Session branch (required if the plan has multiple sessions).",
)
@click.option(
    "--repo",
    "repo",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Repo path (default: auto-detect).",
)
@click.option("--agent", default="claude", help="Agent CLI to dispatch.")
@click.option("--no-tmux", "no_tmux", is_flag=True, help="Run without tmux.")
@click.option("--pr-title", default=None, type=str, help="Override the agent-generated title.")
@click.option(
    "--pr-body-file",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Skip the PR writer and use this file's content as the body.",
)
@click.option(
    "--pr-base",
    default=None,
    type=str,
    help="Override the PR base branch (default: session's recorded base).",
)
@click.option(
    "--pr-writer-directive",
    default=None,
    type=str,
    help="Override the PR writer agent's instructions.",
)
@click.option(
    "--profile",
    "profile_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a profile.yaml.",
)
@click.option("--profile-name", default=None, help="Named profile to resolve.")
def pull_request_cmd(
    plan: str,
    session_branch: str | None,
    repo: Path | None,
    agent: str,
    no_tmux: bool,
    pr_title: str | None,
    pr_body_file: Path | None,
    pr_base: str | None,
    pr_writer_directive: str | None,
    profile_path: Path | None,
    profile_name: str | None,
):
    """Open a GitHub PR for a session, with an AI-written description.

    \b
    Looks up the session for the given plan and runs the PR writer agent
    over the diff and plan context. Opens a PR via 'gh pr create'.

    \b
    Example:
      wb pull-request myfeature
      wb pull-request myfeature -b workbench-3
      wb pull-request myfeature --pr-title "Custom title"
    """
    if not no_tmux and not check_tmux_available():
        raise click.ClickException(
            "tmux is required but not found on PATH. "
            "Install with: brew install tmux (macOS) or apt install tmux (Linux). "
            "Or use --no-tmux to run without it."
        )

    repo = repo or _find_repo_root()
    _ensure_workbench_dir(repo)

    plan_slug = _resolve_plan_slug(plan)
    status = _load_session_for_plan(repo, plan_slug, session_branch)
    session = status.session_branch

    if not status.plan_source:
        raise click.ClickException(
            f"Session '{session}' has no plan_source recorded. "
            f"Cannot determine the plan to use."
        )

    plan_source = Path(status.plan_source)
    if not plan_source.exists():
        raise click.ClickException(
            f"Plan source not found: {plan_source}\n"
            f"The plan file may have been moved or deleted."
        )

    # Parse the plan once; on malformed frontmatter fall back to safe defaults
    # so the command can still produce a PR rather than stack-tracing.
    try:
        plan_parsed = parse_plan(plan_source.resolve())
        merged_titles = _collect_merged_titles(status, plan_parsed)
        base_branch = _resolve_base_from_plan(plan_parsed)
    except Exception as e:  # noqa: BLE001 — parser surface is broad
        console.print(
            f"[yellow]Could not parse plan {plan_source} ({type(e).__name__}: {e}); "
            f"falling back to base='main' and empty merged-task list[/yellow]"
        )
        merged_titles = []
        base_branch = "main"

    profile_paths_for_pr = resolve_profile_paths(
        repo,
        plan_slug=plan_slug,
        explicit_path=profile_path,
        name=profile_name,
    )
    profile_obj = Profile.from_layered_yaml(list(reversed(profile_paths_for_pr)))
    agents_config_paths = resolve_agents_config_paths(repo, plan_slug=plan_slug)
    plan_text = plan_source.read_text(encoding="utf-8")

    if pr_body_file is not None:
        title = pr_title or derive_title_from_plan(plan_text, plan_slug)
        body = pr_body_file.read_text(encoding="utf-8")
    else:
        try:
            agent_title, agent_body = asyncio.run(
                run_pr_writer(
                    repo=repo,
                    session_branch=session,
                    plan_slug=plan_slug,
                    base_branch=base_branch,
                    plan_source=plan_source,
                    merged_task_titles=merged_titles,
                    agent_cmd=agent,
                    use_tmux=not no_tmux,
                    profile=profile_obj,
                    directive_override=pr_writer_directive,
                    agents_config_paths=agents_config_paths,
                )
            )
            title = pr_title or agent_title
            body = agent_body
        except PrWriterError as e:
            console.print(
                f"[yellow]PR writer failed ({type(e).__name__}: {e}); "
                f"falling back to plan-derived PR body[/yellow]"
            )
            title = pr_title or derive_title_from_plan(plan_text, plan_slug)
            body = derive_body_from_plan(plan_text, merged_titles, review_path=None)

    base = pr_base or base_branch
    push_ok, push_msg = push_session_branch(repo, session)
    if not push_ok:
        console.print(f"[yellow]Could not push {session} before opening PR: {push_msg}[/yellow]")
    success, message = asyncio.run(create_pr(repo, session, base, title, body))

    console.print(f"[bold]Title:[/bold] {title}")
    if success:
        console.print(f"[green]PR opened:[/green] {message}")
    else:
        console.print(f"[yellow]Could not open PR.[/yellow] Run manually:\n  {message}")


@main.command()
@click.option(
    "--agent",
    type=click.Choice(["claude", "antigravity", "cursor", "codex", "copilot", "manual"]),
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
    "--agent",
    type=click.Choice(["claude", "antigravity", "cursor", "codex", "manual"]),
    default=None,
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
    help="Set role fields inline (e.g. --set reviewer.agent=antigravity).",
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
    """Set a profile field (e.g. reviewer.agent antigravity, tester.tdd.directive 'text')."""
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
    "antigravity": "Google Antigravity (agy)",
    "codex": "Codex CLI (OpenAI)",
    "cursor": "Cursor CLI (agent command)",
}


def _resolve_agents_yaml_path(repo: Path, plan_slug: str | None) -> Path:
    if plan_slug:
        return repo / ".workbench" / plan_slug / "agents.yaml"
    return repo / ".workbench" / "agents.yaml"


def _load_agents_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _save_agents_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False))


@main.group()
@click.option(
    "--plan",
    "plan_slug",
    type=str,
    default=None,
    help="Target a plan's agents.yaml (.workbench/<plan>/agents.yaml).",
)
@click.pass_context
def agents(ctx, plan_slug):
    """Manage agent adapters (.workbench/agents.yaml)."""
    ctx.ensure_object(dict)
    ctx.obj["plan_slug"] = plan_slug


@agents.command("init")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.pass_context
def agents_init(ctx, repo: Path | None):
    """Create agents.yaml with the default built-in agent configs."""
    from .adapters import default_agents_config

    repo = repo or _find_repo_root()
    plan_slug = ctx.obj.get("plan_slug")

    if plan_slug:
        plan_dir = repo / ".workbench" / plan_slug
        if not plan_dir.exists():
            raise click.ClickException(f"Plan directory does not exist: {plan_dir}")

    config_path = _resolve_agents_yaml_path(repo, plan_slug)

    if config_path.exists():
        if not click.confirm(f"{config_path} already exists. Overwrite?"):
            return

    data = {"agents": default_agents_config()}
    _save_agents_yaml(config_path, data)
    console.print(f"Created {config_path} with {len(data['agents'])} agent(s)")


@agents.command("list")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.pass_context
def agents_list(ctx, repo: Path | None):
    """List built-in and configured agents."""
    repo = repo or _find_repo_root()
    plan_slug = ctx.obj.get("plan_slug")
    config_path = _resolve_agents_yaml_path(repo, plan_slug)
    custom = _load_agents_yaml(config_path).get("agents", {})

    console.print("[bold]Built-in agents:[/bold]")
    for name, desc in BUILTIN_AGENTS.items():
        console.print(f"  {name:<12} {desc}")

    if custom:
        if plan_slug:
            label = f"Plan agents ({plan_slug})"
        else:
            label = "Custom agents"
        console.print(f"\n[bold]{label}[/bold] ({config_path}):")
        for name, entry in custom.items():
            cmd = entry.get("command", name)
            fmt = entry.get("output_format", "text")
            console.print(f"  {name:<12} command={cmd}  format={fmt}")
    else:
        console.print(f"\n[dim]No custom agents configured.[/dim]")


@agents.command("show")
@click.argument("name")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), default=None)
@click.pass_context
def agents_show(ctx, name: str, repo: Path | None):
    """Show details for an agent adapter."""
    repo = repo or _find_repo_root()
    plan_slug = ctx.obj.get("plan_slug")

    if name in BUILTIN_AGENTS:
        console.print(f"[bold]{name}[/bold] (built-in)")
        console.print(f"  Description: {BUILTIN_AGENTS[name]}")
        console.print(f"  Type: built-in adapter")
        return

    config_path = _resolve_agents_yaml_path(repo, plan_slug)
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
@click.pass_context
def agents_add(
    ctx,
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
    plan_slug = ctx.obj.get("plan_slug")
    config_path = _resolve_agents_yaml_path(repo, plan_slug)
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
@click.pass_context
def agents_remove(ctx, name: str, repo: Path | None):
    """Remove a custom agent adapter."""
    repo = repo or _find_repo_root()
    plan_slug = ctx.obj.get("plan_slug")
    config_path = _resolve_agents_yaml_path(repo, plan_slug)
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
