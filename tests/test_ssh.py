"""Tests for remote command execution via AsyncSSH."""

import asyncio
from pathlib import Path
import pytest
from pytest_mock import MockerFixture
import asyncssh

from src.config import SSHConfig
from src.ssh import execute_ssh_command, send_shutdown_command, send_sleep_command


@pytest.mark.asyncio
async def test_ssh_missing_key_file():
    ssh_cfg = SSHConfig(
        user="user",
        key_path="/non/existent/key",
    )
    res = await execute_ssh_command("192.168.1.50", ssh_cfg, "echo test")
    assert res.success is False
    assert "private key file not found" in res.error


@pytest.mark.asyncio
async def test_ssh_successful_command(mock_ssh_key: Path, mocker: MockerFixture):
    ssh_cfg = SSHConfig(
        user="sparkplug-agent",
        key_path=str(mock_ssh_key),
        port=22,
    )

    mock_result = mocker.MagicMock()
    mock_result.exit_status = 0
    mock_result.stdout = "poweroff scheduled\n"
    mock_result.stderr = ""

    mock_conn = mocker.MagicMock()
    mock_conn.run = mocker.AsyncMock(return_value=mock_result)

    mock_connect = mocker.AsyncMock()
    mock_connect.__aenter__.return_value = mock_conn
    mock_connect.__aexit__.return_value = None
    mocker.patch("asyncssh.connect", return_value=mock_connect)

    res = await send_shutdown_command("192.168.1.50", ssh_cfg)
    assert res.success is True
    assert "poweroff scheduled" in res.output


@pytest.mark.asyncio
async def test_ssh_connection_dropped_on_poweroff(mock_ssh_key: Path, mocker: MockerFixture):
    ssh_cfg = SSHConfig(
        user="sparkplug-agent",
        key_path=str(mock_ssh_key),
        port=22,
    )

    mock_conn = mocker.MagicMock()
    # Simulate connection dropping while poweroff shuts down network stack
    mock_conn.run = mocker.AsyncMock(side_effect=asyncssh.DisconnectError(1, "Connection reset"))

    mock_connect = mocker.AsyncMock()
    mock_connect.__aenter__.return_value = mock_conn
    mock_connect.__aexit__.return_value = None
    mocker.patch("asyncssh.connect", return_value=mock_connect)

    res = await send_shutdown_command("192.168.1.50", ssh_cfg)
    # Dropped connection during poweroff is treated as expected success
    assert res.success is True
    assert "Connection closed during power state change" in res.output


@pytest.mark.asyncio
async def test_ssh_timeout(mock_ssh_key: Path, mocker: MockerFixture):
    ssh_cfg = SSHConfig(
        user="sparkplug-agent",
        key_path=str(mock_ssh_key),
        port=22,
    )

    mocker.patch("asyncssh.connect", side_effect=asyncio.TimeoutError())

    res = await send_sleep_command("192.168.1.50", ssh_cfg)
    assert res.success is False
    assert "timed out" in res.error.lower()


class DummySSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False  # No authentication required for test server


@pytest.mark.asyncio
async def test_ssh_auto_add_host_key_lifecycle(temp_dir: Path):
    """Test the full lifecycle: auto-add key, verify subsequent, and reject on key change."""
    kh_file = temp_dir / "test_known_hosts"
    key_file = temp_dir / "valid_id_ed25519"
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_file.write_text(client_key.export_private_key().decode("ascii"), encoding="utf-8")

    server_key1 = asyncssh.generate_private_key("ssh-ed25519")
    server_key2 = asyncssh.generate_private_key("ssh-ed25519")

    # 1. Start server with server_key1
    server = await asyncssh.create_server(
        DummySSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[server_key1],
        process_factory=lambda process: process.exit(0),
    )
    port = server.sockets[0].getsockname()[1]

    ssh_cfg = SSHConfig(
        user="test-user",
        key_path=str(key_file),
        port=port,
        known_hosts_path=str(kh_file),
        auto_add_host_keys=True,
    )

    try:
        # First connection: host key should be automatically added
        res1 = await execute_ssh_command("127.0.0.1", ssh_cfg, "echo hello")
        assert res1.success is True
        assert kh_file.exists()
        kh_content = kh_file.read_text(encoding="utf-8")
        assert "127.0.0.1" in kh_content
        assert "ssh-ed25519" in kh_content

        # Second connection: host key is verified from known_hosts
        res2 = await execute_ssh_command("127.0.0.1", ssh_cfg, "echo hello again")
        assert res2.success is True

    finally:
        server.close()
        await server.wait_closed()

    # 2. Start server with server_key2 on the same port (simulate changed key / MITM)
    server2 = await asyncssh.create_server(
        DummySSHServer,
        "127.0.0.1",
        port,
        server_host_keys=[server_key2],
        process_factory=lambda process: process.exit(0),
    )
    try:
        # Connection should fail host key verification due to key mismatch
        res_mismatch = await execute_ssh_command("127.0.0.1", ssh_cfg, "echo attack")
        assert res_mismatch.success is False
        assert "Host key verification failed" in res_mismatch.error
    finally:
        server2.close()
        await server2.wait_closed()


@pytest.mark.asyncio
async def test_ssh_auto_add_disabled_rejects_unknown_host(temp_dir: Path):
    """When auto_add_host_keys is False, unknown hosts should be rejected."""
    kh_file = temp_dir / "empty_known_hosts"
    kh_file.touch()

    key_file = temp_dir / "valid_id_ed25519_2"
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_file.write_text(client_key.export_private_key().decode("ascii"), encoding="utf-8")

    server_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        DummySSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[server_key],
        process_factory=lambda process: process.exit(0),
    )
    port = server.sockets[0].getsockname()[1]

    ssh_cfg = SSHConfig(
        user="test-user",
        key_path=str(key_file),
        port=port,
        known_hosts_path=str(kh_file),
        auto_add_host_keys=False,
    )

    try:
        res = await execute_ssh_command("127.0.0.1", ssh_cfg, "echo test")
        assert res.success is False
        assert "Host key verification failed" in res.error
    finally:
        server.close()
        await server.wait_closed()


