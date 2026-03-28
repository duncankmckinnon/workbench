"""Agent spawning and management via Claude Code CLI."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .plan_parser import Task
from .worktree import Worktree, get_diff, get_main_branch


class Role(str, Enum):
    IMPLEMENTOR = "implementor"
    TESTER = "tester"
    REVIEWER = "reviewer"
    FIXER = "fixer"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    DONE = "done"
    FAILED = "failed"


DEFAULT_DIRECTIVES: dict[Role, str] = {
    Role.IMPLEMENTOR: (
        "You are an implementation agent. Your job is to implement the task described below. "
        "Make clean, well-structured changes. Commit your work when done with a clear commit message. "
        "Do not run tests yourself — a separate agent handles testing.\n"
    ),
    Role.TESTER: (
        "You are a testing agent. Your job is to verify the implementation by:\n"
        "1. Reading the changes made (git diff)\n"
        "2. Running existing tests to check for regressions\n"
        "3. Writing new tests if the plan specifies them\n"
        "4. Reporting pass/fail status\n\n"
        "IMPORTANT: You MUST end your response with exactly one of these lines:\n"
        "VERDICT: PASS\n"
        "VERDICT: FAIL\n\n"
        "If FAIL, explain what failed and what needs to change before the verdict line.\n"
        "Do NOT modify the implementation code. Only add/run tests.\n"
    ),
    Role.REVIEWER: (
        "You are a code review agent. Your job is to review the diff for:\n"
        "1. Correctness — does it match the task description?\n"
        "2. Quality — clean code, no obvious bugs, good patterns\n"
        "3. Completeness — are edge cases handled?\n\n"
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

    # 2. Branch pinning (implementor and fixer commit, so they need this)
    if role in (Role.IMPLEMENTOR, Role.FIXER):
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
) -> AgentResult:
    """Spawn a Claude Code agent in a worktree."""

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
        proc = await asyncio.create_subprocess_exec(
            agent_cmd, "-p", prompt,
            "--output-format", "json",
            "--allowedTools", "Edit,Write,Read,Glob,Grep,Bash(git *),Bash(uv run *),Bash(cd *),Bash(ls *),Bash(npx *)",
            cwd=str(worktree.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        output = stdout.decode("utf-8", errors="replace")

        # Try to parse JSON output
        try:
            result_data = json.loads(output)
            output_text = result_data.get("result", output)
            cost_data = result_data.get("cost_usd", {})
        except (json.JSONDecodeError, TypeError):
            output_text = output
            cost_data = {}

        status = TaskStatus.DONE if proc.returncode == 0 else TaskStatus.FAILED

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

    # 1. Implement
    _notify(TaskStatus.IMPLEMENTING)
    impl_result = await run_agent(
        Role.IMPLEMENTOR, task, worktree, repo, agent_cmd,
        session_branch=session_branch,
        plan_context=plan_context,
        plan_conventions=plan_conventions,
        directive=directives.get(Role.IMPLEMENTOR) if directives else None,
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
                Role.TESTER, task, worktree, repo, agent_cmd,
                session_branch=session_branch,
                plan_context=plan_context,
                plan_conventions=plan_conventions,
                directive=directives.get(Role.TESTER) if directives else None,
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
                    Role.FIXER, task, worktree, repo, agent_cmd,
                    extra_context=f"[Test failure, attempt {attempt}]\n{feedback}",
                    session_branch=session_branch,
                    plan_context=plan_context,
                    plan_conventions=plan_conventions,
                    directive=directives.get(Role.FIXER) if directives else None,
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
                Role.REVIEWER, task, worktree, repo, agent_cmd,
                session_branch=session_branch,
                plan_context=plan_context,
                plan_conventions=plan_conventions,
                directive=directives.get(Role.REVIEWER) if directives else None,
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
                    Role.FIXER, task, worktree, repo, agent_cmd,
                    extra_context=f"[Review failure, attempt {attempt}]\n{feedback}",
                    session_branch=session_branch,
                    plan_context=plan_context,
                    plan_conventions=plan_conventions,
                    directive=directives.get(Role.FIXER) if directives else None,
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
