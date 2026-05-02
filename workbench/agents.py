"""Agent spawning and management via Claude Code CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapters import AgentAdapter, get_adapter
from .plan_parser import Task
from .tmux import run_in_tmux
from .worktree import Worktree, get_head_sha, get_main_branch

if TYPE_CHECKING:
    from .directives import PipelineDirective, PromptContext
    from .profile import Profile, RoleConfig


class Role(StrEnum):
    IMPLEMENTOR = "implementor"
    TESTER = "tester"
    REVIEWER = "reviewer"
    FIXER = "fixer"
    MERGER = "merger"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    MERGING = "merging"
    DONE = "done"
    FAILED = "failed"


@dataclass
class AgentResult:
    task_id: str
    role: Role
    status: TaskStatus
    output: str
    attempt: int = 1
    cost: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Check if a tester/reviewer verdict was PASS."""
        if self.status == TaskStatus.FAILED:
            return False
        return "VERDICT: PASS" in self.output

    @property
    def feedback(self) -> str:
        """Extract feedback text (everything before the VERDICT line)."""
        lines = self.output.strip().split("\n")
        feedback_lines = []
        for line in lines:
            if line.strip().startswith("VERDICT:"):
                break
            feedback_lines.append(line)
        return "\n".join(feedback_lines).strip()


async def run_agent(
    directive: PipelineDirective,
    ctx: PromptContext,
    repo: Path,
    agent_cmd: str = "claude",
    use_tmux: bool = True,
    adapter: AgentAdapter | None = None,
    task_id: str | None = None,
) -> AgentResult:
    """Spawn an agent in a worktree to run a single pipeline stage."""
    adapter = adapter or get_adapter(agent_cmd, repo / ".workbench" / "agents.yaml")
    prompt = directive.render(ctx)
    effective_task_id = task_id or ctx.task.id

    try:
        cmd = adapter.build_command(prompt, ctx.worktree.path)
        if use_tmux:
            session_name = f"wb-{effective_task_id}-{directive.role.value}"
            returncode, raw_output = await run_in_tmux(session_name, cmd, ctx.worktree.path)
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ctx.worktree.path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            returncode = proc.returncode
            raw_output = stdout.decode("utf-8", errors="replace")

        output_text, cost_data = adapter.parse_output(raw_output)

        status = TaskStatus.DONE if returncode == 0 else TaskStatus.FAILED

        return AgentResult(
            task_id=effective_task_id,
            role=directive.role,
            status=status,
            output=output_text if isinstance(output_text, str) else str(output_text),
            cost=cost_data,
        )

    except Exception as e:
        return AgentResult(
            task_id=effective_task_id,
            role=directive.role,
            status=TaskStatus.FAILED,
            output=f"Agent error: {e}",
        )


async def run_pipeline(
    task: Task,
    worktree: Worktree,
    repo: Path,
    skip_test: bool = False,
    skip_review: bool = False,
    max_retries: int = 2,
    agent_cmd: str = "claude",
    on_status_change: callable = None,
    session_branch: str | None = None,
    plan_context: str = "",
    plan_conventions: str = "",
    directives: dict[Role, str] | None = None,
    use_tmux: bool = True,
    tdd: bool = False,
    profile: Profile | None = None,
) -> list[AgentResult]:
    """Run the implement → test → review pipeline with retry loops.

    When a test or review fails, feedback is passed back to a fixer agent
    which addresses the issues before re-running the failing stage.

    Flow:
        implement → test ──PASS──→ review ──PASS──→ done
                     │                │
                     FAIL             FAIL
                     │                │
                     ↓                ↓
                    fix ──→ test     fix ──→ review
                    (up to max_retries)
    """
    from .directives import (
        FixerDirective,
        ImplementorDirective,
        PromptContext,
        ReviewerDirective,
        ReviewerFollowupDirective,
        TddImplementorDirective,
        TddTesterDirective,
        TesterDirective,
    )

    results: list[AgentResult] = []
    base = session_branch or get_main_branch(repo)
    ctx = PromptContext(
        task=task,
        worktree=worktree,
        base_branch=base,
        plan_context=plan_context,
        plan_conventions=plan_conventions,
    )

    def _notify(status: TaskStatus):
        if on_status_change:
            on_status_change(task.id, status)

    def _agent_for(role: Role) -> str:
        """Resolve effective agent_cmd for a role."""
        if profile and agent_cmd == "claude":
            return getattr(profile, role.value).agent
        return agent_cmd

    def _text_for(role: Role, mode: str = "main") -> str:
        """Resolve directive_text for a (role, mode) from CLI / profile.

        Priority: CLI flags > profile sub-mode > profile main > empty string.
        """
        cli_override = (directives or {}).get(role)
        if cli_override is not None:
            return cli_override
        if profile is None:
            return ""
        rc = getattr(profile, role.value)
        if mode == "main":
            return rc.directive
        if mode == "tdd":
            return rc.tdd.directive if rc.tdd else ""
        if mode == "followup":
            return rc.followup.directive if rc.followup else ""
        return ""

    if tdd:
        # TDD Phase 1: Write failing tests
        # Directive priority for TDD: CLI > profile.tester.tdd > TddTesterDirective.DEFAULT_TEXT
        _notify(TaskStatus.TESTING)
        tdd_test_directive = TddTesterDirective(
            directive_text=_text_for(Role.TESTER, "tdd"),
        )
        test_write_result = await run_agent(
            tdd_test_directive,
            ctx,
            repo,
            agent_cmd=_agent_for(Role.TESTER),
            use_tmux=use_tmux,
        )
        results.append(test_write_result)

        if test_write_result.status == TaskStatus.FAILED:
            _notify(TaskStatus.FAILED)
            return results

        # TDD Phase 2: Implement to make tests pass
        _notify(TaskStatus.IMPLEMENTING)
        tdd_impl_directive = TddImplementorDirective(
            directive_text=_text_for(Role.IMPLEMENTOR, "tdd"),
        )
        impl_result = await run_agent(
            tdd_impl_directive,
            ctx,
            repo,
            agent_cmd=_agent_for(Role.IMPLEMENTOR),
            use_tmux=use_tmux,
        )
        results.append(impl_result)

        if impl_result.status == TaskStatus.FAILED:
            _notify(TaskStatus.FAILED)
            return results

        # Continue to normal test verification (phase 2) and review (phase 3)
        # regardless of the TDD implementor's self-reported verdict — the
        # dedicated tester is the authoritative source of truth, and a verdict-
        # fail here previously skipped test/review and was silently marked DONE.

    # 1. Implement (skipped in TDD mode — already done above)
    if not tdd:
        _notify(TaskStatus.IMPLEMENTING)
        impl_directive = ImplementorDirective(
            directive_text=_text_for(Role.IMPLEMENTOR),
        )
        impl_result = await run_agent(
            impl_directive,
            ctx,
            repo,
            agent_cmd=_agent_for(Role.IMPLEMENTOR),
            use_tmux=use_tmux,
        )
        results.append(impl_result)

        if impl_result.status == TaskStatus.FAILED:
            _notify(TaskStatus.FAILED)
            return results

    # 2. Test (with retry loop)
    if not skip_test:
        for attempt in range(1, max_retries + 2):  # +2: 1 initial + max_retries fixes
            _notify(TaskStatus.TESTING)
            test_directive = TesterDirective(
                directive_text=_text_for(Role.TESTER),
            )
            test_result = await run_agent(
                test_directive,
                ctx,
                repo,
                agent_cmd=_agent_for(Role.TESTER),
                use_tmux=use_tmux,
            )
            test_result.attempt = attempt
            results.append(test_result)

            if test_result.status == TaskStatus.FAILED:
                # Agent itself crashed — don't retry
                _notify(TaskStatus.FAILED)
                return results

            if test_result.passed:
                break

            # Test failed with feedback — send to fixer
            if attempt <= max_retries:
                _notify(TaskStatus.FIXING)
                feedback = test_result.feedback or test_result.output
                fix_directive = FixerDirective(
                    directive_text=_text_for(Role.FIXER),
                    feedback=feedback,
                    failure_kind="test",
                    attempt=attempt,
                )
                fix_result = await run_agent(
                    fix_directive,
                    ctx,
                    repo,
                    agent_cmd=_agent_for(Role.FIXER),
                    use_tmux=use_tmux,
                )
                fix_result.attempt = attempt
                results.append(fix_result)

                if fix_result.status == TaskStatus.FAILED:
                    _notify(TaskStatus.FAILED)
                    return results
            else:
                # Out of retries
                _notify(TaskStatus.FAILED)
                return results

    # 3. Review (with retry loop)
    #
    # Attempt 1 is a full, comprehensive review against the full task diff.
    # Attempts > 1 are follow-up reviews: they see only the delta since the
    # immediately prior review's SHA, receive that prior review's feedback,
    # and are directed to verify each item was addressed rather than raise
    # new issues. prior_review_sha always tracks the immediately prior review.
    if not skip_review:
        prior_review_sha: str | None = None
        prior_review_feedback: str | None = None
        for attempt in range(1, max_retries + 2):
            _notify(TaskStatus.REVIEWING)

            # HEAD as the reviewer sees it; becomes prior_review_sha for the next attempt.
            current_sha = get_head_sha(worktree) or None

            if attempt == 1:
                rev_directive: PipelineDirective = ReviewerDirective(
                    directive_text=_text_for(Role.REVIEWER),
                )
            else:
                rev_directive = ReviewerFollowupDirective(
                    directive_text=_text_for(Role.REVIEWER, "followup"),
                    prior_review_sha=prior_review_sha or "",
                    prior_feedback=prior_review_feedback or "",
                )

            review_result = await run_agent(
                rev_directive,
                ctx,
                repo,
                agent_cmd=_agent_for(Role.REVIEWER),
                use_tmux=use_tmux,
            )
            review_result.attempt = attempt
            results.append(review_result)

            if review_result.status == TaskStatus.FAILED:
                _notify(TaskStatus.FAILED)
                return results

            if review_result.passed:
                break

            # Capture state for the next follow-up review (always the immediately prior one).
            prior_review_sha = current_sha
            prior_review_feedback = review_result.feedback or review_result.output

            # Review failed with feedback — send to fixer
            if attempt <= max_retries:
                _notify(TaskStatus.FIXING)
                fix_directive = FixerDirective(
                    directive_text=_text_for(Role.FIXER),
                    feedback=prior_review_feedback,
                    failure_kind="review",
                    attempt=attempt,
                )
                fix_result = await run_agent(
                    fix_directive,
                    ctx,
                    repo,
                    agent_cmd=_agent_for(Role.FIXER),
                    use_tmux=use_tmux,
                )
                fix_result.attempt = attempt
                results.append(fix_result)

                if fix_result.status == TaskStatus.FAILED:
                    _notify(TaskStatus.FAILED)
                    return results
            else:
                _notify(TaskStatus.FAILED)
                return results

    _notify(TaskStatus.DONE)
    return results


async def run_merge_resolver(
    task_branch: str,
    session_branch: str,
    merge_dir: Path,
    conflicts: list[str],
    repo: Path,
    agent_cmd: str = "claude",
    use_tmux: bool = True,
    adapter: AgentAdapter | None = None,
    profile: Profile | None = None,
    directive_override: str | None = None,
) -> AgentResult:
    """Run a merge conflict resolution agent in the merge worktree.

    This is a standalone function (not part of the pipeline) called directly
    by the orchestrator when merge conflicts are detected.
    """
    from .directives import MergerDirective

    adapter = adapter or get_adapter(agent_cmd, repo / ".workbench" / "agents.yaml")

    text = directive_override or (profile.merger.directive if profile else "")
    directive = MergerDirective(
        directive_text=text,
        task_branch=task_branch,
        session_branch=session_branch,
        conflicts=conflicts,
    )
    prompt = directive.render()

    try:
        cmd = adapter.build_command(prompt, merge_dir)
        if use_tmux:
            session_name = f"wb-merge-{task_branch.replace('/', '-')}"
            returncode, raw_output = await run_in_tmux(session_name, cmd, merge_dir)
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(merge_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            returncode = proc.returncode
            raw_output = stdout.decode("utf-8", errors="replace")

        output_text, cost_data = adapter.parse_output(raw_output)

        status = TaskStatus.DONE if returncode == 0 else TaskStatus.FAILED

        return AgentResult(
            task_id=task_branch,
            role=Role.MERGER,
            status=status,
            output=output_text if isinstance(output_text, str) else str(output_text),
            cost=cost_data,
        )

    except Exception as e:
        return AgentResult(
            task_id=task_branch,
            role=Role.MERGER,
            status=TaskStatus.FAILED,
            output=f"Merge resolver error: {e}",
        )


# ---------------------------------------------------------------------------
# Planner agent
# ---------------------------------------------------------------------------


def _load_plan_guide() -> str:
    """Load the bundled plan-writing guide."""
    return resources.files("workbench.directive_texts").joinpath("plan_guide.md").read_text()


async def run_planner(
    repo: Path,
    user_prompt: str = "",
    source_content: str = "",
    plan_name: str = "plan",
    agent_cmd: str = "claude",
    use_tmux: bool = True,
    adapter: AgentAdapter | None = None,
    profile: Profile | None = None,
) -> AgentResult:
    """Spawn a planner agent to generate a workbench plan.

    The agent explores the codebase, then writes a plan file to
    ``.workbench/plans/<plan_name>.md``.

    Provide ``user_prompt`` for generation from scratch, ``source_content``
    to transform an existing document, or both for guided transformation.
    """
    from .directives import PlannerDirective

    adapter = adapter or get_adapter(agent_cmd, repo / ".workbench" / "agents.yaml")

    plans_dir = repo / ".workbench" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    output_path = plans_dir / f"{plan_name}.md"

    text = profile.planner.directive if profile else ""
    plan_guide = _load_plan_guide()
    directive = PlannerDirective(
        directive_text=text,
        output_path=output_path,
        user_prompt=user_prompt,
        source_content=source_content,
        plan_guide=plan_guide,
    )
    prompt = directive.render()

    try:
        cmd = adapter.build_command(prompt, repo)
        if use_tmux:
            session_name = f"wb-planner-{plan_name}"
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

        output_text, cost_data = adapter.parse_output(raw_output)

        status = TaskStatus.DONE if returncode == 0 else TaskStatus.FAILED

        return AgentResult(
            task_id=f"planner-{plan_name}",
            role=Role.IMPLEMENTOR,
            status=status,
            output=output_text if isinstance(output_text, str) else str(output_text),
            cost=cost_data,
        )

    except Exception as e:
        return AgentResult(
            task_id=f"planner-{plan_name}",
            role=Role.IMPLEMENTOR,
            status=TaskStatus.FAILED,
            output=f"Planner error: {e}",
        )
