"""Agent spawning and management via Claude Code CLI."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapters import AgentAdapter, get_adapter
from .headroom import HeadroomConfig, apply_headroom_env
from .plan_parser import Task
from .session_metadata import SessionMetadata, merge_trace_env, with_session_metadata
from .tmux import run_in_tmux
from .worktree import Worktree, get_head_sha, get_main_branch

if TYPE_CHECKING:
    from .directives import PipelineDirective, PromptContext
    from .profile import Profile, RoleConfig


def resolve_model(
    *,
    role: str,
    agent: str,
    cli_models: dict[str, str] | None,
    profile: Profile | None,
    plan_models: dict[str, str] | None,
) -> str | None:
    """Resolve the model for a (role, agent). Returns None if unset.

    Precedence: CLI (agent key, then "" key) > profile.<role>.model
    (if non-empty) > plan (agent key, then "" key) > None.
    """

    def _pick(d: dict[str, str] | None) -> str | None:
        if not d:
            return None
        return d.get(agent) or d.get("") or None

    cli = _pick(cli_models)
    if cli:
        return cli
    if profile is not None:
        pm = getattr(profile, role, None)
        if pm is not None and getattr(pm, "model", ""):
            return pm.model
    return _pick(plan_models)


class Role(StrEnum):
    IMPLEMENTOR = "implementor"
    TESTER = "tester"
    REVIEWER = "reviewer"
    FIXER = "fixer"
    MERGER = "merger"
    SUMMARIZER = "summarizer"
    BRANCH_REVIEWER = "branch_reviewer"
    PR_WRITER = "pr_writer"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    MERGING = "merging"
    DONE = "done"
    FAILED = "failed"


# Stage names for the TDD-specific pipeline phases (write-failing-tests,
# make-tests-pass). Distinct from Role.TESTER/Role.IMPLEMENTOR so resume
# tracking can tell a TDD phase apart from the regular implement/verify
# stages that share the same Role.
TDD_TEST_STAGE = "tdd-test"
TDD_IMPLEMENT_STAGE = "tdd-implement"

# Step prefixes for the two fixer invocations. Steps are rendered as
# f"{PREFIX}#{attempt}"; resume tracking reads the prefix back to tell which
# stage a fixer invalidated (a test-fixer edits code the tester signed off on,
# a review-fixer only invalidates the review).
TEST_FIX_STEP_PREFIX = "test-fix"
REVIEW_FIX_STEP_PREFIX = "review-fix"


@dataclass
class AgentResult:
    task_id: str
    role: Role
    status: TaskStatus
    output: str
    attempt: int = 1
    cost: dict[str, Any] = field(default_factory=dict)
    step: str = ""

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
    agents_config_paths: list[Path] | None = None,
    meta: SessionMetadata | None = None,
    trace_env: bool = True,
    trace_prompt: bool = False,
    model: str | None = None,
    headroom: HeadroomConfig | None = None,
) -> AgentResult:
    """Spawn an agent in a worktree to run a single pipeline stage."""
    if adapter is None:
        paths = (
            agents_config_paths
            if agents_config_paths is not None
            else [repo / ".workbench" / "agents.yaml"]
        )
        adapter = get_adapter(agent_cmd, paths)
    if meta is not None:
        meta = replace(meta, agent=directive.role.value)
    prompt = directive.render(ctx)
    if trace_prompt:
        prompt = with_session_metadata(prompt, meta)
    effective_task_id = task_id or ctx.task.id

    env: dict[str, str] | None = None
    if trace_env and meta is not None and adapter.config.inject_env:
        env = merge_trace_env(os.environ, meta)
    if headroom is not None and headroom.enabled:
        base = env if env is not None else dict(os.environ)
        env = apply_headroom_env(base, agent_cmd, headroom)

    try:
        cmd = adapter.build_command(prompt, ctx.worktree.path, model)
        if use_tmux:
            session_name = f"wb-{effective_task_id}-{directive.role.value}"
            returncode, raw_output = await run_in_tmux(
                session_name, cmd, ctx.worktree.path, env=env
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ctx.worktree.path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
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
            step=meta.step if meta else "",
        )

    except Exception as e:
        return AgentResult(
            task_id=effective_task_id,
            role=directive.role,
            status=TaskStatus.FAILED,
            output=f"Agent error: {e}",
            step=meta.step if meta else "",
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
    on_result: callable = None,
    resume_completed_stages: list[str] | None = None,
    prior_results: list[AgentResult] | None = None,
    session_branch: str | None = None,
    plan_context: str = "",
    plan_conventions: str = "",
    directives: dict[Role, str] | None = None,
    use_tmux: bool = True,
    tdd: bool = False,
    profile: Profile | None = None,
    agents_config_paths: list[Path] | None = None,
    plan_name: str = "",
    wave_num: int | None = None,
    trace_env: bool = True,
    trace_prompt: bool = False,
    cli_models: dict[str, str] | None = None,
    plan_models: dict[str, str] | None = None,
    headroom: HeadroomConfig | None = None,
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

    # Prior results are seeded into the returned list but not replayed through
    # `on_result`; the caller already has them on disk.
    results: list[AgentResult] = list(prior_results or [])
    completed: set[str] = set(resume_completed_stages or [])
    base = session_branch or get_main_branch(repo)
    ctx = PromptContext(
        task=task,
        worktree=worktree,
        base_branch=base,
        plan_context=plan_context,
        plan_conventions=plan_conventions,
    )
    base_meta = SessionMetadata(
        plan=plan_name,
        wave=wave_num,
        task=task.id,
        task_title=task.slug,
    )

    def _notify(status: TaskStatus):
        if on_status_change:
            on_status_change(task.id, status)

    def _record(result: AgentResult):
        results.append(result)
        if on_result:
            on_result(result)

    def _agent_for(role: Role) -> str:
        """Resolve effective agent_cmd for a role."""
        if profile and agent_cmd == "claude":
            return getattr(profile, role.value).agent
        return agent_cmd

    def _model_for(role: Role) -> str | None:
        return resolve_model(
            role=role.value,
            agent=_agent_for(role),
            cli_models=cli_models,
            profile=profile,
            plan_models=plan_models,
        )

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
        if TDD_TEST_STAGE not in completed:
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
                agents_config_paths=agents_config_paths,
                meta=replace(base_meta, step=TDD_TEST_STAGE),
                trace_env=trace_env,
                trace_prompt=trace_prompt,
                model=_model_for(Role.TESTER),
                headroom=headroom,
            )
            _record(test_write_result)

            if test_write_result.status == TaskStatus.FAILED:
                _notify(TaskStatus.FAILED)
                return results

        # TDD Phase 2: Implement to make tests pass
        if TDD_IMPLEMENT_STAGE not in completed:
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
                agents_config_paths=agents_config_paths,
                meta=replace(base_meta, step=TDD_IMPLEMENT_STAGE),
                trace_env=trace_env,
                trace_prompt=trace_prompt,
                model=_model_for(Role.IMPLEMENTOR),
                headroom=headroom,
            )
            _record(impl_result)

            if impl_result.status == TaskStatus.FAILED:
                _notify(TaskStatus.FAILED)
                return results

        # Continue to normal test verification (phase 2) and review (phase 3)
        # regardless of the TDD implementor's self-reported verdict — the
        # dedicated tester is the authoritative source of truth, and a verdict-
        # fail here previously skipped test/review and was silently marked DONE.

    # 1. Implement (skipped in TDD mode — already done above)
    if not tdd and Role.IMPLEMENTOR.value not in completed:
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
            agents_config_paths=agents_config_paths,
            meta=replace(base_meta, step="implement"),
            trace_env=trace_env,
            trace_prompt=trace_prompt,
            model=_model_for(Role.IMPLEMENTOR),
            headroom=headroom,
        )
        _record(impl_result)

        if impl_result.status == TaskStatus.FAILED:
            _notify(TaskStatus.FAILED)
            return results

    # 2. Test (with retry loop)
    if not skip_test and Role.TESTER.value not in completed:
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
                agents_config_paths=agents_config_paths,
                meta=replace(base_meta, step=f"test#{attempt}"),
                trace_env=trace_env,
                trace_prompt=trace_prompt,
                model=_model_for(Role.TESTER),
                headroom=headroom,
            )
            test_result.attempt = attempt
            _record(test_result)

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
                    agents_config_paths=agents_config_paths,
                    meta=replace(base_meta, step=f"{TEST_FIX_STEP_PREFIX}#{attempt}"),
                    trace_env=trace_env,
                    trace_prompt=trace_prompt,
                    model=_model_for(Role.FIXER),
                    headroom=headroom,
                )
                fix_result.attempt = attempt
                _record(fix_result)

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
    if not skip_review and Role.REVIEWER.value not in completed:
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
                agents_config_paths=agents_config_paths,
                meta=replace(base_meta, step=f"review#{attempt}"),
                trace_env=trace_env,
                trace_prompt=trace_prompt,
                model=_model_for(Role.REVIEWER),
                headroom=headroom,
            )
            review_result.attempt = attempt
            _record(review_result)

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
                    agents_config_paths=agents_config_paths,
                    meta=replace(base_meta, step=f"{REVIEW_FIX_STEP_PREFIX}#{attempt}"),
                    trace_env=trace_env,
                    trace_prompt=trace_prompt,
                    model=_model_for(Role.FIXER),
                    headroom=headroom,
                )
                fix_result.attempt = attempt
                _record(fix_result)

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
    agents_config_paths: list[Path] | None = None,
    plan_name: str = "",
    trace_env: bool = True,
    trace_prompt: bool = False,
    model: str | None = None,
    headroom: HeadroomConfig | None = None,
) -> AgentResult:
    """Run a merge conflict resolution agent in the merge worktree.

    This is a standalone function (not part of the pipeline) called directly
    by the orchestrator when merge conflicts are detected.
    """
    from .directives import MergerDirective

    if adapter is None:
        paths = (
            agents_config_paths
            if agents_config_paths is not None
            else [repo / ".workbench" / "agents.yaml"]
        )
        adapter = get_adapter(agent_cmd, paths)

    text = directive_override or (profile.merger.directive if profile else "")
    directive = MergerDirective(
        directive_text=text,
        task_branch=task_branch,
        session_branch=session_branch,
        conflicts=conflicts,
    )
    prompt = directive.render()
    meta = SessionMetadata(
        plan=plan_name,
        task=task_branch,
        agent="merger",
        step="merge",
    )
    if trace_prompt:
        prompt = with_session_metadata(prompt, meta)
    env: dict[str, str] | None = None
    if trace_env and adapter.config.inject_env:
        env = merge_trace_env(os.environ, meta)
    if headroom is not None and headroom.enabled:
        base = env if env is not None else dict(os.environ)
        env = apply_headroom_env(base, agent_cmd, headroom)

    try:
        cmd = adapter.build_command(prompt, merge_dir, model)
        if use_tmux:
            session_name = f"wb-merge-{task_branch.replace('/', '-')}"
            returncode, raw_output = await run_in_tmux(session_name, cmd, merge_dir, env=env)
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(merge_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
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
    """Load the plan-writing guide from the plan-workbench skill.

    The skill is the single source of truth for plan-authoring conventions;
    the planner prompt is built from the same text a human would read.
    """
    text = resources.files("workbench.skills").joinpath("plan-workbench", "SKILL.md").read_text()
    if text.startswith("---"):
        frontmatter_end = text.index("\n---", 3)
        text = text[frontmatter_end + len("\n---") :].lstrip("\n")
    return text


async def run_planner(
    repo: Path,
    user_prompt: str = "",
    source_content: str = "",
    plan_name: str = "plan",
    agent_cmd: str = "claude",
    use_tmux: bool = True,
    adapter: AgentAdapter | None = None,
    profile: Profile | None = None,
    agents_config_paths: list[Path] | None = None,
    conventions_text: str = "",
    trace_env: bool = True,
    trace_prompt: bool = False,
    model: str | None = None,
    headroom: HeadroomConfig | None = None,
) -> AgentResult:
    """Spawn a planner agent to generate a workbench plan.

    The agent explores the codebase, then writes a plan file to
    ``.workbench/<plan_name>/plan.md``.

    Provide ``user_prompt`` for generation from scratch, ``source_content``
    to transform an existing document, or both for guided transformation.
    """
    from .directives import PlannerDirective

    if adapter is None:
        paths = (
            agents_config_paths
            if agents_config_paths is not None
            else [repo / ".workbench" / "agents.yaml"]
        )
        adapter = get_adapter(agent_cmd, paths)

    plan_dir = repo / ".workbench" / plan_name
    plan_dir.mkdir(parents=True, exist_ok=True)
    output_path = plan_dir / "plan.md"

    text = profile.planner.directive if profile else ""
    plan_guide = _load_plan_guide()
    directive = PlannerDirective(
        directive_text=text,
        output_path=output_path,
        user_prompt=user_prompt,
        source_content=source_content,
        plan_guide=plan_guide,
        conventions_text=conventions_text,
    )
    prompt = directive.render()
    meta = SessionMetadata(plan=plan_name, agent="planner", step="plan")
    if trace_prompt:
        prompt = with_session_metadata(prompt, meta)
    env: dict[str, str] | None = None
    if trace_env and adapter.config.inject_env:
        env = merge_trace_env(os.environ, meta)
    if headroom is not None and headroom.enabled:
        base = env if env is not None else dict(os.environ)
        env = apply_headroom_env(base, agent_cmd, headroom)

    try:
        cmd = adapter.build_command(prompt, repo, model)
        if use_tmux:
            session_name = f"wb-planner-{plan_name}"
            returncode, raw_output = await run_in_tmux(session_name, cmd, repo, env=env)
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
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
