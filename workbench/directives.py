"""Directive classes — typed prompt builders for each agent role.

Each Directive subclass loads its DEFAULT_TEXT from a markdown file in
``workbench/directive_texts/``. To edit the built-in instructions for any
role, edit the corresponding ``.md`` file there — no Python changes needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from .agents import Role
from .worktree import get_diff, get_diff_since

if TYPE_CHECKING:
    from .plan_parser import Task
    from .worktree import Worktree


def _load_text(name: str) -> str:
    """Load a built-in directive text from ``workbench/directive_texts/<name>``.

    The trailing newline produced by typical text editors is stripped so
    DEFAULT_TEXT values match the inline triple-quoted strings they replaced.
    """
    return resources.files("workbench.directive_texts").joinpath(name).read_text().rstrip("\n")


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
    DEFAULT_TEXT: ClassVar[str] = _load_text("implementor.md")
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
    DEFAULT_TEXT: ClassVar[str] = _load_text("tester.md")
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
    DEFAULT_TEXT: ClassVar[str] = _load_text("reviewer.md")
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
    DEFAULT_TEXT: ClassVar[str] = _load_text("reviewer_followup.md")
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
    DEFAULT_TEXT: ClassVar[str] = _load_text("fixer.md")
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
    DEFAULT_TEXT: ClassVar[str] = _load_text("tdd_tester.md")
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
    DEFAULT_TEXT: ClassVar[str] = _load_text("tdd_implementor.md")
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
    DEFAULT_TEXT: ClassVar[str] = _load_text("merger.md")
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
    DEFAULT_TEXT: ClassVar[str] = _load_text("planner.md")
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
