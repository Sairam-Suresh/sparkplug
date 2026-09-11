"""Pytest fixtures and common configuration."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Generator

import pytest
import yaml

from src.config import AppConfig, HostConfig, SSHConfig, SparkPlugServerConfig


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def mock_ssh_key(temp_dir: Path) -> Path:
    key_file = temp_dir / "id_ed25519"
    key_file.write_text("dummy-private-key-data\n", encoding="utf-8")
    return key_file


@pytest.fixture
def sample_host_config(mock_ssh_key: Path) -> HostConfig:
    return HostConfig(
        id="media-server",
        name="Home Media Server",
        mac_address="AA:BB:CC:DD:EE:FF",
        ip_address="192.168.1.50",
        check_port=8096,
        broadcast_ip="192.168.1.255",
        sleep_timeout_minutes=15,
        idle_timeout_minutes=30,
        boot_grace_period_seconds=90,
        caddy_hosts=["media.home.lan", "jellyfin.local"],
        ssh=SSHConfig(
            user="sparkplug-agent",
            key_path=str(mock_ssh_key),
            port=22,
            suspend_command="sudo systemctl suspend",
            shutdown_command="sudo poweroff",
        ),
    )


@pytest.fixture
def sample_app_config(sample_host_config: HostConfig, temp_dir: Path) -> AppConfig:
    socket_path = str(temp_dir / "sparkplug.sock")
    return AppConfig(
        sparkplug=SparkPlugServerConfig(
            host="127.0.0.1",
            port=8080,
            log_level="DEBUG",
            socket_path=socket_path,
            monitor_interval_seconds=1,
        ),
        hosts=[sample_host_config],
    )


@pytest.fixture
def sample_yaml_file(sample_app_config: AppConfig, temp_dir: Path) -> Path:
    config_dict = sample_app_config.model_dump()
    yaml_path = temp_dir / "config.yaml"
    with yaml_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config_dict, f)
    return yaml_path

