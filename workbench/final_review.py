"""Final review orchestration: two-agent sequence + conditional PR creation."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from workbench.adapters import get_adapter
from workbench.directives import BranchReviewerDirective, RequirementsSummarizerDirective
from workbench.github_pr import create_pr
from workbench.pr_writer import (
    PrWriterError,
    derive_body_from_plan,
    derive_title_from_plan,
    run_pr_writer,
)
from workbench.session_status import FinalReviewRecord, SessionStatus
from workbench.tmux import run_in_tmux
from workbench.worktree import push_session_branch

if TYPE_CHECKING:
    from workbench.profile import Profile

console = Console()


class PostTaskAgentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PostTaskAgentState:
    name: str
    status: PostTaskAgentStatus = PostTaskAgentStatus.PENDING
    started_at: float | None = None
    finished_at: float | None = None
    output_path: Path | None = None
    note: str = ""

    @property
    def elapsed(self) -> str:
        if self.started_at is None:
            return "-"
        end = self.finished_at or time.time()
        mins = int(end - self.started_at) // 60
        secs = int(end - self.started_at) % 60
        return f"{mins}m{secs:02d}s"


def _post_task_table(states: list[PostTaskAgentState]) -> Table:
    table = Table(title="Post-task agents", show_lines=True, expand=True)
    table.add_column("Agent", style="bold", min_width=18)
    table.add_column("Status", min_width=10)
    table.add_column("Time", min_width=8)
    table.add_column("Output / Note", ratio=1)

    style_for = {
        PostTaskAgentStatus.PENDING: "dim",
        PostTaskAgentStatus.RUNNING: "yellow",
        PostTaskAgentStatus.DONE: "green",
        PostTaskAgentStatus.FAILED: "red bold",
        PostTaskAgentStatus.SKIPPED: "dim",
    }
    for s in states:
        note = s.note or (str(s.output_path) if s.output_path else "")
        table.add_row(
            s.name,
            Text(s.status.value, style=style_for[s.status]),
            s.elapsed,
            note,
        )
    return table


async def run_final_review(
    repo: Path,
    session_branch: str,
    plan_slug: str,
    base_branch: str,
    plan_source: Path,
    merged_task_titles: list[str],
    agent_cmd: str = "claude",
    use_tmux: bool = True,
    profile: Profile | None = None,
    summarizer_directive: str | None = None,
    branch_reviewer_directive: str | None = None,
    pr_writer_directive: str | None = None,
    pr_title: str | None = None,
    pr_body_file: Path | None = None,
    pr_base: str | None = None,
    skip_pr: bool = False,
    agents_config_paths: list[Path] | None = None,
) -> FinalReviewRecord:
    """Run the two-agent final review sequence and return the persisted record.

    On a PASS verdict (and when ``pr_body_file`` is not supplied), a dedicated
    ``pr_writer`` agent authors the PR title and body from the actual diff. If
    that agent fails, the orchestrator falls back to a plan-derived body so the
    PR can still be opened. Pass ``pr_writer_directive`` to override the
    writer's directive text (CLI override > profile > built-in default).
    """

    # 1. Validate inputs
    if not plan_source.exists():
        raise FileNotFoundError(f"Plan source not found: {plan_source}")
    if not merged_task_titles:
        raise ValueError("Final review needs at least one merged task.")

    # 2. Acquire advisory lock
    lock_path = repo / ".workbench" / plan_slug / ".review.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        raise RuntimeError("Another final-review is running for this plan.")

    try:
        return await _run_review_sequence(
            repo=repo,
            session_branch=session_branch,
            plan_slug=plan_slug,
            base_branch=base_branch,
            plan_source=plan_source,
            merged_task_titles=merged_task_titles,
            agent_cmd=agent_cmd,
            use_tmux=use_tmux,
            profile=profile,
            summarizer_directive=summarizer_directive,
            branch_reviewer_directive=branch_reviewer_directive,
            pr_writer_directive=pr_writer_directive,
            pr_title=pr_title,
            pr_body_file=pr_body_file,
            pr_base=pr_base,
            skip_pr=skip_pr,
            agents_config_paths=agents_config_paths,
        )
    finally:
        lock_path.unlink(missing_ok=True)


async def _run_review_sequence(
    repo: Path,
    session_branch: str,
    plan_slug: str,
    base_branch: str,
    plan_source: Path,
    merged_task_titles: list[str],
    agent_cmd: str,
    use_tmux: bool,
    profile: Profile | None,
    summarizer_directive: str | None,
    branch_reviewer_directive: str | None,
    pr_writer_directive: str | None,
    pr_title: str | None,
    pr_body_file: Path | None,
    pr_base: str | None,
    skip_pr: bool,
    agents_config_paths: list[Path] | None,
) -> FinalReviewRecord:
    """Inner sequence, runs inside the lock."""

    states = [
        PostTaskAgentState(name="Summarizer"),
        PostTaskAgentState(name="Branch reviewer"),
        PostTaskAgentState(name="PR writer"),
    ]

    shutdown = asyncio.Event()

    async def _refresh(live: Live) -> None:
        while not shutdown.is_set():
            live.update(_post_task_table(states))
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
        live.update(_post_task_table(states))

    with Live(_post_task_table(states), console=console, refresh_per_second=2) as live:
        refresh_task = asyncio.create_task(_refresh(live))
        try:
            return await _execute_sequence(
                states=states,
                repo=repo,
                session_branch=session_branch,
                plan_slug=plan_slug,
                base_branch=base_branch,
                plan_source=plan_source,
                merged_task_titles=merged_task_titles,
                agent_cmd=agent_cmd,
                use_tmux=use_tmux,
                profile=profile,
                summarizer_directive=summarizer_directive,
                branch_reviewer_directive=branch_reviewer_directive,
                pr_writer_directive=pr_writer_directive,
                pr_title=pr_title,
                pr_body_file=pr_body_file,
                pr_base=pr_base,
                skip_pr=skip_pr,
                agents_config_paths=agents_config_paths,
            )
        finally:
            shutdown.set()
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            live.update(_post_task_table(states))


async def _execute_sequence(
    states: list[PostTaskAgentState],
    repo: Path,
    session_branch: str,
    plan_slug: str,
    base_branch: str,
    plan_source: Path,
    merged_task_titles: list[str],
    agent_cmd: str,
    use_tmux: bool,
    profile: Profile | None,
    summarizer_directive: str | None,
    branch_reviewer_directive: str | None,
    pr_writer_directive: str | None,
    pr_title: str | None,
    pr_body_file: Path | None,
    pr_base: str | None,
    skip_pr: bool,
    agents_config_paths: list[Path] | None,
) -> FinalReviewRecord:
    wrap_up_dir = repo / ".workbench" / plan_slug / "wrap-up" / session_branch
    wrap_up_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = wrap_up_dir / "requirements.md"
    review_path = wrap_up_dir / "review.md"

    try:
        return await _execute_sequence_inner(
            states=states,
            repo=repo,
            session_branch=session_branch,
            plan_slug=plan_slug,
            base_branch=base_branch,
            plan_source=plan_source,
            merged_task_titles=merged_task_titles,
            agent_cmd=agent_cmd,
            use_tmux=use_tmux,
            profile=profile,
            summarizer_directive=summarizer_directive,
            branch_reviewer_directive=branch_reviewer_directive,
            pr_writer_directive=pr_writer_directive,
            pr_title=pr_title,
            pr_body_file=pr_body_file,
            pr_base=pr_base,
            skip_pr=skip_pr,
            agents_config_paths=agents_config_paths,
            wrap_up_dir=wrap_up_dir,
            requirements_path=requirements_path,
            review_path=review_path,
        )
    finally:
        _cleanup_wrap_up_worktrees(repo, wrap_up_dir)


async def _execute_sequence_inner(
    states: list[PostTaskAgentState],
    repo: Path,
    session_branch: str,
    plan_slug: str,
    base_branch: str,
    plan_source: Path,
    merged_task_titles: list[str],
    agent_cmd: str,
    use_tmux: bool,
    profile: Profile | None,
    summarizer_directive: str | None,
    branch_reviewer_directive: str | None,
    pr_writer_directive: str | None,
    pr_title: str | None,
    pr_body_file: Path | None,
    pr_base: str | None,
    skip_pr: bool,
    agents_config_paths: list[Path] | None,
    wrap_up_dir: Path,
    requirements_path: Path,
    review_path: Path,
) -> FinalReviewRecord:
    plan_content = plan_source.read_text(encoding="utf-8")

    summarizer_text = _resolve_directive_text(
        summarizer_directive, profile, "summarizer", RequirementsSummarizerDirective.DEFAULT_TEXT
    )
    reviewer_text = _resolve_directive_text(
        branch_reviewer_directive, profile, "branch_reviewer", BranchReviewerDirective.DEFAULT_TEXT
    )
    summarizer_agent_cmd = _resolve_agent_cmd(agent_cmd, profile, "summarizer")
    reviewer_agent_cmd = _resolve_agent_cmd(agent_cmd, profile, "branch_reviewer")

    adapter_paths = (
        agents_config_paths
        if agents_config_paths is not None
        else [repo / ".workbench" / "agents.yaml"]
    )
    summarizer_adapter = get_adapter(summarizer_agent_cmd, adapter_paths)
    summarizer_dir = RequirementsSummarizerDirective(
        directive_text=summarizer_text,
        plan_content=plan_content,
        output_path=requirements_path,
    )
    summarizer_prompt = summarizer_dir.render()

    states[0].status = PostTaskAgentStatus.RUNNING
    states[0].started_at = time.time()
    summarizer_cost: dict = {}
    try:
        try:
            cmd = summarizer_adapter.build_command(summarizer_prompt, repo)
            if use_tmux:
                session_name = "wb-final-review-summarizer"
                returncode, raw_output = await run_in_tmux(session_name, cmd, repo)
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(repo),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                returncode = proc.returncode
                raw_output = stdout.decode("utf-8", errors="replace")

            _output_text, summarizer_cost = summarizer_adapter.parse_output(raw_output)
        except Exception as e:
            returncode = 1
            raw_output = f"Summarizer error: {e}"

        if (
            returncode != 0
            or not requirements_path.exists()
            or requirements_path.stat().st_size == 0
        ):
            states[0].status = PostTaskAgentStatus.FAILED
            states[0].note = f"exit={returncode}"[:120]
            states[1].status = PostTaskAgentStatus.SKIPPED
            states[1].note = "summarizer failed"
            states[2].status = PostTaskAgentStatus.SKIPPED
            states[2].note = "summarizer failed"
            review_path.write_text(
                f"Summarizer failed (exit={returncode}).\n\n{raw_output[:2000]}",
                encoding="utf-8",
            )
            record = _build_record(
                verdict="error",
                review_path=review_path,
                requirements_path=requirements_path,
                repo=repo,
                summarizer_agent=summarizer_agent_cmd,
                reviewer_agent=reviewer_agent_cmd,
                cost=summarizer_cost,
            )
            await _persist_record(repo, plan_slug, session_branch, record)
            return record

        states[0].output_path = requirements_path
        states[0].status = PostTaskAgentStatus.DONE
    finally:
        states[0].finished_at = time.time()

    wt_path = wrap_up_dir / ".review-wt"
    reviewer_returncode = 1
    reviewer_output = ""
    reviewer_cost: dict = {}
    states[1].status = PostTaskAgentStatus.RUNNING
    states[1].started_at = time.time()
    try:
        try:
            _create_review_worktree(repo, wt_path, session_branch)

            reviewer_adapter = get_adapter(reviewer_agent_cmd, adapter_paths)
            reviewer_dir = BranchReviewerDirective(
                directive_text=reviewer_text,
                requirements_path=requirements_path,
                base_branch=base_branch,
                merged_tasks=merged_task_titles,
                output_path=review_path,
            )
            reviewer_prompt = reviewer_dir.render()

            try:
                cmd = reviewer_adapter.build_command(reviewer_prompt, wt_path)
                if use_tmux:
                    session_name = "wb-final-review-branch-reviewer"
                    reviewer_returncode, reviewer_output = await run_in_tmux(
                        session_name, cmd, wt_path
                    )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(wt_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await proc.communicate()
                    reviewer_returncode = proc.returncode
                    reviewer_output = stdout.decode("utf-8", errors="replace")

                _output_text, reviewer_cost = reviewer_adapter.parse_output(reviewer_output)
            except Exception as e:
                reviewer_returncode = 1
                reviewer_output = f"Branch reviewer error: {e}"

        except Exception as e:
            reviewer_output = f"Worktree/reviewer setup error: {e}"
            review_path.write_text(reviewer_output, encoding="utf-8")
            states[1].status = PostTaskAgentStatus.FAILED
            states[1].note = str(e)[:120]
            states[2].status = PostTaskAgentStatus.SKIPPED
            states[2].note = "reviewer failed"
            combined_cost = {"cost_usd": summarizer_cost.get("cost_usd", 0.0)}
            record = _build_record(
                verdict="error",
                review_path=review_path,
                requirements_path=requirements_path,
                repo=repo,
                summarizer_agent=summarizer_agent_cmd,
                reviewer_agent=reviewer_agent_cmd,
                cost=combined_cost,
            )
            await _persist_record(repo, plan_slug, session_branch, record)
            raise
        finally:
            _cleanup_review_worktree(repo, wt_path)
    finally:
        states[1].finished_at = time.time()

    total_cost = summarizer_cost.get("cost_usd", 0.0) + reviewer_cost.get("cost_usd", 0.0)
    combined_cost = {"cost_usd": total_cost}

    if reviewer_returncode != 0 or not review_path.exists():
        if not review_path.exists():
            review_path.write_text(
                f"Branch reviewer failed (exit={reviewer_returncode}).\n\n"
                f"{reviewer_output[:2000]}",
                encoding="utf-8",
            )
        states[1].status = PostTaskAgentStatus.FAILED
        states[1].note = f"exit={reviewer_returncode}"[:120]
        states[2].status = PostTaskAgentStatus.SKIPPED
        states[2].note = "reviewer failed"
        record = _build_record(
            verdict="error",
            review_path=review_path,
            requirements_path=requirements_path,
            repo=repo,
            summarizer_agent=summarizer_agent_cmd,
            reviewer_agent=reviewer_agent_cmd,
            cost=combined_cost,
        )
        await _persist_record(repo, plan_slug, session_branch, record)
        return record

    states[1].output_path = review_path
    states[1].status = PostTaskAgentStatus.DONE

    verdict = _parse_verdict(review_path.read_text(encoding="utf-8"))

    pr_url: str | None = None
    if verdict != "pass" or skip_pr:
        states[2].status = PostTaskAgentStatus.SKIPPED
        states[2].note = "skip_pr" if skip_pr else f"verdict: {verdict.upper()}"
    else:
        states[2].status = PostTaskAgentStatus.RUNNING
        states[2].started_at = time.time()
        try:
            rel_report = review_path.relative_to(repo)
            if pr_body_file is not None:
                title = pr_title or derive_title_from_plan(plan_content, plan_slug)
                body = pr_body_file.read_text(encoding="utf-8")
                states[2].note = "user-supplied body"
            else:
                try:
                    agent_title, agent_body = await run_pr_writer(
                        repo=repo,
                        session_branch=session_branch,
                        plan_slug=plan_slug,
                        base_branch=base_branch,
                        plan_source=plan_source,
                        merged_task_titles=merged_task_titles,
                        agent_cmd=agent_cmd,
                        use_tmux=use_tmux,
                        profile=profile,
                        directive_override=pr_writer_directive,
                        agents_config_paths=agents_config_paths,
                        wrap_up_dir=wrap_up_dir,
                    )
                    title = pr_title or agent_title
                    body = agent_body
                except PrWriterError as e:
                    title = pr_title or derive_title_from_plan(plan_content, plan_slug)
                    body = derive_body_from_plan(plan_content, merged_task_titles, rel_report)
                    states[2].note = f"fallback: {type(e).__name__}: {e}"[:120]

            base = pr_base or base_branch
            push_ok, push_msg = push_session_branch(repo, session_branch)
            push_warn = "" if push_ok else f"push failed: {push_msg}; "
            success, result_msg = await create_pr(repo, session_branch, base, title, body)
            if success:
                pr_url = result_msg
                states[2].note = f"{push_warn}{result_msg}"[:120]
            else:
                states[2].note = f"{push_warn}create_pr failed: {result_msg}"[:120]
            states[2].status = PostTaskAgentStatus.DONE
        except Exception as e:
            states[2].status = PostTaskAgentStatus.FAILED
            states[2].note = str(e)[:120]
            raise
        finally:
            states[2].finished_at = time.time()

    record = _build_record(
        verdict=verdict,
        review_path=review_path,
        requirements_path=requirements_path,
        repo=repo,
        summarizer_agent=summarizer_agent_cmd,
        reviewer_agent=reviewer_agent_cmd,
        cost=combined_cost,
        pr_url=pr_url,
    )
    await _persist_record(repo, plan_slug, session_branch, record)

    return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_directive_text(
    cli_override: str | None,
    profile: Profile | None,
    role_name: str,
    default_text: str,
) -> str:
    """Resolve directive text: CLI override > profile > default."""
    if cli_override:
        return cli_override
    if profile:
        role_config = getattr(profile, role_name, None)
        if role_config and role_config.directive:
            return role_config.directive
    return default_text


def _resolve_agent_cmd(agent_cmd: str, profile: Profile | None, role_name: str) -> str:
    """Resolve agent command: profile role agent (if default) > agent_cmd."""
    if agent_cmd != "claude":
        return agent_cmd
    if profile:
        role_config = getattr(profile, role_name, None)
        if role_config and role_config.agent != "claude":
            return role_config.agent
    return agent_cmd


def _parse_verdict(content: str) -> str:
    """Extract VERDICT: PASS or VERDICT: FAIL from report content."""
    for line in content.splitlines():
        match = re.match(r"^VERDICT:\s*(PASS|FAIL)$", line.strip())
        if match:
            return match.group(1).lower()
    return "error"


def _build_record(
    verdict: str,
    review_path: Path,
    requirements_path: Path,
    repo: Path,
    summarizer_agent: str,
    reviewer_agent: str,
    cost: dict,
    pr_url: str | None = None,
) -> FinalReviewRecord:
    """Construct a FinalReviewRecord."""
    return FinalReviewRecord(
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        verdict=verdict,
        review_path=str(review_path.relative_to(repo)),
        requirements_path=str(requirements_path.relative_to(repo)),
        summarizer_agent=summarizer_agent,
        reviewer_agent=reviewer_agent,
        cost_usd=cost.get("cost_usd", 0.0),
        pr_url=pr_url,
    )


async def _persist_record(
    repo: Path, plan_slug: str, session_branch: str, record: FinalReviewRecord
) -> None:
    """Load or create SessionStatus and append the record."""
    status = SessionStatus.load(repo, plan_slug, session_branch) or SessionStatus(
        plan_slug=plan_slug, session_branch=session_branch
    )
    await status.append_final_review(repo, record)


def _create_review_worktree(repo: Path, wt_path: Path, session_branch: str) -> None:
    """Create a detached git worktree at wt_path pointing at session_branch's tip.

    Uses --detach because the branch reviewer only reads the tree to write its
    report. A non-detached `git worktree add <path> <branch>` fails with exit
    128 when the branch is already checked out elsewhere (the main repo
    typically sits on the session branch right after a run).
    """
    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=repo,
            capture_output=True,
        )
    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt_path), session_branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree add (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip() or 'no output'}"
        )


def _cleanup_review_worktree(repo: Path, wt_path: Path) -> None:
    """Remove the review worktree."""
    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=repo,
            capture_output=True,
        )


def _cleanup_wrap_up_worktrees(repo: Path, wrap_up_dir: Path) -> None:
    """Forcefully remove any ephemeral worktrees under ``wrap_up_dir``.

    Per-agent cleanup already runs in each agent's ``finally:`` block, but if
    those calls were skipped (crash before cleanup, ``git worktree remove``
    failure), we sweep the wrap-up folder one more time at end-of-phase so the
    .review-wt and .pr-writer-wt directories never linger between runs.
    """
    if not wrap_up_dir.exists():
        return
    for name in (".review-wt", ".pr-writer-wt"):
        wt = wrap_up_dir / name
        if not wt.exists():
            continue
        subprocess.run(
            ["git", "worktree", "remove", str(wt), "--force"],
            cwd=repo,
            capture_output=True,
        )
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
