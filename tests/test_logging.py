"""Tests for verbose INFO logging including ping info, state transitions, and server decisions."""

import logging
import time
import pytest
from httpx import ASGITransport, AsyncClient
from pytest_mock import MockerFixture

from src.checker import ProbeResult
from src.config import AppConfig, HostConfig, SSHConfig
from src.monitor import IdleMonitor
from src.ssh import SSHExecutionResult
from src.state import HostState, HostStateManager
from src.web import create_app


def test_probe_result_summary():
    # Online with latency
    p1 = ProbeResult(online=True, latency_ms=1.456)
    assert p1.summary() == "1.5ms"

    # Online with port refused note
    p2 = ProbeResult(online=True, latency_ms=2.1, error="Port refused but host active")
    assert p2.summary() == "2.1ms (Port refused but host active)"

    # Offline with error
    p3 = ProbeResult(online=False, error="Connection timed out")
    assert p3.summary() == "unreachable (Connection timed out)"

    # Offline without error
    p4 = ProbeResult(online=False)
    assert p4.summary() == "unreachable (connection failed)"


def test_state_transition_logging(caplog):
    caplog.set_level(logging.INFO)
    mgr = HostStateManager(["media-server"])

    # Wake requested
    mgr.record_wake_requested("media-server")
    assert any(
        "Host 'media-server' state changed: offline -> waking (wake requested)" in record.message
        for record in caplog.records
    )

    # Host responds to probe -> booting
    caplog.clear()
    mgr.record_probe("media-server", ProbeResult(online=True, latency_ms=3.4), grace_period_seconds=90)
    assert any(
        "Host 'media-server' state changed: waking -> booting [last ping: 3.4ms]" in record.message
        for record in caplog.records
    )

    # Sleeping
    caplog.clear()
    mgr.record_sleeping("media-server")
    assert any(
        "Host 'media-server' state changed: booting -> sleeping" in record.message
        for record in caplog.records
    )

    # Shutting down
    caplog.clear()
    mgr.record_shutting_down("media-server")
    assert any(
        "Host 'media-server' state changed: sleeping -> shutting_down" in record.message
        for record in caplog.records
    )

    # Offline
    caplog.clear()
    mgr.record_offline("media-server")
    assert any(
        "Host 'media-server' state changed: shutting_down -> offline" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_monitor_logging_decisions(sample_app_config: AppConfig, mocker: MockerFixture, caplog):
    caplog.set_level(logging.INFO)
    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    monitor = IdleMonitor(sample_app_config, state_mgr)

    # 1. Host online within idle timeout -> Decision: keep online
    mocker.patch(
        "src.monitor.check_host_reachability",
        mocker.AsyncMock(return_value=ProbeResult(online=True, latency_ms=1.2)),
    )
    state = state_mgr.get("media-server")
    state.state = HostState.ONLINE
    state.boot_timestamp = time.time() - 500  # past grace period
    state.last_activity_time = time.time() - 60  # idle for 1 min (thresholds: 15m sleep, 30m shutdown)

    await monitor.check_all_hosts()
    assert any(
        "Host 'media-server' [last ping: 1.2ms] | State: online" in r.message
        and "Decision: keep online" in r.message
        for r in caplog.records
    )

    # 2. Host online exceeding sleep threshold -> Decision: put system to sleep (suspend)
    caplog.clear()
    mocker.patch(
        "src.monitor.send_sleep_command",
        mocker.AsyncMock(return_value=SSHExecutionResult(success=True)),
    )
    state.last_activity_time = time.time() - (16 * 60)
    await monitor.check_all_hosts()
    assert any(
        "Host 'media-server' [last ping: 1.2ms] | State: online" in r.message
        and "Decision: put system to sleep (suspend)" in r.message
        for r in caplog.records
    )

    # 3. Host sleeping exceeding shutdown threshold -> Decision: wake and shut system down
    caplog.clear()
    mocker.patch(
        "src.monitor.check_host_reachability",
        mocker.AsyncMock(return_value=ProbeResult(online=False, error="Connection refused")),
    )
    mocker.patch(
        "src.monitor.send_shutdown_command",
        mocker.AsyncMock(return_value=SSHExecutionResult(success=True)),
    )
    mocker.patch("src.monitor.async_send_magic_packet", mocker.AsyncMock())
    mocker.patch("asyncio.sleep", mocker.AsyncMock())

    state.state = HostState.SLEEPING
    state.last_activity_time = time.time() - (31 * 60)
    await monitor.check_all_hosts()
    assert any(
        "Host 'media-server' [last ping: unreachable (Connection refused)] | State: sleeping" in r.message
        and "Decision: wake and shut system down" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_web_routes_logging_decisions(sample_app_config: AppConfig, mocker: MockerFixture, caplog):
    caplog.set_level(logging.INFO)
    mocker.patch("src.web.async_send_magic_packet", mocker.AsyncMock())
    mocker.patch("src.web.send_sleep_command", mocker.AsyncMock(return_value=SSHExecutionResult(success=True)))
    mocker.patch("src.web.send_shutdown_command", mocker.AsyncMock(return_value=SSHExecutionResult(success=True)))
    mocker.patch("src.web.check_host_reachability", mocker.AsyncMock(return_value=ProbeResult(online=False, error="timed out")))

    app = create_app(sample_app_config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # POST /wake
        caplog.clear()
        await c.post("/wake/media-server")
        assert any(
            "Wake endpoint called for host 'media-server'" in r.message
            and "Decision: wake system via WOL" in r.message
            for r in caplog.records
        )

        # POST /sleep
        caplog.clear()
        await c.post("/sleep/media-server")
        assert any(
            "Sleep endpoint called for host 'media-server'" in r.message
            and "Decision: put system to sleep" in r.message
            for r in caplog.records
        )

        # POST /shutdown
        caplog.clear()
        await c.post("/shutdown/media-server")
        assert any(
            "Shutdown endpoint called for host 'media-server'" in r.message
            and "Decision: shut system down" in r.message
            for r in caplog.records
        )

