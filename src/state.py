"""Host power and activity state management."""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .checker import ProbeResult

logger = logging.getLogger(__name__)


class HostState(str, enum.Enum):
    OFFLINE = "offline"
    WAKING = "waking"
    BOOTING = "booting"
    ONLINE = "online"
    SLEEPING = "sleeping"
    SHUTTING_DOWN = "shutting_down"


@dataclass
class HostRuntimeState:
    host_id: str
    state: HostState = HostState.OFFLINE
    last_wake_time: Optional[float] = None
    boot_timestamp: Optional[float] = None
    last_activity_time: float = field(default_factory=time.time)
    sleep_timestamp: Optional[float] = None
    shutdown_timestamp: Optional[float] = None
    last_probe: Optional[ProbeResult] = None
    last_probe_time: Optional[float] = None

    def last_ping_summary(self) -> str:
        if self.last_probe is not None:
            return self.last_probe.summary()
        return "no ping yet"

    def seconds_since_last_activity(self) -> float:
        return max(0.0, time.time() - self.last_activity_time)

    def seconds_since_boot(self) -> Optional[float]:
        if self.boot_timestamp is None:
            return None
        return max(0.0, time.time() - self.boot_timestamp)

    def seconds_since_sleep(self) -> Optional[float]:
        if self.sleep_timestamp is None:
            return None
        return max(0.0, time.time() - self.sleep_timestamp)

    def is_in_boot_grace_period(self, grace_period_seconds: int) -> bool:
        if self.boot_timestamp is None:
            return False
        return (time.time() - self.boot_timestamp) < grace_period_seconds


class HostStateManager:
    """Thread-safe and async-safe state manager for configured hosts."""

    def __init__(self, host_ids: List[str]):
        self._states: Dict[str, HostRuntimeState] = {
            hid: HostRuntimeState(host_id=hid) for hid in host_ids
        }

    def get(self, host_id: str) -> Optional[HostRuntimeState]:
        return self._states.get(host_id)

    def get_all(self) -> Dict[str, HostRuntimeState]:
        return dict(self._states)

    def record_activity(self, host_id: str) -> None:
        state = self._states.get(host_id)
        if state:
            state.last_activity_time = time.time()

    def record_wake_requested(self, host_id: str) -> None:
        state = self._states.get(host_id)
        if state:
            old_state = state.state
            state.state = HostState.WAKING
            state.last_wake_time = time.time()
            state.last_activity_time = time.time()
            if old_state != HostState.WAKING:
                logger.info(
                    "Host '%s' state changed: %s -> waking (wake requested) [last ping: %s]",
                    host_id,
                    old_state.value,
                    state.last_ping_summary(),
                )

    def record_probe(self, host_id: str, probe: ProbeResult, grace_period_seconds: int) -> None:
        state = self._states.get(host_id)
        if not state:
            return

        old_state = state.state
        state.last_probe = probe
        now = time.time()
        state.last_probe_time = now

        if probe.online:
            # If transitioning from OFFLINE / WAKING / SLEEPING / SHUTTING_DOWN to online
            if state.state in (HostState.OFFLINE, HostState.WAKING, HostState.SLEEPING, HostState.SHUTTING_DOWN):
                state.state = HostState.BOOTING
                state.boot_timestamp = now
                state.sleep_timestamp = None
                state.shutdown_timestamp = None
                state.last_activity_time = now
            elif state.state == HostState.BOOTING:
                if not state.is_in_boot_grace_period(grace_period_seconds):
                    state.state = HostState.ONLINE
        else:
            # Probe says offline
            if state.state == HostState.WAKING:
                # Still waking up, stay in WAKING
                pass
            elif state.state == HostState.SHUTTING_DOWN:
                state.state = HostState.OFFLINE
                state.boot_timestamp = None
            elif state.state == HostState.SLEEPING:
                # Sleeping hosts are expected to be offline to network probes
                pass
            elif state.state in (HostState.ONLINE, HostState.BOOTING):
                state.state = HostState.OFFLINE
                state.boot_timestamp = None

        if old_state != state.state:
            logger.info(
                "Host '%s' state changed: %s -> %s [last ping: %s]",
                host_id,
                old_state.value,
                state.state.value,
                probe.summary(),
            )

    def record_sleeping(self, host_id: str) -> None:
        state = self._states.get(host_id)
        if state:
            old_state = state.state
            state.state = HostState.SLEEPING
            state.sleep_timestamp = time.time()
            state.boot_timestamp = None
            if old_state != HostState.SLEEPING:
                logger.info(
                    "Host '%s' state changed: %s -> sleeping [last ping: %s]",
                    host_id,
                    old_state.value,
                    state.last_ping_summary(),
                )

    def record_shutting_down(self, host_id: str) -> None:
        state = self._states.get(host_id)
        if state:
            old_state = state.state
            state.state = HostState.SHUTTING_DOWN
            state.shutdown_timestamp = time.time()
            state.boot_timestamp = None
            if old_state != HostState.SHUTTING_DOWN:
                logger.info(
                    "Host '%s' state changed: %s -> shutting_down [last ping: %s]",
                    host_id,
                    old_state.value,
                    state.last_ping_summary(),
                )

    def record_offline(self, host_id: str) -> None:
        state = self._states.get(host_id)
        if state:
            old_state = state.state
            state.state = HostState.OFFLINE
            state.boot_timestamp = None
            state.sleep_timestamp = None
            state.shutdown_timestamp = None
            if old_state != HostState.OFFLINE:
                logger.info(
                    "Host '%s' state changed: %s -> offline [last ping: %s]",
                    host_id,
                    old_state.value,
                    state.last_ping_summary(),
                )

