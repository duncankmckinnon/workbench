"""Tests for the profile data model and YAML load/save/merge logic."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from workbench.agents import DEFAULT_DIRECTIVES, Role
from workbench.profile import Profile, RoleConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_ROLES = [Role.IMPLEMENTOR, Role.TESTER, Role.REVIEWER, Role.FIXER, Role.MERGER]


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
        """Profile.default() returns a profile with all 5 roles populated."""
        profile = Profile.default()
        for role in ALL_ROLES:
            cfg = getattr(profile, role.value)
            assert isinstance(cfg, RoleConfig)

    def test_default_profile_agent_is_claude(self):
        """Every role defaults to agent='claude'."""
        profile = Profile.default()
        for role in ALL_ROLES:
            cfg = getattr(profile, role.value)
            assert cfg.agent == "claude", f"{role} agent should be 'claude'"

    def test_default_profile_directives_match(self):
        """Every role directive matches DEFAULT_DIRECTIVES."""
        profile = Profile.default()
        for role in ALL_ROLES:
            cfg = getattr(profile, role.value)
            assert cfg.directive == DEFAULT_DIRECTIVES[role], (
                f"{role} directive does not match DEFAULT_DIRECTIVES"
            )


# ---------------------------------------------------------------------------
# Profile.from_yaml() — agent overrides
# ---------------------------------------------------------------------------


class TestFromYamlAgent:
    def test_override_single_role_agent(self, tmp_path):
        """YAML with reviewer.agent: gemini overrides only that role."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"reviewer": {"agent": "gemini"}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.reviewer.agent == "gemini"
        # All others unchanged
        for role in [Role.IMPLEMENTOR, Role.TESTER, Role.FIXER, Role.MERGER]:
            assert getattr(profile, role.value).agent == "claude"

    def test_override_multiple_role_agents(self, tmp_path):
        """YAML can override agents on multiple roles at once."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {
                "roles": {
                    "implementor": {"agent": "codex"},
                    "tester": {"agent": "gemini"},
                }
            },
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.implementor.agent == "codex"
        assert profile.tester.agent == "gemini"
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
        expected = DEFAULT_DIRECTIVES[Role.TESTER] + "\n\n" + "Extra instructions."
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
        for role in ALL_ROLES:
            cfg = getattr(profile, role.value)
            assert cfg.agent == "claude"

    def test_unknown_field_ignored(self, tmp_path):
        """YAML with an unknown field on a known role does not raise."""
        yaml_path = _write_yaml(
            tmp_path / "profile.yaml",
            {"roles": {"tester": {"agent": "gemini", "bogus_field": 42}}},
        )
        profile = Profile.from_yaml(yaml_path)
        assert profile.tester.agent == "gemini"

    def test_empty_roles_section(self, tmp_path):
        """YAML with empty roles: produces pure defaults."""
        yaml_path = _write_yaml(tmp_path / "profile.yaml", {"roles": {}})
        profile = Profile.from_yaml(yaml_path)
        default = Profile.default()
        for role in ALL_ROLES:
            assert getattr(profile, role.value).agent == getattr(default, role.value).agent
            assert getattr(profile, role.value).directive == getattr(default, role.value).directive

    def test_missing_roles_key(self, tmp_path):
        """YAML without a roles key at all produces pure defaults."""
        yaml_path = _write_yaml(tmp_path / "profile.yaml", {"something_else": True})
        profile = Profile.from_yaml(yaml_path)
        default = Profile.default()
        for role in ALL_ROLES:
            assert getattr(profile, role.value).agent == getattr(default, role.value).agent


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
        """Saved YAML contains entries for all 5 roles."""
        out = tmp_path / "output.yaml"
        Profile.default().save(out)
        data = yaml.safe_load(out.read_text())
        for role in ALL_ROLES:
            assert role.value in data["roles"], f"Missing role: {role.value}"

    def test_save_includes_agent_and_directive(self, tmp_path):
        """Each role in saved YAML has both agent and directive fields."""
        out = tmp_path / "output.yaml"
        Profile.default().save(out)
        data = yaml.safe_load(out.read_text())
        for role in ALL_ROLES:
            role_data = data["roles"][role.value]
            assert "agent" in role_data
            assert "directive" in role_data

    def test_save_roundtrip(self, tmp_path):
        """Profile.default() saved then loaded back produces an equal profile."""
        out = tmp_path / "roundtrip.yaml"
        original = Profile.default()
        original.save(out)
        loaded = Profile.from_yaml(out)
        for role in ALL_ROLES:
            orig_cfg = getattr(original, role.value)
            load_cfg = getattr(loaded, role.value)
            assert orig_cfg.agent == load_cfg.agent, f"{role} agent mismatch"
            assert orig_cfg.directive == load_cfg.directive, f"{role} directive mismatch"

    def test_save_roundtrip_with_overrides(self, tmp_path):
        """A profile with overrides survives save/load roundtrip."""
        yaml_path = _write_yaml(
            tmp_path / "input.yaml",
            {
                "roles": {
                    "reviewer": {"agent": "gemini"},
                    "tester": {"directive": "Custom."},
                }
            },
        )
        profile = Profile.from_yaml(yaml_path)
        out = tmp_path / "roundtrip.yaml"
        profile.save(out)
        reloaded = Profile.from_yaml(out)
        assert reloaded.reviewer.agent == "gemini"
        assert reloaded.tester.directive == "Custom."


# ---------------------------------------------------------------------------
# Profile.resolve() — merge order
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_defaults_when_no_files_exist(self, tmp_path):
        """resolve() with no YAML files returns pure defaults."""
        profile = Profile.resolve(repo=tmp_path)
        default = Profile.default()
        for role in ALL_ROLES:
            assert getattr(profile, role.value).agent == getattr(default, role.value).agent

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
        # Local sets implementor to gemini
        _write_yaml(
            repo / ".workbench" / "profile.yaml",
            {"roles": {"implementor": {"agent": "gemini"}}},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo)
        assert profile.implementor.agent == "gemini"

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
        # Local sets implementor to gemini
        _write_yaml(
            repo / ".workbench" / "profile.yaml",
            {"roles": {"implementor": {"agent": "gemini"}}},
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
        # Global sets reviewer agent to gemini AND tester agent to codex
        _write_yaml(
            home / ".workbench" / "profile.yaml",
            {
                "roles": {
                    "reviewer": {"agent": "gemini"},
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
        expected = DEFAULT_DIRECTIVES[Role.TESTER] + "\n\n" + "Global addition." + "\n\n" + "Local addition."
        assert profile.tester.directive == expected

    def test_resolve_explicit_path_none_skipped(self, tmp_path):
        """When profile_path is None, only global and local are used."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_yaml(
            repo / ".workbench" / "profile.yaml",
            {"roles": {"fixer": {"agent": "gemini"}}},
        )
        # Use a fake home with no global profile
        home = tmp_path / "home"
        home.mkdir()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "home", classmethod(lambda cls: home))
            profile = Profile.resolve(repo=repo, profile_path=None)
        assert profile.fixer.agent == "gemini"

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
        for role in ALL_ROLES:
            assert getattr(profile, role.value).agent == getattr(default, role.value).agent


# ---------------------------------------------------------------------------
# RoleConfig basics
# ---------------------------------------------------------------------------


class TestRoleConfig:
    def test_default_values(self):
        """RoleConfig() has agent='claude' and empty directive."""
        rc = RoleConfig()
        assert rc.agent == "claude"
        assert rc.directive == ""

    def test_custom_values(self):
        """RoleConfig accepts custom agent and directive."""
        rc = RoleConfig(agent="gemini", directive="Do things.")
        assert rc.agent == "gemini"
        assert rc.directive == "Do things."
