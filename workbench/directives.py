"""Directive classes — typed prompt builders for each agent role."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from .agents import Role
from .worktree import get_diff, get_diff_since

if TYPE_CHECKING:
    from .plan_parser import Task
    from .worktree import Worktree


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class PromptContext:
    """Inputs every pipeline directive needs to render."""

    task: Task
    worktree: Worktree
    base_branch: str
    plan_context: str = ""
    plan_conventions: str = ""


@dataclass(kw_only=True)
class Directive:
    """Base directive. Concrete subclasses define DEFAULT_TEXT and render()."""

    DEFAULT_TEXT: ClassVar[str] = ""
    role: Role
    directive_text: str = ""

    def resolved_text(self) -> str:
        """Return overridden directive_text if set, else the class DEFAULT_TEXT."""
        return self.directive_text or self.DEFAULT_TEXT


@dataclass(kw_only=True)
class PipelineDirective(Directive):
    """Directives used inside the implement->test->review->fix pipeline.
    All require a PromptContext to render."""

    def render(self, ctx: PromptContext) -> str:
        raise NotImplementedError


@dataclass(kw_only=True)
class StandaloneDirective(Directive):
    """Directives invoked outside the pipeline (planner, merger).
    All required inputs live on `self`; no PromptContext needed."""

    def render(self) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared pipeline rendering helper
# ---------------------------------------------------------------------------


def _render_pipeline(
    directive: PipelineDirective,
    ctx: PromptContext,
    *,
    include_diff: bool = False,
    diff_label: str = "",
    include_branch_pinning: bool = True,
    action: str = "",
    prior_feedback: str = "",
    prior_review_sha: str = "",
    feedback_section: str = "",
) -> str:
    """Assemble the standard pipeline prompt sections.

    Section order:
      1. Directive text
      2. Branch pinning (optional)
      3. Plan context (if non-empty)
      4. Plan conventions (if non-empty)
      5. Task (title + description + files)
      6. Prior review feedback (reviewer-followup only)
      7. Diff (tester, reviewer, fixer — optional)
      8. Feedback to address (fixer only)
      9. Action line
    """
    parts: list[str] = []

    # 1. Directive
    parts.append(directive.resolved_text())

    # 2. Branch pinning
    if include_branch_pinning:
        parts.append(
            f"IMPORTANT: You are working on branch '{ctx.worktree.branch}'. "
            "Stay on this branch. Do not create new branches or switch branches. "
            "Commit all changes directly to this branch."
        )

    # 3. Plan context
    if ctx.plan_context:
        parts.append(f"## Context\n\n{ctx.plan_context}")

    # 4. Plan conventions
    if ctx.plan_conventions:
        parts.append(f"## Conventions\n\n{ctx.plan_conventions}")

    # 5. Task
    parts.append(f"## Task: {ctx.task.title}\n\n{ctx.task.description}")
    if ctx.task.files:
        parts.append(f"Relevant files: {', '.join(ctx.task.files)}")

    # 6. Prior review feedback
    if prior_feedback:
        parts.append(f"## Prior review feedback\n\n{prior_feedback}")

    # 7. Diff
    if include_diff:
        if prior_review_sha:
            diff = get_diff_since(ctx.worktree, prior_review_sha)
        else:
            diff = get_diff(ctx.worktree, ctx.base_branch)
        parts.append(f"## {diff_label}\n\n```diff\n{diff}\n```")

    # 8. Feedback to address
    if feedback_section:
        parts.append(f"## Feedback to address\n\n{feedback_section}")

    # 9. Action line
    parts.append(action)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Concrete pipeline directives
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class ImplementorDirective(PipelineDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
You are an implementation agent. Your job is to implement the task described below.
Make clean, well-structured changes. Follow patterns established in the existing codebase if available to reference.
Commit your work when done with a clear commit message.
Do not create and run tests yourself — a separate agent handles testing."""
    role: Role = Role.IMPLEMENTOR

    def render(self, ctx: PromptContext) -> str:
        return _render_pipeline(
            self,
            ctx,
            include_diff=False,
            action="Implement this task and commit your changes.",
        )


@dataclass(kw_only=True)
class TesterDirective(PipelineDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
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
Do not force meaningless tests or fail solely because automated tests cannot cover the change."""
    role: Role = Role.TESTER

    def render(self, ctx: PromptContext) -> str:
        return _render_pipeline(
            self,
            ctx,
            include_diff=True,
            diff_label="Changes made",
            action="Run tests and verify the implementation.",
        )


@dataclass(kw_only=True)
class ReviewerDirective(PipelineDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
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
Do NOT modify any code."""
    role: Role = Role.REVIEWER

    def render(self, ctx: PromptContext) -> str:
        return _render_pipeline(
            self,
            ctx,
            include_diff=True,
            diff_label="Diff to review",
            include_branch_pinning=False,
            action="Provide your review.",
        )


@dataclass(kw_only=True)
class ReviewerFollowupDirective(PipelineDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
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
    role: Role = Role.REVIEWER
    prior_review_sha: str
    prior_feedback: str

    def render(self, ctx: PromptContext) -> str:
        return _render_pipeline(
            self,
            ctx,
            include_diff=True,
            diff_label="Changes since prior review",
            include_branch_pinning=False,
            action=(
                "Verify each item in the prior review feedback has been addressed "
                "by the changes above. Do not raise new issues."
            ),
            prior_feedback=self.prior_feedback,
            prior_review_sha=self.prior_review_sha,
        )


@dataclass(kw_only=True)
class FixerDirective(PipelineDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
You are a fix agent. A previous implementation attempt received feedback from testing or code review.
Your job is to address the feedback, fix the issues, and commit the changes.

Do NOT start from scratch. Read the existing code, understand the feedback, and make targeted fixes."""
    role: Role = Role.FIXER
    feedback: str
    failure_kind: Literal["test", "review"]
    attempt: int

    def render(self, ctx: PromptContext) -> str:
        kind_label = "Test failure" if self.failure_kind == "test" else "Review failure"
        feedback_section = f"[{kind_label}, attempt {self.attempt}]\n{self.feedback}"
        return _render_pipeline(
            self,
            ctx,
            include_diff=True,
            diff_label="Current changes",
            action="Fix the issues described above and commit your changes.",
            feedback_section=feedback_section,
        )


@dataclass(kw_only=True)
class TddTesterDirective(PipelineDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
You are a test-driven development agent. Your job is to write comprehensive
tests for the task described below BEFORE any implementation exists.

Write tests that:
1. Cover the expected behavior described in the task
2. Cover edge cases and error conditions
3. Follow the project's existing test patterns and conventions
4. Will FAIL because the implementation does not exist yet

Do NOT implement the feature. Only write tests.
Do NOT create stub implementations to make tests pass.
Commit your test files when done with a clear commit message."""
    role: Role = Role.TESTER

    def render(self, ctx: PromptContext) -> str:
        return _render_pipeline(
            self,
            ctx,
            include_diff=False,
            action="Run tests and verify the implementation.",
        )


@dataclass(kw_only=True)
class TddImplementorDirective(PipelineDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
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
If FAIL, explain what is missing or broken before the verdict line."""
    role: Role = Role.IMPLEMENTOR

    def render(self, ctx: PromptContext) -> str:
        return _render_pipeline(
            self,
            ctx,
            include_diff=False,
            action="Implement this task and commit your changes.",
        )


# ---------------------------------------------------------------------------
# Concrete standalone directives
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class MergerDirective(StandaloneDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
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

If FAIL, explain which files could not be resolved and why."""
    role: Role = Role.MERGER
    task_branch: str
    session_branch: str
    conflicts: list[str]

    def render(self) -> str:
        parts = [
            self.resolved_text(),
            f"Merging branch '{self.task_branch}' into '{self.session_branch}'",
            "Conflicted files:\n" + "\n".join(f"  - {c}" for c in self.conflicts),
            "Read each file, resolve the conflict markers, and stage with git add.",
        ]
        return "\n\n".join(parts)


@dataclass(kw_only=True)
class PlannerDirective(StandaloneDirective):
    DEFAULT_TEXT: ClassVar[
        str
    ] = """\
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
    role: Role = Role.IMPLEMENTOR
    output_path: Path
    user_prompt: str = ""
    source_content: str = ""
    plan_guide: str = ""

    def render(self) -> str:
        parts = [
            self.resolved_text(),
            f"## Plan Writing Guide\n\n{self.plan_guide}",
        ]

        if self.source_content:
            parts.append(
                "## Source Document\n\n"
                "The following document describes work to be done. Transform it into "
                "a workbench plan that follows the plan writing guide above. Preserve "
                "the intent and scope of the original document, but restructure it "
                "into proper `## Task:` sections with Files, Depends, detailed "
                "descriptions, and test plans. Survey the codebase to fill in "
                "specifics (file paths, function signatures, conventions) that the "
                "source document may have left vague.\n\n"
                f"{self.source_content}"
            )

        if self.user_prompt:
            label = "Additional Guidance" if self.source_content else "User Request"
            parts.append(f"## {label}\n\n{self.user_prompt}")

        parts.append(
            f"## Output\n\nWrite the plan to: {self.output_path}\n\n"
            "Explore the codebase thoroughly before writing. The plan must be "
            "detailed enough that each task can be implemented by an agent that "
            "has never seen the rest of the plan."
        )
        return "\n\n".join(parts)
