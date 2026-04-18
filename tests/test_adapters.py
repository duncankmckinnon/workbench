"""Tests for agent platform adapters."""

import json
from pathlib import Path

import pytest

from workbench.adapters import (
    AgentAdapter,
    AgentConfig,
    ClaudeAdapter,
    CodexAdapter,
    ConfigAdapter,
    CopilotAdapter,
    GeminiAdapter,
    GenericAdapter,
    OutputFormat,
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
            "claude",
            "-p",
            "do something",
            "--output-format",
            "json",
            "--allowedTools",
            ClaudeAdapter.ALLOWED_TOOLS,
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
            "codex",
            "exec",
            "--full-auto",
            "--json",
            "fix bug",
        ]

    def test_parse_output_ndjson_assistant_message(self):
        lines = [
            json.dumps({"type": "message", "role": "user", "content": "fix bug"}),
            json.dumps({"type": "message", "role": "assistant", "content": "done"}),
        ]
        raw = "\n".join(lines)
        text, cost = self.adapter.parse_output(raw)
        assert text == "done"
        assert cost == {}

    def test_parse_output_no_assistant_message_falls_back(self):
        raw = "plain text output"
        text, cost = self.adapter.parse_output(raw)
        assert text == "plain text output"
        assert cost == {}

    def test_parse_output_multiple_assistant_messages_takes_last(self):
        lines = [
            json.dumps({"type": "message", "role": "assistant", "content": "first"}),
            json.dumps({"type": "message", "role": "assistant", "content": "second"}),
        ]
        raw = "\n".join(lines)
        text, cost = self.adapter.parse_output(raw)
        assert text == "second"
        assert cost == {}

    def test_is_agent_adapter(self):
        assert isinstance(self.adapter, AgentAdapter)


class TestGeminiAdapter:
    def setup_method(self):
        self.adapter = GeminiAdapter()

    def test_name(self):
        assert self.adapter.name == "gemini"

    def test_build_command(self, tmp_path):
        cmd = self.adapter.build_command("refactor module", tmp_path)
        assert cmd == [
            "gemini",
            "-p",
            "refactor module",
            "--output-format",
            "json",
            "--approval-mode",
            "yolo",
        ]

    def test_build_command_prompt_with_special_chars(self, tmp_path):
        prompt = 'fix the "bug" in foo\'s module & run tests'
        cmd = self.adapter.build_command(prompt, tmp_path)
        assert cmd[0] == "gemini"
        assert cmd[1] == "-p"
        assert cmd[2] == prompt  # prompt passed as-is, shell escaping is caller's job

    def test_parse_output_valid_json(self):
        raw = json.dumps({"response": "all done", "stats": {"tokens": 150}})
        text, stats = self.adapter.parse_output(raw)
        assert text == "all done"
        assert stats == {"tokens": 150}

    def test_parse_output_json_missing_keys(self):
        raw = json.dumps({"other": "data"})
        text, stats = self.adapter.parse_output(raw)
        assert text == raw  # falls back to raw when response key missing
        assert stats == {}

    def test_parse_output_invalid_json(self):
        raw = "not json at all"
        text, stats = self.adapter.parse_output(raw)
        assert text == raw
        assert stats == {}

    def test_parse_output_empty_string(self):
        text, stats = self.adapter.parse_output("")
        assert text == ""
        assert stats == {}

    def test_parse_output_json_with_error(self):
        raw = json.dumps({"response": "", "stats": {}, "error": {"message": "rate limited"}})
        text, stats = self.adapter.parse_output(raw)
        assert text == ""
        assert stats == {}

    def test_is_agent_adapter(self):
        assert isinstance(self.adapter, AgentAdapter)


class TestCopilotAdapter:
    def setup_method(self):
        self.adapter = CopilotAdapter()

    def test_name(self):
        assert self.adapter.name == "copilot"

    def test_build_command(self, tmp_path):
        cmd = self.adapter.build_command("fix bug", tmp_path)
        assert cmd == [
            "copilot",
            "-p",
            "fix bug",
            "--output-format",
            "json",
            "--no-ask-user",
        ]

    def test_parse_output_jsonl_assistant_message(self):
        lines = [
            json.dumps({"type": "message", "role": "user", "content": "fix bug"}),
            json.dumps({"type": "message", "role": "assistant", "content": "done"}),
        ]
        raw = "\n".join(lines)
        text, cost = self.adapter.parse_output(raw)
        assert text == "done"
        assert cost == {}

    def test_parse_output_jsonl_result_key(self):
        raw = json.dumps({"result": "all fixed"})
        text, cost = self.adapter.parse_output(raw)
        assert text == "all fixed"
        assert cost == {}

    def test_parse_output_jsonl_message_key(self):
        raw = json.dumps({"message": "completed task"})
        text, cost = self.adapter.parse_output(raw)
        assert text == "completed task"
        assert cost == {}

    def test_parse_output_no_recognized_format_falls_back(self):
        raw = "plain text output"
        text, cost = self.adapter.parse_output(raw)
        assert text == "plain text output"
        assert cost == {}

    def test_parse_output_multiple_assistant_messages_takes_last(self):
        lines = [
            json.dumps({"type": "message", "role": "assistant", "content": "first"}),
            json.dumps({"type": "message", "role": "assistant", "content": "second"}),
        ]
        raw = "\n".join(lines)
        text, cost = self.adapter.parse_output(raw)
        assert text == "second"
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
        config = AgentConfig(command="my-cli", args=["--headless", "{prompt}", "--verbose"])
        adapter = ConfigAdapter(name="custom", config=config)
        cmd = adapter.build_command("do work", tmp_path)
        assert cmd == ["my-cli", "--headless", "do work", "--verbose"]

    def test_parse_output_text_format(self):
        config = AgentConfig(command="my-cli", args=["{prompt}"], output_format="text")
        adapter = ConfigAdapter(name="custom", config=config)
        text, cost = adapter.parse_output("  some output  \n")
        assert text == "some output"
        assert cost == {}

    def test_parse_output_json_format(self):
        config = AgentConfig(
            command="my-cli",
            args=["{prompt}"],
            output_format="json",
            json_result_key="answer",
            json_cost_key="price",
        )
        adapter = ConfigAdapter(name="custom", config=config)
        raw = json.dumps({"answer": "42", "price": {"total": 0.05}})
        text, cost = adapter.parse_output(raw)
        assert text == "42"
        assert cost == {"total": 0.05}

    def test_parse_output_json_format_invalid(self):
        config = AgentConfig(command="my-cli", args=["{prompt}"], output_format="json")
        adapter = ConfigAdapter(name="custom", config=config)
        text, cost = adapter.parse_output("not json")
        assert text == "not json"
        assert cost == {}

    def test_parse_output_json_missing_keys(self):
        config = AgentConfig(
            command="my-cli",
            args=["{prompt}"],
            output_format="json",
            json_result_key="answer",
            json_cost_key="price",
        )
        adapter = ConfigAdapter(name="custom", config=config)
        raw = json.dumps({"unrelated": "data"})
        text, cost = adapter.parse_output(raw)
        assert text == raw  # falls back to raw
        assert cost == {}

    def test_is_agent_adapter(self):
        config = AgentConfig(command="x", args=["{prompt}"])
        adapter = ConfigAdapter(name="x", config=config)
        assert isinstance(adapter, AgentAdapter)


class TestAgentConfig:
    def test_valid_text_config(self):
        config = AgentConfig(command="my-cli", args=["{prompt}"])
        assert config.output_format == OutputFormat.TEXT

    def test_valid_json_config(self):
        config = AgentConfig(command="my-cli", args=["{prompt}"], output_format="json")
        assert config.output_format == OutputFormat.JSON

    def test_invalid_output_format(self):
        with pytest.raises(ValueError, match="is not a valid OutputFormat"):
            AgentConfig(command="my-cli", args=["{prompt}"], output_format="xml")

    def test_empty_command(self):
        with pytest.raises(ValueError, match="command must not be empty"):
            AgentConfig(command="", args=["{prompt}"])

    def test_empty_args(self):
        with pytest.raises(ValueError, match="args must not be empty"):
            AgentConfig(command="my-cli", args=[])

    def test_missing_prompt_placeholder(self):
        with pytest.raises(ValueError, match="must contain"):
            AgentConfig(command="my-cli", args=["--flag"])

    def test_to_dict_text(self):
        config = AgentConfig(command="my-cli", args=["{prompt}"])
        d = config.to_dict()
        assert d == {"command": "my-cli", "args": ["{prompt}"], "output_format": "text"}
        assert "json_result_key" not in d

    def test_to_dict_json(self):
        config = AgentConfig(
            command="my-cli",
            args=["{prompt}"],
            output_format="json",
            json_result_key="answer",
            json_cost_key="price",
        )
        d = config.to_dict()
        assert d["json_result_key"] == "answer"
        assert d["json_cost_key"] == "price"

    def test_from_dict_minimal(self):
        config = AgentConfig.from_dict({"command": "my-cli"})
        assert config.command == "my-cli"
        assert config.args == ["{prompt}"]
        assert config.output_format == OutputFormat.TEXT

    def test_from_dict_full(self):
        config = AgentConfig.from_dict(
            {
                "command": "my-cli",
                "args": ["--flag", "{prompt}"],
                "output_format": "json",
                "json_result_key": "answer",
                "json_cost_key": "price",
            }
        )
        assert config.output_format == OutputFormat.JSON
        assert config.json_result_key == "answer"

    def test_round_trip(self):
        original = AgentConfig(
            command="my-cli",
            args=["--headless", "{prompt}"],
            output_format="json",
            json_result_key="out",
            json_cost_key="cost",
        )
        restored = AgentConfig.from_dict(original.to_dict())
        assert restored.command == original.command
        assert restored.args == original.args
        assert restored.output_format == original.output_format
        assert restored.json_result_key == original.json_result_key
        assert restored.json_cost_key == original.json_cost_key


class TestGetAdapter:
    def test_returns_claude_adapter(self):
        adapter = get_adapter("claude")
        assert isinstance(adapter, ClaudeAdapter)

    def test_returns_codex_adapter(self):
        adapter = get_adapter("codex")
        assert isinstance(adapter, CodexAdapter)

    def test_returns_gemini_adapter(self):
        adapter = get_adapter("gemini")
        assert isinstance(adapter, GeminiAdapter)

    def test_returns_copilot_adapter(self):
        adapter = get_adapter("copilot")
        assert isinstance(adapter, CopilotAdapter)

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
        assert adapter.config.command == "my-agent-cli"
        assert adapter.config.output_format == OutputFormat.JSON

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
            "agents:\n" "  claude:\n" "    command: custom-claude\n" "    args: ['{prompt}']\n"
        )
        adapter = get_adapter("claude", config_path=config_file)
        assert isinstance(adapter, ConfigAdapter)
        assert adapter.config.command == "custom-claude"

    def test_config_defaults(self, tmp_path):
        """Minimal config entry should get sensible defaults."""
        config_file = tmp_path / "agents.yaml"
        config_file.write_text("agents:\n  minimal:\n    command: min-cli\n")
        adapter = get_adapter("minimal", config_path=config_file)
        assert isinstance(adapter, ConfigAdapter)
        assert adapter.config.args == ["{prompt}"]
        assert adapter.config.output_format == OutputFormat.TEXT
        assert adapter.config.json_result_key == "result"
        assert adapter.config.json_cost_key == "cost_usd"
