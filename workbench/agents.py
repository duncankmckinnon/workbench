"""Agent spawning and management via Claude Code CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .adapters import AgentAdapter, get_adapter
from .plan_parser import Task
from .tmux import run_in_tmux
from .worktree import Worktree, get_diff, get_main_branch


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
    Role.IMPLEMENTOR: (
        "You are an implementation agent. Your job is to implement the task described below. "
        "Make clean, well-structured changes. Follow patterns established in the existing codebase if available to reference. "
        "Commit your work when done with a clear commit message. "
        "Do not create and run tests yourself — a separate agent handles testing.\n"
    ),
    Role.TESTER: (
        "You are a testing agent. Your job is to verify the implementation by:\n"
        "1. Reading the changes made (git diff)\n"
        "2. Running existing tests to check for regressions\n"
        "3. Carefully designing tests to cover a full scope of scenarios with respect to the task\n"
        "4. Writing tests that will comprehensively cover the task, and ensure the implementation is correct\n"
        "5. Reporting pass/fail status based on the testability, correctness, and coverage of the tests relative to the task\n\n"
        "IMPORTANT: You MUST end your response with exactly one of these lines:\n"
        "VERDICT: PASS\n"
        "VERDICT: FAIL\n"
        "If FAIL, explain what failed and what needs to change before the verdict line.\n"
        "Do NOT modify the implementation code. Only add/run tests.\n"
    ),
    Role.REVIEWER: (
        "You are a code review agent. Your job is to review the diff for:\n"
        "1. Correctness — does it match the task description? Is it comprehensive?\n"
        "2. Quality - clean code, no obvious bugs, no unnecessary duplication of logic, consistent with patterns used in the codebase\n"
        "3. Completeness — are edge cases handled? Are tests comprehensive?\n"
        "IMPORTANT: You MUST end your response with exactly one of these lines:\n"
        "VERDICT: PASS\n"
        "VERDICT: FAIL\n\n"
        "If FAIL, provide specific, actionable feedback before the verdict line.\n"
        "Do NOT modify any code.\n"
    ),
    Role.FIXER: (
        "You are a fix agent. A previous implementation attempt received feedback "
        "from testing or code review. Your job is to address the feedback, fix the issues, "
        "and commit the changes.\n\n"
        "Do NOT start from scratch. Read the existing code, understand the feedback, "
        "and make targeted fixes.\n"
    ),
    Role.MERGER: (
        "You are a merge conflict resolution agent. A merge between two branches has produced conflicts. "
        "Your job is to resolve ALL merge conflicts in the working tree.\n\n"
        "For each conflicted file:\n"
        "1. Read the file and understand both sides of the conflict\n"
        "2. Resolve the conflict by keeping the correct combination of changes\n"
        "3. The incoming branch (theirs) contains the new feature work\n"
        "4. The target branch (ours) contains previously merged work from other tasks\n"
        "5. In most cases you want BOTH sets of changes integrated correctly\n\n"
        "After resolving all conflicts:\n"
        "1. Stage all resolved files with git add\n"
        "2. Do NOT commit — the orchestrator will handle the merge commit\n\n"
        "IMPORTANT: You MUST end your response with exactly one of:\n"
        "VERDICT: PASS  (all conflicts resolved)\n"
        "VERDICT: FAIL  (unable to resolve one or more conflicts)\n\n"
        "If FAIL, explain which files could not be resolved and why.\n\n"
    ),
}


TDD_DIRECTIVES: dict[Role, str] = {
    Role.TESTER: (
        "You are a test-driven development agent. Your job is to write comprehensive "
        "tests for the task described below BEFORE any implementation exists.\n\n"
        "Write tests that:\n"
        "1. Cover the expected behavior described in the task\n"
        "2. Cover edge cases and error conditions\n"
        "3. Follow the project's existing test patterns and conventions\n"
        "4. Will FAIL because the implementation does not exist yet\n\n"
        "Do NOT implement the feature. Only write tests.\n"
        "Do NOT create stub implementations to make tests pass.\n"
        "Commit your test files when done with a clear commit message.\n"
    ),
    Role.IMPLEMENTOR: (
        "You are an implementation agent working in test-driven development mode. "
        "Tests have already been written for this task and they are currently FAILING.\n\n"
        "Your job is to:\n"
        "1. Read the existing test files to understand what is expected\n"
        "2. Implement the code to make ALL tests pass\n"
        "3. Run the tests to verify they pass\n"
        "4. Evaluate whether the tests are comprehensive enough to validate the task\n"
        "5. Commit your work when done with a clear commit message\n\n"
        "Do NOT modify the test files. Only write implementation code.\n"
        "Do NOT delete or skip any tests.\n\n"
        "IMPORTANT: You MUST end your response with exactly one of these lines:\n"
        "VERDICT: PASS\n"
        "VERDICT: FAIL\n\n"
        "Use VERDICT: PASS if ALL tests pass and the tests comprehensively cover the task.\n"
        "Use VERDICT: FAIL if any tests fail or the tests are not comprehensive enough "
        "to validate the implementation against the task requirements.\n"
        "If FAIL, explain what is missing or broken before the verdict line.\n"
    ),
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
) -> str:
    """Build a structured prompt for any agent role.

    Prompt structure (all roles):
        1. Directive (role instructions — overridable)
        2. Branch pinning
        3. Plan context (if present)
        4. Plan conventions (if present)
        5. Task (title, description, files)
        6. Diff (tester, reviewer, fixer only)
        7. Feedback (fixer only)
        8. Action line
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

    # 6. Diff (for roles that inspect changes)
    if role in (Role.TESTER, Role.REVIEWER, Role.FIXER):
        diff = get_diff(worktree, base_branch)
        label = {
            Role.TESTER: "Changes made",
            Role.REVIEWER: "Diff to review",
            Role.FIXER: "Current changes",
        }[role]
        max_len = 6000 if role == Role.FIXER else 8000
        parts.append(f"## {label}\n\n```diff\n{diff[:max_len]}\n```")

    # 7. Feedback (fixer only)
    if role == Role.FIXER and extra_context:
        parts.append(f"## Feedback to address\n\n{extra_context}")

    # 8. Action line
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
    role: Role,
    task: Task,
    worktree: Worktree,
    repo: Path,
    agent_cmd: str = "claude",
    extra_context: str = "",
    session_branch: str | None = None,
    plan_context: str = "",
    plan_conventions: str = "",
    directive: str | None = None,
    use_tmux: bool = True,
    adapter: AgentAdapter | None = None,
) -> AgentResult:
    """Spawn a Claude Code agent in a worktree."""

    adapter = adapter or get_adapter(agent_cmd, repo / ".workbench" / "agents.yaml")

    base = session_branch or get_main_branch(repo)
    prompt = build_prompt(
        role=role,
        task=task,
        worktree=worktree,
        base_branch=base,
        plan_context=plan_context,
        plan_conventions=plan_conventions,
        directive=directive,
        extra_context=extra_context,
    )

    try:
        cmd = adapter.build_command(prompt, worktree.path)
        if use_tmux:
            session_name = f"wb-{task.id}-{role.value}"
            returncode, raw_output = await run_in_tmux(session_name, cmd, worktree.path)
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(worktree.path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            returncode = proc.returncode
            raw_output = stdout.decode("utf-8", errors="replace")

        output_text, cost_data = adapter.parse_output(raw_output)

        status = TaskStatus.DONE if returncode == 0 else TaskStatus.FAILED

        return AgentResult(
            task_id=task.id,
            role=role,
            status=status,
            output=output_text if isinstance(output_text, str) else str(output_text),
            cost=cost_data,
        )

    except Exception as e:
        return AgentResult(
            task_id=task.id,
            role=role,
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
    results: list[AgentResult] = []

    def _notify(status: TaskStatus):
        if on_status_change:
            on_status_change(task.id, status)

    if tdd:
        # TDD Phase 1: Write failing tests
        _notify(TaskStatus.TESTING)
        tdd_test_directive = (directives or {}).get(Role.TESTER) or TDD_DIRECTIVES[Role.TESTER]
        test_write_result = await run_agent(
            Role.TESTER,
            task,
            worktree,
            repo,
            agent_cmd,
            session_branch=session_branch,
            plan_context=plan_context,
            plan_conventions=plan_conventions,
            directive=tdd_test_directive,
            use_tmux=use_tmux,
        )
        results.append(test_write_result)

        if test_write_result.status == TaskStatus.FAILED:
            _notify(TaskStatus.FAILED)
            return results

        # TDD Phase 2: Implement to make tests pass
        _notify(TaskStatus.IMPLEMENTING)
        tdd_impl_directive = (directives or {}).get(Role.IMPLEMENTOR) or TDD_DIRECTIVES[
            Role.IMPLEMENTOR
        ]
        impl_result = await run_agent(
            Role.IMPLEMENTOR,
            task,
            worktree,
            repo,
            agent_cmd,
            session_branch=session_branch,
            plan_context=plan_context,
            plan_conventions=plan_conventions,
            directive=tdd_impl_directive,
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
        impl_result = await run_agent(
            Role.IMPLEMENTOR,
            task,
            worktree,
            repo,
            agent_cmd,
            session_branch=session_branch,
            plan_context=plan_context,
            plan_conventions=plan_conventions,
            directive=directives.get(Role.IMPLEMENTOR) if directives else None,
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
            test_result = await run_agent(
                Role.TESTER,
                task,
                worktree,
                repo,
                agent_cmd,
                session_branch=session_branch,
                plan_context=plan_context,
                plan_conventions=plan_conventions,
                directive=directives.get(Role.TESTER) if directives else None,
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
                feedback = test_result.feedback or test_result.output[:2000]
                fix_result = await run_agent(
                    Role.FIXER,
                    task,
                    worktree,
                    repo,
                    agent_cmd,
                    extra_context=f"[Test failure, attempt {attempt}]\n{feedback}",
                    session_branch=session_branch,
                    plan_context=plan_context,
                    plan_conventions=plan_conventions,
                    directive=directives.get(Role.FIXER) if directives else None,
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
    if not skip_review:
        for attempt in range(1, max_retries + 2):
            _notify(TaskStatus.REVIEWING)
            review_result = await run_agent(
                Role.REVIEWER,
                task,
                worktree,
                repo,
                agent_cmd,
                session_branch=session_branch,
                plan_context=plan_context,
                plan_conventions=plan_conventions,
                directive=directives.get(Role.REVIEWER) if directives else None,
                use_tmux=use_tmux,
            )
            review_result.attempt = attempt
            results.append(review_result)

            if review_result.status == TaskStatus.FAILED:
                _notify(TaskStatus.FAILED)
                return results

            if review_result.passed:
                break

            # Review failed with feedback — send to fixer
            if attempt <= max_retries:
                _notify(TaskStatus.FIXING)
                feedback = review_result.feedback or review_result.output[:2000]
                fix_result = await run_agent(
                    Role.FIXER,
                    task,
                    worktree,
                    repo,
                    agent_cmd,
                    extra_context=f"[Review failure, attempt {attempt}]\n{feedback}",
                    session_branch=session_branch,
                    plan_context=plan_context,
                    plan_conventions=plan_conventions,
                    directive=directives.get(Role.FIXER) if directives else None,
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
) -> AgentResult:
    """Run a merge conflict resolution agent in the merge worktree.

    This is a standalone function (not part of the pipeline) called directly
    by the orchestrator when merge conflicts are detected.
    """
    adapter = adapter or get_adapter(agent_cmd, repo / ".workbench" / "agents.yaml")

    prompt_parts = [
        DEFAULT_DIRECTIVES[Role.MERGER],
        f"Merging branch '{task_branch}' into '{session_branch}'",
        "Conflicted files:\n" + "\n".join(f"  - {c}" for c in conflicts),
        "Read each file, resolve the conflict markers, and stage with git add.",
    ]
    prompt = "\n\n".join(prompt_parts)

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
