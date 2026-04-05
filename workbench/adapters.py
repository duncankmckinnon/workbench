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

    @classmethod
    def to_config(cls) -> dict[str, Any]:
        """Return a YAML-compatible config dict for this adapter."""
        raise NotImplementedError(f"{cls.__name__} does not support to_config")

    @classmethod
    def from_config(cls, name: str, entry: dict[str, Any]) -> "ConfigAdapter":
        """Create a ConfigAdapter from a YAML config dict."""
        return ConfigAdapter(
            name=name,
            command=entry.get("command", name),
            args=entry.get("args", ["{prompt}"]),
            output_format=entry.get("output_format", "text"),
            json_result_key=entry.get("json_result_key", "result"),
            json_cost_key=entry.get("json_cost_key", "cost_usd"),
        )


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

    @classmethod
    def to_config(cls) -> dict[str, Any]:
        return {
            "command": "claude",
            "args": ["-p", "{prompt}", "--output-format", "json", "--allowedTools", ALLOWED_TOOLS],
            "output_format": "json",
            "json_result_key": "result",
            "json_cost_key": "cost_usd",
        }


class CodexAdapter(AgentAdapter):
    """Adapter for the Codex CLI (OpenAI).

    Uses `codex exec` for non-interactive execution with JSON output.
    """

    name = "codex"

    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        return [
            "codex",
            "exec",
            "--full-auto",
            "--json",
            prompt,
        ]

    def parse_output(self, raw: str) -> tuple[str, dict]:
        # codex exec --json outputs newline-delimited JSON events
        # The last message event contains the assistant's response
        lines = raw.strip().split("\n")
        last_message = ""
        for line in reversed(lines):
            try:
                data = json.loads(line)
                if data.get("type") == "message" and data.get("role") == "assistant":
                    last_message = data.get("content", "")
                    break
            except (json.JSONDecodeError, TypeError):
                continue
        return (last_message or raw.strip(), {})

    @classmethod
    def to_config(cls) -> dict[str, Any]:
        return {
            "command": "codex",
            "args": ["exec", "--full-auto", "--json", "{prompt}"],
            "output_format": "json",
            "json_result_key": "result",
            "json_cost_key": "cost_usd",
        }


class CursorAdapter(AgentAdapter):
    """Adapter for the Cursor CLI (agent command).

    Uses ``agent -p`` for non-interactive (print) mode.
    See https://cursor.com/docs/cli/overview
    """

    name = "cursor"

    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        return [
            "agent",
            "-p",
            prompt,
            "--output-format",
            "text",
        ]

    def parse_output(self, raw: str) -> tuple[str, dict]:
        return (raw.strip(), {})

    @classmethod
    def to_config(cls) -> dict[str, Any]:
        return {
            "command": "agent",
            "args": ["-p", "{prompt}", "--output-format", "text"],
            "output_format": "text",
        }


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

    def to_config(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "command": self.command,
            "args": self.args,
            "output_format": self.output_format,
        }
        if self.output_format == "json":
            entry["json_result_key"] = self.json_result_key
            entry["json_cost_key"] = self.json_cost_key
        return entry


class GeminiAdapter(AgentAdapter):
    """Adapter for the Gemini CLI."""

    name = "gemini"

    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        return [
            "gemini",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--approval-mode",
            "yolo",
        ]

    def parse_output(self, raw: str) -> tuple[str, dict]:
        try:
            data = json.loads(raw)
            result = data.get("response", raw)
            stats = data.get("stats", {})
            return (result, stats)
        except (json.JSONDecodeError, TypeError):
            return (raw, {})

    @classmethod
    def to_config(cls) -> dict[str, Any]:
        return {
            "command": "gemini",
            "args": ["-p", "{prompt}", "--output-format", "json", "--approval-mode", "yolo"],
            "output_format": "json",
            "json_result_key": "response",
            "json_cost_key": "stats",
        }


class GenericAdapter(AgentAdapter):
    """Fallback adapter for unknown agent commands."""

    def __init__(self, agent_cmd: str) -> None:
        self.name = agent_cmd
        self._cmd = agent_cmd

    def build_command(self, prompt: str, cwd: Path) -> list[str]:
        return [self._cmd, prompt]

    def parse_output(self, raw: str) -> tuple[str, dict]:
        return (raw.strip(), {})


BUILTIN_ADAPTERS: dict[str, type[AgentAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "cursor": CursorAdapter,
}


def default_agents_config() -> dict[str, dict[str, Any]]:
    """Generate the default agents.yaml config from all built-in adapters."""
    return {name: cls.to_config() for name, cls in BUILTIN_ADAPTERS.items()}


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
    2. Built-in adapters: "claude", "codex", "gemini", "cursor"
    3. Fallback: GenericAdapter
    """
    if config_path is not None and config_path.exists():
        config = _load_yaml_config(config_path)
        agents = config.get("agents", {})
        if agent_cmd in agents:
            return AgentAdapter.from_config(agent_cmd, agents[agent_cmd])

    if agent_cmd in BUILTIN_ADAPTERS:
        return BUILTIN_ADAPTERS[agent_cmd]()
    return GenericAdapter(agent_cmd)
