"""Tests for documentation content — README.md and SKILL.md.

These tests verify that required sections, commands, and examples
are present and accurate in the project documentation after the
profile system and Gemini support were added.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
README = PROJECT_ROOT / "README.md"
SKILL_MD = PROJECT_ROOT / "workbench" / "skills" / "use-workbench" / "SKILL.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# README.md — Profiles section
# ---------------------------------------------------------------------------


class TestReadmeProfilesSection:
    """README.md must contain a Profiles section documenting the profile system."""

    def test_profiles_heading_exists(self):
        content = _read(README)
        assert "## Profiles" in content, "README.md missing '## Profiles' heading"

    def test_profiles_after_branching_strategy(self):
        content = _read(README)
        branching_pos = content.index("## Branching strategy")
        profiles_pos = content.index("## Profiles")
        assert (
            profiles_pos > branching_pos
        ), "Profiles section should appear after Branching strategy"

    def test_profile_init_command_documented(self):
        content = _read(README)
        assert "wb profile init" in content

    def test_profile_init_global_flag_documented(self):
        content = _read(README)
        assert "--global" in content
        # Should show the global init usage
        assert "wb profile init" in content

    def test_profile_set_command_documented(self):
        content = _read(README)
        assert "wb profile set" in content

    def test_profile_show_command_documented(self):
        content = _read(README)
        assert "wb profile show" in content

    def test_profile_diff_command_documented(self):
        content = _read(README)
        assert "wb profile diff" in content

    def test_profile_yaml_example(self):
        """README should contain an example profile.yaml snippet."""
        content = _read(README)
        assert "profile.yaml" in content
        assert "roles:" in content

    def test_profile_set_agent_example(self):
        content = _read(README)
        assert "wb profile set reviewer.agent gemini" in content

    def test_profile_set_directive_extend_example(self):
        content = _read(README)
        assert "directive_extend" in content

    def test_merge_order_documented(self):
        """README should document the profile merge precedence."""
        content = _read(README)
        # Check for merge order description
        assert "built-in defaults" in content.lower() or "defaults" in content.lower()
        assert "profile.yaml" in content
        assert "--profile" in content


# ---------------------------------------------------------------------------
# README.md — CLI reference tables updated
# ---------------------------------------------------------------------------


class TestReadmeCliReference:
    """CLI reference tables must include profile commands and flags."""

    def test_profile_init_in_commands_table(self):
        content = _read(README)
        # Should appear in the commands table
        assert "wb profile init" in content
        assert "Create profile.yaml from defaults" in content or "profile.yaml" in content

    def test_profile_show_in_commands_table(self):
        content = _read(README)
        assert "wb profile show" in content

    def test_profile_set_in_commands_table(self):
        content = _read(README)
        assert "wb profile set" in content

    def test_profile_diff_in_commands_table(self):
        content = _read(README)
        assert "wb profile diff" in content

    def test_run_profile_flag_in_flags_table(self):
        content = _read(README)
        assert "--profile" in content
        # Should be in the wb run flags section
        assert "profile" in content.lower()


# ---------------------------------------------------------------------------
# README.md — Install section updated
# ---------------------------------------------------------------------------


class TestReadmeInstallSection:
    """Install section must show setup command with agent and global options."""

    def test_setup_global_flag_documented(self):
        content = _read(README)
        assert "--global" in content

    def test_setup_gemini_documented(self):
        content = _read(README)
        assert "wb setup --agent gemini" in content or "setup --global --agent gemini" in content

    def test_setup_claude_documented(self):
        content = _read(README)
        assert "--agent claude" in content

    def test_setup_update_documented(self):
        content = _read(README)
        assert "--update" in content


# ---------------------------------------------------------------------------
# README.md — Requirements section updated
# ---------------------------------------------------------------------------


class TestReadmeRequirements:
    """Requirements section must list Gemini as a supported agent CLI."""

    def test_gemini_in_requirements(self):
        content = _read(README)
        # Find the requirements section and check gemini is mentioned
        assert (
            "gemini" in content.lower()
        ), "README.md requirements should mention Gemini as a supported agent"


# ---------------------------------------------------------------------------
# SKILL.md — Profiles section
# ---------------------------------------------------------------------------


class TestSkillProfilesSection:
    """SKILL.md must contain a Profiles section documenting the profile system."""

    def test_profiles_heading_exists(self):
        content = _read(SKILL_MD)
        assert (
            "## Profiles" in content or "## Profile" in content
        ), "SKILL.md missing Profiles heading"

    def test_profiles_after_branching_strategy(self):
        content = _read(SKILL_MD)
        branching_pos = content.index("## Branching Strategy")
        # Profiles section should appear after Branching Strategy
        profiles_section = "## Profiles" if "## Profiles" in content else "## Profile"
        profiles_pos = content.index(profiles_section)
        assert (
            profiles_pos > branching_pos
        ), "Profiles section should appear after Branching Strategy in SKILL.md"

    def test_profile_yaml_format_documented(self):
        """SKILL.md should document the YAML format for profiles."""
        content = _read(SKILL_MD)
        assert "profile.yaml" in content
        assert "roles:" in content

    def test_profile_cli_commands_documented(self):
        """SKILL.md should document profile CLI commands."""
        content = _read(SKILL_MD)
        assert "wb profile init" in content
        assert "wb profile show" in content
        assert "wb profile set" in content
        assert "wb profile diff" in content

    def test_directive_extend_documented(self):
        content = _read(SKILL_MD)
        assert "directive_extend" in content

    def test_merge_order_documented(self):
        """SKILL.md should explain profile merge precedence."""
        content = _read(SKILL_MD)
        lower = content.lower()
        assert "defaults" in lower
        assert "--profile" in content


# ---------------------------------------------------------------------------
# SKILL.md — Gemini agent support
# ---------------------------------------------------------------------------


class TestSkillGeminiSupport:
    """SKILL.md must mention Gemini as a supported agent."""

    def test_gemini_mentioned(self):
        content = _read(SKILL_MD)
        assert "gemini" in content.lower(), "SKILL.md should mention Gemini as a supported agent"


# ---------------------------------------------------------------------------
# SKILL.md — Updated skill install section
# ---------------------------------------------------------------------------


class TestSkillInstallSection:
    """SKILL.md must document --local flag and .agents/skills/ convention."""

    def test_local_flag_documented(self):
        content = _read(SKILL_MD)
        assert "--local" in content

    def test_agents_skills_path_documented(self):
        content = _read(SKILL_MD)
        assert ".agents/skills/" in content or ".agents/skills" in content

    def test_setup_gemini_documented(self):
        content = _read(SKILL_MD)
        assert "wb setup --agent gemini" in content or "setup --global --agent gemini" in content


# ---------------------------------------------------------------------------
# SKILL.md — Frontmatter intact
# ---------------------------------------------------------------------------


class TestSkillFrontmatter:
    """SKILL.md frontmatter should remain valid after edits."""

    def test_frontmatter_present(self):
        content = _read(SKILL_MD)
        assert content.startswith("---"), "SKILL.md should start with YAML frontmatter"
        # Find closing ---
        second_dash = content.index("---", 3)
        assert second_dash > 3, "SKILL.md should have closing frontmatter delimiter"

    def test_frontmatter_has_name(self):
        content = _read(SKILL_MD)
        second_dash = content.index("---", 3)
        frontmatter = content[3:second_dash]
        assert "name:" in frontmatter

    def test_frontmatter_has_description(self):
        content = _read(SKILL_MD)
        second_dash = content.index("---", 3)
        frontmatter = content[3:second_dash]
        assert "description:" in frontmatter


# ---------------------------------------------------------------------------
# Cross-document consistency
# ---------------------------------------------------------------------------


class TestDocConsistency:
    """README.md and SKILL.md should be consistent on key facts."""

    def test_both_mention_profile_yaml(self):
        readme = _read(README)
        skill = _read(SKILL_MD)
        assert "profile.yaml" in readme
        assert "profile.yaml" in skill

    def test_both_mention_gemini(self):
        readme = _read(README)
        skill = _read(SKILL_MD)
        assert "gemini" in readme.lower()
        assert "gemini" in skill.lower()

    def test_both_document_profile_commands(self):
        readme = _read(README)
        skill = _read(SKILL_MD)
        for cmd in ["wb profile init", "wb profile show", "wb profile set", "wb profile diff"]:
            assert cmd in readme, f"README.md missing {cmd}"
            assert cmd in skill, f"SKILL.md missing {cmd}"

    def test_both_document_local_flag(self):
        readme = _read(README)
        skill = _read(SKILL_MD)
        assert "--local" in readme
        assert "--local" in skill

    def test_both_document_directive_extend(self):
        readme = _read(README)
        skill = _read(SKILL_MD)
        assert "directive_extend" in readme
        assert "directive_extend" in skill
