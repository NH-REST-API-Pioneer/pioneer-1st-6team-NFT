import base64

import pytest

from nh_mcp.config import ENV, NH_BASE_URL, NH_STREAM_URL, ConfigError, load_settings

FAKE_SECRET = "fake-google-secret-0000"
GOOD = {
    "PUBLIC_BASE_URL": "https://mcp.example.com/",
    "GOOGLE_CLIENT_ID": "fake-client-id",
    "GOOGLE_CLIENT_SECRET": FAKE_SECRET,
    "ADMIN_EMAILS": " Admin@Example.com , ops@example.com ",
    "DB_PATH": "data/nh.db",
    "KEY_ENCRYPTION_KEY": base64.b64encode(bytes(32)).decode(),
}


def test_nh_addresses_are_demo() -> None:
    assert NH_BASE_URL == "https://apidemo.futures.co.kr"
    assert NH_STREAM_URL == "wss://apidemo.futures.co.kr"
    assert ENV == "demo"


def test_loads_settings() -> None:
    settings = load_settings(GOOD)
    assert settings.public_base_url == "https://mcp.example.com"
    assert settings.admin_emails == frozenset({"admin@example.com", "ops@example.com"})
    assert settings.open_signup is False


def test_secret_is_not_in_repr() -> None:
    assert FAKE_SECRET not in repr(load_settings(GOOD))


@pytest.mark.parametrize(
    "name",
    [
        "PUBLIC_BASE_URL",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "DB_PATH",
        "KEY_ENCRYPTION_KEY",
    ],
)
def test_missing_required_value_fails_fast(name: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({k: v for k, v in GOOD.items() if k != name})


def test_public_url_must_be_https_except_localhost() -> None:
    with pytest.raises(ConfigError):
        load_settings({**GOOD, "PUBLIC_BASE_URL": "http://mcp.example.com"})
    assert load_settings({**GOOD, "PUBLIC_BASE_URL": "http://localhost:8000"}).public_base_url


def test_closed_signup_needs_admin_emails() -> None:
    with pytest.raises(ConfigError):
        load_settings({**GOOD, "ADMIN_EMAILS": ""})
    assert load_settings({**GOOD, "ADMIN_EMAILS": "", "OPEN_SIGNUP": "true"}).open_signup


def test_open_signup_must_be_true_or_false() -> None:
    with pytest.raises(ConfigError):
        load_settings({**GOOD, "OPEN_SIGNUP": "yes"})


def test_orders_are_off_when_the_switch_is_missing() -> None:
    assert load_settings(GOOD).orders_enabled is False


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False), (" TRUE ", True)])
def test_orders_switch_reads_true_or_false(raw: str, expected: bool) -> None:
    assert load_settings({**GOOD, "ORDERS_ENABLED": raw}).orders_enabled is expected


@pytest.mark.parametrize("raw", ["yes", "1", "on", ""])
def test_orders_switch_must_be_true_or_false(raw: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({**GOOD, "ORDERS_ENABLED": raw})


@pytest.mark.parametrize("raw", ["not-base64!", base64.b64encode(bytes(16)).decode()])
def test_encryption_key_must_be_32_bytes_of_base64(raw: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({**GOOD, "KEY_ENCRYPTION_KEY": raw})


def test_encryption_key_is_not_in_repr() -> None:
    key = bytes(range(32))
    settings = load_settings({**GOOD, "KEY_ENCRYPTION_KEY": base64.b64encode(key).decode()})
    assert settings.key_encryption_key == key
    assert repr(key) not in repr(settings)


def test_bind_host_is_loopback_unless_container() -> None:
    assert load_settings(GOOD).bind_host == "127.0.0.1"
    assert load_settings({**GOOD, "BIND_HOST": "0.0.0.0"}).bind_host == "0.0.0.0"
    with pytest.raises(ConfigError):
        load_settings({**GOOD, "BIND_HOST": "192.168.0.10"})
