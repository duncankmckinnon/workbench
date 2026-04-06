"""Orchestrator - coordinates parallel agent work across tasks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .agents import AgentResult, Role, TaskStatus, run_merge_resolver, run_pipeline
from .plan_parser import Plan, Task
from .profile import Profile
from .session_status import SessionStatus
from .worktree import (
    Worktree,
    cleanup_merge_worktree,
    complete_merge,
    create_session_branch,
    create_worktree,
    delete_branch,
    get_main_branch,
    get_merged_branches,
    merge_into_session,
)


@dataclass
class TaskState:
    task: Task
    worktree: Worktree | None = None
    status: TaskStatus = TaskStatus.PENDING
    results: list[AgentResult] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def elapsed(self) -> str:
        if self.started_at is None:
            return "-"
        end = self.finished_at or time.time()
        mins = int(end - self.started_at) // 60
        secs = int(end - self.started_at) % 60
        return f"{mins}m{secs:02d}s"

    @property
    def fix_count(self) -> int:
        """How many fix cycles have run."""
        return sum(1 for r in self.results if r.role == Role.FIXER)

    @property
    def phase_summary(self) -> str:
        """Short summary of where we are in the pipeline."""
        if not self.results:
            return ""

        phases = []
        for r in self.results:
            if r.role == Role.IMPLEMENTOR:
                phases.append("impl:ok" if r.status != TaskStatus.FAILED else "impl:fail")
            elif r.role == Role.TESTER:
                if r.passed:
                    phases.append("test:pass")
                elif r.status == TaskStatus.FAILED:
                    phases.append("test:crash")
                else:
                    phases.append(f"test:fail")
            elif r.role == Role.FIXER:
                phases.append("fix" if r.status != TaskStatus.FAILED else "fix:fail")
            elif r.role == Role.REVIEWER:
                if r.passed:
                    phases.append("review:pass")
                elif r.status == TaskStatus.FAILED:
                    phases.append("review:crash")
                else:
                    phases.append(f"review:fail")

        return " → ".join(phases)


def _status_table(states: list[TaskState]) -> Table:
    table = Table(title="Workbench", show_lines=True)
    table.add_column("Task", style="bold", min_width=30)
    table.add_column("Status", min_width=14)
    table.add_column("Fixes", min_width=5, justify="center")
    table.add_column("Time", min_width=8)
    table.add_column("Pipeline", min_width=40)

    status_styles = {
        TaskStatus.PENDING: "dim",
        TaskStatus.IMPLEMENTING: "yellow",
        TaskStatus.TESTING: "cyan",
        TaskStatus.REVIEWING: "magenta",
        TaskStatus.FIXING: "yellow bold",
        TaskStatus.MERGING: "blue bold",
        TaskStatus.DONE: "green",
        TaskStatus.FAILED: "red bold",
    }

    for s in states:
        style = status_styles.get(s.status, "")
        fixes = str(s.fix_count) if s.fix_count > 0 else "-"
        pipeline = s.phase_summary or (f"branch: {s.worktree.branch}" if s.worktree else "")

        table.add_row(
            s.task.title,
            Text(s.status.value, style=style),
            fixes,
            s.elapsed,
            pipeline,
        )

    return table


async def run_plan(
    plan: Plan,
    repo: Path,
    max_concurrent: int = 4,
    max_retries: int = 2,
    skip_test: bool = False,
    skip_review: bool = False,
    agent_cmd: str = "claude",
    cleanup_on_done: bool = False,
    session_branch: str | None = None,
    start_wave: int = 1,
    use_tmux: bool = True,
    directives: dict[Role, str] | None = None,
    tdd: bool = False,
    local: bool = False,
    base_branch: str | None = None,
    profile_path: Path | None = None,
    profile_name: str | None = None,
    session_name: str | None = None,
    keep_branches: bool = False,
    retry_failed: bool = False,
    fail_fast: bool = False,
    only_failed: bool = False,
    task_filter: set[str] | None = None,
) -> list[TaskState]:
    """Execute a plan with parallel agent workers."""
    console = Console()
    profile = Profile.resolve(repo, profile_path=profile_path, profile_name=profile_name)
    waves = plan.independent_groups
    all_states: list[TaskState] = []
    state_map: dict[str, TaskState] = {}

    # Use existing session branch or create a new one
    if session_branch is None:
        session_branch = create_session_branch(
            repo, local=local, base=base_branch, session_name=session_name
        )

    # Initialize session status tracking
    plan_slug = plan.slug
    plan_source = str(plan.source)
    session_status = SessionStatus(
        plan_slug=plan_slug,
        session_branch=session_branch,
        plan_source=plan_source,
    )

    # Resolve --task filter: accept task IDs or slugs
    filtered_task_ids: set[str] | None = None
    if task_filter:
        filtered_task_ids = set()
        for task in plan.tasks:
            if task.id in task_filter or task.slug in task_filter:
                filtered_task_ids.add(task.id)
        unmatched = task_filter - filtered_task_ids - {t.slug for t in plan.tasks}
        if unmatched:
            console.print(f"[yellow]Warning: no tasks matched: {', '.join(unmatched)}[/yellow]")

    # Load prior session status to carry forward records for non-targeted tasks
    prior = SessionStatus.load(repo, plan_slug, session_branch)
    if prior:
        for tid, rec in prior.tasks.items():
            if filtered_task_ids is None or tid not in filtered_task_ids:
                # Carry forward — this task is not being re-run
                session_status.tasks[tid] = rec

    # --only-failed: skip completed tasks
    skipped_task_ids: set[str] = set()
    if only_failed and prior:
        skipped_task_ids = prior.completed_task_ids()
        if skipped_task_ids:
            console.print(
                f"[dim]--only-failed: skipping {len(skipped_task_ids)} completed task(s)[/dim]"
            )

    console.print(f"\n[bold]Plan:[/bold] {plan.title}")
    console.print(f"[bold]Tasks:[/bold] {len(plan.tasks)} across {len(waves)} wave(s)")
    console.print(f"[bold]Concurrency:[/bold] {max_concurrent}")
    console.print(f"[bold]Max retries:[/bold] {max_retries}")
    console.print(f"[bold]Repo:[/bold] {repo}")
    console.print(f"[bold]Session branch:[/bold] {session_branch}")
    console.print(f"[bold]tmux:[/bold] {'enabled' if use_tmux else 'disabled'}")
    if retry_failed:
        console.print("[bold]Retry failed:[/bold] enabled (one retry pass per wave)")
    if fail_fast:
        console.print("[bold]Fail fast:[/bold] enabled (stop on first wave with failures)")
    if filtered_task_ids is not None:
        names = [t.title for t in plan.tasks if t.id in filtered_task_ids]
        console.print(f"[bold]Task filter:[/bold] {', '.join(names)}")
    if tdd:
        console.print("[bold]Mode:[/bold] test-driven development (tests first)")
    if directives:
        for role, _ in directives.items():
            console.print(f"[bold]Custom directive:[/bold] {role.value}")
    for role in Role:
        rc = getattr(profile, role.value)
        if rc.agent != "claude":
            console.print(f"[bold]{role.value}:[/bold] using {rc.agent}")
    console.print()

    for wave_idx, wave in enumerate(waves):
        wave_num = wave_idx + 1

        # Skip waves before start_wave
        if wave_num < start_wave:
            console.print(
                f"[dim]━━━ Wave {wave_num}/{len(waves)} ({len(wave)} tasks) — skipped (already merged) ━━━[/dim]\n"
            )
            for task in wave:
                state = TaskState(task=task)
                state.status = TaskStatus.DONE
                all_states.append(state)
                state_map[task.id] = state
            continue

        console.print(
            f"[bold cyan]━━━ Wave {wave_num}/{len(waves)} ({len(wave)} tasks) ━━━[/bold cyan]\n"
        )

        # Initialize state for this wave
        # --task: filtered-out tasks are excluded entirely
        # --only-failed: completed tasks are pre-marked DONE
        wave_states: list[TaskState] = []
        for task in wave:
            if filtered_task_ids is not None and task.id not in filtered_task_ids:
                continue
            state = TaskState(task=task)
            if task.id in skipped_task_ids:
                state.status = TaskStatus.DONE
            wave_states.append(state)
            all_states.append(state)
            state_map[task.id] = state

        # Create worktrees for all tasks in wave, branching from session branch
        # Skip tasks pre-marked DONE by --only-failed
        for state in wave_states:
            if state.status == TaskStatus.DONE:
                continue
            # Clean up existing worktree/branch from a prior run (e.g. --task re-run)
            old_worktree_path = repo / ".workbench" / state.task.id
            if old_worktree_path.exists():
                Worktree(
                    path=old_worktree_path,
                    branch=f"wb/{state.task.slug}",
                    task_id=state.task.id,
                ).cleanup()
            else:
                delete_branch(repo, f"wb/{state.task.slug}")
            try:
                wt = create_worktree(
                    repo, state.task.id, state.task.slug, base_branch=session_branch
                )
                state.worktree = wt
            except Exception as e:
                state.status = TaskStatus.FAILED
                state.results.append(
                    AgentResult(
                        task_id=state.task.id,
                        role=Role.IMPLEMENTOR,
                        status=TaskStatus.FAILED,
                        output=f"Worktree creation failed: {e}",
                    )
                )

        # Status callback so the pipeline can update our display state
        def _make_callback(state: TaskState):
            def _on_status(task_id: str, status: TaskStatus):
                state.status = status

            return _on_status

        # Run tasks concurrently with semaphore
        sem = asyncio.Semaphore(max_concurrent)

        async def _run_task(state: TaskState):
            if state.status in (TaskStatus.FAILED, TaskStatus.DONE):
                return

            async with sem:
                state.started_at = time.time()
                state.status = TaskStatus.IMPLEMENTING

                results = await run_pipeline(
                    task=state.task,
                    worktree=state.worktree,
                    repo=repo,
                    skip_test=skip_test,
                    skip_review=skip_review,
                    max_retries=max_retries,
                    agent_cmd=agent_cmd,
                    on_status_change=_make_callback(state),
                    session_branch=session_branch,
                    plan_context=plan.context,
                    plan_conventions=plan.conventions,
                    directives=directives,
                    use_tmux=use_tmux,
                    tdd=tdd,
                    profile=profile,
                )

                state.results = results
                state.finished_at = time.time()

                # Final status based on last result
                if any(
                    r.role in (Role.TESTER, Role.REVIEWER) and not r.passed and r == results[-1]
                    for r in results
                ):
                    state.status = TaskStatus.FAILED
                elif any(r.status == TaskStatus.FAILED for r in results):
                    state.status = TaskStatus.FAILED
                else:
                    state.status = TaskStatus.DONE

                # Persist task outcome immediately (lock-protected for concurrency)
                await session_status.update_task(
                    repo=repo,
                    task_id=state.task.id,
                    status=state.status.value,
                    branch=state.worktree.branch if state.worktree else None,
                    last_agent=results[-1].role.value if results else "",
                )

        # Run with live status display
        tasks = [_run_task(s) for s in wave_states]

        with Live(_status_table(all_states), console=console, refresh_per_second=1) as live:

            async def _update_display():
                while not all(
                    s.status in (TaskStatus.DONE, TaskStatus.FAILED) for s in wave_states
                ):
                    live.update(_status_table(all_states))
                    await asyncio.sleep(1)
                live.update(_status_table(all_states))

            await asyncio.gather(*tasks, _update_display())

        # After the wave: merge all successful task branches into the session branch
        # Exclude tasks that were pre-skipped by --only-failed (no worktree)
        done_states = [
            s for s in wave_states if s.status == TaskStatus.DONE and s.worktree is not None
        ]
        if done_states:
            console.print(
                f"\n[bold]Merging {len(done_states)} branch(es) into {session_branch}...[/bold]\n"
            )

            for state in done_states:
                result = merge_into_session(repo, session_branch, state.worktree.branch)
                if result.success:
                    console.print(f"  [green]✓[/green] {state.worktree.branch} — {result.message}")
                    await session_status.update_merged(repo, state.task.id)
                    if not keep_branches:
                        delete_branch(repo, state.worktree.branch)
                elif result.conflicts and result.merge_dir:
                    # Conflicts detected — dispatch merge resolver agent
                    console.print(
                        f"  [blue]⚡[/blue] {state.worktree.branch} — "
                        f"{result.message} Resolving..."
                    )
                    for cf in result.conflicts:
                        console.print(f"      [dim]{cf}[/dim]")

                    state.status = TaskStatus.MERGING

                    resolver_result = await run_merge_resolver(
                        task_branch=state.worktree.branch,
                        session_branch=session_branch,
                        merge_dir=result.merge_dir,
                        conflicts=result.conflicts,
                        repo=repo,
                        agent_cmd=agent_cmd,
                    )
                    state.results.append(resolver_result)

                    if resolver_result.passed:
                        # Resolver succeeded — complete the merge
                        merge_finish = complete_merge(
                            result.merge_dir,
                            repo,
                            session_branch,
                            state.worktree.branch,
                        )
                        if merge_finish.success:
                            console.print(
                                f"  [green]✓[/green] {state.worktree.branch} — "
                                f"{merge_finish.message}"
                            )
                            state.status = TaskStatus.DONE
                            await session_status.update_merged(repo, state.task.id)
                            if not keep_branches:
                                delete_branch(repo, state.worktree.branch)
                        else:
                            console.print(
                                f"  [red]✗[/red] {state.worktree.branch} — "
                                f"{merge_finish.message}"
                            )
                            cleanup_merge_worktree(repo, result.merge_dir)
                            state.status = TaskStatus.FAILED
                    else:
                        # Resolver failed — abort and mark failed
                        console.print(
                            f"  [red]✗[/red] {state.worktree.branch} — " f"Merge resolver failed"
                        )
                        cleanup_merge_worktree(repo, result.merge_dir)
                        state.status = TaskStatus.FAILED
                else:
                    console.print(f"  [red]✗[/red] {state.worktree.branch} — {result.message}")
                    if result.conflicts:
                        for cf in result.conflicts:
                            console.print(f"      [dim]{cf}[/dim]")
                    state.status = TaskStatus.FAILED

            console.print()

        # --retry-failed: re-run tasks that failed due to transient errors.
        # A task is retryable if it crashed before exhausting its fix cycles
        # (i.e. fix_count < max_retries). Tasks that went through all retries
        # and still failed need human intervention, not another blind run.
        if retry_failed:
            retryable = [
                s
                for s in wave_states
                if s.status == TaskStatus.FAILED
                and s.worktree is not None
                and s.fix_count < max_retries
            ]

            if retryable:
                console.print(
                    f"[bold yellow]Retrying {len(retryable)} failed task(s)...[/bold yellow]\n"
                )

                # Reset state for retry
                for state in retryable:
                    state.status = TaskStatus.PENDING
                    state.results.clear()
                    state.started_at = None
                    state.finished_at = None

                    # Clean up old worktree and branch, create fresh ones
                    if state.worktree:
                        state.worktree.cleanup()
                        state.worktree = None
                    try:
                        wt = create_worktree(
                            repo,
                            state.task.id,
                            state.task.slug,
                            base_branch=session_branch,
                        )
                        state.worktree = wt
                    except Exception as e:
                        state.status = TaskStatus.FAILED
                        state.results.append(
                            AgentResult(
                                task_id=state.task.id,
                                role=Role.IMPLEMENTOR,
                                status=TaskStatus.FAILED,
                                output=f"Retry worktree creation failed: {e}",
                            )
                        )

                retry_tasks = [_run_task(s) for s in retryable]

                with Live(
                    _status_table(all_states), console=console, refresh_per_second=1
                ) as live:

                    async def _update_retry_display():
                        while not all(
                            s.status in (TaskStatus.DONE, TaskStatus.FAILED) for s in retryable
                        ):
                            live.update(_status_table(all_states))
                            await asyncio.sleep(1)
                        live.update(_status_table(all_states))

                    await asyncio.gather(*retry_tasks, _update_retry_display())

                # Merge any newly successful retried tasks
                retry_done = [s for s in retryable if s.status == TaskStatus.DONE]
                if retry_done:
                    console.print(
                        f"\n[bold]Merging {len(retry_done)} retried branch(es) "
                        f"into {session_branch}...[/bold]\n"
                    )
                    for state in retry_done:
                        result = merge_into_session(repo, session_branch, state.worktree.branch)
                        if result.success:
                            console.print(
                                f"  [green]✓[/green] {state.worktree.branch} — "
                                f"{result.message}"
                            )
                            await session_status.update_merged(repo, state.task.id)
                            if not keep_branches:
                                delete_branch(repo, state.worktree.branch)
                        else:
                            console.print(
                                f"  [red]✗[/red] {state.worktree.branch} — " f"{result.message}"
                            )
                            state.status = TaskStatus.FAILED
                    console.print()

        # --fail-fast: stop processing further waves if any task failed
        if fail_fast:
            wave_failed = [s for s in wave_states if s.status == TaskStatus.FAILED]
            if wave_failed:
                console.print(
                    f"[bold red]--fail-fast: {len(wave_failed)} task(s) failed in "
                    f"wave {wave_num}, stopping.[/bold red]\n"
                )
                break

    # Summary
    console.print("\n[bold]━━━ Summary ━━━[/bold]\n")
    done = [s for s in all_states if s.status == TaskStatus.DONE]
    failed = [s for s in all_states if s.status == TaskStatus.FAILED]
    fixed = [s for s in done if s.fix_count > 0]

    console.print(f"  [green]✓ {len(done)} completed[/green]")
    if fixed:
        console.print(f"  [yellow]↻ {len(fixed)} required fixes[/yellow]")
        for s in fixed:
            console.print(f"    - {s.task.title} ({s.fix_count} fix cycle(s))")
    if failed:
        console.print(f"  [red]✗ {len(failed)} failed[/red]")
        for s in failed:
            console.print(f"    - {s.task.title}: {s.phase_summary}")

    # Show session branch for review
    if done:
        console.print(f"\n[bold]All changes merged into:[/bold] {session_branch}")
        console.print(f"  git checkout {session_branch}")
        console.print(f"  git diff main...{session_branch}")

    if cleanup_on_done:
        for s in all_states:
            if s.worktree:
                s.worktree.cleanup()
        console.print("\n[dim]Worktrees cleaned up.[/dim]")

    return all_states


async def merge_unmerged(
    repo: Path,
    session_branch: str,
    plan_slug: str | None = None,
    agent_cmd: str = "claude",
    use_tmux: bool = True,
    keep_branches: bool = False,
) -> SessionStatus:
    """Merge all completed-but-unmerged task branches into the session branch.

    If ``plan_slug`` is provided, loads that specific status file.
    Otherwise, scans all status files for the session branch.
    """
    console = Console()

    if plan_slug:
        status = SessionStatus.load(repo, plan_slug, session_branch)
    else:
        try:
            status = SessionStatus.find_by_session(repo, session_branch)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return SessionStatus(plan_slug="", session_branch=session_branch)

    if status is None:
        console.print("[red]No status found for this session. Run 'wb run' first.[/red]")
        return SessionStatus(plan_slug=plan_slug or "", session_branch=session_branch)

    # Find tasks that completed but haven't been merged
    unmerged = {
        tid: rec
        for tid, rec in status.tasks.items()
        if rec.status == "done" and not rec.merged and rec.branch
    }

    if not unmerged:
        console.print("[dim]No unmerged tasks found.[/dim]")
        return status

    # Pre-check: skip branches already merged via git (manual merge)
    already_merged = get_merged_branches(repo, session_branch)

    console.print(f"\n[bold]Merging {len(unmerged)} branch(es) into {session_branch}...[/bold]\n")

    for tid, rec in unmerged.items():
        if rec.branch in already_merged:
            console.print(f"  [green]✓[/green] {rec.branch} — already merged")
            await status.update_merged(repo, tid)
            if not keep_branches:
                delete_branch(repo, rec.branch)
            continue

        result = merge_into_session(repo, session_branch, rec.branch)
        if result.success:
            console.print(f"  [green]✓[/green] {rec.branch} — {result.message}")
            await status.update_merged(repo, tid)
            if not keep_branches:
                delete_branch(repo, rec.branch)
        elif result.conflicts and result.merge_dir:
            console.print(f"  [blue]⚡[/blue] {rec.branch} — " f"{result.message} Resolving...")
            for cf in result.conflicts:
                console.print(f"      [dim]{cf}[/dim]")

            resolver_result = await run_merge_resolver(
                task_branch=rec.branch,
                session_branch=session_branch,
                merge_dir=result.merge_dir,
                conflicts=result.conflicts,
                repo=repo,
                agent_cmd=agent_cmd,
                use_tmux=use_tmux,
            )

            if resolver_result.passed:
                merge_finish = complete_merge(result.merge_dir, repo, session_branch, rec.branch)
                if merge_finish.success:
                    console.print(f"  [green]✓[/green] {rec.branch} — {merge_finish.message}")
                    await status.update_merged(repo, tid)
                    if not keep_branches:
                        delete_branch(repo, rec.branch)
                else:
                    console.print(f"  [red]✗[/red] {rec.branch} — {merge_finish.message}")
                    cleanup_merge_worktree(repo, result.merge_dir)
            else:
                console.print(f"  [red]✗[/red] {rec.branch} — Merge resolver failed")
                cleanup_merge_worktree(repo, result.merge_dir)
        else:
            console.print(f"  [red]✗[/red] {rec.branch} — {result.message}")

    # Summary
    merged_count = sum(1 for rec in status.tasks.values() if rec.merged)
    total = len(status.tasks)
    console.print(f"\n[bold]{merged_count}/{total} task(s) merged into {session_branch}[/bold]")

    return status
