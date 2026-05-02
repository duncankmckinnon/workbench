"""Tests for standalone runner migration — run_merge_resolver and run_planner.

Verifies that both functions now use MergerDirective / PlannerDirective
while producing prompts that are backward-compatible with the pre-refactor
inline assembly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workbench.agents import (
    AgentResult,
    Role,
    TaskStatus,
    _load_plan_guide,
    run_merge_resolver,
    run_planner,
)
from workbench.directives import MergerDirective, PlannerDirective
from workbench.profile import Profile, RoleConfig

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a minimal repo-like directory with .workbench/plans."""
    wb_dir = tmp_path / ".workbench" / "plans"
    wb_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def mock_adapter():
    """An adapter mock that returns a dummy command and parses output."""
    adapter = MagicMock()
    adapter.build_command.return_value = ["echo", "ok"]
    adapter.parse_output.return_value = ("done", {"input": 0.01})
    return adapter


# ── Helpers ──────────────────────────────────────────────────────────


def _run_merge(
    *,
    adapter,
    tmp_repo,
    task_branch="feature/auth",
    session_branch="wb/session-1",
    conflicts=None,
    profile=None,
    directive_override=None,
):
    """Helper to call run_merge_resolver with standard defaults."""
    if conflicts is None:
        conflicts = ["src/auth.py", "src/config.py"]
    merge_dir = tmp_repo / "merge-work"
    merge_dir.mkdir(exist_ok=True)

    with patch(
        "workbench.agents.run_in_tmux",
        new_callable=AsyncMock,
        return_value=(0, "resolved"),
    ):
        return asyncio.run(
            run_merge_resolver(
                task_branch=task_branch,
                session_branch=session_branch,
                merge_dir=merge_dir,
                conflicts=conflicts,
                repo=tmp_repo,
                adapter=adapter,
                profile=profile,
                directive_override=directive_override,
            )
        )


def _capture_merge_prompt(
    *,
    adapter,
    tmp_repo,
    task_branch="feature/auth",
    session_branch="wb/session-1",
    conflicts=None,
    profile=None,
    directive_override=None,
) -> str:
    """Run run_merge_resolver and return the prompt passed to adapter.build_command."""
    _run_merge(
        adapter=adapter,
        tmp_repo=tmp_repo,
        task_branch=task_branch,
        session_branch=session_branch,
        conflicts=conflicts,
        profile=profile,
        directive_override=directive_override,
    )
    prompt = adapter.build_command.call_args[0][0]
    return prompt


def _run_planner(
    *,
    adapter,
    tmp_repo,
    user_prompt="",
    source_content="",
    plan_name="plan",
    profile=None,
):
    """Helper to call run_planner with standard defaults."""
    with patch(
        "workbench.agents.run_in_tmux",
        new_callable=AsyncMock,
        return_value=(0, "plan output"),
    ):
        return asyncio.run(
            run_planner(
                repo=tmp_repo,
                user_prompt=user_prompt,
                source_content=source_content,
                plan_name=plan_name,
                adapter=adapter,
                profile=profile,
            )
        )


def _capture_planner_prompt(
    *,
    adapter,
    tmp_repo,
    user_prompt="",
    source_content="",
    plan_name="plan",
    profile=None,
) -> str:
    """Run run_planner and return the prompt passed to adapter.build_command."""
    _run_planner(
        adapter=adapter,
        tmp_repo=tmp_repo,
        user_prompt=user_prompt,
        source_content=source_content,
        plan_name=plan_name,
        profile=profile,
    )
    prompt = adapter.build_command.call_args[0][0]
    return prompt


# ── run_merge_resolver backward compatibility ────────────────────────


class TestMergeResolverBackwardCompat:
    """With no profile or directive_override, the generated prompt must match
    the old inline assembly byte-for-byte."""

    def test_default_prompt_matches_old_assembly(self, mock_adapter, tmp_repo):
        """Prompt from MergerDirective.render() with no override matches the
        original prompt_parts assembly."""
        conflicts = ["src/auth.py", "src/config.py"]
        task_branch = "feature/auth"
        session_branch = "wb/session-1"

        # Old assembly
        old_parts = [
            MergerDirective.DEFAULT_TEXT,
            f"Merging branch '{task_branch}' into '{session_branch}'",
            "Conflicted files:\n" + "\n".join(f"  - {c}" for c in conflicts),
            "Read each file, resolve the conflict markers, and stage with git add.",
        ]
        old_prompt = "\n\n".join(old_parts)

        # New assembly via the function
        new_prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            task_branch=task_branch,
            session_branch=session_branch,
            conflicts=conflicts,
        )

        assert new_prompt == old_prompt

    def test_single_conflict_file(self, mock_adapter, tmp_repo):
        conflicts = ["README.md"]
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            conflicts=conflicts,
        )
        assert "  - README.md" in prompt
        assert MergerDirective.DEFAULT_TEXT in prompt

    def test_many_conflict_files(self, mock_adapter, tmp_repo):
        conflicts = [f"src/mod{i}.py" for i in range(10)]
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            conflicts=conflicts,
        )
        for c in conflicts:
            assert f"  - {c}" in prompt


# ── run_merge_resolver with profile ──────────────────────────────────


class TestMergeResolverWithProfile:
    def test_profile_directive_overrides_default(self, mock_adapter, tmp_repo):
        profile = Profile.default()
        profile.merger = RoleConfig(directive="Custom merger role.")
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            profile=profile,
        )
        assert "Custom merger role." in prompt
        assert MergerDirective.DEFAULT_TEXT not in prompt

    def test_profile_empty_directive_uses_default(self, mock_adapter, tmp_repo):
        profile = Profile.default()
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            profile=profile,
        )
        assert MergerDirective.DEFAULT_TEXT in prompt


# ── run_merge_resolver with directive_override ───────────────────────


class TestMergeResolverWithOverride:
    def test_directive_override_used(self, mock_adapter, tmp_repo):
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            directive_override="Resolve conflicts carefully.",
        )
        assert "Resolve conflicts carefully." in prompt
        assert MergerDirective.DEFAULT_TEXT not in prompt

    def test_directive_override_beats_profile(self, mock_adapter, tmp_repo):
        """directive_override takes precedence over profile.merger.directive."""
        profile = Profile.default()
        profile.merger = RoleConfig(directive="Profile directive.")
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            profile=profile,
            directive_override="Override directive.",
        )
        assert "Override directive." in prompt
        assert "Profile directive." not in prompt


# ── run_merge_resolver result ────────────────────────────────────────


class TestMergeResolverResult:
    def test_success_returns_done(self, mock_adapter, tmp_repo):
        result = _run_merge(adapter=mock_adapter, tmp_repo=tmp_repo)
        assert result.status == TaskStatus.DONE
        assert result.role == Role.MERGER

    def test_failure_returns_failed(self, mock_adapter, tmp_repo):
        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(1, "error"),
        ):
            merge_dir = tmp_repo / "merge-work"
            merge_dir.mkdir(exist_ok=True)
            result = asyncio.run(
                run_merge_resolver(
                    task_branch="feature/x",
                    session_branch="wb/s",
                    merge_dir=merge_dir,
                    conflicts=["a.py"],
                    repo=tmp_repo,
                    adapter=mock_adapter,
                )
            )
        assert result.status == TaskStatus.FAILED

    def test_exception_returns_failed(self, mock_adapter, tmp_repo):
        mock_adapter.build_command.side_effect = RuntimeError("boom")
        merge_dir = tmp_repo / "merge-work"
        merge_dir.mkdir(exist_ok=True)
        result = asyncio.run(
            run_merge_resolver(
                task_branch="feature/x",
                session_branch="wb/s",
                merge_dir=merge_dir,
                conflicts=["a.py"],
                repo=tmp_repo,
                adapter=mock_adapter,
            )
        )
        assert result.status == TaskStatus.FAILED
        assert "Merge resolver error" in result.output


# ── run_merge_resolver signature ─────────────────────────────────────


class TestMergeResolverSignature:
    def test_profile_defaults_to_none(self, mock_adapter, tmp_repo):
        """Calling without profile should work (backward compat)."""
        result = _run_merge(adapter=mock_adapter, tmp_repo=tmp_repo)
        assert result.status == TaskStatus.DONE

    def test_directive_override_defaults_to_none(self, mock_adapter, tmp_repo):
        """Calling without directive_override should work."""
        result = _run_merge(adapter=mock_adapter, tmp_repo=tmp_repo)
        assert result.status == TaskStatus.DONE


# ── run_planner backward compatibility ───────────────────────────────


class TestPlannerBackwardCompat:
    """With no profile, the generated prompt must match PlannerDirective.render()."""

    def test_default_prompt_matches_planner_directive(self, mock_adapter, tmp_repo):
        """Prompt from run_planner with no override matches
        PlannerDirective(...).render() directly."""
        plan_name = "my-plan"
        output_path = tmp_repo / ".workbench" / "plans" / f"{plan_name}.md"

        expected = PlannerDirective(
            output_path=output_path,
            plan_guide=_load_plan_guide(),
        ).render()

        new_prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            plan_name=plan_name,
        )

        assert new_prompt == expected

    def test_with_user_prompt_matches(self, mock_adapter, tmp_repo):
        plan_name = "auth-plan"
        output_path = tmp_repo / ".workbench" / "plans" / f"{plan_name}.md"
        user_prompt = "Add JWT authentication"

        expected = PlannerDirective(
            output_path=output_path,
            user_prompt=user_prompt,
            plan_guide=_load_plan_guide(),
        ).render()

        new_prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            user_prompt=user_prompt,
            plan_name=plan_name,
        )

        assert new_prompt == expected

    def test_with_source_content_matches(self, mock_adapter, tmp_repo):
        plan_name = "refactor"
        output_path = tmp_repo / ".workbench" / "plans" / f"{plan_name}.md"
        source_content = "# Spec\nBuild the widget system.\n"

        expected = PlannerDirective(
            output_path=output_path,
            source_content=source_content,
            plan_guide=_load_plan_guide(),
        ).render()

        new_prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            source_content=source_content,
            plan_name=plan_name,
        )

        assert new_prompt == expected

    def test_with_both_user_and_source_matches(self, mock_adapter, tmp_repo):
        plan_name = "full"
        output_path = tmp_repo / ".workbench" / "plans" / f"{plan_name}.md"
        user_prompt = "Focus on security"
        source_content = "# Design\nAdd auth module.\n"

        expected = PlannerDirective(
            output_path=output_path,
            user_prompt=user_prompt,
            source_content=source_content,
            plan_guide=_load_plan_guide(),
        ).render()

        new_prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            user_prompt=user_prompt,
            source_content=source_content,
            plan_name=plan_name,
        )

        assert new_prompt == expected


# ── run_planner with profile ─────────────────────────────────────────


class TestPlannerWithProfile:
    def test_profile_directive_overrides_default(self, mock_adapter, tmp_repo):
        profile = Profile.default()
        profile.planner = RoleConfig(directive="Custom planner instructions.")
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            profile=profile,
        )
        assert "Custom planner instructions." in prompt
        assert PlannerDirective.DEFAULT_TEXT not in prompt

    def test_profile_empty_directive_uses_default(self, mock_adapter, tmp_repo):
        profile = Profile.default()
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            profile=profile,
        )
        assert PlannerDirective.DEFAULT_TEXT in prompt


# ── run_planner result ───────────────────────────────────────────────


class TestPlannerResult:
    def test_success_returns_done(self, mock_adapter, tmp_repo):
        result = _run_planner(adapter=mock_adapter, tmp_repo=tmp_repo)
        assert result.status == TaskStatus.DONE
        assert result.task_id == "planner-plan"

    def test_custom_plan_name_in_task_id(self, mock_adapter, tmp_repo):
        result = _run_planner(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            plan_name="auth",
        )
        assert result.task_id == "planner-auth"

    def test_failure_returns_failed(self, mock_adapter, tmp_repo):
        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(1, "error"),
        ):
            result = asyncio.run(
                run_planner(
                    repo=tmp_repo,
                    adapter=mock_adapter,
                )
            )
        assert result.status == TaskStatus.FAILED

    def test_exception_returns_failed(self, mock_adapter, tmp_repo):
        mock_adapter.build_command.side_effect = RuntimeError("boom")
        result = asyncio.run(
            run_planner(
                repo=tmp_repo,
                adapter=mock_adapter,
            )
        )
        assert result.status == TaskStatus.FAILED
        assert "Planner error" in result.output

    def test_creates_plans_directory(self, mock_adapter, tmp_path):
        """Plans directory is created if it doesn't exist."""
        repo = tmp_path / "newrepo"
        repo.mkdir()
        with patch(
            "workbench.agents.run_in_tmux",
            new_callable=AsyncMock,
            return_value=(0, "output"),
        ):
            asyncio.run(
                run_planner(
                    repo=repo,
                    adapter=mock_adapter,
                )
            )
        assert (repo / ".workbench" / "plans").is_dir()


# ── run_planner signature ────────────────────────────────────────────


class TestPlannerSignature:
    def test_profile_defaults_to_none(self, mock_adapter, tmp_repo):
        """Calling without profile should work (backward compat)."""
        result = _run_planner(adapter=mock_adapter, tmp_repo=tmp_repo)
        assert result.status == TaskStatus.DONE


# ── Prompt content verification ──────────────────────────────────────


class TestMergePromptContent:
    """Verify specific content sections in the merge prompt."""

    def test_contains_branch_merging_info(self, mock_adapter, tmp_repo):
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            task_branch="feat/login",
            session_branch="wb/sess-2",
        )
        assert "Merging branch 'feat/login' into 'wb/sess-2'" in prompt

    def test_contains_conflict_file_list(self, mock_adapter, tmp_repo):
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            conflicts=["a.py", "b.py", "c.py"],
        )
        assert "Conflicted files:" in prompt
        assert "  - a.py" in prompt
        assert "  - b.py" in prompt
        assert "  - c.py" in prompt

    def test_contains_action_instruction(self, mock_adapter, tmp_repo):
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
        )
        assert "Read each file, resolve the conflict markers, and stage with git add." in prompt

    def test_four_sections_joined_by_double_newline(self, mock_adapter, tmp_repo):
        """The prompt has exactly 4 top-level sections separated by \\n\\n:
        directive text, merge info, conflict list, and action instruction.
        We verify the join markers exist between sections."""
        prompt = _capture_merge_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
        )
        assert "Merging branch" in prompt
        assert "Conflicted files:" in prompt
        assert "Read each file" in prompt
        # Verify join boundaries: each section is separated by \n\n
        assert "\n\nMerging branch" in prompt
        assert "\n\nConflicted files:" in prompt
        assert "\n\nRead each file" in prompt


class TestPlannerPromptContent:
    """Verify specific content sections in the planner prompt."""

    def test_contains_plan_guide(self, mock_adapter, tmp_repo):
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
        )
        assert "## Plan Writing Guide" in prompt

    def test_contains_output_path(self, mock_adapter, tmp_repo):
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            plan_name="my-plan",
        )
        expected_path = str(tmp_repo / ".workbench" / "plans" / "my-plan.md")
        assert expected_path in prompt

    def test_contains_explore_instruction(self, mock_adapter, tmp_repo):
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
        )
        assert "Explore the codebase thoroughly before writing." in prompt

    def test_user_request_label(self, mock_adapter, tmp_repo):
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            user_prompt="Build a widget",
        )
        assert "## User Request" in prompt
        assert "Build a widget" in prompt

    def test_additional_guidance_label_with_source(self, mock_adapter, tmp_repo):
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            user_prompt="Focus on perf",
            source_content="# Spec\nSome spec.",
        )
        assert "## Additional Guidance" in prompt
        assert "## User Request" not in prompt

    def test_source_document_section(self, mock_adapter, tmp_repo):
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
            source_content="# Design Doc\nBuild auth.",
        )
        assert "## Source Document" in prompt
        assert "# Design Doc" in prompt

    def test_no_user_section_when_empty(self, mock_adapter, tmp_repo):
        prompt = _capture_planner_prompt(
            adapter=mock_adapter,
            tmp_repo=tmp_repo,
        )
        assert "## User Request" not in prompt
        assert "## Additional Guidance" not in prompt


# ── Directive class usage verification ───────────────────────────────


class TestDirectiveClassUsage:
    """Verify that the functions actually use the Directive classes."""

    def test_merge_resolver_uses_merger_directive(self, mock_adapter, tmp_repo):
        """Verify MergerDirective is instantiated and render() is called."""
        with (
            patch("workbench.directives.MergerDirective", wraps=MergerDirective) as MockDirective,
            patch(
                "workbench.agents.run_in_tmux",
                new_callable=AsyncMock,
                return_value=(0, "ok"),
            ),
        ):
            merge_dir = tmp_repo / "merge-work"
            merge_dir.mkdir(exist_ok=True)
            asyncio.run(
                run_merge_resolver(
                    task_branch="feat/x",
                    session_branch="wb/s",
                    merge_dir=merge_dir,
                    conflicts=["a.py"],
                    repo=tmp_repo,
                    adapter=mock_adapter,
                )
            )
            MockDirective.assert_called_once_with(
                directive_text="",
                task_branch="feat/x",
                session_branch="wb/s",
                conflicts=["a.py"],
            )
            # The prompt passed to build_command should be a MergerDirective render
            prompt = mock_adapter.build_command.call_args[0][0]
            assert "merge conflict" in prompt.lower()
            assert "feat/x" in prompt

    def test_planner_uses_planner_directive(self, mock_adapter, tmp_repo):
        """Verify PlannerDirective is instantiated and render() is called."""
        with (
            patch(
                "workbench.directives.PlannerDirective", wraps=PlannerDirective
            ) as MockDirective,
            patch(
                "workbench.agents.run_in_tmux",
                new_callable=AsyncMock,
                return_value=(0, "ok"),
            ),
        ):
            asyncio.run(
                run_planner(
                    repo=tmp_repo,
                    user_prompt="build auth",
                    adapter=mock_adapter,
                )
            )
            MockDirective.assert_called_once()
            call_kwargs = MockDirective.call_args[1]
            assert call_kwargs["directive_text"] == ""
            assert call_kwargs["user_prompt"] == "build auth"
            assert "output_path" in call_kwargs
            assert "plan_guide" in call_kwargs
            prompt = mock_adapter.build_command.call_args[0][0]
            assert "planning agent" in prompt.lower()
            assert "build auth" in prompt


# ── Subprocess mode (use_tmux=False) ─────────────────────────────────


class TestSubprocessMode:
    def test_merge_resolver_subprocess_mode(self, mock_adapter, tmp_repo):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"resolved", b"")
        mock_proc.returncode = 0

        merge_dir = tmp_repo / "merge-work"
        merge_dir.mkdir(exist_ok=True)

        with patch(
            "workbench.agents.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            result = asyncio.run(
                run_merge_resolver(
                    task_branch="feat/x",
                    session_branch="wb/s",
                    merge_dir=merge_dir,
                    conflicts=["a.py"],
                    repo=tmp_repo,
                    adapter=mock_adapter,
                    use_tmux=False,
                )
            )
        assert result.status == TaskStatus.DONE

    def test_planner_subprocess_mode(self, mock_adapter, tmp_repo):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"plan output", b"")
        mock_proc.returncode = 0

        with patch(
            "workbench.agents.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            result = asyncio.run(
                run_planner(
                    repo=tmp_repo,
                    adapter=mock_adapter,
                    use_tmux=False,
                )
            )
        assert result.status == TaskStatus.DONE
