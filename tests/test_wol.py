"""Tests for Wake-on-LAN magic packet generation and transmission."""

import socket
import pytest
from pytest_mock import MockerFixture

from src.wol import async_send_magic_packet, build_magic_packet, mac_to_bytes, send_magic_packet


def test_mac_to_bytes():
    mac = "AA:BB:CC:DD:EE:FF"
    raw = mac_to_bytes(mac)
    assert raw == b"\xaa\xbb\xcc\xdd\xee\xff"
    assert len(raw) == 6


def test_invalid_mac_to_bytes():
    with pytest.raises(ValueError):
        mac_to_bytes("AA:BB:CC")


def test_build_magic_packet():
    mac = "01:23:45:67:89:ab"
    packet = build_magic_packet(mac)
    assert len(packet) == 102
    # First 6 bytes must be 0xFF
    assert packet[:6] == b"\xff" * 6
    # Followed by 16 repetitions of 6-byte MAC
    expected_mac = b"\x01\x23\x45\x67\x89\xab"
    for i in range(16):
        start = 6 + i * 6
        assert packet[start : start + 6] == expected_mac


def test_send_magic_packet(mocker: MockerFixture):
    mock_socket_instance = mocker.MagicMock()
    mock_socket_class = mocker.patch("socket.socket", return_value=mock_socket_instance)
    mock_socket_instance.__enter__.return_value = mock_socket_instance

    send_magic_packet("AA:BB:CC:DD:EE:FF", broadcast_ip="192.168.1.255", port=9)

    mock_socket_class.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
    mock_socket_instance.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    mock_socket_instance.sendto.assert_called_once()
    args, _ = mock_socket_instance.sendto.call_args
    assert len(args[0]) == 102
    assert args[1] == ("192.168.1.255", 9)


@pytest.mark.asyncio
async def test_async_send_magic_packet(mocker: MockerFixture):
    mock_send = mocker.patch("src.wol.send_magic_packet")
    await async_send_magic_packet("AA:BB:CC:DD:EE:FF", "192.168.1.255", 9)
    mock_send.assert_called_once_with("AA:BB:CC:DD:EE:FF", "192.168.1.255", 9)

