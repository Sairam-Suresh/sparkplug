"""Tests for host reachability checking."""

import asyncio
import pytest
from pytest_mock import MockerFixture

from src.checker import check_host_reachability, check_tcp_port


@pytest.mark.asyncio
async def test_check_tcp_port_open(mocker: MockerFixture):
    mock_reader = mocker.MagicMock()
    mock_writer = mocker.MagicMock()
    mock_writer.wait_closed = mocker.AsyncMock()

    mocker.patch(
        "asyncio.open_connection",
        mocker.AsyncMock(return_value=(mock_reader, mock_writer)),
    )

    res = await check_tcp_port("192.168.1.50", 8096, timeout=1.0)
    assert res.online is True
    assert res.latency_ms >= 0
    assert res.error is None
    mock_writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_check_tcp_port_refused(mocker: MockerFixture):
    mocker.patch(
        "asyncio.open_connection",
        mocker.AsyncMock(side_effect=ConnectionRefusedError("Connection refused")),
    )

    res = await check_tcp_port("192.168.1.50", 8096, timeout=1.0)
    # ConnectionRefused means the host OS network stack is active and replying RST
    assert res.online is True
    assert "refused but host active" in res.error


@pytest.mark.asyncio
async def test_check_tcp_port_timeout(mocker: MockerFixture):
    mocker.patch(
        "asyncio.open_connection",
        mocker.AsyncMock(side_effect=asyncio.TimeoutError()),
    )

    res = await check_tcp_port("192.168.1.50", 8096, timeout=0.1)
    assert res.online is False
    assert "timed out" in res.error.lower()


@pytest.mark.asyncio
async def test_check_tcp_port_unreachable_network(mocker: MockerFixture):
    mocker.patch(
        "asyncio.open_connection",
        mocker.AsyncMock(side_effect=OSError(113, "No route to host")),
    )

    res = await check_host_reachability("192.168.1.50", 8096, timeout=1.0)
    assert res.online is False
    assert "No route to host" in res.error

