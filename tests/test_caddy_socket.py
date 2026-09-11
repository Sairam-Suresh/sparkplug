"""Tests for Caddy Unix domain socket JSON log streaming."""

import asyncio
import json
from pathlib import Path
import pytest
from pytest_mock import MockerFixture

from src.caddy_socket import CaddySocketListener
from src.config import AppConfig
from src.state import HostState, HostStateManager


@pytest.mark.asyncio
async def test_caddy_socket_activity_tracking(sample_app_config: AppConfig):
    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    listener = CaddySocketListener(sample_app_config, state_mgr)

    await listener.start()
    try:
        # Age activity time
        state = state_mgr.get("media-server")
        state.last_activity_time = 0

        # Connect as Caddy client and send a JSON log line
        reader, writer = await asyncio.open_unix_connection(str(listener.socket_path))
        log_entry = {
            "level": "info",
            "ts": 1726068300.0,
            "logger": "http.log.access",
            "msg": "handled request",
            "request": {
                "host": "media.home.lan",
                "uri": "/stream/test",
                "method": "GET",
            },
            "status": 200,
            "upstream": "192.168.1.50:8096",
        }
        writer.write((json.dumps(log_entry) + "\n").encode("utf-8"))
        await writer.drain()

        # Wait briefly for server processing
        await asyncio.sleep(0.1)

        assert state.seconds_since_last_activity() < 5.0

        writer.close()
        await writer.wait_closed()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_caddy_socket_wake_trigger_on_502(sample_app_config: AppConfig, mocker: MockerFixture):
    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    wake_callback = mocker.AsyncMock()

    listener = CaddySocketListener(
        config=sample_app_config,
        state_manager=state_mgr,
        on_wake_trigger=wake_callback,
    )

    await listener.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(listener.socket_path))
        log_entry = {
            "level": "error",
            "request": {
                "host": "media.home.lan",
                "uri": "/",
            },
            "status": 502,
        }
        writer.write((json.dumps(log_entry) + "\n").encode("utf-8"))
        await writer.drain()

        await asyncio.sleep(0.1)

        wake_callback.assert_called_once()
        called_host = wake_callback.call_args[0][0]
        assert called_host.id == "media-server"

        # Host state should now be WAKING
        assert state_mgr.get("media-server").state == HostState.WAKING

        writer.close()
        await writer.wait_closed()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_caddy_socket_match_upstream_ip(sample_app_config: AppConfig):
    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    listener = CaddySocketListener(sample_app_config, state_mgr)

    await listener.start()
    try:
        state = state_mgr.get("media-server")
        state.last_activity_time = 0

        # Connect and send log matching upstream IP rather than Host header
        reader, writer = await asyncio.open_unix_connection(str(listener.socket_path))
        log_entry = {
            "status": 200,
            "upstream": "192.168.1.50:8096",
        }
        writer.write((json.dumps(log_entry) + "\n").encode("utf-8"))
        await writer.drain()

        await asyncio.sleep(0.1)
        assert state.seconds_since_last_activity() < 5.0

        writer.close()
        await writer.wait_closed()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_caddy_socket_malformed_lines(sample_app_config: AppConfig):
    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    listener = CaddySocketListener(sample_app_config, state_mgr)

    await listener.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(listener.socket_path))
        # Send garbage / malformed text
        writer.write(b"this is not json at all\n\n   \n")
        await writer.drain()
        await asyncio.sleep(0.1)

        writer.close()
        await writer.wait_closed()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_caddy_socket_wildcard_domain_match(sample_app_config: AppConfig):
    # Configure host with wildcard domain
    sample_app_config.hosts[0].caddy_hosts = ["*.coder.service.internal", "coder.service.internal"]
    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    listener = CaddySocketListener(sample_app_config, state_mgr)

    await listener.start()
    try:
        state = state_mgr.get("media-server")
        state.last_activity_time = 0

        # Connect and send a log for a wildcard subdomain
        reader, writer = await asyncio.open_unix_connection(str(listener.socket_path))
        log_entry = {
            "status": 200,
            "request": {
                "host": "workspace-abc.coder.service.internal",
                "uri": "/api/v1/status",
            },
        }
        writer.write((json.dumps(log_entry) + "\n").encode("utf-8"))
        await writer.drain()

        await asyncio.sleep(0.1)
        assert state.seconds_since_last_activity() < 5.0

        writer.close()
        await writer.wait_closed()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_caddy_socket_keep_alive_user_filter(sample_app_config: AppConfig):
    # Configure host with specific keep_alive_users
    sample_app_config.hosts[0].keep_alive_users = ["alice", "bob"]
    # Re-validate to populate keep_alive_filter
    sample_app_config.hosts[0].validate_timeouts_and_filters()

    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    listener = CaddySocketListener(sample_app_config, state_mgr)

    await listener.start()
    try:
        state = state_mgr.get("media-server")
        state.last_activity_time = 0

        reader, writer = await asyncio.open_unix_connection(str(listener.socket_path))

        # 1. Request from unauthorized user 'eve' -> should be ignored
        log_unauth = {
            "status": 200,
            "user_id": "eve",
            "request": {"host": "media.home.lan", "uri": "/"},
        }
        writer.write((json.dumps(log_unauth) + "\n").encode("utf-8"))
        await writer.drain()
        await asyncio.sleep(0.1)
        # Activity timer should NOT have reset
        assert state.last_activity_time == 0

        # 2. Request from authorized user 'alice' -> should reset timer
        log_auth = {
            "status": 200,
            "user_id": "alice",
            "request": {"host": "media.home.lan", "uri": "/"},
        }
        writer.write((json.dumps(log_auth) + "\n").encode("utf-8"))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert state.seconds_since_last_activity() < 5.0

        writer.close()
        await writer.wait_closed()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_caddy_socket_keep_alive_ip_filter(sample_app_config: AppConfig):
    sample_app_config.hosts[0].keep_alive_ips = ["192.168.1.0/24"]
    sample_app_config.hosts[0].validate_timeouts_and_filters()

    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    listener = CaddySocketListener(sample_app_config, state_mgr)

    await listener.start()
    try:
        state = state_mgr.get("media-server")
        state.last_activity_time = 0

        reader, writer = await asyncio.open_unix_connection(str(listener.socket_path))

        # 1. Request from outside subnet -> ignored
        log_outside = {
            "status": 200,
            "request": {"host": "media.home.lan", "client_ip": "10.0.0.99"},
        }
        writer.write((json.dumps(log_outside) + "\n").encode("utf-8"))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert state.last_activity_time == 0

        # 2. Request from inside subnet -> resets activity
        log_inside = {
            "status": 200,
            "request": {"host": "media.home.lan", "client_ip": "192.168.1.42"},
        }
        writer.write((json.dumps(log_inside) + "\n").encode("utf-8"))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert state.seconds_since_last_activity() < 5.0

        writer.close()
        await writer.wait_closed()
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_caddy_socket_keep_alive_wake_filter(sample_app_config: AppConfig, mocker: MockerFixture):
    sample_app_config.hosts[0].keep_alive_users = ["admin"]
    sample_app_config.hosts[0].validate_timeouts_and_filters()

    state_mgr = HostStateManager([h.id for h in sample_app_config.hosts])
    wake_callback = mocker.AsyncMock()

    listener = CaddySocketListener(
        config=sample_app_config,
        state_manager=state_mgr,
        on_wake_trigger=wake_callback,
    )

    await listener.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(listener.socket_path))

        # 1. 502 from unauthorized bot -> should not trigger wake
        log_bot = {
            "status": 502,
            "user_id": "random_bot",
            "request": {"host": "media.home.lan"},
        }
        writer.write((json.dumps(log_bot) + "\n").encode("utf-8"))
        await writer.drain()
        await asyncio.sleep(0.1)
        wake_callback.assert_not_called()
        assert state_mgr.get("media-server").state == HostState.OFFLINE

        # 2. 502 from authorized admin -> should trigger wake
        log_admin = {
            "status": 502,
            "user_id": "admin",
            "request": {"host": "media.home.lan"},
        }
        writer.write((json.dumps(log_admin) + "\n").encode("utf-8"))
        await writer.drain()
        await asyncio.sleep(0.1)
        wake_callback.assert_called_once()
        assert state_mgr.get("media-server").state == HostState.WAKING

        writer.close()
        await writer.wait_closed()
    finally:
        await listener.stop()



