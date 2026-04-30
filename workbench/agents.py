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
from .worktree import Worktree, get_diff, get_diff_since, get_head_sha, get_main_branch

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


DEFAULT_DIRECTIVES: dict[Role, str] = {
    Role.IMPLEMENTOR: """\
You are an implementation agent. Your job is to implement the task described below.
Make clean, well-structured changes. Follow patterns established in the existing codebase if available to reference.
Commit your work when done with a clear commit message.
Do not create and run tests yourself — a separate agent handles testing.""",
    Role.TESTER: """\
You are a testing agent. Your job is to verify the implementation by:
1. Reading the changes made (git diff)
2. Determining what aspects of the changes can be meaningfully tested
3. Running existing tests to check for regressions
4. Carefully designing tests to cover a full scope of scenarios with respect to the task
5. Writing tests that will comprehensively cover the task, and ensure the implementation is correct
6. Reporting pass/fail status based on the testability, correctness, and coverage of the tests relative to the task

IMPORTANT: You MUST end your response with exactly one of these lines:
VERDICT: PASS
VERDICT: FAIL
If FAIL, explain what failed and what needs to change before the verdict line.
Do NOT modify the implementation code. Only add/run tests.

When changes are not directly testable (configuration, documentation, CI/CD,
visual/UI, or code requiring unavailable external dependencies):
- Verify syntax, structure, and correctness by other means (lint, parse, dry-run)
- Check for obvious errors (typos, broken references, invalid values)
- Run existing tests to confirm no regressions
- Add a note on what was verified and why full testing was not possible
- End your response with VERDICT: PASS
Do not force meaningless tests or fail solely because automated tests cannot cover the change.""",
    Role.REVIEWER: """\
You are a code review agent. Your job is to review the diff for:
1. Correctness — does it match the task description? Is it comprehensive?
2. Quality - clean code, no obvious bugs, no unnecessary duplication of logic, consistent with patterns used in the codebase
3. Completeness — are edge cases handled? Are tests comprehensive?

Be thorough and comprehensive: this is your one chance to identify every issue.
Any follow-up review will only verify that your feedback was addressed — it will
not raise new concerns. Surface everything you want fixed now.

IMPORTANT: You MUST end your response with exactly one of these lines:
VERDICT: PASS
VERDICT: FAIL

If FAIL, provide specific, actionable feedback before the verdict line.
Do NOT modify any code.""",
    Role.FIXER: """\
You are a fix agent. A previous implementation attempt received feedback from testing or code review.
Your job is to address the feedback, fix the issues, and commit the changes.

Do NOT start from scratch. Read the existing code, understand the feedback, and make targeted fixes.""",
    Role.MERGER: """\
You are a merge conflict resolution agent. A merge between two branches has produced conflicts.
Your job is to resolve ALL merge conflicts in the working tree.

For each conflicted file:
1. Read the file and understand both sides of the conflict
2. Resolve the conflict by keeping the correct combination of changes
3. The incoming branch (theirs) contains the new feature work
4. The target branch (ours) contains previously merged work from other tasks
5. In most cases you want BOTH sets of changes integrated correctly

After resolving all conflicts:
1. Stage all resolved files with git add
2. Do NOT commit — the orchestrator will handle the merge commit

IMPORTANT: You MUST end your response with exactly one of:
VERDICT: PASS  (all conflicts resolved)
VERDICT: FAIL  (unable to resolve one or more conflicts)

If FAIL, explain which files could not be resolved and why.""",
}


REVIEWER_FOLLOWUP_DIRECTIVE = """\
You are a follow-up code review agent. You previously reviewed this task and
produced the feedback shown below. A fixer agent has since made changes — the
diff below is only the delta since your prior review, not the full task diff.

Your job is narrow: verify that every item in your prior feedback has been
addressed by the changes shown. Do NOT raise new issues beyond your prior
feedback. The only exception is a regression the fixer introduced within the
changed lines shown below — if the fix itself broke something, flag it.

IMPORTANT: You MUST end your response with exactly one of these lines:
VERDICT: PASS — every prior feedback item is addressed (and no regressions)
VERDICT: FAIL — one or more prior items remain, or the fix introduced a regression

If FAIL, list specifically which prior feedback items remain unaddressed.
Do NOT modify any code."""


TDD_DIRECTIVES: dict[Role, str] = {
    Role.TESTER: """\
You are a test-driven development agent. Your job is to write comprehensive
tests for the task described below BEFORE any implementation exists.

Write tests that:
1. Cover the expected behavior described in the task
2. Cover edge cases and error conditions
3. Follow the project's existing test patterns and conventions
4. Will FAIL because the implementation does not exist yet

Do NOT implement the feature. Only write tests.
Do NOT create stub implementations to make tests pass.
Commit your test files when done with a clear commit message.""",
    Role.IMPLEMENTOR: """\
You are an implementation agent working in test-driven development mode.
Tests have already been written for this task and they are currently FAILING.

Your job is to:
1. Read the existing test files to understand what is expected
2. Implement the code to make ALL tests pass
3. Run the tests to verify they pass
4. Evaluate whether the tests are comprehensive enough to validate the task
5. Commit your work when done with a clear commit message

Do NOT modify the test files. Only write implementation code.
Do NOT delete or skip any tests.

IMPORTANT: You MUST end your response with exactly one of these lines:
VERDICT: PASS
VERDICT: FAIL

Use VERDICT: PASS if ALL tests pass and the tests comprehensively cover the task.
Use VERDICT: FAIL if any tests fail or the tests are not comprehensive enough
to validate the implementation against the task requirements.
If FAIL, explain what is missing or broken before the verdict line.""",
}


def build_prompt(
    role: Role,
    task: Task,
    worktree: Worktree,
    base_branch: str,
    plan_context: str = "",
    plan_conventions: str = "",
    directive: str | None = None,
    extra_context: str = "",
    prior_review_sha: str | None = None,
) -> str:
    """Build a structured prompt for any agent role.

    Prompt structure (all roles):
        1. Directive (role instructions — overridable)
        2. Branch pinning
        3. Plan context (if present)
        4. Plan conventions (if present)
        5. Task (title, description, files)
        6. Prior review feedback (reviewer follow-up only)
        7. Diff (tester, reviewer, fixer only)
        8. Feedback (fixer only)
        9. Action line

    When ``role == Role.REVIEWER`` and ``prior_review_sha`` is provided, the
    reviewer is in follow-up mode: the diff shown is only the delta since that
    SHA (the fixer's changes since the prior review), and ``extra_context`` is
    rendered as the prior review's feedback for the reviewer to verify against.
    """
    parts: list[str] = []

    # 1. Directive
    parts.append(directive or DEFAULT_DIRECTIVES[role])

    # 2. Branch pinning (roles that commit to a branch)
    if role in (Role.IMPLEMENTOR, Role.TESTER, Role.FIXER, Role.MERGER):
        parts.append(
            f"IMPORTANT: You are working on branch '{worktree.branch}'. "
            "Stay on this branch. Do not create new branches or switch branches. "
            "Commit all changes directly to this branch."
        )

    # 3. Plan context
    if plan_context:
        parts.append(f"## Context\n\n{plan_context}")

    # 4. Plan conventions
    if plan_conventions:
        parts.append(f"## Conventions\n\n{plan_conventions}")

    # 5. Task
    parts.append(f"## Task: {task.title}\n\n{task.description}")
    if task.files:
        parts.append(f"Relevant files: {', '.join(task.files)}")

    reviewer_followup = role == Role.REVIEWER and prior_review_sha is not None

    # 6. Prior review feedback (reviewer follow-up only)
    if reviewer_followup and extra_context:
        parts.append(f"## Prior review feedback\n\n{extra_context}")

    # 7. Diff (for roles that inspect changes)
    if role in (Role.TESTER, Role.REVIEWER, Role.FIXER):
        if reviewer_followup:
            diff = get_diff_since(worktree, prior_review_sha)
            label = "Changes since prior review"
        else:
            diff = get_diff(worktree, base_branch)
            label = {
                Role.TESTER: "Changes made",
                Role.REVIEWER: "Diff to review",
                Role.FIXER: "Current changes",
            }[role]
        parts.append(f"## {label}\n\n```diff\n{diff}\n```")

    # 8. Feedback (fixer only)
    if role == Role.FIXER and extra_context:
        parts.append(f"## Feedback to address\n\n{extra_context}")

    # 9. Action line
    if reviewer_followup:
        action = (
            "Verify each item in the prior review feedback has been addressed "
            "by the changes above. Do not raise new issues."
        )
    else:
        action = {
            Role.IMPLEMENTOR: "Implement this task and commit your changes.",
            Role.TESTER: "Run tests and verify the implementation.",
            Role.REVIEWER: "Provide your review.",
            Role.FIXER: "Fix the issues described above and commit your changes.",
            Role.MERGER: "Resolve all merge conflicts, stage the resolved files, and do not commit.",
        }[role]
    parts.append(action)

    return "\n\n".join(parts)


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

        if not impl_result.passed:
            _notify(TaskStatus.FAILED)
            return results

        # Continue to normal test verification (phase 2) and review (phase 3)
        # The existing test/review loop below will verify the implementation
        # and handle fix retries as normal.

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

PLANNER_DIRECTIVE = """\
You are a planning agent for the Workbench multi-agent orchestrator. Your job
is to take a user's request and produce a detailed workbench plan file that
can be executed by `wb run` to dispatch parallel coding agents.

## Your Process

1. **Understand the request** — What is the user trying to achieve?
2. **Survey the codebase** — Read the code to understand:
   - Project structure, module organization, entry points
   - Existing patterns — how are similar things already done?
   - Dependencies and interfaces between modules
   - Test infrastructure — framework, location, test command
   - Build and config files
3. **Design the task graph** — Break work into parallel-safe tasks:
   - Group work by file ownership (each task owns distinct files)
   - Push shared infrastructure to earlier waves using `Depends:`
   - Maximize parallelism while avoiding merge conflicts
4. **Write the plan** — Output a complete, detailed plan following the guide below.

## Critical Rules

- Each task runs in an ISOLATED worktree — the agent only sees its own
  task description. Every task must be completely self-contained.
- Tasks in the same wave run simultaneously and CANNOT see each other's
  changes. Same-file edits across parallel tasks cause merge conflicts.
- If a task depends on interfaces from an earlier wave, describe those
  interfaces IN FULL in the dependent task — the agent cannot look them up.
- Keep task titles to 2-4 words (they become dependency slugs).
- Always specify the test command in each task.
- Write the plan to the output path specified at the end of this prompt."""


def _load_plan_guide() -> str:
    """Load the bundled plan-writing guide."""
    guide_path = Path(resources.files("workbench")) / "plan_guide.md"
    return guide_path.read_text()


def build_planner_prompt(
    output_path: Path,
    user_prompt: str = "",
    source_content: str = "",
) -> str:
    """Build the full prompt for the planner agent.

    Args:
        output_path: Where to write the plan file.
        user_prompt: A natural language description of what to build.
        source_content: Content of an existing document to transform into
            workbench plan format. When provided, the planner restructures
            it into proper task format. Can be combined with ``user_prompt``
            for additional guidance on the transformation.
    """
    parts = [
        PLANNER_DIRECTIVE,
        f"## Plan Writing Guide\n\n{_load_plan_guide()}",
    ]

    if source_content:
        parts.append(
            "## Source Document\n\n"
            "The following document describes work to be done. Transform it into "
            "a workbench plan that follows the plan writing guide above. Preserve "
            "the intent and scope of the original document, but restructure it "
            "into proper `## Task:` sections with Files, Depends, detailed "
            "descriptions, and test plans. Survey the codebase to fill in "
            "specifics (file paths, function signatures, conventions) that the "
            "source document may have left vague.\n\n"
            f"{source_content}"
        )

    if user_prompt:
        label = "Additional Guidance" if source_content else "User Request"
        parts.append(f"## {label}\n\n{user_prompt}")

    parts.append(
        f"## Output\n\nWrite the plan to: {output_path}\n\n"
        "Explore the codebase thoroughly before writing. The plan must be "
        "detailed enough that each task can be implemented by an agent that "
        "has never seen the rest of the plan."
    )
    return "\n\n".join(parts)


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
