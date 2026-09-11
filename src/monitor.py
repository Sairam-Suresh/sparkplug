"""Background idle monitor daemon evaluating power state transitions."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .checker import check_host_reachability
from .config import AppConfig, HostConfig
from .ssh import send_shutdown_command, send_sleep_command
from .state import HostState, HostStateManager
from .wol import async_send_magic_packet

logger = logging.getLogger(__name__)


class IdleMonitor:
    """Background monitor that periodically evaluates host reachability and idle power transitions."""

    def __init__(self, config: AppConfig, state_manager: HostStateManager):
        self.config = config
        self.state_manager = state_manager
        self.interval = config.sparkplug.monitor_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        """Start the background monitor task."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("SparkPlug IdleMonitor started with interval %ds", self.interval)

    async def stop(self) -> None:
        """Stop the background monitor task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("SparkPlug IdleMonitor stopped.")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.check_all_hosts()
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error("Error in IdleMonitor check loop: %s", err, exc_info=True)

            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    async def check_all_hosts(self) -> None:
        """Evaluate each configured host concurrently."""
        tasks = [self._check_host(host) for host in self.config.hosts]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_host(self, host: HostConfig) -> None:
        """Inspect and update single host state, issuing sleep or shutdown if idle."""
        probe = await check_host_reachability(host.ip_address, host.check_port)
        self.state_manager.record_probe(host.id, probe, host.boot_grace_period_seconds)

        state = self.state_manager.get(host.id)
        if not state:
            return

        # If host is in boot grace period, do not trigger any idle timeouts
        if state.is_in_boot_grace_period(host.boot_grace_period_seconds):
            logger.debug(
                "Host '%s' is in boot grace period (%d s remaining); skipping idle checks",
                host.id,
                int(host.boot_grace_period_seconds - (state.seconds_since_boot() or 0)),
            )
            return

        idle_seconds = state.seconds_since_last_activity()

        # Handle ONLINE state
        if state.state == HostState.ONLINE:
            # Check for Sleep/Suspend first (Stage 1)
            if host.is_sleep_enabled and idle_seconds >= (host.sleep_timeout_minutes * 60):
                logger.info(
                    "Host '%s' has been idle for %.1f minutes (sleep threshold: %d min). Triggering suspend.",
                    host.id,
                    idle_seconds / 60.0,
                    host.sleep_timeout_minutes,
                )
                self.state_manager.record_sleeping(host.id)
                res = await send_sleep_command(host.ip_address, host.ssh)
                if not res.success:
                    logger.warning("Suspend command returned error for '%s': %s", host.id, res.error)

            # Otherwise, check for full Shutdown (Stage 2 / single-stage) if shutdown is enabled
            elif host.is_shutdown_enabled and idle_seconds >= (host.idle_timeout_minutes * 60):
                logger.info(
                    "Host '%s' has been idle for %.1f minutes (shutdown threshold: %d min). Triggering poweroff.",
                    host.id,
                    idle_seconds / 60.0,
                    host.idle_timeout_minutes,
                )
                self.state_manager.record_shutting_down(host.id)
                res = await send_shutdown_command(host.ip_address, host.ssh)
                if not res.success:
                    logger.warning("Shutdown command returned error for '%s': %s", host.id, res.error)

        # Handle SLEEPING state transition to deep shutdown if idle continues (and shutdown is enabled)
        elif state.state == HostState.SLEEPING:
            if host.is_shutdown_enabled and idle_seconds >= (host.idle_timeout_minutes * 60):
                logger.info(
                    "Host '%s' in sleep mode has exceeded total idle threshold (%d min). Transitioning to full shutdown.",
                    host.id,
                    host.idle_timeout_minutes,
                )
                # To shut down a suspended host, send WOL packet to resume, then issue shutdown
                self.state_manager.record_shutting_down(host.id)
                try:
                    await async_send_magic_packet(host.mac_address, host.broadcast_ip)
                    # Brief pause for SSH to be reachable after wake
                    await asyncio.sleep(5)
                    await send_shutdown_command(host.ip_address, host.ssh)
                except Exception as err:
                    logger.error("Error shutting down sleeping host '%s': %s", host.id, err)

