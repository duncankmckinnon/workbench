"""Profile system for mapping pipeline roles to agent CLIs and directives."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Roles that support each sub-mode
_TDD_ROLES = {"implementor", "tester"}
_FOLLOWUP_ROLES = {"reviewer"}
_ALL_ROLE_NAMES = ("implementor", "tester", "reviewer", "fixer", "merger", "planner")


@dataclass
class ModeConfig:
    """Sub-mode override (TDD, followup) — directive only.

    Sub-modes inherit the parent role's agent.
    """

    directive: str = ""


@dataclass
class RoleConfig:
    agent: str = "claude"
    directive: str = ""
    tdd: ModeConfig | None = None
    followup: ModeConfig | None = None


@dataclass
class Profile:
    implementor: RoleConfig = field(default_factory=RoleConfig)
    tester: RoleConfig = field(default_factory=RoleConfig)
    reviewer: RoleConfig = field(default_factory=RoleConfig)
    fixer: RoleConfig = field(default_factory=RoleConfig)
    merger: RoleConfig = field(default_factory=RoleConfig)
    planner: RoleConfig = field(default_factory=RoleConfig)

    _ROLE_NAMES: tuple[str, ...] = _ALL_ROLE_NAMES

    @classmethod
    def default(cls) -> Profile:
        """Return a Profile with all fields populated from built-in defaults."""
        return cls(
            implementor=RoleConfig(agent="claude", directive=""),
            tester=RoleConfig(agent="claude", directive=""),
            reviewer=RoleConfig(agent="claude", directive=""),
            fixer=RoleConfig(agent="claude", directive=""),
            merger=RoleConfig(agent="claude", directive=""),
            planner=RoleConfig(agent="claude", directive=""),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> Profile:
        """Read a YAML file and merge onto defaults."""
        profile = cls.default()
        profile._merge_from_yaml(path)
        return profile

    def _merge_from_yaml(self, path: Path) -> None:
        """Apply a single YAML file's overrides onto this profile in-place."""
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            return
        roles = data.get("roles")
        if not isinstance(roles, dict):
            return

        for role_name in self._ROLE_NAMES:
            role_data = roles.get(role_name)
            if not isinstance(role_data, dict):
                continue
            cfg: RoleConfig = getattr(self, role_name)

            if "agent" in role_data:
                cfg.agent = role_data["agent"]

            if "directive" in role_data:
                cfg.directive = role_data["directive"]
            elif "directive_extend" in role_data:
                cfg.directive = cfg.directive + "\n\n" + role_data["directive_extend"]

            # Validate and parse sub-modes
            self._parse_sub_modes(role_name, role_data, cfg)

    @staticmethod
    def _parse_sub_modes(role_name: str, role_data: dict, cfg: RoleConfig) -> None:
        """Parse tdd/followup sub-mode blocks and validate placement."""
        # Check for tdd sub-mode
        if "tdd" in role_data:
            if role_name not in _TDD_ROLES:
                raise ValueError(f"{role_name} does not support a 'tdd' sub-mode")
            tdd_data = role_data["tdd"]
            if isinstance(tdd_data, dict):
                directive = ""
                if "directive" in tdd_data:
                    directive = tdd_data["directive"]
                elif "directive_extend" in tdd_data:
                    directive = tdd_data["directive_extend"]
                cfg.tdd = ModeConfig(directive=directive)

        # Check for followup sub-mode
        if "followup" in role_data:
            if role_name not in _FOLLOWUP_ROLES:
                raise ValueError(f"{role_name} does not support a 'followup' sub-mode")
            followup_data = role_data["followup"]
            if isinstance(followup_data, dict):
                directive = ""
                if "directive" in followup_data:
                    directive = followup_data["directive"]
                elif "directive_extend" in followup_data:
                    directive = followup_data["directive_extend"]
                cfg.followup = ModeConfig(directive=directive)

    def save(self, path: Path) -> None:
        """Write the current profile state to a YAML file."""
        path.parent.mkdir(parents=True, exist_ok=True)

        # Use a custom representer for clean multiline block scalars
        class _Dumper(yaml.SafeDumper):
            pass

        def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
            if "\n" in data:
                return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
            return dumper.represent_scalar("tag:yaml.org,2002:str", data)

        _Dumper.add_representer(str, _str_representer)

        roles_dict: dict[str, Any] = {}
        for role_name in self._ROLE_NAMES:
            cfg: RoleConfig = getattr(self, role_name)
            role_dict: dict[str, Any] = {
                "agent": cfg.agent,
                "directive": cfg.directive.strip(),
            }
            if cfg.tdd is not None:
                role_dict["tdd"] = {"directive": cfg.tdd.directive.strip()}
            if cfg.followup is not None:
                role_dict["followup"] = {"directive": cfg.followup.directive.strip()}
            roles_dict[role_name] = role_dict
        data = {"roles": roles_dict}
        path.write_text(
            yaml.dump(
                data, Dumper=_Dumper, default_flow_style=False, sort_keys=False, allow_unicode=True
            )
        )

    @staticmethod
    def _profile_filename(name: str | None = None) -> str:
        """Return the profile filename for a given name."""
        if name:
            return f"profile.{name}.yaml"
        return "profile.yaml"

    @classmethod
    def resolve(
        cls,
        repo: Path,
        profile_path: Path | None = None,
        profile_name: str | None = None,
    ) -> Profile:
        """Merge profiles in priority order: default -> global -> local -> explicit.

        When profile_name is provided, look for profile.<name>.yaml instead of
        profile.yaml. An explicit profile_path always takes highest priority.
        """
        profile = cls.default()
        filename = cls._profile_filename(profile_name)

        # Global: ~/.workbench/profile.yaml (or profile.<name>.yaml)
        global_path = Path.home() / ".workbench" / filename
        if global_path.exists():
            profile._merge_from_yaml(global_path)

        # Local: <repo>/.workbench/profile.yaml (or profile.<name>.yaml)
        local_path = repo / ".workbench" / filename
        if local_path.exists():
            profile._merge_from_yaml(local_path)

        # Explicit path (highest priority)
        if profile_path is not None and profile_path.exists():
            profile._merge_from_yaml(profile_path)

        return profile
