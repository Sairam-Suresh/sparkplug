"""Tests for host state machine and runtime tracking."""

import time
import pytest

from src.checker import ProbeResult
from src.state import HostState, HostStateManager


def test_initial_state():
    mgr = HostStateManager(["media-server"])
    state = mgr.get("media-server")
    assert state is not None
    assert state.state == HostState.OFFLINE
    assert state.boot_timestamp is None
    assert state.sleep_timestamp is None


def test_wake_and_probe_transition():
    mgr = HostStateManager(["media-server"])

    # 1. Wake requested
    mgr.record_wake_requested("media-server")
    state = mgr.get("media-server")
    assert state.state == HostState.WAKING
    assert state.last_wake_time is not None

    # 2. Host begins replying to probe -> enters BOOTING
    mgr.record_probe("media-server", ProbeResult(online=True), grace_period_seconds=90)
    assert state.state == HostState.BOOTING
    assert state.boot_timestamp is not None
    assert state.is_in_boot_grace_period(90) is True

    # 3. Grace period expires -> enters ONLINE
    # Artificially shift boot timestamp back
    state.boot_timestamp = time.time() - 95
    assert state.is_in_boot_grace_period(90) is False
    mgr.record_probe("media-server", ProbeResult(online=True), grace_period_seconds=90)
    assert state.state == HostState.ONLINE


def test_sleep_and_wake_transition():
    mgr = HostStateManager(["media-server"])
    mgr.record_sleeping("media-server")
    state = mgr.get("media-server")
    assert state.state == HostState.SLEEPING
    assert state.sleep_timestamp is not None

    # Probe while sleeping is offline, should remain SLEEPING
    mgr.record_probe("media-server", ProbeResult(online=False), grace_period_seconds=90)
    assert state.state == HostState.SLEEPING

    # WOL sent, host responds -> enters BOOTING
    mgr.record_probe("media-server", ProbeResult(online=True), grace_period_seconds=90)
    assert state.state == HostState.BOOTING


def test_activity_reset():
    mgr = HostStateManager(["media-server"])
    state = mgr.get("media-server")
    state.last_activity_time = time.time() - 100
    assert state.seconds_since_last_activity() >= 100

    mgr.record_activity("media-server")
    assert state.seconds_since_last_activity() < 1.0


def test_shutdown_transition():
    mgr = HostStateManager(["media-server"])
    mgr.record_shutting_down("media-server")
    state = mgr.get("media-server")
    assert state.state == HostState.SHUTTING_DOWN

    # Host goes offline
    mgr.record_probe("media-server", ProbeResult(online=False), grace_period_seconds=90)
    assert state.state == HostState.OFFLINE

