import pytest

from nh_mcp.config import DEMO_BASE_URL, ConfigError, load_settings


def test_demo_url_is_demo_env() -> None:
    assert load_settings({"NH_BASE_URL": DEMO_BASE_URL}).env == "demo"


def test_trailing_slash_is_ignored() -> None:
    assert load_settings({"NH_BASE_URL": f"{DEMO_BASE_URL}/"}).env == "demo"


def test_other_url_is_not_demo() -> None:
    assert load_settings({"NH_BASE_URL": "https://example.com"}).env == "not_demo"


@pytest.mark.parametrize("environ", [{}, {"NH_BASE_URL": "  "}])
def test_missing_base_url_fails_fast(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_settings(environ)
