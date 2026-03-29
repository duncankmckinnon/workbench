"""Agent platform adapters for different AI coding CLIs."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AgentAdapter(ABC):
    """Abstraction for different agent CLI platforms."""

    name: str

    @abstractmethod
    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        """Return the full command list to invoke the agent."""

    @abstractmethod
    def parse_output(self, raw: str) -> tuple[str, dict]:
        """Parse agent stdout. Returns (output_text, cost_dict)."""


ALLOWED_TOOLS = (
    "Edit,Write,Read,Glob,Grep," "Bash(git *),Bash(uv run *),Bash(cd *),Bash(ls *),Bash(npx *)"
)


class ClaudeAdapter(AgentAdapter):
    """Adapter for the Claude Code CLI."""

    name = "claude"

    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        return [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--allowedTools",
            ALLOWED_TOOLS,
        ]

    def parse_output(self, raw: str) -> tuple[str, dict]:
        try:
            data = json.loads(raw)
            result = data.get("result", raw)
            cost = data.get("cost_usd", {})
            return (result, cost)
        except (json.JSONDecodeError, TypeError):
            return (raw, {})


class CodexAdapter(AgentAdapter):
    """Adapter for the Codex CLI."""

    name = "codex"

    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        return [
            "codex",
            "-q",
            "--full-auto",
            "--approval-mode",
            "full-auto",
            prompt,
        ]

    def parse_output(self, raw: str) -> tuple[str, dict]:
        return (raw.strip(), {})


@dataclass
class ConfigAdapter(AgentAdapter):
    """Adapter driven by a YAML config entry from .workbench/agents.yaml."""

    name: str
    command: str
    args: list[str]
    output_format: str = "text"
    json_result_key: str = "result"
    json_cost_key: str = "cost_usd"

    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        resolved_args = [a.replace("{prompt}", prompt) for a in self.args]
        return [self.command, *resolved_args]

    def parse_output(self, raw: str) -> tuple[str, dict]:
        if self.output_format == "json":
            try:
                data = json.loads(raw)
                result = data.get(self.json_result_key, raw)
                cost = data.get(self.json_cost_key, {})
                return (result, cost)
            except (json.JSONDecodeError, TypeError):
                return (raw, {})
        return (raw.strip(), {})


class GenericAdapter(AgentAdapter):
    """Fallback adapter for unknown agent commands."""

    def __init__(self, agent_cmd: str) -> None:
        self.name = agent_cmd
        self._cmd = agent_cmd

    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        return [self._cmd, prompt]

    def parse_output(self, raw: str) -> tuple[str, dict]:
        return (raw.strip(), {})


def _load_yaml_config(config_path: Path) -> dict[str, Any]:
    """Load agents config from a YAML file."""
    try:
        import yaml
    except ImportError:
        # Fall back to a basic parser if PyYAML isn't installed.
        # For robustness, we require PyYAML for YAML configs.
        raise ImportError(
            "PyYAML is required for custom agent configs. " "Install it with: pip install pyyaml"
        )
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def get_adapter(agent_cmd: str, config_path: Path | None = None) -> AgentAdapter:
    """Return the appropriate adapter for the given agent command.

    Resolution order:
    1. If config_path is provided and contains agent_cmd, use ConfigAdapter
    2. Built-in adapters: "claude", "codex"
    3. Fallback: GenericAdapter
    """
    if config_path is not None and config_path.exists():
        config = _load_yaml_config(config_path)
        agents = config.get("agents", {})
        if agent_cmd in agents:
            entry = agents[agent_cmd]
            return ConfigAdapter(
                name=agent_cmd,
                command=entry.get("command", agent_cmd),
                args=entry.get("args", ["{prompt}"]),
                output_format=entry.get("output_format", "text"),
                json_result_key=entry.get("json_result_key", "result"),
                json_cost_key=entry.get("json_cost_key", "cost_usd"),
            )

    if agent_cmd == "claude":
        return ClaudeAdapter()
    if agent_cmd == "codex":
        return CodexAdapter()
    return GenericAdapter(agent_cmd)
