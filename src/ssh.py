"""AsyncSSH client for remote power state commands (suspend, shutdown)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import asyncssh

from .config import SSHConfig

logger = logging.getLogger(__name__)


class SSHExecutionResult:
    def __init__(self, success: bool, output: str = "", error: str = ""):
        self.success = success
        self.output = output
        self.error = error

    def __repr__(self) -> str:
        return f"<SSHExecutionResult success={self.success} error={self.error!r}>"


class AutoAddHostKeyClient(asyncssh.SSHClient):
    """SSH client callback handler to automatically verify and add host keys."""

    def __init__(self, known_hosts_path: Path):
        self.known_hosts_path = known_hosts_path

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        """Validate an unknown host key or auto-add it if host is unseen."""
        if not self.known_hosts_path.exists():
            try:
                self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
                self.known_hosts_path.touch(exist_ok=True)
            except OSError as err:
                logger.error("Failed to create known_hosts file at %s: %s", self.known_hosts_path, err)
                return False

        # Check if this host already had an entry in known_hosts
        kh = asyncssh.read_known_hosts(str(self.known_hosts_path))
        port_num = port if port != 22 else None
        trusted_keys, _, _, _, _, _, _ = kh.match(host, addr, port_num)

        # If keys were already recorded for this host/addr/port, but this key wasn't matched,
        # this indicates a host key change / potential MITM attack!
        if trusted_keys:
            logger.error(
                "Host key verification failed for %s (%s:%d): host key does not match known_hosts entry! Potential MITM attack.",
                host,
                addr,
                port,
            )
            return False

        # Host is unknown: auto-add the new host key
        try:
            pubkey_str = key.export_public_key().decode("ascii").strip()
            # Standard OpenSSH known_hosts format
            # Non-default ports require brackets: [host]:port
            host_spec = f"[{addr}]:{port}" if port != 22 else addr
            if host != addr:
                host_spec = f"{host},{host_spec}" if port == 22 else f"[{host}]:{port},{host_spec}"

            line = f"{host_spec} {pubkey_str}\n"
            with open(self.known_hosts_path, "a", encoding="utf-8") as f:
                f.write(line)

            logger.info(
                "Automatically added new SSH host key for %s (%s:%d) to %s",
                host,
                addr,
                port,
                self.known_hosts_path,
            )
            return True
        except Exception as err:
            logger.error("Failed to append host key to %s: %s", self.known_hosts_path, err)
            return False


def resolve_known_hosts_path(ssh_config: SSHConfig) -> Path:
    """Determine known_hosts path and ensure the file exists."""
    if ssh_config.known_hosts_path:
        p = Path(ssh_config.known_hosts_path).expanduser()
    else:
        p = Path.home() / ".ssh" / "known_hosts"

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.touch(exist_ok=True)
    except OSError as err:
        logger.warning("Could not initialize known_hosts file at %s: %s", p, err)

    return p


async def execute_ssh_command(
    ip: str,
    ssh_config: SSHConfig,
    command: str,
    connect_timeout: float = 10.0,
    command_timeout: float = 15.0,
) -> SSHExecutionResult:
    """Connect to a remote host via SSH using a private key and execute a command."""
    key_path = Path(ssh_config.key_path)
    if not key_path.exists():
        msg = f"SSH private key file not found: {key_path}"
        logger.error(msg)
        return SSHExecutionResult(success=False, error=msg)

    known_hosts_file = resolve_known_hosts_path(ssh_config)
    known_hosts_arg = str(known_hosts_file) if known_hosts_file.exists() else None

    client_factory = None
    if ssh_config.auto_add_host_keys:
        client_factory = lambda: AutoAddHostKeyClient(known_hosts_file)

    logger.info("Executing remote command via SSH on %s:%d: %s", ip, ssh_config.port, command)

    try:
        async with asyncssh.connect(
            host=ip,
            port=ssh_config.port,
            username=ssh_config.user,
            client_keys=[str(key_path)],
            known_hosts=known_hosts_arg,
            client_factory=client_factory,
            login_timeout=connect_timeout,
        ) as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=command_timeout,
            )
            logger.info(
                "SSH command '%s' completed on %s with exit status %s",
                command,
                ip,
                result.exit_status,
            )
            return SSHExecutionResult(
                success=(result.exit_status == 0),
                output=result.stdout or "",
                error=result.stderr or "",
            )
    except asyncssh.HostKeyNotVerifiable as err:
        msg = f"Host key verification failed for {ip}:{ssh_config.port}: {err}"
        logger.error(msg)
        return SSHExecutionResult(success=False, error=msg)
    except (asyncssh.DisconnectError, ConnectionResetError, BrokenPipeError, EOFError) as err:
        # Commands like 'poweroff' or 'systemctl suspend' frequently terminate or drop the SSH connection
        # immediately as the system powers down or sleeps.
        logger.info(
            "SSH connection dropped during command execution on %s (expected during shutdown/sleep): %s",
            ip,
            err,
        )
        return SSHExecutionResult(
            success=True,
            output="Connection closed during power state change (expected behavior)",
        )
    except (asyncio.TimeoutError, TimeoutError):
        msg = f"SSH operation timed out on {ip}:{ssh_config.port}"
        logger.warning(msg)
        return SSHExecutionResult(success=False, error=msg)
    except asyncssh.Error as err:
        msg = f"AsyncSSH error connecting to {ip}: {err}"
        logger.error(msg)
        return SSHExecutionResult(success=False, error=msg)
    except Exception as err:
        msg = f"Unexpected error during SSH execution to {ip}: {err}"
        logger.error(msg)
        return SSHExecutionResult(success=False, error=msg)


async def send_sleep_command(ip: str, ssh_config: SSHConfig) -> SSHExecutionResult:
    """Send suspend/sleep command via SSH."""
    return await execute_ssh_command(ip, ssh_config, ssh_config.suspend_command)


async def send_shutdown_command(ip: str, ssh_config: SSHConfig) -> SSHExecutionResult:
    """Send shutdown command via SSH."""
    return await execute_ssh_command(ip, ssh_config, ssh_config.shutdown_command)

