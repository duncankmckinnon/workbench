"""Tests for agent platform adapters."""

import json
from pathlib import Path

import pytest

from workbench.adapters import (
    ALLOWED_TOOLS,
    AgentAdapter,
    ClaudeAdapter,
    CodexAdapter,
    ConfigAdapter,
    GenericAdapter,
    get_adapter,
)


class TestClaudeAdapter:
    def setup_method(self):
        self.adapter = ClaudeAdapter()

    def test_name(self):
        assert self.adapter.name == "claude"

    def test_build_command(self, tmp_path):
        cmd = self.adapter.build_command("do something", tmp_path)
        assert cmd == [
            "claude", "-p", "do something",
            "--output-format", "json",
            "--allowedTools", ALLOWED_TOOLS,
        ]

    def test_parse_output_valid_json(self):
        raw = json.dumps({"result": "done", "cost_usd": {"input": 0.01}})
        text, cost = self.adapter.parse_output(raw)
        assert text == "done"
        assert cost == {"input": 0.01}

    def test_parse_output_missing_keys(self):
        raw = json.dumps({"other": "data"})
        text, cost = self.adapter.parse_output(raw)
        assert text == raw  # falls back to raw when result key missing
        assert cost == {}

    def test_parse_output_invalid_json(self):
        raw = "not json at all"
        text, cost = self.adapter.parse_output(raw)
        assert text == raw
        assert cost == {}

    def test_is_agent_adapter(self):
        assert isinstance(self.adapter, AgentAdapter)


class TestCodexAdapter:
    def setup_method(self):
        self.adapter = CodexAdapter()

    def test_name(self):
        assert self.adapter.name == "codex"

    def test_build_command(self, tmp_path):
        cmd = self.adapter.build_command("fix bug", tmp_path)
        assert cmd == [
            "codex", "-q", "--full-auto",
            "--approval-mode", "full-auto",
            "fix bug",
        ]

    def test_parse_output_strips_whitespace(self):
        text, cost = self.adapter.parse_output("  hello world  \n")
        assert text == "hello world"
        assert cost == {}

    def test_is_agent_adapter(self):
        assert isinstance(self.adapter, AgentAdapter)


class TestGenericAdapter:
    def test_name_matches_cmd(self):
        adapter = GenericAdapter("my-tool")
        assert adapter.name == "my-tool"

    def test_build_command(self, tmp_path):
        adapter = GenericAdapter("my-tool")
        cmd = adapter.build_command("hello", tmp_path)
        assert cmd == ["my-tool", "hello"]

    def test_parse_output(self):
        adapter = GenericAdapter("my-tool")
        text, cost = adapter.parse_output("  output  \n")
        assert text == "output"
        assert cost == {}

    def test_is_agent_adapter(self):
        assert isinstance(GenericAdapter("x"), AgentAdapter)


class TestConfigAdapter:
    def test_build_command_substitutes_prompt(self, tmp_path):
        adapter = ConfigAdapter(
            name="custom",
            command="my-cli",
            args=["--headless", "{prompt}", "--verbose"],
        )
        cmd = adapter.build_command("do work", tmp_path)
        assert cmd == ["my-cli", "--headless", "do work", "--verbose"]

    def test_parse_output_text_format(self):
        adapter = ConfigAdapter(
            name="custom",
            command="my-cli",
            args=["{prompt}"],
            output_format="text",
        )
        text, cost = adapter.parse_output("  some output  \n")
        assert text == "some output"
        assert cost == {}

    def test_parse_output_json_format(self):
        adapter = ConfigAdapter(
            name="custom",
            command="my-cli",
            args=["{prompt}"],
            output_format="json",
            json_result_key="answer",
            json_cost_key="price",
        )
        raw = json.dumps({"answer": "42", "price": {"total": 0.05}})
        text, cost = adapter.parse_output(raw)
        assert text == "42"
        assert cost == {"total": 0.05}

    def test_parse_output_json_format_invalid(self):
        adapter = ConfigAdapter(
            name="custom",
            command="my-cli",
            args=["{prompt}"],
            output_format="json",
        )
        text, cost = adapter.parse_output("not json")
        assert text == "not json"
        assert cost == {}

    def test_parse_output_json_missing_keys(self):
        adapter = ConfigAdapter(
            name="custom",
            command="my-cli",
            args=["{prompt}"],
            output_format="json",
            json_result_key="answer",
            json_cost_key="price",
        )
        raw = json.dumps({"unrelated": "data"})
        text, cost = adapter.parse_output(raw)
        assert text == raw  # falls back to raw
        assert cost == {}

    def test_is_agent_adapter(self):
        adapter = ConfigAdapter(name="x", command="x", args=[])
        assert isinstance(adapter, AgentAdapter)


class TestGetAdapter:
    def test_returns_claude_adapter(self):
        adapter = get_adapter("claude")
        assert isinstance(adapter, ClaudeAdapter)

    def test_returns_codex_adapter(self):
        adapter = get_adapter("codex")
        assert isinstance(adapter, CodexAdapter)

    def test_returns_generic_for_unknown(self):
        adapter = get_adapter("some-random-tool")
        assert isinstance(adapter, GenericAdapter)
        assert adapter.name == "some-random-tool"

    def test_config_path_not_exists_falls_through(self, tmp_path):
        adapter = get_adapter("claude", config_path=tmp_path / "nonexistent.yaml")
        assert isinstance(adapter, ClaudeAdapter)

    def test_config_adapter_from_yaml(self, tmp_path):
        config_file = tmp_path / "agents.yaml"
        config_file.write_text(
            "agents:\n"
            "  my-agent:\n"
            "    command: my-agent-cli\n"
            "    args:\n"
            "      - '--headless'\n"
            "      - '{prompt}'\n"
            "    output_format: json\n"
            "    json_result_key: result\n"
            "    json_cost_key: cost_usd\n"
        )
        adapter = get_adapter("my-agent", config_path=config_file)
        assert isinstance(adapter, ConfigAdapter)
        assert adapter.name == "my-agent"
        assert adapter.command == "my-agent-cli"
        assert adapter.output_format == "json"

        cmd = adapter.build_command("test prompt", tmp_path)
        assert cmd == ["my-agent-cli", "--headless", "test prompt"]

    def test_config_agent_not_in_yaml_falls_through(self, tmp_path):
        config_file = tmp_path / "agents.yaml"
        config_file.write_text("agents:\n  other-agent:\n    command: other\n")
        adapter = get_adapter("claude", config_path=config_file)
        assert isinstance(adapter, ClaudeAdapter)

    def test_config_overrides_builtin(self, tmp_path):
        """Config entry for 'claude' should override the built-in ClaudeAdapter."""
        config_file = tmp_path / "agents.yaml"
        config_file.write_text(
            "agents:\n"
            "  claude:\n"
            "    command: custom-claude\n"
            "    args: ['{prompt}']\n"
        )
        adapter = get_adapter("claude", config_path=config_file)
        assert isinstance(adapter, ConfigAdapter)
        assert adapter.command == "custom-claude"

    def test_config_defaults(self, tmp_path):
        """Minimal config entry should get sensible defaults."""
        config_file = tmp_path / "agents.yaml"
        config_file.write_text("agents:\n  minimal:\n    command: min-cli\n")
        adapter = get_adapter("minimal", config_path=config_file)
        assert isinstance(adapter, ConfigAdapter)
        assert adapter.args == ["{prompt}"]
        assert adapter.output_format == "text"
        assert adapter.json_result_key == "result"
        assert adapter.json_cost_key == "cost_usd"
