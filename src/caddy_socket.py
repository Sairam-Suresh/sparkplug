"""Caddy Unix domain socket listener for real-time JSON log streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config import AppConfig, HostConfig
from .state import HostState, HostStateManager

logger = logging.getLogger(__name__)


def extract_requester_info(data: dict) -> tuple[Optional[str], Optional[str], dict]:
    """Extract user, client IP, and headers from Caddy JSON access log entry."""
    request_info = data.get("request", {})
    headers = request_info.get("headers", {})

    # 1. Client IP extraction
    client_ip = request_info.get("client_ip") or request_info.get("remote_ip")
    if not client_ip:
        for hname, hval in headers.items():
            hlower = hname.lower()
            if hlower == "x-forwarded-for":
                raw = hval[0] if isinstance(hval, list) and hval else str(hval)
                client_ip = raw.split(",")[0].strip()
                break
            elif hlower == "x-real-ip":
                client_ip = hval[0] if isinstance(hval, list) and hval else str(hval)
                break

    if client_ip and ":" in client_ip and not client_ip.startswith("["):
        if client_ip.count(":") == 1:
            client_ip = client_ip.split(":")[0]

    # 2. User extraction
    user = data.get("user_id") or request_info.get("user_id") or request_info.get("client_id")
    if not user:
        user_header_names = {
            "remote-user",
            "x-forwarded-user",
            "x-user",
            "x-auth-request-user",
            "x-webauth-user",
        }
        for hname, hval in headers.items():
            hlower = hname.lower()
            if hlower in user_header_names:
                user = hval[0] if isinstance(hval, list) and hval else str(hval)
                break
            elif hlower == "authorization":
                raw_auth = hval[0] if isinstance(hval, list) and hval else str(hval)
                if raw_auth.strip().lower().startswith("basic "):
                    try:
                        import base64
                        decoded = base64.b64decode(raw_auth.strip()[6:]).decode("utf-8", errors="ignore")
                        if ":" in decoded:
                            user = decoded.split(":", 1)[0]
                    except Exception:
                        pass

    return user, client_ip, headers


class CaddySocketListener:
    """Listens on a Unix domain socket for real-time JSON access logs streamed from Caddy."""

    def __init__(
        self,
        config: AppConfig,
        state_manager: HostStateManager,
        on_wake_trigger: Optional[Callable[[HostConfig], Awaitable[None]]] = None,
    ):
        self.config = config
        self.state_manager = state_manager
        self.on_wake_trigger = on_wake_trigger
        self.socket_path = (
            Path(config.sparkplug.socket_path) if config.sparkplug.socket_path else None
        )
        self.server: Optional[asyncio.AbstractServer] = None
        self._running = False

    async def start(self) -> None:
        """Create the Unix socket server and begin accepting connections from Caddy."""
        if not self.socket_path:
            logger.info("No socket_path configured; Caddy socket listener disabled.")
            return

        # Ensure directory exists
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket file if it exists
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError as err:
                logger.warning("Failed to remove stale socket at %s: %s", self.socket_path, err)

        try:
            self.server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(self.socket_path),
            )
            # Ensure permissions allow Caddy to write to the socket
            try:
                os.chmod(str(self.socket_path), 0o666)
            except OSError:
                pass

            self._running = True
            logger.info("Caddy socket listener active on %s", self.socket_path)
        except Exception as err:
            logger.error("Failed to start Caddy socket listener on %s: %s", self.socket_path, err)

    async def stop(self) -> None:
        """Stop socket server and clean up socket file."""
        self._running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

        if self.socket_path and self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        logger.info("Caddy socket listener stopped.")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read newline-delimited JSON logs from connected Caddy instance."""
        logger.debug("New client connected to Caddy log socket")
        try:
            while self._running and not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                await self.process_log_line(line.decode("utf-8", errors="replace"))
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as err:
            logger.error("Error reading from Caddy socket: %s", err)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.debug("Caddy log socket client disconnected")

    async def process_log_line(self, line: str) -> None:
        """Parse a single JSON log line and update host activity or trigger wake."""
        line = line.strip()
        if not line:
            return

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Non-JSON log line received from Caddy: %s", line)
            return

        request_info = data.get("request", {})
        host_header = request_info.get("host", "")
        upstream = data.get("upstream", "")
        status = data.get("status", 200)

        # Match target host
        target_host: Optional[HostConfig] = None
        if host_header:
            target_host = self.config.find_host_by_caddy_domain(host_header)

        # Fallback match by upstream ip
        if not target_host and upstream:
            upstream_ip = upstream.split(":")[0]
            for host in self.config.hosts:
                if host.ip_address == upstream_ip:
                    target_host = host
                    break

        if not target_host:
            return

        # Check if requester satisfies keep_alive filter
        user, client_ip, headers = extract_requester_info(data)
        if not target_host.matches_keep_alive(user=user, client_ip=client_ip, headers=headers):
            logger.debug(
                "Ignoring request for host '%s': requester (user=%r, client_ip=%r) does not match keep_alive filter",
                target_host.id,
                user,
                client_ip,
            )
            return

        # Upstream returned 502/503/504, indicating host is offline or unreachable
        if status in (502, 503, 504):
            host_state = self.state_manager.get(target_host.id)
            if host_state and host_state.state in (HostState.OFFLINE, HostState.SLEEPING):
                logger.info(
                    "Detected %d error in Caddy log for host '%s'; triggering proactive wake",
                    status,
                    target_host.id,
                )
                self.state_manager.record_wake_requested(target_host.id)
                if self.on_wake_trigger:
                    try:
                        await self.on_wake_trigger(target_host)
                    except Exception as err:
                        logger.error("Error running wake trigger for %s: %s", target_host.id, err)
        else:
            # Active request served, reset idle timer
            self.state_manager.record_activity(target_host.id)
            logger.debug("Reset idle activity timer for host '%s' via Caddy log", target_host.id)

