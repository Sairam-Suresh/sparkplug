"""Tests for configuration loading and validation."""

from pathlib import Path
import pytest
from pydantic import ValidationError

from src.config import AppConfig, HostConfig, SSHConfig, SparkPlugServerConfig, load_config


def test_valid_config_loading(sample_yaml_file: Path):
    config = load_config(sample_yaml_file)
    assert len(config.hosts) == 1
    host = config.hosts[0]
    assert host.id == "media-server"
    assert host.mac_address == "AA:BB:CC:DD:EE:FF"
    assert host.ip_address == "192.168.1.50"
    assert host.check_port == 8096
    assert host.sleep_timeout_minutes == 15
    assert host.idle_timeout_minutes == 30
    assert host.boot_grace_period_seconds == 90
    assert host.ssh.user == "sparkplug-agent"
    assert host.ssh.shutdown_command == "sudo poweroff"
    assert host.ssh.suspend_command == "sudo systemctl suspend"


def test_missing_config_file():
    with pytest.raises(FileNotFoundError):
        load_config("/non/existent/path/config.yaml")


def test_mac_normalization_and_validation(mock_ssh_key: Path):
    # Dashes should normalize to colons and uppercase
    h1 = HostConfig(
        id="host1",
        name="Host 1",
        mac_address="aa-bb-cc-11-22-33",
        ip_address="192.168.1.10",
        ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
    )
    assert h1.mac_address == "AA:BB:CC:11:22:33"

    # Invalid MAC
    with pytest.raises(ValidationError):
        HostConfig(
            id="host2",
            name="Host 2",
            mac_address="invalid-mac",
            ip_address="192.168.1.10",
            ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
        )


def test_invalid_ipv4_address(mock_ssh_key: Path):
    with pytest.raises(ValidationError):
        HostConfig(
            id="host1",
            name="Host 1",
            mac_address="AA:BB:CC:DD:EE:FF",
            ip_address="999.999.999.999",
            ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
        )


def test_sleep_timeout_validation(mock_ssh_key: Path):
    # sleep_timeout_minutes >= idle_timeout_minutes should fail validation
    with pytest.raises(ValidationError, match="must be less than idle_timeout_minutes"):
        HostConfig(
            id="host1",
            name="Host 1",
            mac_address="AA:BB:CC:DD:EE:FF",
            ip_address="192.168.1.10",
            sleep_timeout_minutes=30,
            idle_timeout_minutes=30,
            ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
        )


def test_duplicate_host_ids(sample_host_config: HostConfig):
    with pytest.raises(ValidationError, match="Duplicate host id detected"):
        AppConfig(hosts=[sample_host_config, sample_host_config])


def test_find_host_by_caddy_domain(sample_app_config: AppConfig):
    host = sample_app_config.find_host_by_caddy_domain("media.home.lan:443")
    assert host is not None
    assert host.id == "media-server"

    unknown = sample_app_config.find_host_by_caddy_domain("unknown.example.com")
    assert unknown is None


def test_invalid_host_id(mock_ssh_key: Path):
    with pytest.raises(ValidationError, match="Must contain only alphanumeric"):
        HostConfig(
            id="invalid host id!",
            name="Host",
            mac_address="AA:BB:CC:DD:EE:FF",
            ip_address="192.168.1.10",
            ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
        )


def test_invalid_yaml_syntax(temp_dir: Path):
    bad_yaml = temp_dir / "bad.yaml"
    bad_yaml.write_text("sparkplug: [invalid-unclosed", encoding="utf-8")
    with pytest.raises(ValueError, match="Failed to parse YAML"):
        load_config(bad_yaml)


def test_non_dict_yaml(temp_dir: Path):
    list_yaml = temp_dir / "list.yaml"
    list_yaml.write_text("- item1\n- item2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a top-level dictionary"):
        load_config(list_yaml)


def test_sleep_only_configuration(mock_ssh_key: Path):
    # Host with sleep enabled and shutdown disabled (idle_timeout_minutes=None or 0)
    h_none = HostConfig(
        id="sleep-only-1",
        name="Sleep Only Host",
        mac_address="AA:BB:CC:DD:EE:FF",
        ip_address="192.168.1.100",
        sleep_timeout_minutes=15,
        idle_timeout_minutes=None,
        ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
    )
    assert h_none.is_sleep_enabled is True
    assert h_none.is_shutdown_enabled is False

    h_zero = HostConfig(
        id="sleep-only-2",
        name="Sleep Only Host 2",
        mac_address="AA:BB:CC:DD:EE:FF",
        ip_address="192.168.1.101",
        sleep_timeout_minutes=15,
        idle_timeout_minutes=0,
        ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
    )
    assert h_zero.is_sleep_enabled is True
    assert h_zero.is_shutdown_enabled is False


def test_domain_matches_wildcard():
    from src.config import domain_matches

    # *.coder.service.internal
    pattern = "*.coder.service.internal"
    assert domain_matches(pattern, "app.coder.service.internal") is True
    assert domain_matches(pattern, "nested.sub.coder.service.internal") is True
    assert domain_matches(pattern, "coder.service.internal") is True
    assert domain_matches(pattern, "app.coder.service.internal:8080") is True
    assert domain_matches(pattern, "other.service.internal") is False
    assert domain_matches(pattern, "coder.service.internal.other") is False

    # Exact domain
    assert domain_matches("coder.service.internal", "coder.service.internal") is True
    assert domain_matches("coder.service.internal", "coder.service.internal:443") is True
    assert domain_matches("coder.service.internal", "other.service.internal") is False

    # Custom glob
    assert domain_matches("svc-*.internal", "svc-media.internal") is True
    assert domain_matches("svc-*.internal", "service.internal") is False


def test_find_host_by_caddy_domain_wildcard(mock_ssh_key: Path):
    h_wildcard = HostConfig(
        id="wildcard-host",
        name="Wildcard Host",
        mac_address="11:22:33:44:55:66",
        ip_address="192.168.1.10",
        caddy_hosts=["*.coder.service.internal", "coder.service.internal"],
        ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
    )
    h_specific = HostConfig(
        id="specific-host",
        name="Specific Host",
        mac_address="22:33:44:55:66:77",
        ip_address="192.168.1.20",
        caddy_hosts=["special.coder.service.internal"],
        ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
    )
    app_cfg = AppConfig(hosts=[h_wildcard, h_specific])

    # Specific host exact match should win over wildcard host
    matched_specific = app_cfg.find_host_by_caddy_domain("special.coder.service.internal")
    assert matched_specific is not None
    assert matched_specific.id == "specific-host"

    # Subdomains should match the wildcard host
    matched_sub = app_cfg.find_host_by_caddy_domain("dev.coder.service.internal")
    assert matched_sub is not None
    assert matched_sub.id == "wildcard-host"

    # Apex domain should match
    matched_apex = app_cfg.find_host_by_caddy_domain("coder.service.internal")
    assert matched_apex is not None
    assert matched_apex.id == "wildcard-host"

    # Unrelated domain should not match
    assert app_cfg.find_host_by_caddy_domain("unrelated.internal") is None


def test_keep_alive_filter_shorthands_merge(mock_ssh_key: Path):
    h = HostConfig(
        id="filtered-host",
        name="Filtered Host",
        mac_address="AA:BB:CC:DD:EE:FF",
        ip_address="192.168.1.50",
        ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
        keep_alive_users=["alice", "bob"],
        keep_alive_ips=["192.168.1.0/24", "10.0.0.1"],
    )
    assert h.keep_alive_filter is not None
    assert set(h.keep_alive_filter.users) == {"alice", "bob"}
    assert set(h.keep_alive_filter.ips) == {"192.168.1.0/24", "10.0.0.1"}


def test_keep_alive_filter_invalid_ip(mock_ssh_key: Path):
    with pytest.raises(ValueError, match="Invalid IP address or CIDR network"):
        HostConfig(
            id="bad-ip-host",
            name="Bad IP Host",
            mac_address="AA:BB:CC:DD:EE:FF",
            ip_address="192.168.1.50",
            ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
            keep_alive_ips=["999.999.999.999"],
        )


def test_matches_keep_alive_users_and_ips(mock_ssh_key: Path):
    from src.config import KeepAliveFilter

    h = HostConfig(
        id="filter-test",
        name="Filter Test",
        mac_address="AA:BB:CC:DD:EE:FF",
        ip_address="192.168.1.50",
        ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
        keep_alive_filter=KeepAliveFilter(
            users=["alice", "*@company.org"],
            ips=["192.168.1.0/24", "10.0.0.5"],
            require_all=False,
        ),
    )

    # Allowed user from unknown IP
    assert h.matches_keep_alive(user="alice", client_ip="8.8.8.8") is True
    # Wildcard user from unknown IP
    assert h.matches_keep_alive(user="john@company.org", client_ip="8.8.8.8") is True
    # Allowed subnet IP from unknown user
    assert h.matches_keep_alive(user="anonymous", client_ip="192.168.1.150") is True
    # Single allowed IP
    assert h.matches_keep_alive(user="stranger", client_ip="10.0.0.5") is True
    # Disallowed user and IP
    assert h.matches_keep_alive(user="eve", client_ip="172.16.0.1") is False


def test_matches_keep_alive_require_all(mock_ssh_key: Path):
    from src.config import KeepAliveFilter

    h = HostConfig(
        id="strict-host",
        name="Strict Host",
        mac_address="AA:BB:CC:DD:EE:FF",
        ip_address="192.168.1.50",
        ssh=SSHConfig(user="user", key_path=str(mock_ssh_key)),
        keep_alive_filter=KeepAliveFilter(
            users=["admin"],
            ips=["192.168.1.0/24"],
            headers={"X-Authorized": "true"},
            require_all=True,
        ),
    )

    # All criteria match
    assert (
        h.matches_keep_alive(
            user="admin",
            client_ip="192.168.1.100",
            headers={"X-Authorized": "true"},
        )
        is True
    )

    # Missing required header
    assert (
        h.matches_keep_alive(
            user="admin",
            client_ip="192.168.1.100",
            headers={"X-Authorized": "false"},
        )
        is False
    )

    # Wrong IP with correct user and header
    assert (
        h.matches_keep_alive(
            user="admin",
            client_ip="10.0.0.1",
            headers={"X-Authorized": "true"},
        )
        is False
    )




