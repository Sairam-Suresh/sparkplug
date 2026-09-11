"""Pure Python Wake-on-LAN (WOL) magic packet generation and transmission."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

logger = logging.getLogger(__name__)


def mac_to_bytes(mac_address: str) -> bytes:
    """Convert a MAC address string (e.g. 'AA:BB:CC:DD:EE:FF' or 'aa-bb-cc-dd-ee-ff') to 6 raw bytes."""
    cleaned = mac_address.replace(":", "").replace("-", "").replace(".", "").strip()
    if len(cleaned) != 12:
        raise ValueError(f"Invalid MAC address length for '{mac_address}'. Must have 12 hex digits.")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as err:
        raise ValueError(f"Invalid hex characters in MAC address '{mac_address}': {err}") from err


def build_magic_packet(mac_address: str) -> bytes:
    """Construct a 102-byte Wake-on-LAN magic packet.

    The packet consists of 6 bytes of 0xFF followed by 16 repetitions
    of the 6-byte MAC address.
    """
    mac_bytes = mac_to_bytes(mac_address)
    return (b"\xff" * 6) + (mac_bytes * 16)


def send_magic_packet(
    mac_address: str,
    broadcast_ip: str = "255.255.255.255",
    port: int = 9,
) -> None:
    """Send a Wake-on-LAN magic packet to the target MAC address via UDP broadcast synchronously."""
    packet = build_magic_packet(mac_address)
    logger.info("Broadcasting WOL magic packet for MAC %s to %s:%d", mac_address, broadcast_ip, port)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_ip, port))


async def async_send_magic_packet(
    mac_address: str,
    broadcast_ip: str = "255.255.255.255",
    port: int = 9,
) -> None:
    """Asynchronously broadcast a Wake-on-LAN magic packet in an executor thread."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_magic_packet, mac_address, broadcast_ip, port)

