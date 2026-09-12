"""FastAPI web service handling Caddy error hooks, WOL triggers, and status polling."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .caddy_socket import CaddySocketListener
from .checker import check_host_reachability
from .config import AppConfig, HostConfig
from .monitor import IdleMonitor
from .ssh import send_shutdown_command, send_sleep_command
from .state import HostState, HostStateManager
from .wol import async_send_magic_packet

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(config: AppConfig) -> FastAPI:
    """Application factory initializing state, background services, and API routes."""
    state_manager = HostStateManager([h.id for h in config.hosts])
    idle_monitor = IdleMonitor(config, state_manager)

    async def on_socket_wake(host: HostConfig) -> None:
        runtime = state_manager.get(host.id)
        last_ping = runtime.last_ping_summary() if runtime else "unknown"
        logger.info(
            "Triggering WOL for host '%s' (%s) from Caddy socket listener [last ping: %s]",
            host.id,
            host.mac_address,
            last_ping,
        )
        await async_send_magic_packet(host.mac_address, host.broadcast_ip)

    caddy_listener = CaddySocketListener(
        config=config,
        state_manager=state_manager,
        on_wake_trigger=on_socket_wake,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting SparkPlug daemon and background tasks...")
        idle_monitor.start()
        await caddy_listener.start()
        yield
        logger.info("Shutting down SparkPlug daemon...")
        await caddy_listener.stop()
        await idle_monitor.stop()

    app = FastAPI(
        title="SparkPlug",
        description="Wake-on-LAN and power management daemon for Caddy reverse proxy",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Attach shared instances to app.state
    app.state.config = config
    app.state.state_manager = state_manager
    app.state.idle_monitor = idle_monitor
    app.state.caddy_listener = caddy_listener

    @app.get("/healthz", tags=["System"])
    async def healthcheck():
        return {"status": "healthy", "service": "sparkplug"}

    @app.get("/hosts", tags=["Hosts"])
    async def list_hosts():
        results = []
        for host in config.hosts:
            runtime = state_manager.get(host.id)
            results.append(
                {
                    "id": host.id,
                    "name": host.name,
                    "ip_address": host.ip_address,
                    "check_port": host.check_port,
                    "mac_address": host.mac_address,
                    "state": runtime.state.value if runtime else "unknown",
                    "idle_seconds": (
                        runtime.seconds_since_last_activity() if runtime else None
                    ),
                    "sleep_timeout_minutes": host.sleep_timeout_minutes,
                    "idle_timeout_minutes": host.idle_timeout_minutes,
                }
            )
        return {"hosts": results}

    @app.get("/status/{host_id}", tags=["Hosts"])
    async def get_host_status(host_id: str, probe_now: bool = False):
        host = config.get_host(host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")

        runtime = state_manager.get(host_id)
        if probe_now:
            probe = await check_host_reachability(host.ip_address, host.check_port)
            state_manager.record_probe(host_id, probe, host.boot_grace_period_seconds)

        return {
            "id": host.id,
            "name": host.name,
            "ip_address": host.ip_address,
            "check_port": host.check_port,
            "state": runtime.state.value if runtime else "unknown",
            "idle_seconds": round(runtime.seconds_since_last_activity(), 1) if runtime else 0,
            "boot_seconds": (
                round(runtime.seconds_since_boot(), 1)
                if runtime and runtime.seconds_since_boot() is not None
                else None
            ),
            "last_probe": (
                {
                    "online": runtime.last_probe.online,
                    "latency_ms": runtime.last_probe.latency_ms,
                    "error": runtime.last_probe.error,
                }
                if runtime and runtime.last_probe
                else None
            ),
        }

    @app.post("/activity/{host_id}", tags=["Activity"])
    async def record_activity(
        host_id: str,
        request: Request,
        user: Optional[str] = Query(default=None, description="Requester username or user_id"),
    ):
        host = config.get_host(host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")

        # Determine requester user and IP
        effective_user = user
        if not effective_user:
            for h in ("remote-user", "x-forwarded-user", "x-user", "x-auth-request-user"):
                if h in request.headers:
                    effective_user = request.headers[h]
                    break

        client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if not client_ip:
            client_ip = request.headers.get("x-real-ip")
        if not client_ip and request.client:
            client_ip = request.client.host

        req_headers = dict(request.headers)

        if not host.matches_keep_alive(user=effective_user, client_ip=client_ip, headers=req_headers):
            logger.info(
                "Activity received for host '%s' from user=%r, ip=%r | Decision: ignored (does not match keep_alive criteria)",
                host_id,
                effective_user,
                client_ip,
            )
            return {
                "status": "ignored",
                "host_id": host_id,
                "message": "Activity ignored; requester does not match keep_alive criteria.",
            }

        state_manager.record_activity(host_id)
        runtime = state_manager.get(host_id)
        last_ping = runtime.last_ping_summary() if runtime else "unknown"
        logger.info(
            "Activity recorded for host '%s' [last ping: %s] from user=%r, ip=%r | Decision: reset idle timer",
            host_id,
            last_ping,
            effective_user,
            client_ip,
        )
        return {
            "status": "ok",
            "host_id": host_id,
            "message": "Activity recorded; idle timer reset.",
        }

    @app.post("/wake/{host_id}", tags=["Power"])
    async def trigger_wake(host_id: str):
        host = config.get_host(host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")

        runtime = state_manager.get(host_id)
        last_ping = runtime.last_ping_summary() if runtime else "unknown"
        logger.info(
            "Wake endpoint called for host '%s' [last ping: %s] | State: %s | Decision: wake system via WOL (%s)",
            host_id,
            last_ping,
            runtime.state.value if runtime else "unknown",
            host.mac_address,
        )
        state_manager.record_wake_requested(host_id)
        await async_send_magic_packet(host.mac_address, host.broadcast_ip)
        return {
            "status": "waking",
            "host_id": host_id,
            "message": f"Wake-on-LAN packet broadcasted to {host.mac_address}",
        }

    @app.get("/wake/{host_id}", response_class=HTMLResponse, tags=["Power"])
    async def wake_ui(
        request: Request,
        host_id: str,
        redirect: Optional[str] = Query(default=None, description="URL to redirect after host is online"),
    ):
        host = config.get_host(host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")

        # Fallback redirect target if not explicitly passed
        target_redirect = redirect or f"http://{host.ip_address}:{host.check_port}"

        # Probe current state immediately
        probe = await check_host_reachability(host.ip_address, host.check_port)
        state_manager.record_probe(host_id, probe, host.boot_grace_period_seconds)
        runtime = state_manager.get(host_id)

        # If already online, redirect immediately
        if runtime and runtime.state in (HostState.ONLINE, HostState.BOOTING):
            logger.info(
                "Wake UI visited for host '%s' [last ping: %s] | State: %s | Decision: host already active, redirecting to %s",
                host_id,
                probe.summary(),
                runtime.state.value,
                target_redirect,
            )
            return RedirectResponse(url=target_redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

        # Host is offline or sleeping: broadcast WOL
        logger.info(
            "Wake UI visited for host '%s' [last ping: %s] | State: %s | Decision: wake system via WOL (%s)",
            host_id,
            probe.summary(),
            runtime.state.value if runtime else "unknown",
            host.mac_address,
        )
        state_manager.record_wake_requested(host_id)
        await async_send_magic_packet(host.mac_address, host.broadcast_ip)

        return templates.TemplateResponse(
            request=request,
            name="waking.html",
            context={
                "host": host,
                "current_state": runtime.state.value if runtime else "waking",
                "redirect_url": target_redirect,
            },
        )

    @app.post("/sleep/{host_id}", tags=["Power"])
    async def trigger_sleep(host_id: str):
        host = config.get_host(host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")

        runtime = state_manager.get(host_id)
        last_ping = runtime.last_ping_summary() if runtime else "unknown"
        logger.info(
            "Sleep endpoint called for host '%s' [last ping: %s] | State: %s | Decision: put system to sleep (suspend via SSH)",
            host_id,
            last_ping,
            runtime.state.value if runtime else "unknown",
        )
        state_manager.record_sleeping(host_id)
        res = await send_sleep_command(host.ip_address, host.ssh)
        if not res.success:
            logger.warning("Suspend command returned error for '%s': %s", host.id, res.error)
        else:
            logger.info("Suspend command succeeded for '%s'", host.id)
        return {
            "status": "sleeping" if res.success else "error",
            "host_id": host_id,
            "output": res.output,
            "error": res.error,
        }

    @app.post("/shutdown/{host_id}", tags=["Power"])
    async def trigger_shutdown(host_id: str):
        host = config.get_host(host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host '{host_id}' not found")

        if not host.ssh.shutdown_command:
            raise HTTPException(
                status_code=400,
                detail=f"Shutdown is disabled or shutdown_command is not configured for host '{host_id}'",
            )

        runtime = state_manager.get(host_id)
        last_ping = runtime.last_ping_summary() if runtime else "unknown"
        logger.info(
            "Shutdown endpoint called for host '%s' [last ping: %s] | State: %s | Decision: shut system down (poweroff via SSH)",
            host_id,
            last_ping,
            runtime.state.value if runtime else "unknown",
        )
        state_manager.record_shutting_down(host_id)
        res = await send_shutdown_command(host.ip_address, host.ssh)
        if not res.success:
            logger.warning("Shutdown command returned error for '%s': %s", host.id, res.error)
        else:
            logger.info("Shutdown command succeeded for '%s'", host.id)
        return {
            "status": "shutting_down" if res.success else "error",
            "host_id": host_id,
            "output": res.output,
            "error": res.error,
        }

    return app
