"""Tests for the profile data model and YAML load/save/merge logic."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from workbench.profile import ModeConfig, Profile, RoleConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_ROLE_NAMES = [
    "implementor",
    "tester",
    "reviewer",
    "fixer",
    "merger",
    "planner",
    "summarizer",
    "branch_reviewer",
    "pr_writer",
]


def _write_yaml(path: Path, data: dict) -> Path:
    """Write a dict as YAML to the given path, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False))
    return path


# ---------------------------------------------------------------------------
# Profile.default()
# ---------------------------------------------------------------------------


class TestDefaultProfile:
    def test_default_profile_has_all_roles(self):
        """Profile.default() returns a profile with all 6 roles populated."""
        profile = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            cfg = getattr(profile, role_name)
            assert isinstance(cfg, RoleConfig)

    def test_default_profile_agent_is_claude(self):
        """Every role defaults to agent='claude'."""
        profile = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            cfg = getattr(profile, role_name)
            assert cfg.agent == "claude", f"{role_name} agent should be 'claude'"

    def test_default_profile_directives_are_empty(self):
        """Every role directive defaults to empty string."""
        profile = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            cfg = getattr(profile, role_name)
            assert cfg.directive == "", f"{role_name} directive should be empty"

    def test_default_planner(self):
        """Profile.default().planner is RoleConfig(agent='claude', directive='')."""
        profile = Profile.default()
        assert profile.planner.agent == "claude"
        assert profile.planner.directive == ""

    def test_default_summarizer(self):
        """Profile.default().summarizer is RoleConfig(agent='claude', directive='')."""
        profile = Profile.default()
        assert profile.summarizer.agent == "claude"
        assert profile.summarizer.directive == ""

    def test_default_branch_reviewer(self):
        """Profile.default().branch_reviewer is RoleConfig(agent='claude', directive='')."""
        profile = Profile.default()
        assert profile.branch_reviewer.agent == "claude"
        assert profile.branch_reviewer.directive == ""

    def test_default_pr_writer(self):
        """Profile.default().pr_writer is RoleConfig(agent='claude', directive='')."""
        profile = Profile.default()
        assert profile.pr_writer.agent == "claude"
        assert profile.pr_writer.directive == ""

    def test_default_sub_modes_are_none(self):
        """Default profile has no sub-modes configured."""
        profile = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            cfg = getattr(profile, role_name)
            assert cfg.tdd is None
            assert cfg.followup is None


# ---------------------------------------------------------------------------
# Profile.from_yaml() — agent overrides
# ---------------------------------------------------------------------------


class TestFromYamlAgent:
    def test_override_single_role_agent(self, tmp_path):
        """YAML with reviewer.agent: antigravity overrides only that role."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"reviewer": {"agent": "antigravity"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.reviewer.agent == "antigravity"
        # All others unchanged
        for role_name in [
            "implementor",
            "tester",
            "fixer",
            "merger",
            "planner",
            "summarizer",
            "branch_reviewer",
            "pr_writer",
        ]:
            assert getattr(profile, role_name).agent == "claude"

    def test_override_multiple_role_agents(self, tmp_path):
        """YAML can override agents on multiple roles at once."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {
                "roles": {
                    "implementor": {"agent": "codex"},
                    "tester": {"agent": "antigravity"},
                }
            },
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.implementor.agent == "codex"
        assert profile.tester.agent == "antigravity"
        assert profile.reviewer.agent == "claude"


# ---------------------------------------------------------------------------
# Profile.from_yaml() — directive overrides
# ---------------------------------------------------------------------------


class TestFromYamlDirective:
    def test_override_directive_replaces_default(self, tmp_path):
        """YAML with tester.directive: 'Custom' replaces the default."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"directive": "Custom tester directive."}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.tester.directive == "Custom tester directive."

    def test_directive_extend_appends_to_default(self, tmp_path):
        """directive_extend appends to default with '\\n\\n' separator."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"directive_extend": "Extra instructions."}}},
        )
        profile = Profile.from_yaml(yaml_path)
        # Default is "", so extend produces "\n\n" + "Extra instructions."
        expected = "\n\nExtra instructions."
        assert profile.tester.directive == expected

    def test_directive_wins_over_directive_extend(self, tmp_path):
        """When both directive and directive_extend are set, directive wins."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {
                "roles": {
                    "reviewer": {
                        "directive": "Full replacement.",
                        "directive_extend": "This should be ignored.",
                    }
                }
            },
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.reviewer.directive == "Full replacement."
        assert "This should be ignored" not in profile.reviewer.directive


# ---------------------------------------------------------------------------
# Profile.from_yaml() — planner role
# ---------------------------------------------------------------------------


class TestFromYamlPlanner:
    def test_planner_parses(self, tmp_path):
        """Planner role parses from YAML."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"planner": {"agent": "antigravity", "directive": "Custom"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.planner.agent == "antigravity"
        assert profile.planner.directive == "Custom"

    def test_planner_absent_uses_default(self, tmp_path):
        """When planner is absent from YAML, default is used."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"agent": "antigravity"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.planner.agent == "claude"
        assert profile.planner.directive == ""


# ---------------------------------------------------------------------------
# Profile.from_yaml() — summarizer and branch_reviewer roles
# ---------------------------------------------------------------------------


class TestFromYamlNewRoles:
    def test_summarizer_parses(self, tmp_path):
        """Summarizer role parses from YAML."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"summarizer": {"agent": "antigravity", "directive": "Summarize reqs"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.summarizer.agent == "antigravity"
        assert profile.summarizer.directive == "Summarize reqs"

    def test_branch_reviewer_parses(self, tmp_path):
        """Branch reviewer role parses from YAML."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"branch_reviewer": {"agent": "codex", "directive": "Review branch"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.branch_reviewer.agent == "codex"
        assert profile.branch_reviewer.directive == "Review branch"

    def test_both_new_roles_roundtrip(self, tmp_path):
        """YAML with both new roles configured round-trips identically."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {
                "roles": {
                    "summarizer": {"agent": "claude", "directive": "Sum directive"},
                    "branch_reviewer": {"agent": "codex", "directive": "BR directive"},
                }
            },
        )
        profile = Profile.from_yaml(yaml_path)
        out = tmp_path / "roundtrip.yaml"
        profile.save(out)
        reloaded = Profile.from_yaml(out)
        assert reloaded.summarizer.agent == "claude"
        assert reloaded.summarizer.directive == "Sum directive"
        assert reloaded.branch_reviewer.agent == "codex"
        assert reloaded.branch_reviewer.directive == "BR directive"

    def test_neither_new_role_configured_uses_defaults(self, tmp_path):
        """YAML with neither new role still produces valid defaults."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"agent": "antigravity"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.summarizer.agent == "claude"
        assert profile.summarizer.directive == ""
        assert profile.branch_reviewer.agent == "claude"
        assert profile.branch_reviewer.directive == ""

    def test_pr_writer_parses(self, tmp_path):
        """pr_writer role parses from YAML."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"pr_writer": {"agent": "codex", "directive": "Write PR"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.pr_writer.agent == "codex"
        assert profile.pr_writer.directive == "Write PR"

    def test_pr_writer_roundtrip(self, tmp_path):
        """YAML with pr_writer configured round-trips identically."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"pr_writer": {"agent": "codex", "directive": "PR directive"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        out = tmp_path / "roundtrip.yaml"
        profile.save(out)
        reloaded = Profile.from_yaml(out)
        assert reloaded.pr_writer.agent == "codex"
        assert reloaded.pr_writer.directive == "PR directive"

    def test_pr_writer_absent_uses_default(self, tmp_path):
        """YAML without pr_writer still produces a valid profile (defaults applied)."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"agent": "antigravity"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.pr_writer.agent == "claude"
        assert profile.pr_writer.directive == ""


# ---------------------------------------------------------------------------
# Profile.from_yaml() — TDD sub-mode
# ---------------------------------------------------------------------------


class TestFromYamlTddSubMode:
    def test_tdd_sub_mode_parses_on_tester(self, tmp_path):
        """TDD sub-mode parses on tester role."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"tdd": {"directive": "Custom TDD"}}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.tester.tdd == ModeConfig(directive="Custom TDD")

    def test_tdd_sub_mode_parses_on_implementor(self, tmp_path):
        """TDD sub-mode parses on implementor role."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"implementor": {"tdd": {"directive": "TDD impl"}}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.implementor.tdd == ModeConfig(directive="TDD impl")

    def test_tdd_directive_extend_flattens(self, tmp_path):
        """directive_extend inside tdd sub-mode flattens into directive."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"tdd": {"directive_extend": "Extra"}}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.tester.tdd is not None
        assert profile.tester.tdd.directive == "Extra"

    def test_tdd_directive_wins_over_extend(self, tmp_path):
        """When both directive and directive_extend in tdd, directive wins."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"tdd": {"directive": "Full", "directive_extend": "Ignored"}}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.tester.tdd is not None
        assert profile.tester.tdd.directive == "Full"


# ---------------------------------------------------------------------------
# Profile.from_yaml() — followup sub-mode
# ---------------------------------------------------------------------------


class TestFromYamlFollowupSubMode:
    def test_followup_sub_mode_parses_on_reviewer(self, tmp_path):
        """Followup sub-mode parses on reviewer role."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"reviewer": {"followup": {"directive": "Followup review"}}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.reviewer.followup == ModeConfig(directive="Followup review")

    def test_followup_directive_extend_flattens(self, tmp_path):
        """directive_extend inside followup sub-mode flattens into directive."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"reviewer": {"followup": {"directive_extend": "Extra followup"}}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.reviewer.followup is not None
        assert profile.reviewer.followup.directive == "Extra followup"


# ---------------------------------------------------------------------------
# Profile.from_yaml() — sub-mode validation
# ---------------------------------------------------------------------------


class TestSubModeValidation:
    @pytest.mark.parametrize(
        "role_name, sub_mode, expected_msg",
        [
            ("merger", "tdd", "merger does not support a 'tdd' sub-mode"),
            ("merger", "followup", "merger does not support a 'followup' sub-mode"),
            ("fixer", "tdd", "fixer does not support a 'tdd' sub-mode"),
            ("fixer", "followup", "fixer does not support a 'followup' sub-mode"),
            ("planner", "tdd", "planner does not support a 'tdd' sub-mode"),
            ("planner", "followup", "planner does not support a 'followup' sub-mode"),
            ("reviewer", "tdd", "reviewer does not support a 'tdd' sub-mode"),
            ("implementor", "followup", "implementor does not support a 'followup' sub-mode"),
            ("tester", "followup", "tester does not support a 'followup' sub-mode"),
            ("summarizer", "tdd", "summarizer does not support a 'tdd' sub-mode"),
            ("summarizer", "followup", "summarizer does not support a 'followup' sub-mode"),
            ("branch_reviewer", "tdd", "branch_reviewer does not support a 'tdd' sub-mode"),
            (
                "branch_reviewer",
                "followup",
                "branch_reviewer does not support a 'followup' sub-mode",
            ),
            ("pr_writer", "tdd", "pr_writer does not support a 'tdd' sub-mode"),
            ("pr_writer", "followup", "pr_writer does not support a 'followup' sub-mode"),
        ],
    )
    def test_invalid_sub_mode_raises(self, tmp_path, role_name, sub_mode, expected_msg):
        """Invalid sub-mode placement raises ValueError with clear message."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {role_name: {sub_mode: {"directive": "bad"}}}},
        )
        with pytest.raises(ValueError, match=expected_msg):
            Profile.from_yaml(yaml_path)


# ---------------------------------------------------------------------------
# Profile.from_yaml() — unknown / edge cases
# ---------------------------------------------------------------------------


class TestFromYamlEdgeCases:
    def test_unknown_role_ignored(self, tmp_path):
        """YAML with an unknown role name does not raise an error."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"unknown_role": {"agent": "something"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        # Should still produce valid defaults
        for role_name in ALL_ROLE_NAMES:
            cfg = getattr(profile, role_name)
            assert cfg.agent == "claude"

    def test_unknown_field_ignored(self, tmp_path):
        """YAML with an unknown field on a known role does not raise."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"agent": "antigravity", "bogus_field": 42}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.tester.agent == "antigravity"

    def test_empty_roles_section(self, tmp_path):
        """YAML with empty roles: produces pure defaults."""
        yaml_path = _write_yaml(tmp_path / "profile.yaml", {"roles": {}})
        profile = Profile.from_yaml(yaml_path)
        default = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            assert getattr(profile, role_name).agent == getattr(default, role_name).agent
            assert getattr(profile, role_name).directive == getattr(default, role_name).directive

    def test_missing_roles_key(self, tmp_path):
        """YAML without a roles key at all produces pure defaults."""
        yaml_path = _write_yaml(tmp_path / "profile.yaml", {"something_else": True})
        profile = Profile.from_yaml(yaml_path)
        default = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            assert getattr(profile, role_name).agent == getattr(default, role_name).agent


# ---------------------------------------------------------------------------
# Profile.save() and roundtrip
# ---------------------------------------------------------------------------


class TestSave:
    def test_save_creates_yaml_file(self, tmp_path):
        """save() writes a YAML file to the given path."""
        out = tmp_path / "output.yaml"
        Profile.default().save(out)
        assert out.exists()
        data = yaml.safe_load(out.read_text())
        assert "roles" in data

    def test_save_includes_all_roles(self, tmp_path):
        """Saved YAML contains entries for all 6 roles."""
        out = tmp_path / "output.yaml"
        Profile.default().save(out)
        data = yaml.safe_load(out.read_text())
        for role_name in ALL_ROLE_NAMES:
            assert role_name in data["roles"], f"Missing role: {role_name}"

    def test_save_includes_agent_and_directive(self, tmp_path):
        """Each role in saved YAML has both agent and directive fields."""
        out = tmp_path / "output.yaml"
        Profile.default().save(out)
        data = yaml.safe_load(out.read_text())
        for role_name in ALL_ROLE_NAMES:
            role_data = data["roles"][role_name]
            assert "agent" in role_data
            assert "directive" in role_data

    def test_default_serialization_is_minimal(self, tmp_path):
        """Profile.default() serialization does not contain tdd or followup keys."""
        out = tmp_path / "output.yaml"
        Profile.default().save(out)
        data = yaml.safe_load(out.read_text())
        for role_name in ALL_ROLE_NAMES:
            role_data = data["roles"][role_name]
            assert "tdd" not in role_data, f"{role_name} should not have tdd key"
            assert "followup" not in role_data, f"{role_name} should not have followup key"

    def test_save_roundtrip(self, tmp_path):
        """Profile.default() saved then loaded back produces an equal profile."""
        out = tmp_path / "roundtrip.yaml"
        original = Profile.default()
        original.save(out)
        loaded = Profile.from_yaml(out)
        for role_name in ALL_ROLE_NAMES:
            orig_cfg = getattr(original, role_name)
            load_cfg = getattr(loaded, role_name)
            assert orig_cfg.agent == load_cfg.agent, f"{role_name} agent mismatch"
            assert orig_cfg.directive == load_cfg.directive, f"{role_name} directive mismatch"

    def test_save_roundtrip_with_overrides(self, tmp_path):
        """A profile with overrides survives save/load roundtrip."""
        yaml_path = _write_yaml(
            tmp_path / "input.yaml",
            {
                "roles": {
                    "reviewer": {"agent": "antigravity"},
                    "tester": {"directive": "Custom."},
                }
            },
        )
        profile = Profile.from_yaml(yaml_path)
        out = tmp_path / "roundtrip.yaml"
        profile.save(out)
        reloaded = Profile.from_yaml(out)
        assert reloaded.reviewer.agent == "antigravity"
        assert reloaded.tester.directive == "Custom."

    def test_save_roundtrip_with_sub_modes(self, tmp_path):
        """A profile with all sub-modes set serializes to YAML, parses back, and is equal."""
        profile = Profile.default()
        profile.implementor.tdd = ModeConfig(directive="TDD impl")
        profile.tester.tdd = ModeConfig(directive="TDD test")
        profile.reviewer.followup = ModeConfig(directive="Followup review")

        out = tmp_path / "roundtrip.yaml"
        profile.save(out)
        reloaded = Profile.from_yaml(out)

        assert reloaded.implementor.tdd == ModeConfig(directive="TDD impl")
        assert reloaded.tester.tdd == ModeConfig(directive="TDD test")
        assert reloaded.reviewer.followup == ModeConfig(directive="Followup review")
        # Other roles should have no sub-modes
        assert reloaded.fixer.tdd is None
        assert reloaded.fixer.followup is None
        assert reloaded.merger.tdd is None
        assert reloaded.merger.followup is None
        assert reloaded.planner.tdd is None
        assert reloaded.planner.followup is None

    def test_save_emits_sub_mode_blocks_only_when_set(self, tmp_path):
        """Serialized YAML only contains tdd/followup blocks when set."""
        profile = Profile.default()
        profile.tester.tdd = ModeConfig(directive="TDD")

        out = tmp_path / "output.yaml"
        profile.save(out)
        data = yaml.safe_load(out.read_text())

        assert "tdd" in data["roles"]["tester"]
        assert data["roles"]["tester"]["tdd"]["directive"] == "TDD"
        # Other roles should not have sub-mode keys
        assert "tdd" not in data["roles"]["implementor"]
        assert "followup" not in data["roles"]["reviewer"]


# ---------------------------------------------------------------------------
# Profile.resolve() — merge order
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_defaults_when_no_files_exist(self, tmp_path):
        """resolve() with no YAML files returns pure defaults."""
        profile = Profile.resolve(repo=tmp_path)
        default = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            assert getattr(profile, role_name).agent == getattr(default, role_name).agent

    def test_resolve_global_only(self, tmp_path):
        """resolve() picks up ~/.workbench/profile.yaml when it exists."""
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_yaml(
            home / ".workbench" / "profile.yaml",
            {"roles": {"implementor": {"agent": "codex"}}},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo)
        assert profile.implementor.agent == "codex"

    def test_resolve_local_overrides_global(self, tmp_path):
        """Local (.workbench/profile.yaml in repo) overrides global."""
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        # Global sets implementor to codex
        _write_yaml(
            home / ".workbench" / "profile.yaml",
            {"roles": {"implementor": {"agent": "codex"}}},
        )
        # Local sets implementor to antigravity
        _write_yaml(
            repo / ".workbench" / "profile.yaml",
            {"roles": {"implementor": {"agent": "antigravity"}}},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo)
        assert profile.implementor.agent == "antigravity"

    def test_resolve_explicit_path_wins(self, tmp_path):
        """Explicit profile_path overrides both global and local."""
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        # Global sets implementor to codex
        _write_yaml(
            home / ".workbench" / "profile.yaml",
            {"roles": {"implementor": {"agent": "codex"}}},
        )
        # Local sets implementor to antigravity
        _write_yaml(
            repo / ".workbench" / "profile.yaml",
            {"roles": {"implementor": {"agent": "antigravity"}}},
        )
        # Explicit sets implementor to custom-agent
        explicit = _write_yaml(
            tmp_path / "explicit.yaml",
            {"roles": {"implementor": {"agent": "custom-agent"}}},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo, profile_path=explicit)
        assert profile.implementor.agent == "custom-agent"

    def test_resolve_merge_preserves_unset_fields(self, tmp_path):
        """Fields not specified in later YAML files retain earlier values."""
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        # Global sets reviewer agent to antigravity AND tester agent to codex
        _write_yaml(
            home / ".workbench" / "profile.yaml",
            {
                "roles": {
                    "reviewer": {"agent": "antigravity"},
                    "tester": {"agent": "codex"},
                }
            },
        )
        # Local only overrides reviewer agent — tester should stay codex from global
        _write_yaml(
            repo / ".workbench" / "profile.yaml",
            {"roles": {"reviewer": {"agent": "custom-reviewer"}}},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo)
        assert profile.reviewer.agent == "custom-reviewer"
        assert profile.tester.agent == "codex"

    def test_resolve_directive_extend_chains_across_layers(self, tmp_path):
        """directive_extend at each layer appends to whatever the directive is at that point."""
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        # Global extends the default tester directive
        _write_yaml(
            home / ".workbench" / "profile.yaml",
            {"roles": {"tester": {"directive_extend": "Global addition."}}},
        )
        # Local extends again (should append to global-extended version)
        _write_yaml(
            repo / ".workbench" / "profile.yaml",
            {"roles": {"tester": {"directive_extend": "Local addition."}}},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo)
        # Default is "" so global extend produces "\n\nGlobal addition."
        # Then local extend appends "\n\nLocal addition."
        expected = "\n\nGlobal addition." + "\n\n" + "Local addition."
        assert profile.tester.directive == expected

    def test_resolve_explicit_path_none_skipped(self, tmp_path):
        """When profile_path is None, only global and local are used."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_yaml(
            repo / ".workbench" / "profile.yaml",
            {"roles": {"fixer": {"agent": "antigravity"}}},
        )
        # Use a fake home with no global profile
        home = tmp_path / "home"
        home.mkdir()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo, profile_path=None)
        assert profile.fixer.agent == "antigravity"

    def test_resolve_nonexistent_explicit_path_ignored(self, tmp_path):
        """An explicit profile_path that doesn't exist is silently skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        nonexistent = tmp_path / "does_not_exist.yaml"
        # Use a fake home with no global profile
        home = tmp_path / "home"
        home.mkdir()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo, profile_path=nonexistent)
        # Should just be defaults
        default = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            assert getattr(profile, role_name).agent == getattr(default, role_name).agent


# ---------------------------------------------------------------------------
# RoleConfig basics
# ---------------------------------------------------------------------------


class TestRoleConfig:
    def test_default_values(self):
        """RoleConfig() has agent='claude' and empty directive."""
        rc = RoleConfig()
        assert rc.agent == "claude"
        assert rc.model == ""
        assert rc.directive == ""
        assert rc.tdd is None
        assert rc.followup is None

    def test_custom_values(self):
        """RoleConfig accepts custom agent and directive."""
        rc = RoleConfig(agent="antigravity", directive="Do things.")
        assert rc.agent == "antigravity"
        assert rc.directive == "Do things."


# ---------------------------------------------------------------------------
# Profile model field
# ---------------------------------------------------------------------------


class TestProfileModel:
    def test_model_unset_by_default(self):
        """Every role on a default Profile has model=''."""
        profile = Profile.default()
        for role_name in ALL_ROLE_NAMES:
            assert getattr(profile, role_name).model == ""

    def test_yaml_sets_role_model(self, tmp_path):
        """A YAML model: field populates RoleConfig.model on the right role."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"implementor": {"model": "claude-opus-4-8"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.implementor.model == "claude-opus-4-8"
        # Other roles still unset
        for role_name in ALL_ROLE_NAMES:
            if role_name != "implementor":
                assert getattr(profile, role_name).model == ""

    def test_role_without_model_keeps_empty(self, tmp_path):
        """A role config that omits 'model' keeps the default empty string."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"implementor": {"agent": "codex"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.implementor.agent == "codex"
        assert profile.implementor.model == ""

    def test_save_omits_model_when_empty(self, tmp_path):
        """Roles with an empty model do not emit a 'model:' key on save."""
        out = tmp_path / "output.yaml"
        Profile.default().save(out)
        data = yaml.safe_load(out.read_text())
        for role_name in ALL_ROLE_NAMES:
            assert "model" not in data["roles"][role_name]

    def test_save_includes_model_when_set(self, tmp_path):
        """A role with model set emits a 'model:' key on save."""
        profile = Profile.default()
        profile.tester.model = "gpt-5-codex"
        out = tmp_path / "output.yaml"
        profile.save(out)
        data = yaml.safe_load(out.read_text())
        assert data["roles"]["tester"]["model"] == "gpt-5-codex"
        # Other roles still omit the key
        assert "model" not in data["roles"]["implementor"]
        assert "model" not in data["roles"]["reviewer"]

    def test_model_roundtrip(self, tmp_path):
        """A profile with model set survives save/load roundtrip."""
        profile = Profile.default()
        profile.implementor.model = "claude-opus-4-8"
        profile.reviewer.model = "gpt-5-codex"
        out = tmp_path / "roundtrip.yaml"
        profile.save(out)
        reloaded = Profile.from_yaml(out)
        assert reloaded.implementor.model == "claude-opus-4-8"
        assert reloaded.reviewer.model == "gpt-5-codex"
        # Unset roles still unset after roundtrip
        assert reloaded.tester.model == ""
        assert reloaded.fixer.model == ""


# ---------------------------------------------------------------------------
# ModeConfig basics
# ---------------------------------------------------------------------------


class TestModeConfig:
    def test_default_values(self):
        """ModeConfig() has empty directive."""
        mc = ModeConfig()
        assert mc.directive == ""

    def test_custom_values(self):
        """ModeConfig accepts custom directive."""
        mc = ModeConfig(directive="Custom mode.")
        assert mc.directive == "Custom mode."

    def test_equality(self):
        """ModeConfig equality works as expected."""
        assert ModeConfig(directive="A") == ModeConfig(directive="A")
        assert ModeConfig(directive="A") != ModeConfig(directive="B")
