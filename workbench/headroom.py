"""Headroom proxy configuration and subprocess environment wiring."""

from __future__ import annotations

import logging
import socket
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import yaml

logger = logging.getLogger(__name__)

HEADROOM_INSTALL_HINT = 'pipx install "headroom-ai[all]"'

HEADROOM_BASE_URL_ENV: dict[str, str] = {
    "claude": "ANTHROPIC_BASE_URL",
    "codex": "OPENAI_BASE_URL",  # TODO: verify against headroom's codex docs before relying on it
}

_WARNED_UNSUPPORTED_AGENTS: set[str] = set()


@dataclass
class HeadroomConfig:
    enabled: bool = False
    port: int = 8787
    autostart: bool = True
    command: str = "headroom"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def resolve_headroom_config(
    config_paths: list[Path], cli_override: bool | None = None
) -> HeadroomConfig:
    """Resolve the first top-level ``headroom:`` block from layered config files."""
    values: dict[str, object] = {}
    for config_path in config_paths:
        if not config_path.exists():
            continue
        data = yaml.safe_load(config_path.read_text()) or {}
        headroom = data.get("headroom")
        if isinstance(headroom, Mapping):
            values = dict(headroom)
            break

    config = HeadroomConfig(
        enabled=bool(values.get("enabled", HeadroomConfig.enabled)),
        port=int(values.get("port", HeadroomConfig.port)),
        autostart=bool(values.get("autostart", HeadroomConfig.autostart)),
        command=str(values.get("command", HeadroomConfig.command)),
    )
    if cli_override is not None:
        config.enabled = cli_override
    return config


def apply_headroom_env(
    base: Mapping[str, str], agent_cmd: str, config: HeadroomConfig
) -> dict[str, str]:
    """Overlay the provider CLI base URL env var for supported headroom agents."""
    env = dict(base)
    if not config.enabled:
        return env

    env_var = HEADROOM_BASE_URL_ENV.get(agent_cmd)
    if env_var is not None:
        env[env_var] = config.base_url
        return env

    if agent_cmd not in _WARNED_UNSUPPORTED_AGENTS:
        _WARNED_UNSUPPORTED_AGENTS.add(agent_cmd)
        logger.warning(
            "Headroom is enabled, but agent %r has no verified base-URL env var; "
            "running without headroom for this agent.",
            agent_cmd,
        )
    return env


class HeadroomProxy:
    """Manage one shared headroom proxy for a workbench run."""

    def __init__(self, config: HeadroomConfig) -> None:
        self.config = config
        self.active = False
        self.started_by_us = False
        self._process: subprocess.Popen[object] | None = None
        self._closed = False

    def __enter__(self) -> HeadroomProxy:
        if not self.config.enabled:
            return self

        if self._is_ready():
            self.active = True
            self.started_by_us = False
            return self

        if not self.config.autostart:
            logger.warning(
                "Headroom is enabled, but no proxy is reachable on 127.0.0.1:%s "
                "and autostart is off; continuing without headroom.",
                self.config.port,
            )
            return self

        try:
            self._process = subprocess.Popen(
                [self.config.command, "proxy", "--port", str(self.config.port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning(
                "Headroom command %r was not found; continuing without headroom. "
                "Install with: %s",
                self.config.command,
                HEADROOM_INSTALL_HINT,
            )
            return self
        except OSError as exc:
            logger.warning(
                "Could not start headroom proxy with command %r: %s; continuing without headroom.",
                self.config.command,
                exc,
            )
            return self

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._is_ready():
                self.active = True
                self.started_by_us = True
                return self
            if self._process.poll() is not None:
                logger.warning(
                    "Headroom proxy exited before becoming ready; continuing without headroom."
                )
                self._stop_process()
                return self
            time.sleep(0.1)

        logger.warning(
            "Headroom proxy did not become ready on 127.0.0.1:%s; continuing without headroom.",
            self.config.port,
        )
        self._stop_process()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if self.started_by_us:
            self._stop_process()
        self.active = False
        self.started_by_us = False

    def _is_ready(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.config.port), timeout=0.25):
                return True
        except OSError:
            return False

    def _stop_process(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2.0)
        self._process = None
