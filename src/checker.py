"""Network reachability checker for upstream target hosts."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    online: bool
    latency_ms: float = 0.0
    error: str | None = None


async def check_tcp_port(ip: str, port: int, timeout: float = 2.0) -> ProbeResult:
    """Check if a TCP port on a target host is reachable and accepting connections."""
    start_time = time.monotonic()
    try:
        conn = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        await writer.wait_closed()
        latency = (time.monotonic() - start_time) * 1000.0
        return ProbeResult(online=True, latency_ms=round(latency, 2))
    except (asyncio.TimeoutError, TimeoutError):
        return ProbeResult(online=False, error="Connection timed out")
    except ConnectionRefusedError:
        # Note: If connection was refused, the host is actually powered ON and network stack is replying with RST!
        # However, for an upstream service port (like 8096), the service might not be running yet.
        # But it does mean the host OS network stack is active.
        latency = (time.monotonic() - start_time) * 1000.0
        return ProbeResult(online=True, latency_ms=round(latency, 2), error="Port refused but host active")
    except OSError as err:
        return ProbeResult(online=False, error=str(err))
    except Exception as err:
        logger.debug("Unexpected error probing %s:%d: %s", ip, port, err)
        return ProbeResult(online=False, error=str(err))


async def check_host_reachability(ip: str, check_port: int, timeout: float = 2.0) -> ProbeResult:
    """Perform a health check on the target host."""
    return await check_tcp_port(ip, check_port, timeout=timeout)

