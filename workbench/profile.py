"""Profile system for mapping pipeline roles to agent CLIs and directives."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .agents import DEFAULT_DIRECTIVES, Role


@dataclass
class RoleConfig:
    agent: str = "claude"
    directive: str = ""


@dataclass
class Profile:
    implementor: RoleConfig = field(default_factory=RoleConfig)
    tester: RoleConfig = field(default_factory=RoleConfig)
    reviewer: RoleConfig = field(default_factory=RoleConfig)
    fixer: RoleConfig = field(default_factory=RoleConfig)
    merger: RoleConfig = field(default_factory=RoleConfig)

    _ROLE_NAMES: tuple[str, ...] = (
        "implementor",
        "tester",
        "reviewer",
        "fixer",
        "merger",
    )

    @classmethod
    def default(cls) -> Profile:
        """Return a Profile with all fields populated from built-in defaults."""
        return cls(
            implementor=RoleConfig(agent="claude", directive=DEFAULT_DIRECTIVES[Role.IMPLEMENTOR]),
            tester=RoleConfig(agent="claude", directive=DEFAULT_DIRECTIVES[Role.TESTER]),
            reviewer=RoleConfig(agent="claude", directive=DEFAULT_DIRECTIVES[Role.REVIEWER]),
            fixer=RoleConfig(agent="claude", directive=DEFAULT_DIRECTIVES[Role.FIXER]),
            merger=RoleConfig(agent="claude", directive=DEFAULT_DIRECTIVES[Role.MERGER]),
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

    def save(self, path: Path) -> None:
        """Write the current profile state to a YAML file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        roles_dict: dict[str, Any] = {}
        for role_name in self._ROLE_NAMES:
            cfg: RoleConfig = getattr(self, role_name)
            roles_dict[role_name] = {
                "agent": cfg.agent,
                "directive": cfg.directive,
            }
        data = {"roles": roles_dict}
        path.write_text(yaml.dump(data, default_flow_style=False))

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
