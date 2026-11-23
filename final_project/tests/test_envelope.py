from datetime import UTC, datetime

from nh_mcp import envelope

NOW = datetime(2026, 12, 1, 1, 15, 3, tzinfo=UTC)


def test_meta_is_kst_iso_seconds() -> None:
    assert envelope.meta("demo", NOW) == {"env": "demo", "as_of": "2026-12-01T10:15:03+09:00"}


def test_ok_shape() -> None:
    meta = envelope.meta("demo", NOW)
    assert envelope.ok({"pong": True}, meta) == {"ok": True, "data": {"pong": True}, "meta": meta}


def test_error_with_rule() -> None:
    meta = envelope.meta("demo", NOW)
    result = envelope.error("RISK_BLOCKED", "막힘", meta, rule="daily_loss_limit")
    assert result == {
        "ok": False,
        "error": {
            "code": "RISK_BLOCKED",
            "message": "막힘",
            "retryable": False,
            "rule": "daily_loss_limit",
        },
        "meta": meta,
    }


def test_error_without_rule_has_no_rule_key() -> None:
    result = envelope.error("INVALID_INPUT", "수량이 0입니다", envelope.meta("demo", NOW))
    assert "rule" not in result["error"]
