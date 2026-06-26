from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from workbench.headroom import (
    HEADROOM_INSTALL_HINT,
    HeadroomConfig,
    HeadroomProxy,
    apply_headroom_env,
    resolve_headroom_config,
)


def test_resolve_headroom_config_defaults_for_missing_files_and_keys(tmp_path):
    config_path = tmp_path / "agents.yaml"
    config_path.write_text("agents: {}\n")

    config = resolve_headroom_config([tmp_path / "missing.yaml", config_path])

    assert config == HeadroomConfig()


def test_resolve_headroom_config_reads_yaml_values(tmp_path):
    config_path = tmp_path / "agents.yaml"
    config_path.write_text(
        "headroom:\n"
        "  enabled: true\n"
        "  port: 9999\n"
        "  autostart: false\n"
        "  command: /opt/bin/headroom\n"
    )

    config = resolve_headroom_config([config_path])

    assert config.enabled is True
    assert config.port == 9999
    assert config.autostart is False
    assert config.command == "/opt/bin/headroom"
    assert config.base_url == "http://127.0.0.1:9999"


def test_resolve_headroom_config_first_file_wins(tmp_path):
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("headroom:\n  enabled: true\n  port: 1111\n")
    second.write_text("headroom:\n  enabled: false\n  port: 2222\n")

    config = resolve_headroom_config([first, second])

    assert config.enabled is True
    assert config.port == 1111


@pytest.mark.parametrize(
    ("override", "expected"),
    [(True, True), (False, False), (None, True)],
)
def test_resolve_headroom_config_cli_override_only_enabled(tmp_path, override, expected):
    config_path = tmp_path / "agents.yaml"
    config_path.write_text("headroom:\n  enabled: true\n  port: 9999\n")

    config = resolve_headroom_config([config_path], cli_override=override)

    assert config.enabled is expected
    assert config.port == 9999


def test_apply_headroom_env_disabled_returns_unchanged_copy():
    base = {"EXISTING": "1"}

    env = apply_headroom_env(base, "claude", HeadroomConfig(enabled=False, port=9999))

    assert env == base
    assert env is not base


def test_apply_headroom_env_sets_claude_base_url():
    env = apply_headroom_env({}, "claude", HeadroomConfig(enabled=True, port=9999))

    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"


def test_apply_headroom_env_sets_codex_base_url():
    env = apply_headroom_env({}, "codex", HeadroomConfig(enabled=True, port=9998))

    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:9998"


def test_apply_headroom_env_unsupported_warns_once(caplog):
    config = HeadroomConfig(enabled=True)
    with caplog.at_level("WARNING"):
        first = apply_headroom_env({"A": "b"}, "unsupported-agent", config)
        second = apply_headroom_env({"A": "b"}, "unsupported-agent", config)

    assert first == {"A": "b"}
    assert second == {"A": "b"}
    assert sum("unsupported-agent" in record.message for record in caplog.records) == 1


def test_headroom_proxy_disabled_no_spawn():
    with patch("workbench.headroom.subprocess.Popen") as mock_popen:
        with HeadroomProxy(HeadroomConfig(enabled=False)) as proxy:
            assert proxy.active is False
            assert proxy.started_by_us is False

    mock_popen.assert_not_called()


def test_headroom_proxy_reuses_existing_listener():
    with (
        patch.object(HeadroomProxy, "_is_ready", return_value=True),
        patch("workbench.headroom.subprocess.Popen") as mock_popen,
    ):
        with HeadroomProxy(HeadroomConfig(enabled=True)) as proxy:
            assert proxy.active is True
            assert proxy.started_by_us is False

    mock_popen.assert_not_called()


def test_headroom_proxy_autostarts_and_terminates():
    fake_process = MagicMock()
    fake_process.poll.return_value = None

    with (
        patch.object(HeadroomProxy, "_is_ready", side_effect=[False, True]),
        patch("workbench.headroom.subprocess.Popen", return_value=fake_process) as mock_popen,
    ):
        proxy = HeadroomProxy(HeadroomConfig(enabled=True, port=9999, command="hr"))
        with proxy:
            assert proxy.active is True
            assert proxy.started_by_us is True

    mock_popen.assert_called_once_with(
        ["hr", "proxy", "--port", "9999"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    fake_process.terminate.assert_called_once()
    fake_process.kill.assert_not_called()


def test_headroom_proxy_binary_missing_warns_with_install_hint(caplog):
    with (
        patch.object(HeadroomProxy, "_is_ready", return_value=False),
        patch("workbench.headroom.subprocess.Popen", side_effect=FileNotFoundError),
        caplog.at_level("WARNING"),
    ):
        with HeadroomProxy(HeadroomConfig(enabled=True, command="missing-headroom")) as proxy:
            assert proxy.active is False

    assert HEADROOM_INSTALL_HINT in caplog.text
