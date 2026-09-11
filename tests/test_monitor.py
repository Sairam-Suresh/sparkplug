from pathlib import Path
import time
import pytest
from pytest_mock import MockerFixture

from src.checker import ProbeResult
from src.config import AppConfig, HostConfig, SSHConfig
from src.monitor import IdleMonitor
from src.ssh import SSHExecutionResult
from src.state import HostState, HostStateManager


@pytest.mark.asyncio
async def test_monitor_skips_during_grace_period(sample_app_config: AppConfig, mocker: MockerFixture):
    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    monitor = IdleMonitor(sample_app_config, state_mgr)

    # Host is online, but just booted (inside grace period)
    mocker.patch(
        "src.monitor.check_host_reachability",
        mocker.AsyncMock(return_value=ProbeResult(online=True)),
    )
    mock_sleep = mocker.patch("src.monitor.send_sleep_command", mocker.AsyncMock())
    mock_shutdown = mocker.patch("src.monitor.send_shutdown_command", mocker.AsyncMock())

    # Simulate host online
    state = state_mgr.get("media-server")
    state.state = HostState.BOOTING
    state.boot_timestamp = time.time()  # just booted
    state.last_activity_time = time.time() - 3600  # old activity

    await monitor.check_all_hosts()

    mock_sleep.assert_not_called()
    mock_shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_triggers_sleep_when_idle(sample_app_config: AppConfig, mocker: MockerFixture):
    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    monitor = IdleMonitor(sample_app_config, state_mgr)

    mocker.patch(
        "src.monitor.check_host_reachability",
        mocker.AsyncMock(return_value=ProbeResult(online=True)),
    )
    mock_sleep = mocker.patch(
        "src.monitor.send_sleep_command",
        mocker.AsyncMock(return_value=SSHExecutionResult(success=True)),
    )

    state = state_mgr.get("media-server")
    state.state = HostState.ONLINE
    state.boot_timestamp = time.time() - 1000  # well outside grace period
    # Idle for 16 minutes (sleep threshold is 15 minutes)
    state.last_activity_time = time.time() - (16 * 60)

    await monitor.check_all_hosts()

    mock_sleep.assert_called_once()
    assert state.state == HostState.SLEEPING


@pytest.mark.asyncio
async def test_monitor_triggers_shutdown_when_idle_without_sleep(mock_ssh_key, mocker: MockerFixture):
    # Host config without sleep mode
    host = HostConfig(
        id="compute-node",
        name="Compute Node",
        mac_address="11:22:33:44:55:66",
        ip_address="192.168.1.60",
        idle_timeout_minutes=20,
        sleep_timeout_minutes=None,
        boot_grace_period_seconds=60,
        ssh=SSHConfig(user="admin", key_path=str(mock_ssh_key)),
    )
    cfg = AppConfig(hosts=[host])
    state_mgr = HostStateManager([host.id])
    monitor = IdleMonitor(cfg, state_mgr)

    mocker.patch(
        "src.monitor.check_host_reachability",
        mocker.AsyncMock(return_value=ProbeResult(online=True)),
    )
    mock_shutdown = mocker.patch(
        "src.monitor.send_shutdown_command",
        mocker.AsyncMock(return_value=SSHExecutionResult(success=True)),
    )

    state = state_mgr.get("compute-node")
    state.state = HostState.ONLINE
    state.boot_timestamp = time.time() - 500
    state.last_activity_time = time.time() - (21 * 60)

    await monitor.check_all_hosts()

    mock_shutdown.assert_called_once()
    assert state.state == HostState.SHUTTING_DOWN


@pytest.mark.asyncio
async def test_monitor_sleep_only_never_shuts_down(mock_ssh_key: Path, mocker: MockerFixture):
    # Host with sleep enabled and shutdown disabled (idle_timeout_minutes=None)
    host = HostConfig(
        id="sleepy-server",
        name="Sleepy Server",
        mac_address="22:33:44:55:66:77",
        ip_address="192.168.1.70",
        sleep_timeout_minutes=10,
        idle_timeout_minutes=None,  # Shutdown disabled!
        boot_grace_period_seconds=30,
        ssh=SSHConfig(user="admin", key_path=str(mock_ssh_key)),
    )
    cfg = AppConfig(hosts=[host])
    state_mgr = HostStateManager([host.id])
    monitor = IdleMonitor(cfg, state_mgr)

    mocker.patch(
        "src.monitor.check_host_reachability",
        mocker.AsyncMock(return_value=ProbeResult(online=False)),
    )
    mock_shutdown = mocker.patch("src.monitor.send_shutdown_command", mocker.AsyncMock())
    mock_sleep = mocker.patch("src.monitor.send_sleep_command", mocker.AsyncMock())

    state = state_mgr.get("sleepy-server")
    state.state = HostState.SLEEPING
    state.sleep_timestamp = time.time() - 3600  # Asleep for 1 hour
    state.last_activity_time = time.time() - 3600

    await monitor.check_all_hosts()

    # Shutdown should NEVER be called when idle_timeout_minutes is disabled
    mock_shutdown.assert_not_called()
    assert state.state == HostState.SLEEPING


