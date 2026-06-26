"""Agent platform adapters for different AI coding CLIs."""

from __future__ import annotations

import json
from abc import ABC
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


@dataclass
class AgentConfig:
    """YAML-serializable configuration for an agent adapter."""

    command: str
    args: list[str] = field(default_factory=lambda: ["{prompt}"])
    output_format: OutputFormat = OutputFormat.TEXT
    json_result_key: str = "result"
    json_cost_key: str = "cost_usd"
    inject_env: bool = True
    model: str | None = None
    model_flag: str | None = None

    def __post_init__(self) -> None:
        # Coerce string to enum (raises ValueError for invalid values)
        if isinstance(self.output_format, str):
            self.output_format = OutputFormat(self.output_format)
        if not self.command:
            raise ValueError("command must not be empty")
        if not self.args:
            raise ValueError("args must not be empty")
        if "{prompt}" not in self.args:
            raise ValueError("args must contain '{prompt}' placeholder")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a YAML-compatible dict."""
        entry: dict[str, Any] = {
            "command": self.command,
            "args": self.args,
            "output_format": self.output_format.value,
        }
        if self.output_format == OutputFormat.JSON:
            entry["json_result_key"] = self.json_result_key
            entry["json_cost_key"] = self.json_cost_key
        entry["inject_env"] = self.inject_env
        if self.model is not None:
            entry["model"] = self.model
        if self.model_flag is not None:
            entry["model_flag"] = self.model_flag
        return entry

    @classmethod
    def from_dict(cls, entry: dict[str, Any], default_command: str = "") -> AgentConfig:
        """Deserialize from a YAML dict with defaults for missing fields."""
        return cls(
            command=entry.get("command", default_command),
            args=entry.get("args", ["{prompt}"]),
            output_format=entry.get("output_format", "text"),
            json_result_key=entry.get("json_result_key", "result"),
            json_cost_key=entry.get("json_cost_key", "cost_usd"),
            inject_env=entry.get("inject_env", False),
            model=entry.get("model"),
            model_flag=entry.get("model_flag"),
        )


class AgentAdapter(ABC):
    """Abstraction for different agent CLI platforms.

    Each adapter holds an ``AgentConfig`` that describes its CLI invocation.
    Built-in adapters set their config directly in ``__init__``.
    Custom adapters are created from YAML via ``from_config()``.
    """

    name: str
    config: AgentConfig

    def parse_output(self, raw: str) -> tuple[str, dict]:
        """Parse agent stdout. Returns (output_text, cost_dict).

        Default implementation handles both text and JSON based on config.
        Override for agents with non-standard output (e.g. NDJSON streams).
        """
        if self.config.output_format == OutputFormat.JSON:
            try:
                data = json.loads(raw)
                result = data.get(self.config.json_result_key, raw)
                cost = data.get(self.config.json_cost_key, {})
                return (result, cost)
            except (json.JSONDecodeError, TypeError):
                return (raw, {})
        return (raw.strip(), {})

    def build_command(self, prompt: str, cwd: Path, model: str | None = None) -> list[str]:
        """Build the CLI command from config, substituting the prompt.

        ``model`` overrides ``self.config.model`` when provided. If a model is
        resolved and ``self.config.model_flag`` is set, ``[flag, model]`` is
        appended to the args.
        """
        resolved = model or self.config.model
        resolved_args = [a.replace("{prompt}", prompt) for a in self.config.args]
        if resolved and self.config.model_flag:
            resolved_args += [self.config.model_flag, resolved]
        return [self.config.command, *resolved_args]

    def to_config(self) -> dict[str, Any]:
        """Serialize this adapter's config to a YAML-compatible dict."""
        return self.config.to_dict()

    @classmethod
    def from_config(cls, name: str, entry: dict[str, Any]) -> ConfigAdapter:
        """Create a ConfigAdapter from a YAML config dict."""
        config = AgentConfig.from_dict(entry, default_command=name)
        return ConfigAdapter(name=name, config=config)


@dataclass
class ConfigAdapter(AgentAdapter):
    """Adapter driven by a YAML config entry from .workbench/agents.yaml."""

    name: str
    config: AgentConfig


class ClaudeAdapter(AgentAdapter):
    """Adapter for the Claude Code CLI."""

    name = "claude"
    ALLOWED_TOOLS = (
        "Edit,Write,Read,Glob,Grep," "Bash(git *),Bash(uv run *),Bash(cd *),Bash(ls *),Bash(npx *)"
    )

    def __init__(self) -> None:
        self.config = AgentConfig(
            command="claude",
            args=[
                "-p",
                "{prompt}",
                "--output-format",
                "json",
                "--allowedTools",
                self.ALLOWED_TOOLS,
            ],
            output_format=OutputFormat.JSON,
            json_result_key="result",
            json_cost_key="cost_usd",
            model_flag="--model",
        )


class CodexAdapter(AgentAdapter):
    """Adapter for the Codex CLI (OpenAI).

    Uses ``codex exec`` for non-interactive execution with JSON output.
    Parses newline-delimited JSON events, extracting the last assistant message.
    """

    name = "codex"

    def __init__(self) -> None:
        self.config = AgentConfig(
            command="codex",
            args=["exec", "--full-auto", "--json", "{prompt}"],
            output_format=OutputFormat.JSON,
            json_result_key="result",
            json_cost_key="cost_usd",
            model_flag="--model",
        )

    def build_command(self, prompt: str, cwd: Path, model: str | None = None) -> list[str]:
        """Insert the model flag *before* the positional prompt arg.

        codex's prompt is positional, so a trailing flag would be swallowed
        into the prompt.
        """
        resolved = model or self.config.model
        args: list[str] = []
        for a in self.config.args:
            if a == "{prompt}":
                if resolved and self.config.model_flag:
                    args += [self.config.model_flag, resolved]
                args.append(prompt)
            else:
                args.append(a.replace("{prompt}", prompt))
        return [self.config.command, *args]

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


class CursorAdapter(AgentAdapter):
    """Adapter for the Cursor CLI (agent command).

    Uses ``agent -p`` for non-interactive (print) mode.
    See https://cursor.com/docs/cli/overview
    """

    name = "cursor"

    def __init__(self) -> None:
        self.config = AgentConfig(
            command="agent",
            args=["-p", "{prompt}", "--output-format", "text"],
            output_format=OutputFormat.TEXT,
            model_flag="--model",
        )


class CopilotAdapter(AgentAdapter):
    """Adapter for the GitHub Copilot CLI.

    Uses ``copilot -p`` for programmatic execution with JSONL output.
    Parses newline-delimited JSON events, extracting the last assistant message.
    See https://docs.github.com/en/copilot/reference/copilot-cli-reference
    """

    name = "copilot"

    def __init__(self) -> None:
        self.config = AgentConfig(
            command="copilot",
            args=[
                "-p",
                "{prompt}",
                "--output-format",
                "json",
                "--no-ask-user",
            ],
            output_format=OutputFormat.JSON,
            json_result_key="result",
            json_cost_key="cost_usd",
            model_flag="--model",
        )

    def parse_output(self, raw: str) -> tuple[str, dict]:
        # copilot -p --output-format json outputs JSONL (one JSON object per line)
        lines = raw.strip().split("\n")
        last_message = ""
        for line in reversed(lines):
            try:
                data = json.loads(line)
                # Try common message shapes
                if data.get("type") == "message" and data.get("role") == "assistant":
                    last_message = data.get("content", "")
                    break
                if "result" in data:
                    last_message = data["result"]
                    break
                if "message" in data:
                    last_message = data["message"]
                    break
            except (json.JSONDecodeError, TypeError):
                continue
        return (last_message or raw.strip(), {})


class AntigravityAdapter(AgentAdapter):
    """Adapter for the Google Antigravity CLI (agy)."""

    name = "antigravity"

    def __init__(self) -> None:
        self.config = AgentConfig(
            command="agy",
            args=["-p", "{prompt}", "--dangerously-skip-permissions"],
            output_format=OutputFormat.TEXT,
            inject_env=True,
            model_flag="--model",
        )


class GenericAdapter(AgentAdapter):
    """Fallback adapter for unknown agent commands."""

    def __init__(self, agent_cmd: str) -> None:
        self.name = agent_cmd
        self.config = AgentConfig(command=agent_cmd, args=["{prompt}"])


BUILTIN_ADAPTERS: dict[str, type[AgentAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "antigravity": AntigravityAdapter,
    "cursor": CursorAdapter,
    "copilot": CopilotAdapter,
}


def default_agents_config() -> dict[str, dict[str, Any]]:
    """Generate the default agents.yaml config from all built-in adapters."""
    return {name: cls().to_config() for name, cls in BUILTIN_ADAPTERS.items()}


def _load_yaml_config(config_path: Path) -> dict[str, Any]:
    """Load agents config from a YAML file."""
    try:
        import yaml
    except ImportError:
        raise ImportError(
            "PyYAML is required for custom agent configs. Install it with: pip install pyyaml"
        )
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def _coerce_config_paths(arg: Path | list[Path] | None) -> list[Path] | None:
    """Accept legacy single-path arg or new list-of-paths."""
    if arg is None:
        return None
    if isinstance(arg, Path):
        return [arg]
    return list(arg)


def get_adapter(
    agent_cmd: str,
    config_paths: Path | list[Path] | None = None,
) -> AgentAdapter:
    """Return the adapter for agent_cmd.

    Resolution order for the agent definition:
    1. Iterate config_paths (highest precedence first). For each path that
       exists and parses, check if it defines agent_cmd. Return that
       ConfigAdapter if so.
    2. Built-in adapters (claude, codex, antigravity, cursor, copilot).
    3. GenericAdapter fallback.

    Per-agent precedence: the highest-precedence file containing agent_cmd wins
    entirely (no inter-file merging of a single agent's fields).

    Accepts a single ``Path`` for backward compatibility; coerced to a one-item
    list internally.
    """
    paths = _coerce_config_paths(config_paths)
    if paths:
        for config_path in paths:
            if not config_path.exists():
                continue
            config = _load_yaml_config(config_path)
            agents = config.get("agents", {})
            if agent_cmd in agents:
                return AgentAdapter.from_config(agent_cmd, agents[agent_cmd])

    if agent_cmd in BUILTIN_ADAPTERS:
        return BUILTIN_ADAPTERS[agent_cmd]()
    return GenericAdapter(agent_cmd)
