# SparkPlug

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

**SparkPlug** is a lightweight Python 3 daemon and Docker service designed to work seamlessly with a **Caddy reverse proxy container**. It automatically manages bare-metal and virtual host power states:

1. **Wake-on-Demand:** Broadcasts Wake-on-LAN (WOL) magic packets when upstream traffic arrives for sleeping or powered-off hosts, serving an auto-refreshing loading screen until the host is online.
2. **Real-time Log & Activity Stream:** Connects to Caddy via a shared Unix domain socket to stream access logs in JSON, resetting idle timers on active traffic and instantly detecting 502/503 errors.
3. **Two-Stage Power Management:**
   * **Stage 1 (Sleep / Suspend):** After a configurable idle period (e.g. 15 minutes), issues an SSH suspend command (`sudo systemctl suspend`) so the machine can be woken up nearly instantaneously via WOL.
   * **Stage 2 (Deep Shutdown):** If the host remains idle past the full idle threshold (e.g. 30 minutes), issues a graceful poweroff (`sudo poweroff`).
4. **Shared Network Namespace:** Runs inside the same container network namespace as Caddy (`network_mode: "service:caddy"`), allowing direct localhost communication with zero network latency or bridge isolation issues.

---

## Architecture Overview

```
                        [ Incoming HTTP Traffic ]
                                   │
                                   ▼
                   ┌───────────────────────────────┐
                   │     Caddy Reverse Proxy       │
                   └───────┬───────────────┬───────┘
                           │               │
                 Upstream  │               │ Stream JSON Access Logs
                 Online    │               │ via Unix Domain Socket
                           ▼               ▼
                 ┌───────────────────┐ ┌───────────────────────────┐
                 │ Target Host / App │ │     SparkPlug Daemon      │
                 │  (192.168.1.50)   │ │  (service:caddy namespace)│
                 └───────────────────┘ └─────────────┬─────────────┘
                           ▲                         │
                           │   Wake-on-LAN (UDP:9)   │
                           ├─────────────────────────┤
                           │   SSH Suspend / Poweroff│
                           └─────────────────────────┘
```

When an upstream host is offline or asleep, Caddy intercepts connection failures (`handle_errors 502 503 504`) and rewrites the request to SparkPlug's `/wake/{host_id}?redirect={scheme}://{host}{uri}`. SparkPlug broadcasts a WOL packet and serves a responsive, self-refreshing loading screen that redirects the user as soon as the host passes health checks.

---

## Quick Start

### 1. Project Files

* [`examples/config.example.yaml`](file:///home/coder/sparkplug/examples/config.example.yaml): Host definitions, SSH credentials, and idle policies.
* [`examples/Caddyfile.example`](file:///home/coder/sparkplug/examples/Caddyfile.example): Caddy reverse proxy and log socket configuration.
* [`examples/docker-compose.yml`](file:///home/coder/sparkplug/examples/docker-compose.yml): Multi-container orchestration sharing network namespace.
* [`Dockerfile`](file:///home/coder/sparkplug/Dockerfile): Multi-stage minimal container build.

### 2. Configuration (`/etc/sparkplug/config.yaml`)

```yaml
sparkplug:
  host: "0.0.0.0"
  port: 8080
  log_level: "INFO"
  socket_path: "/var/run/caddy/sparkplug.sock"
  monitor_interval_seconds: 15

hosts:
  - id: "media-server"
    name: "Home Media Server"
    mac_address: "AA:BB:CC:DD:EE:FF"
    ip_address: "192.168.1.50"
    check_port: 8096
    broadcast_ip: "192.168.1.255"
    sleep_timeout_minutes: 15     # Puts host to sleep after 15m idle
    idle_timeout_minutes: 30      # Shuts down after 30m idle (set to null or 0 for sleep-only mode)
    boot_grace_period_seconds: 90 # Grace period after boot before idle timers run
    caddy_hosts:
      - "coder.service.internal"
      - "*.coder.service.internal"
      - "media.home.lan"
    # Requester filtering: specify who needs to make requests to keep the server up / reset idle timers
    keep_alive_users:
      - "alice"
      - "*@admin.internal"
    keep_alive_ips:
      - "192.168.1.0/24"
    ssh:
      user: "sparkplug-agent"
      key_path: "/etc/sparkplug/keys/id_ed25519"
      port: 22
      known_hosts_path: "/etc/sparkplug/known_hosts"
      auto_add_host_keys: true      # Automatically verifies & adds new host keys, rejects mismatches
      suspend_command: "sudo systemctl suspend"
      shutdown_command: "sudo poweroff"
```

### 3. Caddyfile Setup

```caddyfile
coder.service.internal, *.coder.service.internal, media.home.lan {
    # 1. Stream real-time access logs in JSON to SparkPlug's Unix domain socket
    log {
        output net unix//var/run/caddy/sparkplug.sock
        format json
    }

    # 2. Reverse proxy to target host
    reverse_proxy 192.168.1.50:8096 {
        transport http {
            dial_timeout 3s
        }
    }

    # 3. Intercept offline errors and redirect to SparkPlug
    handle_errors 502 503 504 {
        rewrite * /wake/media-server?redirect={scheme}://{host}{uri}
        reverse_proxy 127.0.0.1:8080
    }
}
```

### 4. Running with Docker Compose

```bash
docker compose up -d
```

Because `sparkplug` uses `network_mode: "service:caddy"`, both containers share the network interface and `localhost:8080`, and the shared volume at `/var/run/caddy` provides the Unix domain socket for real-time log ingestion.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/wake/{host_id}` | HTML auto-refreshing loading screen. Broadcasts WOL if offline/sleeping; redirects immediately if online. |
| `POST` | `/wake/{host_id}` | Programmatic wake trigger via REST API. |
| `GET` | `/status/{host_id}` | Current power state (`offline`, `waking`, `booting`, `online`, `sleeping`, `shutting_down`) and latency. |
| `POST` | `/activity/{host_id}` | Record incoming traffic activity to reset the host's idle countdown timer. |
| `POST` | `/sleep/{host_id}` | Manually trigger host sleep (`suspend_command` via SSH). |
| `POST` | `/shutdown/{host_id}` | Manually trigger host shutdown (`shutdown_command` via SSH). |
| `GET` | `/hosts` | List all configured hosts with current power and idle states. |
| `GET` | `/healthz` | Container healthcheck endpoint. |

---

## Testing & Quality Assurance

SparkPlug includes a comprehensive test suite using `pytest`, `pytest-asyncio`, and `pytest-mock`:

```bash
# Run test suite
uv run pytest -v
```

All network operations (WOL sockets, TCP probes, SSH sessions, Unix domain socket streams) are rigorously covered with unit and integration tests.

---

## Container Image & CI/CD

SparkPlug publishes multi-architecture container images (`linux/amd64`, `linux/arm64`) directly to the **GitHub Container Registry (GHCR)** via [`.github/workflows/publish.yml`](file:///.github/workflows/publish.yml):

```bash
# Pull the latest image
docker pull ghcr.io/<owner>/sparkplug:latest
```

The workflow automatically:
1. Runs the comprehensive `pytest` test suite.
2. Sets up QEMU and Docker Buildx for multi-architecture support (`amd64` and `arm64`).
3. Authenticates with `ghcr.io` using the built-in `${{ secrets.GITHUB_TOKEN }}`.
4. Generates SemVer and branch tags, publishing upon pushes to `main` and release tags (`v*.*.*`).


