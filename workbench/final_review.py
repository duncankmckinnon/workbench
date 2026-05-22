"""Final review orchestration: requirements → (review → fix)* → merge → PR.

The summarizer runs once, then a bounded review/fix loop runs against a
persistent writable worktree on a ``-review`` branch. On the first PASS the
loop stops, the ``-review`` branch is merged back into the session branch,
and a PR is opened. If review never passes within ``max_retries + 1``
attempts, the cycle ends with no PR.
"""

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
from workbench.conventions import apply_conventions_fallback
from workbench.directives import (
    BranchFixerDirective,
    BranchReviewerDirective,
    BranchReviewerFollowupDirective,
    RequirementsSummarizerDirective,
)
from workbench.github_pr import create_pr
from workbench.pr_writer import (
    PrWriterError,
    derive_body_from_plan,
    derive_title_from_plan,
    run_pr_writer,
)
from workbench.session_metadata import SessionMetadata, merge_trace_env, with_session_metadata
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
    # Iterate a snapshot — the live refresh loop reads while the orchestrator
    # may append new rows.
    for s in list(states):
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
    max_retries: int = 2,
    trace_env: bool = True,
    trace_prompt: bool = False,
) -> FinalReviewRecord:
    """Run the final review fix loop and return the persisted record.

    The flow is ``requirements → (review → fix)* → merge → PR``, bounded by
    ``max_retries`` (1 initial review plus up to ``max_retries`` fix-then-
    re-review cycles). On the first PASS the loop stops, the ``-review``
    branch is fast-forward merged into the session branch (falling back to a
    normal merge), and the PR is opened. On exhausted retries the cycle
    ends with no PR.
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
            max_retries=max_retries,
            trace_env=trace_env,
            trace_prompt=trace_prompt,
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
    max_retries: int,
    trace_env: bool,
    trace_prompt: bool,
) -> FinalReviewRecord:
    """Inner sequence, runs inside the lock."""

    states: list[PostTaskAgentState] = [PostTaskAgentState(name="Summarizer")]

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
                max_retries=max_retries,
                trace_env=trace_env,
                trace_prompt=trace_prompt,
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
    max_retries: int,
    trace_env: bool,
    trace_prompt: bool,
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
            max_retries=max_retries,
            trace_env=trace_env,
            trace_prompt=trace_prompt,
            wrap_up_dir=wrap_up_dir,
            requirements_path=requirements_path,
            review_path=review_path,
        )
    finally:
        _cleanup_wrap_up_worktrees(repo, wrap_up_dir, session_branch)


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
    max_retries: int,
    trace_env: bool,
    trace_prompt: bool,
    wrap_up_dir: Path,
    requirements_path: Path,
    review_path: Path,
) -> FinalReviewRecord:
    plan_content = apply_conventions_fallback(plan_source.read_text(encoding="utf-8"), repo)

    summarizer_text = _resolve_directive_text(
        summarizer_directive, profile, "summarizer", RequirementsSummarizerDirective.DEFAULT_TEXT
    )
    reviewer_text = _resolve_directive_text(
        branch_reviewer_directive, profile, "branch_reviewer", BranchReviewerDirective.DEFAULT_TEXT
    )
    fixer_text = _resolve_directive_text(None, profile, "fixer", BranchFixerDirective.DEFAULT_TEXT)
    summarizer_agent_cmd = _resolve_agent_cmd(agent_cmd, profile, "summarizer")
    reviewer_agent_cmd = _resolve_agent_cmd(agent_cmd, profile, "branch_reviewer")
    fixer_agent_cmd = _resolve_agent_cmd(agent_cmd, profile, "fixer")

    adapter_paths = (
        agents_config_paths
        if agents_config_paths is not None
        else [repo / ".workbench" / "agents.yaml"]
    )

    # ── Step 1: Summarizer (once) ────────────────────────────────────
    summarizer_adapter = get_adapter(summarizer_agent_cmd, adapter_paths)
    summarizer_dir = RequirementsSummarizerDirective(
        directive_text=summarizer_text,
        plan_content=plan_content,
        output_path=requirements_path,
    )
    summarizer_prompt = summarizer_dir.render()

    summarizer_meta = SessionMetadata(plan=plan_slug, agent="summarizer", step="requirements")
    if trace_prompt:
        summarizer_prompt = with_session_metadata(summarizer_prompt, summarizer_meta)
    summarizer_env = (
        merge_trace_env(os.environ, summarizer_meta)
        if trace_env and summarizer_adapter.config.inject_env
        else None
    )

    states[0].status = PostTaskAgentStatus.RUNNING
    states[0].started_at = time.time()
    summarizer_cost: dict = {}
    try:
        try:
            cmd = summarizer_adapter.build_command(summarizer_prompt, repo)
            if use_tmux:
                session_name = "wb-final-review-summarizer"
                returncode, raw_output = await run_in_tmux(
                    session_name, cmd, repo, env=summarizer_env
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(repo),
                    env=summarizer_env,
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
            # No review row yet — surface the failure as a single skipped
            # placeholder for visual consistency.
            skipped = PostTaskAgentState(name="Review #1")
            skipped.status = PostTaskAgentStatus.SKIPPED
            skipped.note = "summarizer failed"
            states.append(skipped)
            pr_skip = PostTaskAgentState(name="PR writer")
            pr_skip.status = PostTaskAgentStatus.SKIPPED
            pr_skip.note = "summarizer failed"
            states.append(pr_skip)
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
                iterations=0,
                fixer_runs=0,
                fixer_agent=fixer_agent_cmd,
                merged=False,
            )
            await _persist_record(repo, plan_slug, session_branch, record)
            return record

        states[0].output_path = requirements_path
        states[0].status = PostTaskAgentStatus.DONE
    finally:
        states[0].finished_at = time.time()

    # ── Step 2: Persistent review worktree on a new branch ──────────
    wt_path = wrap_up_dir / ".review-wt"
    review_branch = f"{session_branch}-review"

    # Add the Review #1 row up front so a worktree creation failure surfaces
    # against it (the reviewer can't run if the worktree doesn't exist).
    review_row = PostTaskAgentState(name="Review #1")
    states.append(review_row)
    review_row.status = PostTaskAgentStatus.RUNNING
    review_row.started_at = time.time()

    try:
        _create_review_worktree(repo, wt_path, session_branch, review_branch)
    except Exception as e:
        review_row.status = PostTaskAgentStatus.FAILED
        review_row.note = str(e)[:120]
        review_row.finished_at = time.time()
        pr_skip = PostTaskAgentState(name="PR writer")
        pr_skip.status = PostTaskAgentStatus.SKIPPED
        pr_skip.note = "reviewer failed"
        states.append(pr_skip)
        review_path.write_text(f"Worktree/reviewer setup error: {e}", encoding="utf-8")
        combined_cost = {"cost_usd": summarizer_cost.get("cost_usd", 0.0)}
        record = _build_record(
            verdict="error",
            review_path=review_path,
            requirements_path=requirements_path,
            repo=repo,
            summarizer_agent=summarizer_agent_cmd,
            reviewer_agent=reviewer_agent_cmd,
            cost=combined_cost,
            iterations=0,
            fixer_runs=0,
            fixer_agent=fixer_agent_cmd,
            merged=False,
        )
        await _persist_record(repo, plan_slug, session_branch, record)
        raise

    # ── Step 3: Review → fix loop ───────────────────────────────────
    total_cost_usd = summarizer_cost.get("cost_usd", 0.0)
    verdict = "error"
    iterations = 0
    fixer_runs = 0
    prior_review_sha = ""
    prior_review_feedback = ""

    for attempt in range(1, max_retries + 2):
        iterations = attempt

        if attempt > 1:
            review_row = PostTaskAgentState(name=f"Review #{attempt}")
            states.append(review_row)
            review_row.status = PostTaskAgentStatus.RUNNING
            review_row.started_at = time.time()

        if attempt == 1:
            reviewer_dir = BranchReviewerDirective(
                directive_text=reviewer_text,
                requirements_path=requirements_path,
                base_branch=base_branch,
                merged_tasks=merged_task_titles,
                output_path=review_path,
            )
        else:
            reviewer_dir = BranchReviewerFollowupDirective(
                directive_text=BranchReviewerFollowupDirective.DEFAULT_TEXT,
                requirements_path=requirements_path,
                base_branch=base_branch,
                prior_review_sha=prior_review_sha,
                prior_feedback=prior_review_feedback,
                output_path=review_path,
            )

        # Wipe the prior review.md so we can detect a reviewer crash via its
        # absence after the agent runs.
        if review_path.exists():
            review_path.unlink()

        reviewer_adapter = get_adapter(reviewer_agent_cmd, adapter_paths)
        reviewer_prompt = reviewer_dir.render()
        reviewer_meta = SessionMetadata(
            plan=plan_slug, agent="branch_reviewer", step=f"review#{attempt}"
        )
        if trace_prompt:
            reviewer_prompt = with_session_metadata(reviewer_prompt, reviewer_meta)
        reviewer_env = (
            merge_trace_env(os.environ, reviewer_meta)
            if trace_env and reviewer_adapter.config.inject_env
            else None
        )
        reviewer_returncode = 1
        reviewer_output = ""
        reviewer_cost: dict = {}
        try:
            cmd = reviewer_adapter.build_command(reviewer_prompt, wt_path)
            if use_tmux:
                session_name = f"wb-final-review-branch-reviewer-{attempt}"
                reviewer_returncode, reviewer_output = await run_in_tmux(
                    session_name, cmd, wt_path, env=reviewer_env
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(wt_path),
                    env=reviewer_env,
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

        total_cost_usd += reviewer_cost.get("cost_usd", 0.0)

        if reviewer_returncode != 0 or not review_path.exists():
            if not review_path.exists():
                review_path.write_text(
                    f"Branch reviewer failed (exit={reviewer_returncode}).\n\n"
                    f"{reviewer_output[:2000]}",
                    encoding="utf-8",
                )
            review_row.status = PostTaskAgentStatus.FAILED
            review_row.note = f"exit={reviewer_returncode}"[:120]
            review_row.finished_at = time.time()
            verdict = "error"
            break

        review_text = review_path.read_text(encoding="utf-8")
        current_verdict = _parse_verdict(review_text)
        verdict = current_verdict

        review_row.output_path = review_path
        review_row.note = f"verdict: {current_verdict.upper()}"
        review_row.status = PostTaskAgentStatus.DONE
        review_row.finished_at = time.time()

        if current_verdict == "pass":
            break

        # FAIL — capture state for next follow-up, then either run fixer or exit.
        prior_review_sha = _safe_head_sha(wt_path)
        prior_review_feedback = _extract_feedback(review_text) or review_text

        if attempt > max_retries:
            # Out of retries.
            break

        # Run the fixer in the same worktree.
        fix_row = PostTaskAgentState(name=f"Fix #{attempt}")
        states.append(fix_row)
        fix_row.status = PostTaskAgentStatus.RUNNING
        fix_row.started_at = time.time()

        fixer_adapter = get_adapter(fixer_agent_cmd, adapter_paths)
        fixer_dir = BranchFixerDirective(
            directive_text=fixer_text,
            requirements_path=requirements_path,
            review_findings=prior_review_feedback,
            base_branch=base_branch,
            fix_branch=review_branch,
        )
        fixer_prompt = fixer_dir.render()
        fixer_meta = SessionMetadata(plan=plan_slug, agent="fixer", step=f"fix#{attempt}")
        if trace_prompt:
            fixer_prompt = with_session_metadata(fixer_prompt, fixer_meta)
        fixer_env = (
            merge_trace_env(os.environ, fixer_meta)
            if trace_env and fixer_adapter.config.inject_env
            else None
        )
        fixer_returncode = 1
        fixer_output = ""
        fixer_cost: dict = {}
        try:
            cmd = fixer_adapter.build_command(fixer_prompt, wt_path)
            if use_tmux:
                session_name = f"wb-final-review-fixer-{attempt}"
                fixer_returncode, fixer_output = await run_in_tmux(
                    session_name, cmd, wt_path, env=fixer_env
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(wt_path),
                    env=fixer_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                fixer_returncode = proc.returncode
                fixer_output = stdout.decode("utf-8", errors="replace")

            _output_text, fixer_cost = fixer_adapter.parse_output(fixer_output)
        except Exception as e:
            fixer_returncode = 1
            fixer_output = f"Branch fixer error: {e}"

        fixer_runs += 1
        total_cost_usd += fixer_cost.get("cost_usd", 0.0)

        if fixer_returncode != 0:
            fix_row.status = PostTaskAgentStatus.FAILED
            fix_row.note = f"exit={fixer_returncode}"[:120]
            fix_row.finished_at = time.time()
            # Stop loop, retain the last review verdict.
            break

        fix_row.status = PostTaskAgentStatus.DONE
        fix_row.note = "applied"
        fix_row.finished_at = time.time()

    combined_cost = {"cost_usd": total_cost_usd}

    # ── Step 4: Merge-back ──────────────────────────────────────────
    merged = False
    pr_url: str | None = None

    if verdict == "pass" and not skip_pr:
        merge_row = PostTaskAgentState(name="Merge")
        states.append(merge_row)
        merge_row.status = PostTaskAgentStatus.RUNNING
        merge_row.started_at = time.time()
        try:
            merged, merge_msg = _merge_review_branch(repo, session_branch, review_branch)
        finally:
            merge_row.finished_at = time.time()
        merge_row.note = merge_msg[:120]
        merge_row.status = PostTaskAgentStatus.DONE if merged else PostTaskAgentStatus.FAILED

    # ── Step 5: PR writer ───────────────────────────────────────────
    pr_row = PostTaskAgentState(name="PR writer")
    states.append(pr_row)

    if skip_pr:
        pr_row.status = PostTaskAgentStatus.SKIPPED
        pr_row.note = "skip_pr"
    elif verdict != "pass":
        pr_row.status = PostTaskAgentStatus.SKIPPED
        pr_row.note = f"verdict: {verdict.upper()}"
    elif not merged:
        pr_row.status = PostTaskAgentStatus.SKIPPED
        pr_row.note = f"merge failed: {merge_row.note}"[:120]
    else:
        pr_row.status = PostTaskAgentStatus.RUNNING
        pr_row.started_at = time.time()
        try:
            rel_report = review_path.relative_to(repo)
            if pr_body_file is not None:
                title = pr_title or derive_title_from_plan(plan_content, plan_slug)
                body = pr_body_file.read_text(encoding="utf-8")
                pr_row.note = "user-supplied body"
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
                    pr_row.note = f"fallback: {type(e).__name__}: {e}"[:120]

            base = pr_base or base_branch
            push_ok, push_msg = push_session_branch(repo, session_branch)
            push_warn = "" if push_ok else f"push failed: {push_msg}; "
            success, result_msg = await create_pr(repo, session_branch, base, title, body)
            if success:
                pr_url = result_msg
                pr_row.note = f"{push_warn}{result_msg}"[:120]
            else:
                pr_row.note = f"{push_warn}create_pr failed: {result_msg}"[:120]
            pr_row.status = PostTaskAgentStatus.DONE
        except Exception as e:
            pr_row.status = PostTaskAgentStatus.FAILED
            pr_row.note = str(e)[:120]
            raise
        finally:
            pr_row.finished_at = time.time()

    record = _build_record(
        verdict=verdict,
        review_path=review_path,
        requirements_path=requirements_path,
        repo=repo,
        summarizer_agent=summarizer_agent_cmd,
        reviewer_agent=reviewer_agent_cmd,
        cost=combined_cost,
        pr_url=pr_url,
        iterations=iterations,
        fixer_runs=fixer_runs,
        fixer_agent=fixer_agent_cmd,
        merged=merged,
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


def _extract_feedback(content: str) -> str:
    """Everything in the review report before the VERDICT line."""
    lines: list[str] = []
    for line in content.splitlines():
        if re.match(r"^VERDICT:\s*(PASS|FAIL)$", line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _safe_head_sha(wt_path: Path) -> str:
    """Best-effort ``git rev-parse HEAD`` in ``wt_path``; "" on any failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _build_record(
    verdict: str,
    review_path: Path,
    requirements_path: Path,
    repo: Path,
    summarizer_agent: str,
    reviewer_agent: str,
    cost: dict,
    pr_url: str | None = None,
    iterations: int = 1,
    fixer_runs: int = 0,
    fixer_agent: str = "",
    merged: bool = False,
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
        iterations=iterations,
        fixer_runs=fixer_runs,
        fixer_agent=fixer_agent,
        merged=merged,
    )


async def _persist_record(
    repo: Path, plan_slug: str, session_branch: str, record: FinalReviewRecord
) -> None:
    """Load or create SessionStatus and append the record."""
    status = SessionStatus.load(repo, plan_slug, session_branch) or SessionStatus(
        plan_slug=plan_slug, session_branch=session_branch
    )
    await status.append_final_review(repo, record)


def _create_review_worktree(
    repo: Path, wt_path: Path, session_branch: str, review_branch: str
) -> None:
    """Create a writable worktree at ``wt_path`` on ``review_branch``.

    ``review_branch`` is created fresh from the tip of ``session_branch``; the
    branch reviewer reads it and the branch fixer commits into it across
    iterations. Any stale worktree or branch at the target is force-removed
    first so reruns start clean.
    """
    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=repo,
            capture_output=True,
        )
    # Delete the branch if it exists from a prior failed run, so we can
    # recreate it pointing at the current session-branch tip.
    subprocess.run(
        ["git", "branch", "-D", review_branch],
        cwd=repo,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "worktree", "add", "-b", review_branch, str(wt_path), session_branch],
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


def _merge_review_branch(repo: Path, session_branch: str, review_branch: str) -> tuple[bool, str]:
    """Merge ``review_branch`` into ``session_branch`` inside ``repo``.

    Prefers ``--ff-only``; falls back to a normal merge with a generated
    message. On conflict the merge is aborted. Returns ``(success, message)``.
    """
    if not _branch_exists(repo, review_branch):
        return False, f"review branch {review_branch} not found"

    # The main repo is normally already on session_branch (that's where the
    # task merges happened); checkout is a safety net.
    checkout = subprocess.run(
        ["git", "checkout", session_branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        return False, f"checkout failed: {checkout.stderr.strip() or checkout.stdout.strip()}"

    ff = subprocess.run(
        ["git", "merge", "--ff-only", review_branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if ff.returncode == 0:
        return True, f"ff-merged {review_branch}"

    merge = subprocess.run(
        [
            "git",
            "merge",
            review_branch,
            "-m",
            f"Merge {review_branch} into {session_branch}",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if merge.returncode == 0:
        return True, f"merged {review_branch}"

    subprocess.run(["git", "merge", "--abort"], cwd=repo, capture_output=True)
    return False, f"merge conflict: {merge.stderr.strip() or merge.stdout.strip()}"


def _branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
    )
    return result.returncode == 0


def _cleanup_wrap_up_worktrees(
    repo: Path, wrap_up_dir: Path, session_branch: str | None = None
) -> None:
    """Forcefully remove any ephemeral worktrees under ``wrap_up_dir``.

    Per-agent cleanup already runs in each agent's ``finally:`` block, but if
    those calls were skipped (crash before cleanup, ``git worktree remove``
    failure), we sweep the wrap-up folder one more time at end-of-phase so the
    .review-wt and .pr-writer-wt directories never linger between runs. When
    ``session_branch`` is supplied we also delete the matching ``-review``
    branch so a rerun can recreate it from the current session tip.
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
    if session_branch:
        subprocess.run(
            ["git", "branch", "-D", f"{session_branch}-review"],
            cwd=repo,
            capture_output=True,
        )
