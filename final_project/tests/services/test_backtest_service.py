"""백테스트 서비스: 봉을 모아 실행기에 보내고, 신호로 성과를 계산하고, 실행을 남긴다.

NH는 가짜(일봉 행은 명세 필드 이름으로 만든다), 실행기도 가짜, 시계는 2026-12-01 10:00 KST.
"""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fakes import SETTINGS, T0, FakeNH, FakeSandbox, World

from nh_mcp.domain import KST, Candle, SignalName, UserId
from nh_mcp.markets.domestic import CHART_DAILY, DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL, DomesticProducts
from nh_mcp.nh.client import Credentials
from nh_mcp.sandbox.client import SandboxClient
from nh_mcp.sandbox.wire import bar_to_wire
from nh_mcp.services.backtest import (
    MAX_BARS,
    MAX_CODE_BYTES,
    MAX_PARAMS_BYTES,
    PER_USER,
    BacktestRequest,
    BacktestService,
    RunnerUnavailable,
    StrategyFailed,
    StrategyInvalid,
    code_hash,
    params_key,
)
from nh_mcp.services.queries import KeyMissing, QueryService, UnknownSymbol
from nh_mcp.store import users
from nh_mcp.strategies.backtest import FillTerms, simulate

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"  # 코스피200 F 202612: 틱 0.05(product-detail), 승수 250000(마스터)
SPREAD = "D016C73S"  # 코스피200 스프레드
STOCK_FUTURE = "A0A6C000"  # 주식선물: 틱 표 대응이 없다
CODE = "def on_bar(bar, history, params):\n    return 'none'\n"
PARAMS: dict[str, Any] = {"window": 3, "band": "0.02"}
REQUEST = BacktestRequest(
    symbol=SYMBOL,
    code=CODE,
    params=PARAMS,
    interval="day",
    period=(date(2026, 11, 2), date(2026, 11, 30)),
    quantity=1,
    fee_per_fill=Decimal("1000"),
)


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def occurs(rows: list[dict[str, str]]) -> dict[str, object]:
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": rows}


def day_row(day: date, o: str, h: str, low: str, c: str) -> dict[str, str]:
    return {
        "occurs1_ref_dt": day.strftime("%Y%m%d"),
        "occurs1_open_pric": o,
        "occurs1_high_pric": h,
        "occurs1_low_pric": low,
        "occurs1_close_pric": c,
        "occurs1_cum_trd_qty": "1000",
    }


# 2026-11-02(월)부터 하루씩. (시가, 종가)
PRICES = [
    ("780.00", "781.00"),
    ("782.00", "779.50"),
    ("779.00", "783.25"),
    ("784.00", "786.00"),
    ("785.50", "784.00"),
]


def rows_from(start: date, prices: list[tuple[str, str]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for i, (o, c) in enumerate(prices):
        high, low = max(o, c, key=Decimal), min(o, c, key=Decimal)
        rows.append(day_row(start + timedelta(days=i), o, high, low, c))
    # NH는 최근 것부터 준다.
    return rows[::-1]


def candles_from(start: date, prices: list[tuple[str, str]]) -> list[Candle]:
    out: list[Candle] = []
    for i, (o, c) in enumerate(prices):
        day = start + timedelta(days=i)
        high, low = max(Decimal(o), Decimal(c)), min(Decimal(o), Decimal(c))
        at = datetime(day.year, day.month, day.day, tzinfo=KST)
        out.append(Candle(at, Decimal(o), high, low, Decimal(c), 1000))
    return out


def ok_reply(signals: list[SignalName]) -> dict[str, object]:
    return {"status": "ok", "signals": list(signals), "bar_index": None, "detail": ""}


def failed_reply(status: str, detail: str = "", bar_index: int | None = 1) -> dict[str, object]:
    return {"status": status, "signals": [], "bar_index": bar_index, "detail": detail}


class Lab:
    """가짜 NH·실행기에 붙은 백테스트 서비스. 두 사용자 모두 키를 등록했다."""

    def __init__(self, product_detail: dict[str, Any] | None = None) -> None:
        self.nh = FakeNH()
        detail = recorded("product-detail") if product_detail is None else product_detail
        self.nh.reply(PRODUCT_DETAIL, detail)
        self.nh.reply(CHART_DAILY, occurs(rows_from(date(2026, 11, 2), PRICES)))
        self.sandbox = FakeSandbox()
        self.world = World(nh=httpx2.MockTransport(self.nh), sandbox=self.sandbox)
        self.conn: sqlite3.Connection = self.world.conn
        clock = self.world.clock
        self.user: UserId = users.get_or_create(self.conn, "g-admin", "admin@example.com", T0)
        self.other: UserId = users.get_or_create(self.conn, "g-other", "other@example.com", T0)
        self.keyless: UserId = users.get_or_create(self.conn, "g-none", "none@example.com", T0)
        for user in (self.user, self.other):
            credentials = Credentials("fake-app-key-0000", "fake-app-secret-0000")
            asyncio.run(self.world.store.register(user, "domestic", credentials))
        master = DomesticMaster(clock, transport=httpx2.MockTransport(self.world.master))
        domestic = DomesticMarket(self.world.pool, master, clock)
        client = SandboxClient(
            SETTINGS.sandbox_url, SETTINGS.sandbox_token, httpx2.MockTransport(self.sandbox)
        )
        self.service = BacktestService(
            self.conn,
            clock,
            QueryService(domestic),
            DomesticProducts(self.world.pool, clock),
            client,
        )

    def bars(self, rows: list[dict[str, str]]) -> None:
        self.nh.replies[CHART_DAILY] = [(200, occurs(rows))]

    def runs(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        rows: list[sqlite3.Row] = self.conn.execute(
            "SELECT * FROM backtest_runs ORDER BY id"
        ).fetchall()
        self.conn.row_factory = None
        return rows

    def charts(self) -> int:
        return self.nh.paths().count(CHART_DAILY)


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("조건이 끝내 맞지 않았다")


SIGNALS: list[SignalName] = ["buy", "none", "sell", "exit", "buy"]


def test_feeds_bars_and_computes_report() -> None:
    lab = Lab()
    lab.sandbox.reply(ok_reply(SIGNALS))

    outcome = asyncio.run(lab.service.run(lab.user, REQUEST))

    candles = candles_from(date(2026, 11, 2), PRICES)
    terms = FillTerms(Decimal("0.05"), Decimal("250000"), 1, Decimal("1000"))
    assert outcome.report == simulate(candles, SIGNALS, terms)
    assert outcome.report.trades  # 실제로 거래가 생기는 신호다
    [job] = lab.sandbox.jobs
    assert job == {"code": CODE, "params": PARAMS, "bars": [bar_to_wire(c) for c in candles]}
    assert outcome.bars == len(PRICES)
    assert outcome.first_bar_at == candles[0].at
    assert outcome.last_bar_at == candles[-1].at
    assert outcome.truncated is False
    assert outcome.warnings == []
    assert outcome.code_hash == code_hash(CODE)
    assert lab.service.running(lab.user) == 0


def test_quantity_and_fee_reach_the_fill_terms() -> None:
    lab = Lab()
    lab.sandbox.reply(ok_reply(SIGNALS))
    request = replace(REQUEST, quantity=3, fee_per_fill=Decimal("0"))

    outcome = asyncio.run(lab.service.run(lab.user, request))

    terms = FillTerms(Decimal("0.05"), Decimal("250000"), 3, Decimal("0"))
    assert outcome.report == simulate(candles_from(date(2026, 11, 2), PRICES), SIGNALS, terms)


def test_hash_and_params_key_follow_the_global_rule() -> None:
    assert code_hash("가 b") == hashlib.sha256("가 b".encode()).hexdigest()
    # 정규화하지 않는다: 끝의 줄바꿈 하나도 다른 해시다.
    assert code_hash(CODE) != code_hash(CODE.rstrip())
    assert params_key({"b": "가", "a": [1, True, None]}) == '{"a":[1,true,null],"b":"가"}'


def test_run_is_saved_with_code_and_hash() -> None:
    lab = Lab()
    lab.sandbox.reply(ok_reply(SIGNALS))

    outcome = asyncio.run(lab.service.run(lab.user, REQUEST))

    [row] = lab.runs()
    assert row["user_id"] == lab.user
    assert (row["market"], row["symbol"], row["interval"]) == ("domestic", SYMBOL, "day")
    assert row["code"] == CODE
    assert row["code_hash"] == code_hash(CODE) == hashlib.sha256(CODE.encode()).hexdigest()
    assert row["params"] == params_key(PARAMS) == '{"band":"0.02","window":3}'
    assert row["status"] == "ok"
    assert (row["requested_start"], row["requested_end"]) == ("2026-11-02", "2026-11-30")
    assert row["bar_count"] == len(PRICES)
    assert json.loads(row["settings"]) == {"quantity": 1, "fee_per_fill": "1000"}
    result = json.loads(row["result"])
    assert result["net_pnl"] == str(outcome.report.net_pnl)
    assert result["trades"] == len(outcome.report.trades)


def test_failure_is_saved_and_raised() -> None:
    lab = Lab()
    lab.sandbox.reply(failed_reply("cpu"))

    with pytest.raises(StrategyFailed) as caught:
        asyncio.run(lab.service.run(lab.user, REQUEST))

    assert caught.value.reason == "cpu"
    assert caught.value.message
    [row] = lab.runs()
    assert (row["status"], row["result"], row["code"]) == ("cpu", None, CODE)
    assert lab.service.running(lab.user) == 0


@pytest.mark.parametrize("status", ["invalid_code", "invalid_signal"])
def test_bad_code_or_signal_is_invalid_input(status: str) -> None:
    lab = Lab()
    lab.sandbox.reply(failed_reply(status, detail="on_bar가 없습니다", bar_index=None))

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, REQUEST))

    # detail은 전략 코드가 만들 수 있다(프로토콜 출구에 거짓 줄을 쓰는 것도). 그렇다고 적는다.
    assert caught.value.message.endswith(" 전략 코드가 낸 내용: on_bar가 없습니다")
    assert "실행기가 알린" not in caught.value.message
    [row] = lab.runs()
    assert (row["status"], row["result"]) == (status, None)


@pytest.mark.parametrize("status", ["invalid_code", "invalid_signal"])
def test_without_detail_only_the_server_sentence_is_shown(status: str) -> None:
    lab = Lab()
    lab.sandbox.reply(failed_reply(status, detail="", bar_index=None))

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, REQUEST))

    assert "전략 코드가 낸 내용" not in caught.value.message
    assert caught.value.message.endswith("다.")


def test_exception_message_is_marked_as_strategy_output() -> None:
    lab = Lab()
    lab.sandbox.reply(failed_reply("exception", detail="ZeroDivisionError: division by zero"))

    with pytest.raises(StrategyFailed) as caught:
        asyncio.run(lab.service.run(lab.user, REQUEST))

    assert caught.value.reason == "exception"
    assert caught.value.message.startswith("전략 코드가 낸 오류: ")
    assert "ZeroDivisionError: division by zero" in caught.value.message


@pytest.mark.parametrize("count", [0, 1])
def test_fewer_than_two_bars_is_invalid(count: int) -> None:
    lab = Lab()
    lab.bars(rows_from(date(2026, 11, 2), PRICES[:count]))

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, REQUEST))

    assert "데이터" in caught.value.message
    assert lab.sandbox.jobs == []
    [row] = lab.runs()
    assert (row["status"], row["bar_count"], row["result"]) == ("data_short", count, None)
    assert lab.service.running(lab.user) == 0


@pytest.mark.parametrize(("count", "truncated"), [(MAX_BARS - 1, False), (MAX_BARS, True)])
def test_truncated_when_cap_reached(count: int, truncated: bool) -> None:
    lab = Lab()
    end = date(2026, 11, 30)
    rows = [day_row(end - timedelta(days=i), "780", "781", "779", "780.5") for i in range(count)]
    lab.bars(rows)
    request = replace(REQUEST, period=(date(2020, 1, 1), end))

    outcome = asyncio.run(lab.service.run(lab.user, request))

    assert outcome.bars == count
    assert outcome.truncated is truncated
    sent = lab.sandbox.jobs[0]["bars"]
    assert isinstance(sent, list) and len(cast(list[object], sent)) == count


LATE = "요청한 시작일보다 늦은 날부터 데이터가 있습니다"


@pytest.mark.parametrize(
    ("first_bar", "warns"),
    [
        (date(2026, 11, 2), False),  # 일요일 시작, 월요일 첫 봉: 주말·휴일은 경고하지 않는다
        (date(2026, 11, 6), False),  # 5일 뒤까지는 경고하지 않는다
        (date(2026, 11, 7), True),  # 6일 뒤
        (date(2026, 11, 10), True),
    ],
)
def test_late_first_bar_warns_only_past_five_days(first_bar: date, warns: bool) -> None:
    lab = Lab()
    lab.bars(rows_from(first_bar, PRICES))
    request = replace(REQUEST, period=(date(2026, 11, 1), date(2026, 11, 30)))

    outcome = asyncio.run(lab.service.run(lab.user, request))

    assert any(LATE in w for w in outcome.warnings) is warns
    assert outcome.first_bar_at == datetime(
        first_bar.year, first_bar.month, first_bar.day, tzinfo=KST
    )


def test_not_a_future_is_invalid() -> None:
    lab = Lab()

    with pytest.raises(StrategyInvalid):
        asyncio.run(lab.service.run(lab.user, replace(REQUEST, symbol=SPREAD)))

    assert lab.charts() == 0
    assert lab.sandbox.jobs == []


def test_unknown_symbol_never_reaches_nh() -> None:
    lab = Lab()

    with pytest.raises(UnknownSymbol):
        asyncio.run(lab.service.run(lab.user, replace(REQUEST, symbol="A9999999")))

    assert lab.nh.requests == []


@pytest.mark.parametrize("case", ["empty_tick_table", "no_tick_for_product"])
def test_missing_tick_blocks(case: str) -> None:
    if case == "empty_tick_table":
        lab, symbol = Lab(product_detail={"nrec": "0000", "Occurs1": []}), SYMBOL
    else:
        lab, symbol = Lab(), STOCK_FUTURE

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, replace(REQUEST, symbol=symbol)))

    assert "틱" in caught.value.message
    assert lab.sandbox.jobs == []
    assert lab.service.running(lab.user) == 0


def test_key_missing_is_reported() -> None:
    lab = Lab()

    with pytest.raises(KeyMissing):
        asyncio.run(lab.service.run(lab.keyless, REQUEST))

    assert lab.sandbox.jobs == []
    assert lab.service.running(lab.keyless) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"code": "x" * (MAX_CODE_BYTES + 1)},
        {"code": "가" * (MAX_CODE_BYTES // 3 + 1)},  # 글자 수는 작아도 UTF-8 바이트로 잰다
        {"code": "   "},
        {"params": {"blob": "x" * MAX_PARAMS_BYTES}},
        {"quantity": 0},
        {"quantity": 101},
        {"fee_per_fill": Decimal("-1")},
        {"fee_per_fill": Decimal("NaN")},
        {"fee_per_fill": Decimal("Infinity")},
    ],
)
def test_service_checks_inputs_too(change: dict[str, Any]) -> None:
    lab = Lab()

    with pytest.raises(StrategyInvalid):
        asyncio.run(lab.service.run(lab.user, replace(REQUEST, **change)))

    assert lab.nh.requests == []
    assert lab.sandbox.jobs == []


def test_third_concurrent_run_for_a_user_is_unavailable() -> None:
    lab = Lab()

    async def scenario() -> None:
        hold = asyncio.Event()
        lab.sandbox.hold = hold
        held = [asyncio.create_task(lab.service.run(lab.user, REQUEST)) for _ in range(PER_USER)]
        await until(lambda: len(lab.sandbox.jobs) == PER_USER)
        assert lab.service.running(lab.user) == PER_USER

        with pytest.raises(RunnerUnavailable):
            await lab.service.run(lab.user, REQUEST)

        other = asyncio.create_task(lab.service.run(lab.other, REQUEST))
        await until(lambda: len(lab.sandbox.jobs) == PER_USER + 1)
        hold.set()
        outcomes = await asyncio.gather(*held, other)
        assert len(outcomes) == PER_USER + 1

    asyncio.run(scenario())

    assert lab.service.running(lab.user) == 0
    assert lab.service.running(lab.other) == 0
    assert len(lab.runs()) == PER_USER + 1


@pytest.mark.parametrize("trouble", ["busy", "error", "down"])
def test_runner_busy_or_down_is_unavailable(trouble: str) -> None:
    lab = Lab()
    match trouble:
        case "busy":
            lab.sandbox.reply(503)
        case "error":
            lab.sandbox.reply(500)
        case _:
            lab.sandbox.down = True

    with pytest.raises(RunnerUnavailable):
        asyncio.run(lab.service.run(lab.user, REQUEST))

    assert lab.service.running(lab.user) == 0
    # 코드가 돌지 않았다(이미지 없음·데몬 오류는 실행기가 500으로 답한다). 기록하지 않는다.
    assert lab.runs() == []


def test_cancelled_call_still_saves_and_frees_slot() -> None:
    lab = Lab()

    async def scenario() -> None:
        hold = asyncio.Event()
        lab.sandbox.hold = hold
        call = asyncio.create_task(lab.service.run(lab.user, REQUEST))
        await until(lambda: len(lab.sandbox.jobs) == 1)

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        # 실행은 아직 도는 중이다. 자리를 붙잡고 있고 기록은 없다.
        assert lab.service.running(lab.user) == 1
        assert lab.runs() == []

        hold.set()
        await until(lambda: lab.service.running(lab.user) == 0)

    asyncio.run(scenario())

    [row] = lab.runs()
    assert row["status"] == "ok"
