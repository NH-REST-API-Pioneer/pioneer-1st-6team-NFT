"""저장소에 들어가는 녹화 파일에 계좌 정보와 토큰이 없는지 본다(안전 불변식 7, 11)."""

from pathlib import Path

from nh_mcp.domain import JsonValue
from nh_mcp.nh.recording import Fixture, is_sensitive

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh"


def filled_sensitive_fields(value: JsonValue) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if is_sensitive(key) and inner not in ("", None):
                found.append(key)
            found.extend(filled_sensitive_fields(inner))
    elif isinstance(value, list):
        for inner in value:
            found.extend(filled_sensitive_fields(inner))
    return found


def recorded_files() -> list[Path]:
    return sorted(RECORDED.rglob("*.json"))


def test_checker_finds_nested_account_numbers() -> None:
    body: JsonValue = {"Occurs1": [{"occurs1_ac_no": "12345678901", "occurs1_sym": "A016C000"}]}
    assert filled_sensitive_fields(body) == ["occurs1_ac_no"]


def test_recordings_exist() -> None:
    assert len(recorded_files()) >= 10


def test_recordings_hold_no_account_info_or_token() -> None:
    for file in recorded_files():
        fixture = Fixture.load(file)
        assert filled_sensitive_fields(fixture.request) == [], file.name
        assert filled_sensitive_fields(fixture.body) == [], file.name
        text = file.read_text(encoding="utf-8")
        assert "Bearer" not in text and "access_token" not in text, file.name
