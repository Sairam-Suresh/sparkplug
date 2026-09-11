"""Configuration models and loader for SparkPlug using Pydantic v2."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


MAC_REGEX = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")
ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")


class SSHConfig(BaseModel):
    """SSH credentials and commands for target host management."""

    user: str = Field(..., min_length=1, description="SSH username for remote access")
    key_path: str = Field(..., min_length=1, description="Path to SSH private key file")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port")
    known_hosts_path: Optional[str] = Field(
        default=None, description="Optional known_hosts file path (defaults to ~/.ssh/known_hosts)"
    )
    auto_add_host_keys: bool = Field(
        default=True,
        description="Automatically verify and add new host keys to known_hosts",
    )
    suspend_command: str = Field(
        default="sudo systemctl suspend",
        description="Command to put host into sleep/suspend state",
    )
    shutdown_command: Optional[str] = Field(
        default="sudo poweroff",
        description="Command to gracefully power off the host (optional if shutdown is disabled)",
    )


class KeepAliveFilter(BaseModel):
    """Filter specifying who needs to make requests to keep a host up / reset idle timers."""

    users: List[str] = Field(
        default_factory=list,
        description="Allowed usernames, user IDs, or wildcard patterns (e.g. 'alice', '*@admin.org')",
    )
    ips: List[str] = Field(
        default_factory=list,
        description="Allowed client IPv4/IPv6 addresses or CIDR ranges (e.g. '192.168.1.0/24', '10.0.0.5')",
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="Required HTTP request headers and match patterns (e.g. {'X-Authorized': 'true'})",
    )
    require_all: bool = Field(
        default=False,
        description="If True, all configured criteria (users, ips, headers) must match; if False (default), matching any configured criteria is sufficient",
    )

    @field_validator("ips")
    @classmethod
    def validate_ips(cls, v: List[str]) -> List[str]:
        validated = []
        for item in v:
            item_str = item.strip()
            try:
                ipaddress.ip_network(item_str, strict=False)
                validated.append(item_str)
            except ValueError as err:
                raise ValueError(f"Invalid IP address or CIDR network '{item}': {err}") from err
        return validated


class HostConfig(BaseModel):
    """Configuration for an individual upstream target host."""

    id: str = Field(..., description="Unique URL-friendly identifier for the host")
    name: str = Field(..., min_length=1, description="Human-readable host name")
    mac_address: str = Field(..., description="MAC address for Wake-on-LAN")
    ip_address: str = Field(..., description="IPv4 address of the target host")
    check_port: int = Field(default=80, ge=1, le=65535, description="Port to probe for online status")
    broadcast_ip: str = Field(
        default="255.255.255.255",
        description="Subnet broadcast IPv4 address for WOL magic packet",
    )
    sleep_timeout_minutes: Optional[int] = Field(
        default=None,
        ge=0,
        description="Minutes of idle traffic before putting host to sleep (0/None to disable)",
    )
    idle_timeout_minutes: Optional[int] = Field(
        default=30,
        ge=0,
        description="Minutes of idle traffic before shutting down the host (0 or None to disable shutdown)",
    )
    boot_grace_period_seconds: int = Field(
        default=90,
        ge=0,
        description="Grace period in seconds after boot before idle timer begins",
    )
    caddy_hosts: List[str] = Field(
        default_factory=list,
        description="Virtual host domains / SNIs routed to this host via Caddy",
    )
    ssh: SSHConfig = Field(..., description="SSH access details for power management")
    keep_alive_filter: Optional[KeepAliveFilter] = Field(
        default=None,
        description="Filter specifying who must make requests to reset idle timers and keep server up",
    )
    keep_alive_users: Optional[List[str]] = Field(
        default=None,
        description="Convenience list of usernames/patterns allowed to keep host alive",
    )
    keep_alive_ips: Optional[List[str]] = Field(
        default=None,
        description="Convenience list of client IPs/CIDRs allowed to keep host alive",
    )

    @property
    def is_shutdown_enabled(self) -> bool:
        """Check if automatic idle shutdown is enabled for this host."""
        return self.idle_timeout_minutes is not None and self.idle_timeout_minutes > 0

    @property
    def is_sleep_enabled(self) -> bool:
        """Check if automatic idle sleep/suspend is enabled for this host."""
        return self.sleep_timeout_minutes is not None and self.sleep_timeout_minutes > 0

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not ID_REGEX.match(v):
            raise ValueError(
                f"Host id '{v}' is invalid. Must contain only alphanumeric characters, underscores, or hyphens."
            )
        return v

    @field_validator("mac_address")
    @classmethod
    def validate_mac_address(cls, v: str) -> str:
        cleaned = v.strip()
        # Support formats: 00:11:22:33:44:55 or 00-11-22-33-44-55
        if not MAC_REGEX.match(cleaned):
            raise ValueError(
                f"Invalid MAC address format: '{v}'. Expected format like 'AA:BB:CC:DD:EE:FF' or 'aa-bb-cc-dd-ee-ff'."
            )
        # Normalize to standard uppercase colon-separated
        return cleaned.replace("-", ":").upper()

    @field_validator("ip_address", "broadcast_ip")
    @classmethod
    def validate_ipv4(cls, v: str) -> str:
        try:
            ipaddress.IPv4Address(v.strip())
        except ValueError as err:
            raise ValueError(f"Invalid IPv4 address '{v}': {err}") from err
        return v.strip()

    @model_validator(mode="after")
    def validate_timeouts_and_filters(self) -> HostConfig:
        if self.is_sleep_enabled and self.is_shutdown_enabled:
            if self.sleep_timeout_minutes >= self.idle_timeout_minutes:
                raise ValueError(
                    f"sleep_timeout_minutes ({self.sleep_timeout_minutes}) must be less than "
                    f"idle_timeout_minutes ({self.idle_timeout_minutes}) for host '{self.id}'."
                )

        # Merge shorthand keep_alive_users and keep_alive_ips into keep_alive_filter
        if self.keep_alive_users is not None or self.keep_alive_ips is not None:
            current_users = list(self.keep_alive_users or [])
            current_ips = list(self.keep_alive_ips or [])
            if self.keep_alive_filter is None:
                self.keep_alive_filter = KeepAliveFilter(users=current_users, ips=current_ips)
            else:
                combined_users = list(dict.fromkeys(self.keep_alive_filter.users + current_users))
                combined_ips = list(dict.fromkeys(self.keep_alive_filter.ips + current_ips))
                self.keep_alive_filter = self.keep_alive_filter.model_copy(
                    update={"users": combined_users, "ips": combined_ips}
                )

        return self

    def matches_keep_alive(
        self,
        *,
        user: Optional[str] = None,
        client_ip: Optional[str] = None,
        headers: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Evaluate whether an incoming request qualifies to keep this host alive."""
        import fnmatch

        if not self.keep_alive_filter:
            return True

        filter_cfg = self.keep_alive_filter
        has_user_check = bool(filter_cfg.users)
        has_ip_check = bool(filter_cfg.ips)
        has_header_check = bool(filter_cfg.headers)

        if not has_user_check and not has_ip_check and not has_header_check:
            return True

        results: List[bool] = []

        if has_user_check:
            matched_user = False
            if user:
                clean_user = user.strip().lower()
                for pattern in filter_cfg.users:
                    p = pattern.strip().lower()
                    if clean_user == p or fnmatch.fnmatchcase(clean_user, p):
                        matched_user = True
                        break
            results.append(matched_user)

        if has_ip_check:
            matched_ip = False
            if client_ip:
                try:
                    ip_obj = ipaddress.ip_address(client_ip.strip())
                    for cidr in filter_cfg.ips:
                        net = ipaddress.ip_network(cidr.strip(), strict=False)
                        if ip_obj in net:
                            matched_ip = True
                            break
                except ValueError:
                    matched_ip = False
            results.append(matched_ip)

        if has_header_check:
            matched_header = True
            norm_headers: Dict[str, List[str]] = {}
            if headers:
                for k, v in headers.items():
                    if isinstance(v, list):
                        norm_headers[k.lower()] = [str(x) for x in v]
                    else:
                        norm_headers[k.lower()] = [str(v)]

            for req_key, req_val in filter_cfg.headers.items():
                k_lower = req_key.strip().lower()
                val_pattern = req_val.strip().lower()
                present_vals = norm_headers.get(k_lower, [])
                if not any(
                    pv.strip().lower() == val_pattern
                    or fnmatch.fnmatchcase(pv.strip().lower(), val_pattern)
                    for pv in present_vals
                ):
                    matched_header = False
                    break
            results.append(matched_header)

        if filter_cfg.require_all:
            return all(results)
        return any(results)


class SparkPlugServerConfig(BaseModel):
    """Server and daemon runtime settings."""

    host: str = Field(default="0.0.0.0", description="Bind IP for HTTP API")
    port: int = Field(default=8080, ge=1, le=65535, description="Port for HTTP API")
    log_level: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")
    socket_path: Optional[str] = Field(
        default="/var/run/caddy/sparkplug.sock",
        description="Unix domain socket path for Caddy log streaming",
    )
    monitor_interval_seconds: int = Field(
        default=15,
        ge=1,
        description="Polling interval in seconds for the background state monitor",
    )


class AppConfig(BaseModel):
    """Root configuration for SparkPlug."""

    sparkplug: SparkPlugServerConfig = Field(default_factory=SparkPlugServerConfig)
    hosts: List[HostConfig] = Field(..., min_length=1, description="List of configured hosts")

    @model_validator(mode="after")
    def validate_unique_hosts(self) -> AppConfig:
        seen_ids = set()
        for host in self.hosts:
            if host.id in seen_ids:
                raise ValueError(f"Duplicate host id detected: '{host.id}'")
            seen_ids.add(host.id)
        return self

    def get_host(self, host_id: str) -> Optional[HostConfig]:
        """Find a host config by ID."""
        for host in self.hosts:
            if host.id == host_id:
                return host
        return None

    def find_host_by_caddy_domain(self, domain: str) -> Optional[HostConfig]:
        """Find a host config matching a Caddy Host header.

        Performs a two-pass lookup:
        1. First pass checks for exact domain matches.
        2. Second pass checks for wildcard matches (e.g. *.coder.service.internal).
        """
        clean_domain = domain.split(":")[0].strip().lower()

        # Pass 1: Exact matches
        for host in self.hosts:
            for d in host.caddy_hosts:
                if d.split(":")[0].strip().lower() == clean_domain:
                    return host

        # Pass 2: Wildcard / pattern matches
        for host in self.hosts:
            for d in host.caddy_hosts:
                if domain_matches(d, clean_domain):
                    return host

        return None


def domain_matches(pattern: str, domain: str) -> bool:
    """Check if a domain matches a configured pattern, supporting wildcards.

    Examples:
        - '*.coder.service.internal' matches 'app.coder.service.internal',
          'sub.app.coder.service.internal', and 'coder.service.internal'.
        - 'coder.service.internal' matches 'coder.service.internal'.
        - 'app-*.internal' matches 'app-1.internal'.
    """
    import fnmatch

    clean_pattern = pattern.split(":")[0].strip().lower()
    clean_domain = domain.split(":")[0].strip().lower()

    if clean_pattern == clean_domain:
        return True

    # Wildcard prefix matching like *.coder.service.internal
    if clean_pattern.startswith("*."):
        base_domain = clean_pattern[2:]
        # Match apex domain: coder.service.internal
        if clean_domain == base_domain:
            return True
        # Match subdomains: foo.coder.service.internal
        if clean_domain.endswith("." + base_domain):
            return True

    # General wildcard / glob pattern
    if "*" in clean_pattern or "?" in clean_pattern:
        if fnmatch.fnmatchcase(clean_domain, clean_pattern):
            return True

    return False


DEFAULT_CONFIG_PATH = "/etc/sparkplug/config.yaml"
CONFIG_ENV_VAR = "SPARKPLUG_CONFIG_PATH"


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load and validate SparkPlug configuration from YAML file."""
    if config_path is None:
        config_path = os.getenv(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)

    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path.resolve()}")

    try:
        with path.open("r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)
    except Exception as err:
        raise ValueError(f"Failed to parse YAML file at {path}: {err}") from err

    if not isinstance(raw_data, dict):
        raise ValueError(f"Configuration YAML at {path} must contain a top-level dictionary")

    return AppConfig.model_validate(raw_data)

