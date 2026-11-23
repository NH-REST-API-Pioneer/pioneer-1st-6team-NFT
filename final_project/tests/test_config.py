import pytest

from nh_mcp.config import ENV, NH_BASE_URL, NH_STREAM_URL, ConfigError, load_settings

FAKE_SECRET = "fake-google-secret-0000"
GOOD = {
    "PUBLIC_BASE_URL": "https://mcp.example.com/",
    "GOOGLE_CLIENT_ID": "fake-client-id",
    "GOOGLE_CLIENT_SECRET": FAKE_SECRET,
    "ADMIN_EMAILS": " Admin@Example.com , ops@example.com ",
    "DB_PATH": "data/nh.db",
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
    "name", ["PUBLIC_BASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "DB_PATH"]
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
