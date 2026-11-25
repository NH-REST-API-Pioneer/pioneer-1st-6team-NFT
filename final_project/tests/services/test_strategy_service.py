"""실행 승인 미리보기: 범위가 한도 안이고 같은 조건의 성공한 백테스트가 있어야 토큰이 나온다.

시계는 2026-12-01 10:00 KST(T0). 한도는 하루 손실 500,000원, 최대 3계약, 1회 2계약.
백테스트 기록은 저장소에 직접 넣는다(실행은 이 테스트의 관심이 아니다).
"""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from fakes import T0, World

from nh_mcp.domain import (
    KST,
    JsonValue,
    ModelHash,
    RiskLimits,
    StrategyScope,
    Stream,
    Streams,
    UserId,
)
from nh_mcp.markets.domestic import DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.services.backtest import MIXED, NO_MODEL, StrategyInvalid, code_hash, run_key
from nh_mcp.services.orders import LimitsNotSet, RiskBlocked
from nh_mcp.services.queries import UnknownSymbol
from nh_mcp.services.strategy import (
    MAX_APPROVAL,
    MAX_DAILY_ORDERS,
    NOT_BACKTESTED,
    SCOPE_EXCEEDS_LIMITS,
    STRATEGY_TTL,
    StrategyPreview,
    StrategyRequest,
    StrategyService,
)
from nh_mcp.store import limits, users
from nh_mcp.store.backtests import SUCCESS, NewRun, RunKey, record

SYMBOL = "A016C000"  # 코스피200 F 202612
MINI = "A056C000"  # 미니코스피 F 202612
SPREAD = "D016C73S"  # 코스피200 스프레드
STOCK_FUTURE = "A0A6C000"  # 주식선물: 다른 종목
CODE = "def on_event(event, ctx):\n    return None\n"
PARAMS: dict[str, JsonValue] = {"window": 3, "band": "0.02"}
LIMITS = RiskLimits(Decimal("500000"), 3, 2)
SCOPE = StrategyScope(
    quantity=1,
    max_position=2,
    daily_orders=10,
    daily_loss=Decimal("300000"),
    expires_at=T0 + timedelta(days=7) - timedelta(hours=1),
    on_loss="liquidate",
    mode="auto",
)
DAY: Streams = {SYMBOL: frozenset({"day"})}
REQUEST = StrategyRequest(CODE, PARAMS, DAY, SCOPE)


def streams(**chosen: list[Stream]) -> Streams:
    return {symbol: frozenset(names) for symbol, names in chosen.items()}


def key_for(
    chosen: Streams = DAY,
    code: str = CODE,
    params: dict[str, JsonValue] | None = None,
    model: ModelHash | None = None,
) -> RunKey:
    return run_key(code, PARAMS if params is None else params, chosen, model)


def add_run(conn: sqlite3.Connection, user: UserId, key: RunKey, status: str = SUCCESS) -> None:
    run = NewRun(
        user_id=user,
        key=key,
        code=CODE,
        settings="{}",
        requested=(date(2026, 11, 2), date(2026, 11, 30)),
        first_bar_at=None,
        last_bar_at=None,
        bar_count=5,
        status=status,
        result=None,
    )
    record(conn, run, T0)


class Desk:
    """한도를 정한 사용자, 같은 키의 성공한 백테스트 기록, 서비스."""

    def __init__(
        self, with_limits: bool = True, with_run: bool = True, data_dir: Path | None = None
    ) -> None:
        self.world = World(data_dir=data_dir)
        self.conn: sqlite3.Connection = self.world.conn
        clock = self.world.clock
        self.user: UserId = users.get_or_create(self.conn, "g-admin", "admin@example.com", T0)
        self.other: UserId = users.get_or_create(self.conn, "g-other", "other@example.com", T0)
        if with_limits:
            limits.put(self.conn, self.user, "domestic", LIMITS, T0)
        if with_run:
            add_run(self.conn, self.user, key_for())
        master = DomesticMaster(clock, transport=httpx2.MockTransport(self.world.master))
        domestic = DomesticMarket(self.world.pool, master, clock)
        self.service = StrategyService(self.conn, clock, domestic, self.world.models)

    def upload(self, user: UserId, content: bytes) -> ModelHash:
        draft = self.world.models.draft(user)
        draft.write(content)
        assert asyncio.run(self.world.models.commit(user, draft, "model.pkl")) == "uploaded"
        return ModelHash(hashlib.sha256(content).hexdigest())

    def tokens(self) -> list[tuple[str, str, str]]:
        """(용도, payload, 만료) 모두."""
        rows: list[tuple[str, str, str]] = self.conn.execute(
            "SELECT purpose, payload, expires_at FROM confirm_tokens ORDER BY rowid"
        ).fetchall()
        return rows

    def audits(self) -> list[tuple[int, str, dict[str, object]]]:
        rows = self.conn.execute("SELECT user_id, action, detail FROM audit_log ORDER BY id")
        return [(int(r[0]), str(r[1]), json.loads(r[2])) for r in rows.fetchall()]


def go(
    desk: Desk, request: StrategyRequest = REQUEST, user: UserId | None = None
) -> StrategyPreview:
    return asyncio.run(desk.service.preview(desk.user if user is None else user, request))


def with_scope(**changes: object) -> StrategyRequest:
    return replace(REQUEST, scope=replace(SCOPE, **changes))


def test_issues_strategy_token_with_full_scope() -> None:
    desk = Desk()

    result = go(desk)

    assert result.code_hash == code_hash(CODE)
    assert result.expires_at == T0 + STRATEGY_TTL == T0 + timedelta(minutes=2)
    assert len(result.confirm_token) >= 32
    [(purpose, payload, expires)] = desk.tokens()
    assert purpose == "strategy"
    assert datetime.fromisoformat(expires) == result.expires_at
    assert json.loads(payload) == {
        "market": "domestic",
        "streams": {SYMBOL: ["day"]},
        "code_hash": code_hash(CODE),
        "params": '{"band":"0.02","window":3}',
        "model_hash": None,
        "scope": {
            "quantity": 1,
            "max_position": 2,
            "daily_orders": 10,
            "daily_loss": "300000",
            "expires_at": SCOPE.expires_at.astimezone(UTC).isoformat(),
            "on_loss": "liquidate",
            "mode": "auto",
        },
    }
    # 코드 원문은 토큰에 없다.
    assert "on_event" not in payload and CODE not in payload
    # 토큰 원문은 저장하지 않는다(감사 로그에도 없다).
    [(user, action, detail)] = desk.audits()
    assert (user, action) == (desk.user, "strategy.preview")
    assert detail == {
        "outcome": "issued",
        "streams": {SYMBOL: ["day"]},
        "code_hash": code_hash(CODE),
        "model_hash": None,
    }
    assert result.confirm_token not in json.dumps(detail)
    assert "on_event" not in json.dumps(detail)


def test_summary_is_built_from_stored_values() -> None:
    desk = Desk()

    result = go(desk)

    assert result.summary == (
        "코스피200 F 202612 일봉 · 1회 1계약 · 최대 2계약 · 하루 10회 · "
        "일일 손실 300,000원 · 2026-12-08 09:00까지 · 손실 한도에서 정리 · 자동 모드"
    )


def test_summary_for_minute_hold_suggest() -> None:
    desk = Desk()
    add_run(desk.conn, desk.user, key_for(streams(A016C000=["minute"])))
    expires = datetime(2026, 12, 3, 6, 45, 30, tzinfo=UTC)
    request = replace(
        with_scope(on_loss="hold", mode="suggest", expires_at=expires),
        streams=streams(A016C000=["minute"]),
    )

    result = go(desk, request)

    assert result.summary.startswith("코스피200 F 202612 1분봉 · ")
    assert result.summary.endswith(" · 2026-12-03 15:45까지 · 손실 한도에서 유지 · 제안 모드")


def test_expiry_is_compared_and_stored_in_utc() -> None:
    desk = Desk()
    # KST로 쓴 7일 이내 시각. 저장은 UTC.
    expires = (T0 + timedelta(days=1)).astimezone(KST)

    go(desk, with_scope(expires_at=expires))

    [(_, payload, _)] = desk.tokens()
    stored = json.loads(payload)["scope"]["expires_at"]
    assert stored == (T0 + timedelta(days=1)).isoformat()


def test_limits_not_set() -> None:
    desk = Desk(with_limits=False)

    with pytest.raises(LimitsNotSet):
        go(desk)

    assert desk.tokens() == []
    [(_, action, detail)] = desk.audits()
    assert action == "strategy.preview"
    assert detail["outcome"] == "blocked" and detail["rule"] == "limits_not_set"


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"quantity": 3, "max_position": 3}, "quantity"),
        ({"max_position": 4}, "max_position"),
        ({"daily_loss": Decimal("500001")}, "daily_loss"),
        ({"quantity": 2, "max_position": 1}, "quantity"),
    ],
)
def test_each_scope_item_over_limit_is_blocked(changes: dict[str, object], named: str) -> None:
    desk = Desk()

    with pytest.raises(RiskBlocked) as caught:
        go(desk, with_scope(**changes))

    assert caught.value.rule == SCOPE_EXCEEDS_LIMITS == "scope_exceeds_limits"
    assert named in caught.value.message
    assert desk.tokens() == []
    [(_, _, detail)] = desk.audits()
    assert detail["outcome"] == "blocked" and detail["rule"] == SCOPE_EXCEEDS_LIMITS


def test_quantity_above_one_order_limit_names_the_item() -> None:
    desk = Desk()

    with pytest.raises(RiskBlocked) as caught:
        go(desk, with_scope(quantity=3, max_position=3))

    assert "quantity" in caught.value.message and "2" in caught.value.message


def test_boundaries_pass() -> None:
    desk = Desk()

    go(desk, with_scope(quantity=2, max_position=3, daily_loss=Decimal("500000")))

    assert len(desk.tokens()) == 1


@pytest.mark.parametrize("orders", [0, MAX_DAILY_ORDERS + 1, -1])
def test_daily_orders_out_of_bounds(orders: int) -> None:
    desk = Desk()

    with pytest.raises(StrategyInvalid) as caught:
        go(desk, with_scope(daily_orders=orders))

    assert "daily_orders" in caught.value.message
    assert desk.tokens() == []


@pytest.mark.parametrize("orders", [1, MAX_DAILY_ORDERS])
def test_daily_orders_edges_pass(orders: int) -> None:
    desk = Desk()

    go(desk, with_scope(daily_orders=orders))

    assert len(desk.tokens()) == 1


@pytest.mark.parametrize(
    "expires",
    [
        T0,
        T0 - timedelta(hours=1),
        T0 + MAX_APPROVAL + timedelta(seconds=1),
        T0.replace(tzinfo=None),
    ],
)
def test_expiry_bounds(expires: datetime) -> None:
    desk = Desk()

    with pytest.raises(StrategyInvalid) as caught:
        go(desk, with_scope(expires_at=expires))

    assert "expires_at" in caught.value.message
    assert desk.tokens() == []


def test_expiry_exactly_seven_days_passes() -> None:
    desk = Desk()

    go(desk, with_scope(expires_at=T0 + MAX_APPROVAL))

    assert len(desk.tokens()) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 0},
        {"max_position": 0},
        {"daily_loss": Decimal("0")},
        {"daily_loss": Decimal("-1")},
    ],
)
def test_non_positive_scope_is_invalid(changes: dict[str, object]) -> None:
    desk = Desk()

    with pytest.raises(StrategyInvalid):
        go(desk, with_scope(**changes))


def test_requires_a_successful_backtest_with_the_same_key() -> None:
    other_params: dict[str, JsonValue] = {"window": 4, "band": "0.02"}
    changed_code = CODE + " "
    cases: list[tuple[str, StrategyRequest]] = [
        ("params", replace(REQUEST, params=other_params)),
        ("interval", replace(REQUEST, streams=streams(A016C000=["minute"]))),
        ("more streams", replace(REQUEST, streams=streams(A016C000=["day", "minute"]))),
        ("more symbols", replace(REQUEST, streams=streams(A016C000=["day"], A056C000=["day"]))),
        ("code", replace(REQUEST, code=changed_code)),
    ]
    for name, request in cases:
        desk = Desk()
        with pytest.raises(RiskBlocked) as caught:
            go(desk, request)
        assert caught.value.rule == NOT_BACKTESTED == "not_backtested", name
        assert "backtest_strategy" in caught.value.message, name
        assert desk.tokens() == [], name


def test_no_record_at_all_is_not_backtested() -> None:
    desk = Desk(with_run=False)

    with pytest.raises(RiskBlocked) as caught:
        go(desk)

    assert caught.value.rule == NOT_BACKTESTED
    [(_, _, detail)] = desk.audits()
    assert detail["outcome"] == "blocked" and detail["rule"] == NOT_BACKTESTED


@pytest.mark.parametrize("status", ["cpu", "exception", "invalid_output", "data_short"])
def test_failed_runs_do_not_count(status: str) -> None:
    desk = Desk(with_run=False)
    add_run(desk.conn, desk.user, key_for(), status)

    with pytest.raises(RiskBlocked) as caught:
        go(desk)

    assert caught.value.rule == NOT_BACKTESTED


def test_another_symbol_is_not_backtested() -> None:
    desk = Desk()  # 기록은 SYMBOL 것

    with pytest.raises(RiskBlocked) as caught:
        go(desk, replace(REQUEST, streams=streams(A0A6C000=["day"])))

    assert caught.value.rule == NOT_BACKTESTED


def test_params_order_does_not_matter() -> None:
    desk = Desk()

    go(desk, replace(REQUEST, params={"band": "0.02", "window": 3}))

    assert len(desk.tokens()) == 1


def test_another_users_backtest_does_not_count() -> None:
    desk = Desk(with_run=False)
    add_run(desk.conn, desk.other, key_for())
    limits.put(desk.conn, desk.other, "domestic", LIMITS, T0)

    with pytest.raises(RiskBlocked) as caught:
        go(desk)  # desk.user의 기록은 없다

    assert caught.value.rule == NOT_BACKTESTED
    # 기록의 주인은 통과한다.
    go(desk, user=desk.other)
    [(_, payload, _)] = desk.tokens()
    assert json.loads(payload)["code_hash"] == code_hash(CODE)


def test_not_a_future_is_invalid() -> None:
    desk = Desk()

    with pytest.raises(StrategyInvalid):
        go(desk, replace(REQUEST, streams=streams(D016C73S=["day"])))
    assert desk.tokens() == []


def test_unknown_symbol() -> None:
    desk = Desk()

    with pytest.raises(UnknownSymbol):
        go(desk, replace(REQUEST, streams=streams(NOSUCH=["day"])))
    assert desk.tokens() == []


def test_empty_code_is_invalid() -> None:
    desk = Desk()

    with pytest.raises(StrategyInvalid):
        go(desk, replace(REQUEST, code=""))
    assert desk.tokens() == []


def test_two_symbols_in_any_order_match_one_backtest() -> None:
    desk = Desk(with_run=False)
    add_run(desk.conn, desk.user, key_for(streams(A016C000=["day", "minute"], A056C000=["day"])))
    reordered = streams(A056C000=["day"], A016C000=["minute", "day"])

    result = go(desk, replace(REQUEST, streams=reordered))

    assert result.summary.startswith(
        "코스피200 F 202612 일봉+1분봉, 미니코스피 F 202612 일봉 · 1회 1계약 · "
    )
    [(_, payload, _)] = desk.tokens()
    assert json.loads(payload)["streams"] == {SYMBOL: ["day", "minute"], MINI: ["day"]}


def test_a_tick_run_backtest_lets_the_same_tick_streams_through() -> None:
    desk = Desk(with_run=False)
    ticking = streams(A016C000=["tick", "book", "day"])
    add_run(desk.conn, desk.user, key_for(ticking))

    for other in (streams(A016C000=["tick", "day"]), streams(A016C000=["day"])):
        with pytest.raises(RiskBlocked) as caught:
            go(desk, replace(REQUEST, streams=other))
        assert caught.value.rule == NOT_BACKTESTED
    result = go(desk, replace(REQUEST, streams=streams(A016C000=["day", "book", "tick"])))

    assert result.summary.startswith("코스피200 F 202612 호가(book)+일봉+체결(tick) · 1회 1계약")
    [(_, payload, _)] = desk.tokens()
    assert json.loads(payload)["streams"] == {SYMBOL: ["book", "day", "tick"]}


@pytest.mark.parametrize(
    "chosen",
    [
        streams(A016C000=["tick", "minute"]),
        streams(A016C000=["book"], A056C000=["minute"]),
    ],
)
def test_minute_with_tick_streams_is_refused(chosen: Streams) -> None:
    desk = Desk()

    with pytest.raises(StrategyInvalid) as caught:
        go(desk, replace(REQUEST, streams=chosen))

    assert caught.value.message == MIXED
    assert desk.tokens() == []


def test_model_hash_must_be_the_one_backtested(tmp_path: Path) -> None:
    desk = Desk(with_run=False, data_dir=tmp_path)
    used, other = desk.upload(desk.user, b"model used"), desk.upload(desk.user, b"other model")
    add_run(desk.conn, desk.user, key_for(model=used))

    for request in (REQUEST, replace(REQUEST, model=other)):
        with pytest.raises(RiskBlocked) as caught:
            go(desk, request)
        assert caught.value.rule == NOT_BACKTESTED

    result = go(desk, replace(REQUEST, model=used))

    [(_, payload, _)] = desk.tokens()
    assert json.loads(payload)["model_hash"] == used
    assert f"모델 {used[:12]}" in result.summary
    assert desk.audits()[-1][2]["model_hash"] == used


def test_model_deleted_after_the_backtest_blocks_the_preview(tmp_path: Path) -> None:
    desk = Desk(with_run=False, data_dir=tmp_path)
    used = desk.upload(desk.user, b"model used")
    add_run(desk.conn, desk.user, key_for(model=used))
    assert desk.world.models.delete(desk.user, used)

    with pytest.raises(StrategyInvalid) as caught:
        go(desk, replace(REQUEST, model=used))

    assert caught.value.message == NO_MODEL
    assert "그런 모델 파일이 없습니다" in caught.value.message
    assert desk.tokens() == []


def test_another_users_model_is_a_missing_one(tmp_path: Path) -> None:
    desk = Desk(with_run=False, data_dir=tmp_path)
    theirs = desk.upload(desk.other, b"their model")
    # 다른 사용자의 해시로 백테스트 기록이 있어도(같은 해시를 키로) 내 모델이 아니면 막는다.
    add_run(desk.conn, desk.user, key_for(model=theirs))

    with pytest.raises(StrategyInvalid) as caught:
        go(desk, replace(REQUEST, model=theirs))

    assert caught.value.message == NO_MODEL
    assert desk.tokens() == []
