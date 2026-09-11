"""Integration tests for FastAPI web routes and UI."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pytest_mock import MockerFixture

from src.checker import ProbeResult
from src.config import AppConfig
from src.ssh import SSHExecutionResult
from src.state import HostState
from src.web import create_app


@pytest_asyncio.fixture
async def client(sample_app_config: AppConfig, mocker: MockerFixture):
    # Mock WOL and SSH so web tests don't touch network or hardware
    mocker.patch("src.web.async_send_magic_packet", mocker.AsyncMock())
    mocker.patch("src.web.send_sleep_command", mocker.AsyncMock(return_value=SSHExecutionResult(success=True)))
    mocker.patch("src.web.send_shutdown_command", mocker.AsyncMock(return_value=SSHExecutionResult(success=True)))
    mocker.patch("src.web.check_host_reachability", mocker.AsyncMock(return_value=ProbeResult(online=False)))

    app = create_app(sample_app_config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app


@pytest.mark.asyncio
async def test_healthcheck(client):
    c, _ = client
    resp = await c.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy", "service": "sparkplug"}


@pytest.mark.asyncio
async def test_list_hosts(client):
    c, _ = client
    resp = await c.get("/hosts")
    assert resp.status_code == 200
    data = resp.json()
    assert "hosts" in data
    assert len(data["hosts"]) == 1
    assert data["hosts"][0]["id"] == "media-server"


@pytest.mark.asyncio
async def test_host_status(client):
    c, _ = client
    resp = await c.get("/status/media-server")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "media-server"
    assert data["state"] == "offline"


@pytest.mark.asyncio
async def test_host_status_not_found(client):
    c, _ = client
    resp = await c.get("/status/non-existent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_record_activity(client):
    c, app = client
    state_mgr = app.state.state_manager
    state = state_mgr.get("media-server")
    state.last_activity_time = 0

    resp = await c.post("/activity/media-server")
    assert resp.status_code == 200
    assert state.seconds_since_last_activity() < 5.0


@pytest.mark.asyncio
async def test_record_activity_filtered(sample_app_config: AppConfig, mocker: MockerFixture):
    sample_app_config.hosts[0].keep_alive_users = ["authorized-user"]
    sample_app_config.hosts[0].validate_timeouts_and_filters()

    mocker.patch("src.web.async_send_magic_packet", mocker.AsyncMock())
    mocker.patch("src.web.check_host_reachability", mocker.AsyncMock(return_value=ProbeResult(online=False)))

    app = create_app(sample_app_config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        state = app.state.state_manager.get("media-server")
        state.last_activity_time = 0

        # 1. Unauthorized request
        resp_unauth = await c.post("/activity/media-server?user=random")
        assert resp_unauth.status_code == 200
        assert resp_unauth.json()["status"] == "ignored"
        assert state.last_activity_time == 0

        # 2. Authorized request via query parameter
        resp_auth = await c.post("/activity/media-server?user=authorized-user")
        assert resp_auth.status_code == 200
        assert resp_auth.json()["status"] == "ok"
        assert state.seconds_since_last_activity() < 5.0

        # 3. Authorized request via X-User header
        state.last_activity_time = 0
        resp_header = await c.post("/activity/media-server", headers={"X-User": "authorized-user"})
        assert resp_header.status_code == 200
        assert resp_header.json()["status"] == "ok"
        assert state.seconds_since_last_activity() < 5.0



@pytest.mark.asyncio
async def test_post_wake(client):
    c, app = client
    resp = await c.post("/wake/media-server")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "waking"
    assert app.state.state_manager.get("media-server").state == HostState.WAKING


@pytest.mark.asyncio
async def test_get_wake_ui_offline(client):
    c, app = client
    # Host is offline: GET /wake should return HTML loading screen
    resp = await c.get("/wake/media-server?redirect=http://example.com/app")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Waking Home Media Server" in resp.text
    assert app.state.state_manager.get("media-server").state == HostState.WAKING


@pytest.mark.asyncio
async def test_get_wake_ui_already_online(client, mocker: MockerFixture):
    c, app = client
    # When host is online, GET /wake redirects immediately
    mocker.patch("src.web.check_host_reachability", mocker.AsyncMock(return_value=ProbeResult(online=True)))

    resp = await c.get("/wake/media-server?redirect=http://example.com/app", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "http://example.com/app"


@pytest.mark.asyncio
async def test_manual_sleep_and_shutdown(client):
    c, app = client
    resp_sleep = await c.post("/sleep/media-server")
    assert resp_sleep.status_code == 200
    assert resp_sleep.json()["status"] == "sleeping"
    assert app.state.state_manager.get("media-server").state == HostState.SLEEPING

    resp_shut = await c.post("/shutdown/media-server")
    assert resp_shut.status_code == 200
    assert resp_shut.json()["status"] == "shutting_down"
    assert app.state.state_manager.get("media-server").state == HostState.SHUTTING_DOWN


@pytest.mark.asyncio
async def test_manual_shutdown_disabled(sample_app_config: AppConfig, mocker: MockerFixture):
    # Disable shutdown_command for host
    sample_app_config.hosts[0].ssh.shutdown_command = None
    app = create_app(sample_app_config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/shutdown/media-server")
        assert resp.status_code == 400
        assert "Shutdown is disabled" in resp.json()["detail"]


