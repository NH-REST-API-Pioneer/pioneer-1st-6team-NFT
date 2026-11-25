"""백테스트 서비스: 흐름마다 봉을 모아 시간 순서로 섞어 실행기에 보내고, 출력으로 성과를 다시
계산하고, 실행을 남긴다.

NH는 가짜(차트 행은 명세 필드 이름으로 만든다), 실행기도 가짜, 시계는 2026-12-01 10:00 KST.
"""

import asyncio
import base64
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
from fakes import SETTINGS, T0, FakeNH, FakeSandbox, World, answering, scripted

from nh_mcp.domain import (
    KST,
    BarEvent,
    Candle,
    ModelHash,
    Output,
    SandboxJob,
    Stream,
    Streams,
    Target,
    UserId,
)
from nh_mcp.markets.domestic import CHART_DAILY, CHART_MINUTE, CHART_PAGE_ROWS, DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL, DomesticProducts
from nh_mcp.nh.client import Credentials
from nh_mcp.sandbox import executor
from nh_mcp.sandbox.client import SandboxClient
from nh_mcp.sandbox.wire import event_to_wire
from nh_mcp.services.backtest import (
    MAX_BARS,
    MAX_CODE_BYTES,
    MAX_EVENTS,
    MAX_PARAMS_BYTES,
    MAX_SYMBOLS,
    MAX_TICK_EVENTS,
    MIXED,
    NO_MODEL,
    PER_USER,
    BacktestRequest,
    BacktestService,
    RunnerUnavailable,
    Sources,
    StrategyFailed,
    StrategyInvalid,
    code_hash,
    coverage_data,
    params_key,
    report_data,
    streams_key,
)
from nh_mcp.services.queries import KeyMissing, QueryService, UnknownSymbol
from nh_mcp.store import ticks, users
from nh_mcp.store.ticks import LEVELS, Block, Row, Sub

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"  # 코스피200 F 202612: 틱 0.05(product-detail), 승수 250000(마스터)
MINI = "A056C000"  # 미니코스피 F 202612: 틱 0.02, 승수 50000
SPREAD = "D016C73S"  # 코스피200 스프레드
STOCK_FUTURE = "A0A6C000"  # 주식선물: 틱 표 대응이 없다
CODE = "def on_event(event, ctx):\n    return None\n"
PARAMS: dict[str, Any] = {"window": 3, "band": "0.02"}
DAY: Streams = {SYMBOL: frozenset({"day"})}
REQUEST = BacktestRequest(
    code=CODE,
    params=PARAMS,
    streams=DAY,
    period=(date(2026, 11, 2), date(2026, 11, 30)),
    max_position=1,
    fee_per_fill=Decimal("1000"),
)


def streams(**chosen: list[Stream]) -> Streams:
    """streams(A016C000=["day"]) 꼴로 흐름을 만든다."""
    return {symbol: frozenset(names) for symbol, names in chosen.items()}


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


def minute_row(at: datetime, price: str) -> dict[str, str]:
    """시가=고가=저가=종가인 1분봉. at은 봉이 시작한 KST 시각."""
    return {
        "occurs1_ref_dt": at.strftime("%Y%m%d"),
        "occurs1_exch_tm": at.strftime("%H%M%S"),
        "occurs1_open_pric": price,
        "occurs1_high_pric": price,
        "occurs1_low_pric": price,
        "occurs1_close_pric": price,
        "occurs1_cum_trd_qty": "10",
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


def minute_history(end: date, count: int) -> list[dict[str, str]]:
    """end 날부터 거꾸로 하루 411개(08:45~15:35)씩 count개의 1분봉. NH처럼 최근 것부터."""
    rows: list[dict[str, str]] = []
    day = end
    while len(rows) < count:
        opened = datetime(day.year, day.month, day.day, 8, 45, tzinfo=KST)
        left = min(BARS_PER_DAY, count - len(rows))
        rows += [
            minute_row(opened + timedelta(minutes=m), "780.00")
            for m in reversed(range(BARS_PER_DAY - left, BARS_PER_DAY))
        ]
        day -= timedelta(days=1)
    return rows


def row_at(row: dict[str, str]) -> datetime:
    stamp = row["occurs1_ref_dt"] + row["occurs1_exch_tm"]
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=KST)


BARS_PER_DAY = 411


class ChartHistory:
    """NH처럼 차트에 답한다(Q26): inq_strt_dt~inq_ed_dt 안에서 가장 최근 것부터 한 번에
    CHART_PAGE_ROWS행까지. 끝 날짜를 무시하게 할 수도 있다. 다른 요청은 FakeNH가 받는다.
    """

    def __init__(self, nh: FakeNH) -> None:
        self.nh = nh
        # (경로, 종목) → 행(최근 것부터).
        self.rows: dict[tuple[str, str], list[dict[str, str]]] = {}
        self.ignore_end = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path not in (CHART_MINUTE, CHART_DAILY):
            return self.nh(request)
        payload: dict[str, str] = json.loads(request.content)
        rows = self.rows.get((request.url.path, payload.get("sym", "")))
        if rows is None:
            return self.nh(request)
        self.nh.requests.append((request.url.path, dict[str, object](payload)))
        start, end = payload["inq_strt_dt"], payload["inq_ed_dt"]
        inside = [
            r
            for r in rows
            if start <= r["occurs1_ref_dt"] and (self.ignore_end or r["occurs1_ref_dt"] <= end)
        ]
        page = CHART_PAGE_ROWS["minute" if request.url.path == CHART_MINUTE else "day"]
        return httpx2.Response(200, content=json.dumps(occurs(inside[:page])).encode())


def failed_reply(status: str, detail: str = "", index: int | None = 1) -> dict[str, object]:
    return {"status": status, "outputs": [], "index": index, "detail": detail}


class Lab:
    """가짜 NH·실행기에 붙은 백테스트 서비스. 두 사용자 모두 키를 등록했다."""

    def __init__(
        self, product_detail: dict[str, Any] | None = None, data_dir: Path | None = None
    ) -> None:
        self.nh = FakeNH()
        detail = recorded("product-detail") if product_detail is None else product_detail
        self.nh.reply(PRODUCT_DETAIL, detail)
        self.nh.reply(CHART_DAILY, occurs(rows_from(date(2026, 11, 2), PRICES)))
        self.sandbox = FakeSandbox()
        self.chart = ChartHistory(self.nh)
        self.world = World(
            nh=httpx2.MockTransport(self.chart), sandbox=self.sandbox, data_dir=data_dir
        )
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
        # 사용자가 저장한 체결·호가. 수신기 대신 테스트가 묶음을 직접 쓴다.
        self.ticks = ticks.TickStore(ticks.connect(":memory:"))
        sources = Sources(
            QueryService(domestic),
            DomesticProducts(self.world.pool, clock),
            self.world.models,
            lambda: self.ticks,
        )
        self.service = BacktestService(self.conn, clock, sources, client)

    def upload(self, user: UserId, content: bytes) -> ModelHash:
        draft = self.world.models.draft(user)
        draft.write(content)
        assert asyncio.run(self.world.models.commit(user, draft, "model.pkl")) == "uploaded"
        return ModelHash(hashlib.sha256(content).hexdigest())

    def bars(self, rows: list[dict[str, str]]) -> None:
        self.nh.replies[CHART_DAILY] = [(200, occurs(rows))]

    def history(self, path: str, symbol: str, rows: list[dict[str, str]]) -> None:
        """이 종목은 NH처럼 기간·끝 날짜를 보고 한 번에 최대 행 수까지만 준다."""
        self.chart.rows[(path, symbol)] = rows

    def bars_for(self, path: str, symbol: str, rows: list[dict[str, str]]) -> None:
        self.nh.reply_for(path, symbol, occurs(rows))

    def runs(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        rows: list[sqlite3.Row] = self.conn.execute(
            "SELECT * FROM backtest_runs ORDER BY id"
        ).fetchall()
        self.conn.row_factory = None
        return rows

    def charts(self) -> int:
        return self.nh.paths().count(CHART_DAILY) + self.nh.paths().count(CHART_MINUTE)

    def sent(self) -> list[tuple[str, str, str]]:
        """실행기에 넣은 이벤트의 (종목, 간격, 봉 시작 시각) 순서."""
        [job] = self.sandbox.jobs
        events = cast(list[dict[str, str]], job["events"])
        return [(e["symbol"], e["interval"], e["at"]) for e in events]


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("조건이 끝내 맞지 않았다")


SIGNALS = ["buy", "none", "sell", "exit", "buy"]


def amounts(data: dict[str, Any]) -> dict[str, Any]:
    """금액 문자열을 Decimal로 바꿔 소수 자릿수 표기와 상관없이 비교한다."""
    keys = ("net_pnl", "max_drawdown", "fees", "pnl", "entry_price", "exit_price")
    return {k: Decimal(v) if k in keys else v for k, v in data.items()}


def test_feeds_bars_and_computes_report() -> None:
    lab = Lab()
    lab.sandbox.reply(scripted(SIGNALS))

    outcome = asyncio.run(lab.service.run(lab.user, REQUEST))

    # 11-03 782.05 매수 → 11-05 783.95에 뒤집어 매도(+473000) → 11-06 785.55 정리(-402000).
    # 마지막 봉의 buy는 체결할 봉이 없어 걸린 채 끝난다. 낙폭은 11-03 종가 779.50에서 -638500.
    data = report_data(outcome.report)
    assert amounts(cast(dict[str, Any], data["totals"])) == {
        "trades": 2,
        "fills": 4,
        "win_rate": "0.5000",
        "net_pnl": Decimal("71000"),
        "max_drawdown": Decimal("638500"),
        "fees": Decimal("4000"),
    }
    [summary] = cast(dict[str, dict[str, Any]], data["symbols"]).values()
    assert amounts(summary) == {
        "trades": 2,
        "fills": 4,
        "net_pnl": Decimal("71000"),
        "fees": Decimal("4000"),
    }
    trades = [amounts(t) for t in cast(list[dict[str, Any]], data["trades"])]
    assert [(t["side"], t["entry_price"], t["exit_price"], t["pnl"]) for t in trades] == [
        ("buy", Decimal("782.05"), Decimal("783.95"), Decimal("473000")),
        ("sell", Decimal("783.95"), Decimal("785.55"), Decimal("-402000")),
    ]
    assert data["open_positions"] == []
    assert data["pending_orders"] == [
        {"symbol": SYMBOL, "position": 1, "price": None, "decided_at": "2026-11-07T00:00:00+09:00"}
    ]
    candles = candles_from(date(2026, 11, 2), PRICES)
    [job] = lab.sandbox.jobs
    assert job == {
        "code": CODE,
        "params": PARAMS,
        "streams": {SYMBOL: ["day"]},
        "max_position": 1,
        "terms": {SYMBOL: {"tick": "0.05", "multiplier": "250000", "fee_per_fill": "1000"}},
        "events": [event_to_wire(BarEvent(SYMBOL, "day", c)) for c in candles],
        "model": None,
    }
    coverage = outcome.feed.coverage[(SYMBOL, "day")]
    assert (coverage.events, coverage.truncated) == (len(PRICES), False)
    assert (coverage.first_at, coverage.last_at) == (candles[0].at, candles[-1].at)
    assert outcome.feed.warnings == []
    assert outcome.code_hash == code_hash(CODE)
    assert lab.service.running(lab.user) == 0


def test_max_position_and_fee_reach_the_fill_terms() -> None:
    lab = Lab()
    lab.sandbox.reply(scripted(SIGNALS))
    request = replace(REQUEST, max_position=3, fee_per_fill=Decimal("0"))

    outcome = asyncio.run(lab.service.run(lab.user, request))

    [job] = lab.sandbox.jobs
    assert job["max_position"] == 3
    assert job["terms"] == {SYMBOL: {"tick": "0.05", "multiplier": "250000", "fee_per_fill": "0"}}
    # 수량 3, 수수료 0: 두 거래의 틱 차이 (1.90 - 1.60)에 승수 250000, 3계약
    totals = amounts(cast(dict[str, Any], report_data(outcome.report)["totals"]))
    assert (totals["net_pnl"], totals["fees"], totals["fills"]) == (Decimal("225000"), 0, 4)


def test_each_symbol_gets_its_own_tick_and_multiplier() -> None:
    lab = Lab()
    request = replace(REQUEST, streams=streams(A056C000=["day"], A016C000=["day"]))

    asyncio.run(lab.service.run(lab.user, request))

    [job] = lab.sandbox.jobs
    assert job["streams"] == {SYMBOL: ["day"], MINI: ["day"]}
    assert job["terms"] == {
        SYMBOL: {"tick": "0.05", "multiplier": "250000", "fee_per_fill": "1000"},
        MINI: {"tick": "0.02", "multiplier": "50000", "fee_per_fill": "1000"},
    }


def kst(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=KST)


def test_daily_bar_comes_after_the_minute_bars_of_its_day() -> None:
    """판단 시각(봉이 끝난 시각) 순서. 일봉은 다음 날 0시에 끝나 그날 1분봉 뒤에 온다.

    23:59 1분봉도 다음 날 0시에 끝나는데, 판단 시각이 같으면 일봉이 먼저다.
    """
    lab = Lab()
    minutes = [kst(3, 9, 0), kst(2, 23, 59), kst(2, 15, 44), kst(2, 9, 0)]
    lab.bars_for(CHART_MINUTE, SYMBOL, [minute_row(at, "781.00") for at in minutes])
    lab.bars(rows_from(date(2026, 11, 2), PRICES[:2]))
    request = replace(
        REQUEST,
        streams=streams(A016C000=["minute", "day"]),
        period=(date(2026, 11, 2), date(2026, 11, 3)),
    )

    asyncio.run(lab.service.run(lab.user, request))

    assert lab.sent() == [
        (SYMBOL, "minute", "2026-11-02T09:00:00+09:00"),
        (SYMBOL, "minute", "2026-11-02T15:44:00+09:00"),
        (SYMBOL, "day", "2026-11-02T00:00:00+09:00"),
        (SYMBOL, "minute", "2026-11-02T23:59:00+09:00"),
        (SYMBOL, "minute", "2026-11-03T09:00:00+09:00"),
        (SYMBOL, "day", "2026-11-03T00:00:00+09:00"),
    ]


@pytest.mark.parametrize(
    "chosen",
    [
        streams(A016C000=["minute", "day"], A056C000=["day", "minute"]),
        streams(A056C000=["minute", "day"], A016C000=["day", "minute"]),
    ],
)
def test_two_symbols_interleave_by_minute_then_symbol(chosen: Streams) -> None:
    lab = Lab()
    for symbol in (SYMBOL, MINI):
        rows = [minute_row(at, "781.00") for at in (kst(2, 9, 1), kst(2, 9, 0))]
        lab.bars_for(CHART_MINUTE, symbol, rows)
    lab.bars(rows_from(date(2026, 11, 2), PRICES[:1]))
    request = replace(REQUEST, streams=chosen, period=(date(2026, 11, 2), date(2026, 11, 2)))

    asyncio.run(lab.service.run(lab.user, request))

    assert lab.sent() == [
        (SYMBOL, "minute", "2026-11-02T09:00:00+09:00"),
        (MINI, "minute", "2026-11-02T09:00:00+09:00"),
        (SYMBOL, "minute", "2026-11-02T09:01:00+09:00"),
        (MINI, "minute", "2026-11-02T09:01:00+09:00"),
        (SYMBOL, "day", "2026-11-02T00:00:00+09:00"),
        (MINI, "day", "2026-11-02T00:00:00+09:00"),
    ]


def test_streams_key_ignores_the_order_they_were_written_in() -> None:
    written = {MINI: frozenset[Stream](["minute", "day"]), SYMBOL: frozenset[Stream](["day"])}

    assert streams_key(written) == '{"A016C000":["day"],"A056C000":["day","minute"]}'
    assert streams_key(dict(reversed(written.items()))) == streams_key(written)


def test_same_inputs_give_the_same_result_and_the_same_stored_key() -> None:
    lab = Lab()
    lab.sandbox.reply(scripted(SIGNALS))

    first = asyncio.run(lab.service.run(lab.user, REQUEST))
    second = asyncio.run(lab.service.run(lab.user, REQUEST))

    assert report_data(first.report) == report_data(second.report)
    keys = [(r["streams"], r["code_hash"], r["params"], r["model_hash"]) for r in lab.runs()]
    assert keys == [('{"A016C000":["day"]}', code_hash(CODE), params_key(PARAMS), None)] * 2


def test_reordered_streams_are_stored_under_one_key() -> None:
    lab = Lab()
    lab.bars_for(CHART_MINUTE, SYMBOL, [minute_row(kst(2, 9, 0), "781.00")])
    one = streams(A016C000=["minute", "day"], A056C000=["day"])
    other = streams(A056C000=["day"], A016C000=["day", "minute"])

    for chosen in (one, other):
        asyncio.run(lab.service.run(lab.user, replace(REQUEST, streams=chosen)))

    assert {r["streams"] for r in lab.runs()} == {
        '{"A016C000":["day","minute"],"A056C000":["day"]}'
    }


def test_hash_and_params_key_follow_the_global_rule() -> None:
    assert code_hash("가 b") == hashlib.sha256("가 b".encode()).hexdigest()
    # 정규화하지 않는다: 끝의 줄바꿈 하나도 다른 해시다.
    assert code_hash(CODE) != code_hash(CODE.rstrip())
    assert params_key({"b": "가", "a": [1, True, None]}) == '{"a":[1,true,null],"b":"가"}'


def test_run_is_saved_with_code_streams_and_no_model() -> None:
    lab = Lab()
    lab.sandbox.reply(scripted(SIGNALS))

    outcome = asyncio.run(lab.service.run(lab.user, REQUEST))

    [row] = lab.runs()
    assert row["user_id"] == lab.user
    assert (row["market"], row["streams"], row["model_hash"]) == (
        "domestic",
        '{"A016C000":["day"]}',
        None,
    )
    assert row["code"] == CODE
    assert row["code_hash"] == code_hash(CODE) == hashlib.sha256(CODE.encode()).hexdigest()
    assert row["params"] == params_key(PARAMS) == '{"band":"0.02","window":3}'
    assert row["status"] == "ok"
    assert (row["requested_start"], row["requested_end"]) == ("2026-11-02", "2026-11-30")
    assert row["bar_count"] == len(PRICES)
    assert json.loads(row["settings"]) == {"max_position": 1, "fee_per_fill": "1000"}
    result = json.loads(row["result"])
    assert result["totals"]["net_pnl"] == str(outcome.report.net_pnl)
    assert result["totals"]["trades"] == len(outcome.report.trades)


@pytest.mark.parametrize("extra", [[None], []])
def test_outputs_not_matching_the_events_are_unavailable(extra: list[None]) -> None:
    """실행기가 넣은 이벤트와 줄 수가 다른 출력을 주면 믿지 않는다(코드가 돈 것으로 보지 않는다)."""
    lab = Lab()
    outputs = [None] * (len(PRICES) + 1) if extra else [{SYMBOL: {"position": 1}}] * len(PRICES)
    lab.sandbox.reply({"status": "ok", "outputs": outputs, "index": None, "detail": ""})

    with pytest.raises(RunnerUnavailable):
        asyncio.run(lab.service.run(lab.user, REQUEST))

    assert lab.runs() == []
    assert lab.service.running(lab.user) == 0


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


@pytest.mark.parametrize("status", ["invalid_code", "invalid_output"])
def test_bad_code_or_output_is_invalid_input(status: str) -> None:
    lab = Lab()
    lab.sandbox.reply(failed_reply(status, detail="on_event가 없습니다", index=None))

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, REQUEST))

    # detail은 전략 코드가 만들 수 있다(프로토콜 출구에 거짓 줄을 쓰는 것도). 그렇다고 적는다.
    assert caught.value.message.endswith(" 전략 코드가 낸 내용: on_event가 없습니다")
    assert "실행기가 알린" not in caught.value.message
    [row] = lab.runs()
    assert (row["status"], row["result"]) == (status, None)


@pytest.mark.parametrize("status", ["invalid_code", "invalid_output"])
def test_without_detail_only_the_server_sentence_is_shown(status: str) -> None:
    lab = Lab()
    lab.sandbox.reply(failed_reply(status, detail="", index=None))

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, REQUEST))

    assert "전략 코드가 낸 내용" not in caught.value.message
    assert caught.value.message.endswith("다.")


def test_bad_output_message_states_the_new_contract() -> None:
    lab = Lab()
    lab.sandbox.reply(failed_reply("invalid_output", detail='"buy"', index=2))

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, REQUEST))

    message = caught.value.message
    assert "3번째 이벤트에서" in message
    for word in ('"position"', '"price"', "streams", "settings.max_position", "None"):
        assert word in message, word


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


def test_a_stream_without_bars_is_data_short_and_named() -> None:
    lab = Lab()
    lab.bars_for(CHART_DAILY, MINI, [])
    request = replace(REQUEST, streams=streams(A016C000=["day"], A056C000=["day"]))

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, request))

    assert "데이터" in caught.value.message and f"{MINI} 일봉" in caught.value.message
    assert SYMBOL not in caught.value.message
    assert lab.sandbox.jobs == []
    [row] = lab.runs()
    assert (row["status"], row["bar_count"]) == ("data_short", len(PRICES))


END = date(2026, 11, 30)
MINUTE = streams(A016C000=["minute"])


@pytest.mark.parametrize(
    ("count", "kept", "truncated"),
    [
        (MAX_BARS - 1, MAX_BARS - 1, False),
        (MAX_BARS, MAX_BARS, True),
        (MAX_BARS + 500, MAX_BARS, True),
    ],
)
def test_minute_bars_up_to_the_cap_reach_the_executor(
    count: int, kept: int, truncated: bool
) -> None:
    """2000봉 넘게 받아 다 넣는다. 상한을 넘으면 가장 최근 MAX_BARS개만 받고 알린다."""
    lab = Lab()
    rows = minute_history(END, count)
    lab.history(CHART_MINUTE, SYMBOL, rows)
    request = replace(REQUEST, streams=MINUTE, period=(row_at(rows[-1]).date(), END))

    outcome = asyncio.run(lab.service.run(lab.user, request))

    coverage = outcome.feed.coverage[(SYMBOL, "minute")]
    assert (coverage.events, coverage.truncated, outcome.feed.truncated) == (
        kept,
        truncated,
        truncated,
    )
    assert any(f"{MAX_BARS:,}개만 받았습니다" in w for w in outcome.feed.warnings) is truncated
    assert not any("끝까지 받지 못했습니다" in w for w in outcome.feed.warnings)
    sent = lab.sent()
    assert len(sent) == kept
    assert datetime.fromisoformat(sent[0][2]) == row_at(rows[kept - 1])
    assert datetime.fromisoformat(sent[-1][2]) == row_at(rows[0])
    # 한 번에 2000행까지라 끝 날짜를 옮기며 여러 번 받았다.
    assert lab.charts() > count // CHART_PAGE_ROWS["minute"]


def test_nh_stopping_short_of_the_start_is_reported_not_filled() -> None:
    lab = Lab()
    rows = minute_history(END, 5000)
    lab.history(CHART_MINUTE, SYMBOL, rows)
    # 끝 날짜를 옮겨도 늘 같은 최근 2000행이 온다.
    lab.chart.ignore_end = True
    request = replace(REQUEST, streams=MINUTE, period=(row_at(rows[-1]).date(), END))

    outcome = asyncio.run(lab.service.run(lab.user, request))

    coverage = outcome.feed.coverage[(SYMBOL, "minute")]
    page = CHART_PAGE_ROWS["minute"]
    assert (coverage.events, coverage.truncated) == (page, True)
    first = row_at(rows[page - 1]).date().isoformat()
    assert any(
        "NH에서 기간 앞쪽을 끝까지 받지 못했습니다" in w and first in w and SYMBOL in w
        for w in outcome.feed.warnings
    )
    assert len(lab.sent()) == page
    assert lab.charts() == 2


def test_all_streams_together_keep_only_the_latest_events() -> None:
    """흐름마다 15,000봉, 합쳐 30,000봉이면 가장 최근 25,000개(종목마다 12,500개)만 넣는다."""
    lab = Lab()
    rows = minute_history(END, 15_000)
    for symbol in (SYMBOL, MINI):
        lab.history(CHART_MINUTE, symbol, rows)
    request = replace(
        REQUEST,
        streams=streams(A016C000=["minute"], A056C000=["minute"]),
        period=(row_at(rows[-1]).date(), END),
    )

    outcome = asyncio.run(lab.service.run(lab.user, request))

    assert len(outcome.feed.events) == MAX_EVENTS == 25_000
    for symbol in (SYMBOL, MINI):
        coverage = outcome.feed.coverage[(symbol, "minute")]
        assert (coverage.events, coverage.truncated) == (12_500, True)
        assert coverage.first_at == row_at(rows[12_499])
    assert any("가장 최근 25,000개만 넣었습니다" in w for w in outcome.feed.warnings)
    assert len(lab.sent()) == MAX_EVENTS


def test_event_caps_are_within_what_the_executor_accepts() -> None:
    assert MAX_EVENTS <= executor.MAX_EVENTS
    assert MAX_TICK_EVENTS <= executor.MAX_EVENTS


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

    assert any(LATE in w and SYMBOL in w for w in outcome.feed.warnings) is warns
    assert outcome.feed.coverage[(SYMBOL, "day")].first_at == datetime(
        first_bar.year, first_bar.month, first_bar.day, tzinfo=KST
    )


def test_not_a_future_is_invalid_and_named() -> None:
    lab = Lab()
    request = replace(REQUEST, streams=streams(A016C000=["day"], D016C73S=["day"]))

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, request))

    assert SPREAD in caught.value.message
    assert lab.charts() == 0
    assert lab.sandbox.jobs == []


def test_unknown_symbol_never_reaches_nh() -> None:
    lab = Lab()
    request = replace(REQUEST, streams=streams(A016C000=["day"], A9999999=["day"]))

    with pytest.raises(UnknownSymbol):
        asyncio.run(lab.service.run(lab.user, request))

    assert lab.nh.requests == []
    assert lab.service.running(lab.user) == 0


@pytest.mark.parametrize("case", ["empty_tick_table", "no_tick_for_product"])
def test_missing_tick_blocks(case: str) -> None:
    if case == "empty_tick_table":
        lab, symbol = Lab(product_detail={"nrec": "0000", "Occurs1": []}), SYMBOL
    else:
        lab, symbol = Lab(), STOCK_FUTURE

    request = replace(REQUEST, streams={symbol: frozenset[Stream](["day"])})
    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, request))

    assert "틱" in caught.value.message and symbol in caught.value.message
    assert lab.sandbox.jobs == []
    assert lab.service.running(lab.user) == 0


def test_key_missing_is_reported() -> None:
    lab = Lab()

    with pytest.raises(KeyMissing):
        asyncio.run(lab.service.run(lab.keyless, REQUEST))

    assert lab.sandbox.jobs == []
    assert lab.service.running(lab.keyless) == 0


SIX = {f"A016C00{i}": frozenset[Stream](["day"]) for i in range(MAX_SYMBOLS + 1)}


@pytest.mark.parametrize(
    "change",
    [
        {"code": "x" * (MAX_CODE_BYTES + 1)},
        {"code": "가" * (MAX_CODE_BYTES // 3 + 1)},  # 글자 수는 작아도 UTF-8 바이트로 잰다
        {"code": "   "},
        {"params": {"blob": "x" * MAX_PARAMS_BYTES}},
        {"streams": {}},
        {"streams": {SYMBOL: frozenset[Stream]()}},
        {"streams": SIX},
        {"max_position": 0},
        {"max_position": 101},
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


@pytest.mark.parametrize(
    "chosen",
    [
        streams(A016C000=["minute", "tick"]),
        streams(A016C000=["minute", "book", "day"]),
        streams(A016C000=["tick"], A056C000=["minute"]),
    ],
)
def test_minute_bars_with_ticks_or_books_are_refused_with_the_reason(chosen: Streams) -> None:
    lab = Lab()

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, replace(REQUEST, streams=chosen)))

    assert caught.value.message == MIXED
    # 왜 안 되는지와 무엇을 하면 되는지를 같이 말한다.
    assert "몇 초 뒤" in MIXED and "체결로 만드세요" in MIXED
    assert "strategy://examples/tick-momentum" in MIXED
    assert lab.nh.requests == []
    assert lab.runs() == []


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


def test_my_model_reaches_the_executor_and_keys_the_run(tmp_path: Path) -> None:
    lab = Lab(data_dir=tmp_path)
    mine = lab.upload(lab.user, b"my trained model")

    asyncio.run(lab.service.run(lab.user, replace(REQUEST, model=mine)))
    asyncio.run(lab.service.run(lab.user, REQUEST))

    with_model, without = lab.sandbox.jobs
    assert base64.b64decode(cast(str, with_model["model"])) == b"my trained model"
    assert without["model"] is None
    assert [(r["model_hash"], r["status"]) for r in lab.runs()] == [(mine, "ok"), (None, "ok")]


def test_another_users_or_unknown_model_is_never_sent(tmp_path: Path) -> None:
    lab = Lab(data_dir=tmp_path)
    mine = lab.upload(lab.user, b"my model")
    theirs = lab.upload(lab.other, b"their model")
    unknown = ModelHash("0" * 64)

    for sha256 in (theirs, unknown):
        with pytest.raises(StrategyInvalid) as caught:
            asyncio.run(lab.service.run(lab.user, replace(REQUEST, model=sha256)))
        assert caught.value.message == NO_MODEL
    # 막힌 실행은 NH 봉도 받지 않고 실행기에 아무것도 보내지 않고 기록도 남기지 않는다.
    assert lab.charts() == 0
    assert lab.sandbox.jobs == []
    assert lab.runs() == []
    assert lab.service.running(lab.user) == 0

    asyncio.run(lab.service.run(lab.user, replace(REQUEST, model=mine)))
    asyncio.run(lab.service.run(lab.other, replace(REQUEST, model=theirs)))

    sent = [base64.b64decode(cast(str, job["model"])) for job in lab.sandbox.jobs]
    assert sent == [b"my model", b"their model"]


# --- 틱 실행: 그 사용자가 저장한 체결·호가(+ 일봉) ---------------------------------------------

TICKING = streams(A016C000=["tick", "book"])
D24, D25, D26 = date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)


def at(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=KST)


def sec(hour: int, minute: int, second: int = 0) -> int:
    """거래소 시각(HHMMSS)을 저장 형식(그날 0시부터 초)으로."""
    return hour * 3600 + minute * 60 + second


def trade(seq: int, second: int, price: int, bid: int | None = None, ask: int | None = None) -> Row:
    """체결 한 행. 가격은 저장 형식(1/100 단위 정수)이다."""
    return (seq, second, price, 1, bid, ask, 1, None)


def book(seq: int, second: int, bids: list[tuple[int, int]], asks: list[tuple[int, int]]) -> Row:
    """호가 한 행. 단계마다 (가격, 잔량), 나머지 단계는 NH처럼 가격·잔량 0이다."""
    cells: list[int | None] = [seq, second, sum(q for _, q in asks), 9, sum(q for _, q in bids), 9]
    for level in range(LEVELS):
        ask = asks[level] if level < len(asks) else (0, 0)
        bid = bids[level] if level < len(bids) else (0, 0)
        cells += [ask[0], ask[1], 1, bid[0], bid[1], 1]
    return tuple(cells)


def keep(lab: Lab, user: UserId, sub: Sub, minute: datetime, rows: list[Row]) -> None:
    """수신기처럼 받은 분의 묶음 하나를 쓴다."""
    lab.ticks.write(user, [Block(sub, minute, rows)], [], minute)


def tick_request(
    chosen: Streams = TICKING, period: tuple[date, date] = (D25, D25)
) -> BacktestRequest:
    return replace(REQUEST, streams=chosen, period=period)


def fed(lab: Lab, index: int = 0) -> list[dict[str, Any]]:
    """실행기에 넣은 이벤트(본문 그대로)."""
    return cast(list[dict[str, Any]], lab.sandbox.jobs[index]["events"])


def by_index(targets: dict[int, Output]) -> Callable[[SandboxJob], list[Output]]:
    """시장 이벤트 순번마다 정해 둔 목표를 내는 전략. 나머지는 None."""
    return lambda job: [targets.get(i) for i in range(len(job.events))]


def test_ticks_and_books_go_in_receive_order_across_symbols_and_streams() -> None:
    """받은 순번(seq) 그대로 섞는다. 거래소 시각이 앞 이벤트보다 이르면 앞 이벤트의 시각이 된다."""
    lab = Lab()
    ten = at(D25, 10, 0)
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        ten,
        [trade(1, sec(10, 0, 0), 72600), trade(5, sec(10, 0, 3), 72605)],
    )
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "book"),
        ten,
        [book(2, sec(10, 0, 2), [(72595, 3)], [(72600, 4)])],
    )
    # 미니는 거래소 시각이 10:00:01이지만 10:00:02 호가 뒤에 받았다.
    keep(
        lab,
        lab.user,
        Sub(MINI, "tick"),
        ten,
        [trade(3, sec(10, 0, 1), 72000), trade(6, sec(10, 0, 4), 72002)],
    )
    keep(
        lab, lab.user, Sub(MINI, "book"), ten, [book(4, sec(10, 0, 3), [(71998, 1)], [(72002, 2)])]
    )
    chosen = streams(A016C000=["tick", "book"], A056C000=["book", "tick"])

    outcome = asyncio.run(lab.service.run(lab.user, tick_request(chosen)))

    assert [(e["type"], e["symbol"], e["at"][11:19]) for e in fed(lab)] == [
        ("tick", SYMBOL, "10:00:00"),
        ("book", SYMBOL, "10:00:02"),
        ("tick", MINI, "10:00:02"),
        ("book", MINI, "10:00:03"),
        ("tick", SYMBOL, "10:00:03"),
        ("tick", MINI, "10:00:04"),
    ]
    assert lab.sandbox.jobs[0]["streams"] == {SYMBOL: ["book", "tick"], MINI: ["book", "tick"]}
    coverage = outcome.feed.coverage
    assert set(coverage) == {(s, k) for s in (SYMBOL, MINI) for k in ("tick", "book")}
    assert (coverage[(MINI, "tick")].events, coverage[(MINI, "tick")].first_at) == (
        2,
        at(D25, 10, 0, 2),
    )
    assert outcome.feed.warnings == []
    assert not outcome.feed.truncated


def test_prices_come_out_exactly_and_books_stop_at_the_first_empty_level() -> None:
    lab = Lab()
    ten = at(D25, 10, 0)
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        ten,
        [trade(1, sec(10, 0), 72615, bid=72610), trade(3, sec(10, 0, 1), -5)],
    )
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "book"),
        ten,
        [book(2, sec(10, 0), [(72610, 3), (72605, 7)], [(72615, 2)])],
    )

    asyncio.run(lab.service.run(lab.user, tick_request()))

    assert fed(lab) == [
        {
            "type": "tick",
            "symbol": SYMBOL,
            "at": "2026-11-25T10:00:00+09:00",
            "price": "726.15",
            "qty": 1,
            "bid": "726.10",
            "ask": None,
        },
        {
            "type": "book",
            "symbol": SYMBOL,
            "at": "2026-11-25T10:00:00+09:00",
            "bids": [["726.10", 3], ["726.05", 7]],
            "asks": [["726.15", 2]],
        },
        {
            "type": "tick",
            "symbol": SYMBOL,
            "at": "2026-11-25T10:00:01+09:00",
            "price": "-0.05",
            "qty": 1,
            "bid": None,
            "ask": None,
        },
    ]


def test_a_day_bar_comes_before_the_next_recorded_days_first_event_never_the_same_day() -> None:
    lab = Lab()
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        at(D24, 9, 0),
        [trade(1, sec(9, 0), 72600), trade(2, sec(9, 0, 5), 72605)],
    )
    # 11-25에는 저장한 것이 없다(저장을 꺼 두었다).
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        at(D26, 9, 0),
        [trade(3, sec(9, 0), 72700), trade(4, sec(9, 0, 5), 72705)],
    )
    three = PRICES[:3]
    lab.bars_for(CHART_DAILY, SYMBOL, rows_from(D24, three))
    lab.bars_for(CHART_DAILY, MINI, rows_from(D24, three))
    chosen = streams(A016C000=["tick", "day"], A056C000=["day"])

    outcome = asyncio.run(lab.service.run(lab.user, tick_request(chosen, (D24, D26))))

    assert [(e["type"], e["symbol"], e["at"]) for e in fed(lab)] == [
        ("tick", SYMBOL, "2026-11-24T09:00:00+09:00"),
        ("tick", SYMBOL, "2026-11-24T09:00:05+09:00"),
        ("bar", SYMBOL, "2026-11-24T00:00:00+09:00"),
        ("bar", MINI, "2026-11-24T00:00:00+09:00"),
        ("bar", SYMBOL, "2026-11-25T00:00:00+09:00"),
        ("bar", MINI, "2026-11-25T00:00:00+09:00"),
        ("tick", SYMBOL, "2026-11-26T09:00:00+09:00"),
        ("tick", SYMBOL, "2026-11-26T09:00:05+09:00"),
    ]
    # 11-26 일봉은 그날 뒤에 이벤트가 없어 넣지 않는다. 잘린 것이 아니다.
    day = outcome.feed.coverage[(SYMBOL, "day")]
    assert (day.events, day.last_at, day.truncated) == (2, at(D25, 0, 0), False)
    assert outcome.feed.warnings == []
    # 일봉은 NH에서 받았다. 1분봉은 받지 않는다.
    assert CHART_MINUTE not in lab.nh.paths()


def test_a_day_bar_with_no_later_event_is_warned_not_refused() -> None:
    lab = Lab()
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        at(D25, 9, 0),
        [trade(1, sec(9, 0), 72600), trade(2, sec(9, 1), 72605)],
    )
    lab.bars_for(CHART_DAILY, SYMBOL, rows_from(D25, PRICES[:1]))

    outcome = asyncio.run(
        lab.service.run(lab.user, tick_request(streams(A016C000=["tick", "day"])))
    )

    assert [e["type"] for e in fed(lab)] == ["tick", "tick"]
    assert outcome.feed.coverage[(SYMBOL, "day")].events == 0
    assert any(f"{SYMBOL} 일봉: 넣은 일봉이 없습니다" in w for w in outcome.feed.warnings)


@pytest.mark.parametrize("recorded", [None, "book"])
def test_a_stream_with_nothing_recorded_is_refused_with_how_to_record(recorded: str | None) -> None:
    lab = Lab()
    if recorded == "book":
        keep(
            lab,
            lab.user,
            Sub(SYMBOL, "book"),
            at(D25, 10, 0),
            [book(1, sec(10, 0), [(72600, 1)], [])],
        )
    # 기간 밖(11-24)에 저장한 체결은 쓰지 않는다.
    keep(lab, lab.user, Sub(SYMBOL, "tick"), at(D24, 10, 0), [trade(2, sec(10, 0), 72600)])

    with pytest.raises(StrategyInvalid) as caught:
        asyncio.run(lab.service.run(lab.user, tick_request()))

    message = caught.value.message
    assert f"{SYMBOL} 체결(tick)" in message
    assert (f"{SYMBOL} 호가(book)" in message) is (recorded is None)
    assert "set_tick_recording" in message and "get_strategy_data" in message
    assert lab.sandbox.jobs == []
    [row] = lab.runs()
    assert (row["status"], row["streams"]) == ("data_short", '{"A016C000":["book","tick"]}')
    assert lab.service.running(lab.user) == 0


def test_one_users_ticks_never_reach_another_users_backtest() -> None:
    lab = Lab()
    ten = at(D25, 10, 0)
    keep(
        lab,
        lab.other,
        Sub(SYMBOL, "tick"),
        ten,
        [trade(1, sec(10, 0), 72600), trade(2, sec(10, 0, 1), 72605)],
    )
    keep(
        lab,
        lab.other,
        Sub(SYMBOL, "book"),
        ten,
        [book(3, sec(10, 0, 2), [(72600, 1)], [(72605, 1)])],
    )

    with pytest.raises(StrategyInvalid):
        asyncio.run(lab.service.run(lab.user, tick_request()))
    assert lab.sandbox.jobs == []

    asyncio.run(lab.service.run(lab.other, tick_request()))
    assert [e["type"] for e in fed(lab)] == ["tick", "tick", "book"]


def test_a_tick_run_without_a_key_is_key_missing() -> None:
    lab = Lab()
    keep(lab, lab.keyless, Sub(SYMBOL, "tick"), at(D25, 10, 0), [trade(1, sec(10, 0), 72600)])

    with pytest.raises(KeyMissing):
        asyncio.run(lab.service.run(lab.keyless, tick_request(streams(A016C000=["tick"]))))

    assert lab.sandbox.jobs == []
    assert lab.service.running(lab.keyless) == 0


def test_gaps_overlapping_the_period_are_listed_per_stream_and_warned() -> None:
    lab = Lab()
    tick_sub, book_sub = Sub(SYMBOL, "tick"), Sub(SYMBOL, "book")
    keep(lab, lab.user, Sub(SYMBOL, "tick"), at(D25, 9, 0), [trade(1, sec(9, 0), 72600)])
    keep(lab, lab.user, Sub(SYMBOL, "book"), at(D25, 9, 0), [book(2, sec(9, 0), [(72600, 1)], [])])
    # 기간 전에 끝난 빈 구간은 빼고, 기간 안에 끝난 것 25개와 아직 이어지는 것 하나.
    lab.ticks.open_gap(lab.user, tick_sub, at(D24, 10, 0), "disconnect")
    lab.ticks.close_gap(lab.user, tick_sub, at(D24, 10, 5))
    for n in range(25):
        lab.ticks.open_gap(lab.user, tick_sub, at(D25, 10, n), "disconnect")
        lab.ticks.close_gap(lab.user, tick_sub, at(D25, 10, n, 30))
    lab.ticks.open_gap(lab.user, tick_sub, at(D25, 14, 0), "crash")
    # 전날 시작해 기간 안까지 이어진 호가 빈 구간.
    lab.ticks.open_gap(lab.user, book_sub, at(D24, 15, 0), "server_stop")
    lab.ticks.close_gap(lab.user, book_sub, at(D25, 8, 50))

    outcome = asyncio.run(lab.service.run(lab.user, tick_request()))

    coverage = cast(dict[str, Any], coverage_data(outcome.feed))[SYMBOL]
    tick_gaps = coverage["tick"]["gaps"]
    assert tick_gaps["total"] == 26
    assert len(tick_gaps["recent"]) == 20
    assert tick_gaps["recent"][0] == {
        "start": "2026-11-25T10:06:00+09:00",
        "end": "2026-11-25T10:06:30+09:00",
        "reason": "disconnect",
    }
    assert tick_gaps["recent"][-1] == {
        "start": "2026-11-25T14:00:00+09:00",
        "end": "진행 중",
        "reason": "crash",
    }
    assert coverage["book"]["gaps"] == {
        "total": 1,
        "recent": [
            {
                "start": "2026-11-24T15:00:00+09:00",
                "end": "2026-11-25T08:50:00+09:00",
                "reason": "server_stop",
            }
        ],
    }
    assert coverage["tick"] == coverage["tick"] | {
        "first_event_at": "2026-11-25T09:00:00+09:00",
        "last_event_at": "2026-11-25T09:00:00+09:00",
        "events": 1,
        "truncated": False,
    }
    gap_warnings = [w for w in outcome.feed.warnings if "빈 구간" in w]
    assert len(gap_warnings) == 2
    assert any(f"{SYMBOL} 체결(tick)" in w and "26개" in w for w in gap_warnings)
    assert all("지어내지 않았습니다" in w for w in gap_warnings)
    # 빈 구간을 채운 이벤트는 없다.
    assert len(fed(lab)) == 2


def test_over_the_cap_the_most_recent_events_go_in_and_it_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab = Lab()
    monkeypatch.setattr("nh_mcp.services.backtest.MAX_TICK_EVENTS", 4)
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        at(D24, 9, 0),
        [trade(1, sec(9, 0), 72600), trade(2, sec(9, 0, 1), 72601)],
    )
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        at(D25, 9, 0),
        [trade(3, sec(9, 0), 72602), trade(5, sec(9, 0, 2), 72604)],
    )
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "book"),
        at(D25, 9, 0),
        [book(4, sec(9, 0, 1), [(72600, 1)], []), book(6, sec(9, 0, 3), [(72600, 2)], [])],
    )
    lab.bars_for(CHART_DAILY, SYMBOL, rows_from(D24, PRICES[:2]))
    chosen = streams(A016C000=["tick", "book", "day"])

    outcome = asyncio.run(lab.service.run(lab.user, tick_request(chosen, (D24, D25))))

    # 가장 최근 4개: 11-25의 체결 2개·호가 2개. 그 앞의 일봉(11-24)과 11-24 체결은 빠진다.
    assert [(e["type"], e["at"][:19]) for e in fed(lab)] == [
        ("tick", "2026-11-25T09:00:00"),
        ("book", "2026-11-25T09:00:01"),
        ("tick", "2026-11-25T09:00:02"),
        ("book", "2026-11-25T09:00:03"),
    ]
    coverage = outcome.feed.coverage
    assert (coverage[(SYMBOL, "tick")].events, coverage[(SYMBOL, "tick")].truncated) == (2, True)
    assert (coverage[(SYMBOL, "book")].events, coverage[(SYMBOL, "book")].truncated) == (2, False)
    assert coverage[(SYMBOL, "day")].truncated is True
    assert outcome.feed.truncated
    assert any("6개라 일봉을 합쳐 가장 최근 4개만" in w for w in outcome.feed.warnings)


def test_same_recorded_data_gives_the_same_run_and_the_same_key() -> None:
    lab = Lab()
    ten = at(D25, 10, 0)
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        ten,
        [trade(1, sec(10, 0), 72600), trade(3, sec(10, 0, 2), 72610)],
    )
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "book"),
        ten,
        [book(2, sec(10, 0, 1), [(72600, 1)], [(72605, 1)])],
    )
    lab.sandbox.reply(answering(by_index({0: {SYMBOL: Target(1)}})))

    first = asyncio.run(lab.service.run(lab.user, tick_request()))
    second = asyncio.run(lab.service.run(lab.user, tick_request()))

    assert report_data(first.report) == report_data(second.report)
    assert first.report.fills == 1
    assert fed(lab, 0) == fed(lab, 1)
    keys = [(r["streams"], r["code_hash"], r["status"]) for r in lab.runs()]
    assert keys == [('{"A016C000":["book","tick"]}', code_hash(CODE), "ok")] * 2


# 손 계산 틱 실행(코스피200 F: 틱 0.05, 승수 250,000, 수수료 체결 1건·1계약 1,000원).
# 순번 이벤트           전략 출력                   체결
# 0  체결 10:00:00 726.00  시장가 +1
# 1  호가 10:00:01                                  (호가로는 체결하지 않는다)
# 2  체결 10:00:02 726.10  0으로, 지정가 726.30       시장가 매수 726.10 + 0.05 = 726.15
# 3  체결 10:00:03 726.30                          (지정가에 닿기만 함: 체결 아님)
# 4  호가 10:00:04
# 5  체결 10:00:05 726.35                          지정가 매도 726.30 (넘어섰다)
# 6  체결 10:00:06 726.20  시장가 -1
# 7  체결 10:00:06 726.20                          시장가 매도 726.20 - 0.05 = 726.15 (같은 초)
# 끝난 거래: (726.30 - 726.15) * 250,000 = 37,500 - 수수료 2,000 = 35,500.
# 남은 숏 1: 726.15에 진입, 마지막 체결가 726.20 → 평가 -12,500, 수수료 1,000.
# 자본: 0, 0, -13,500, 36,500, 36,500, 35,500, 35,500, 22,000 → 최대 낙폭 36,500 - 22,000 = 14,500.
HAND_TARGETS: dict[int, Output] = {
    0: {SYMBOL: Target(1)},
    2: {SYMBOL: Target(0, Decimal("726.30"))},
    6: {SYMBOL: Target(-1)},
}


def test_a_hand_computed_tick_backtest_through_the_service() -> None:
    lab = Lab()
    minute = at(D25, 10, 0)
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "tick"),
        minute,
        [
            trade(1, sec(10, 0, 0), 72600),
            trade(3, sec(10, 0, 2), 72610),
            trade(4, sec(10, 0, 3), 72630),
            trade(6, sec(10, 0, 5), 72635),
            trade(7, sec(10, 0, 6), 72620),
            trade(8, sec(10, 0, 6), 72620),
        ],
    )
    keep(
        lab,
        lab.user,
        Sub(SYMBOL, "book"),
        minute,
        [
            book(2, sec(10, 0, 1), [(72600, 5)], [(72605, 5)]),
            book(5, sec(10, 0, 4), [(72630, 5)], [(72635, 5)]),
        ],
    )
    lab.sandbox.reply(answering(by_index(HAND_TARGETS)))

    outcome = asyncio.run(lab.service.run(lab.user, tick_request()))

    data = report_data(outcome.report)
    assert amounts(cast(dict[str, Any], data["totals"])) == {
        "trades": 1,
        "fills": 3,
        "win_rate": "1.0000",
        "net_pnl": Decimal("35500"),
        "max_drawdown": Decimal("14500"),
        "fees": Decimal("3000"),
    }
    assert data["trades"] == [
        {
            "symbol": SYMBOL,
            "side": "buy",
            "quantity": 1,
            "entry_at": "2026-11-25T10:00:02+09:00",
            "entry_price": "726.15",
            "exit_at": "2026-11-25T10:00:05+09:00",
            "exit_price": "726.30",
            "pnl": "35500.00",
        }
    ]
    [held] = cast(list[dict[str, Any]], data["open_positions"])
    money = ("entry_price", "mark_price", "unrealized_pnl", "realized_pnl", "fees")
    assert {k: Decimal(v) if k in money else v for k, v in held.items()} == {
        "symbol": SYMBOL,
        "side": "sell",
        "quantity": 1,
        "entry_at": "2026-11-25T10:00:06+09:00",
        "entry_price": Decimal("726.15"),
        "mark_price": Decimal("726.20"),
        "unrealized_pnl": Decimal("-12500"),
        "realized_pnl": Decimal("0"),
        "fees": Decimal("1000"),
    }
    assert data["pending_orders"] == []
    [run] = lab.runs()
    assert (run["status"], run["bar_count"]) == ("ok", 8)
    assert (run["first_bar_at"], run["last_bar_at"]) == (
        "2026-11-25T01:00:00.000000+00:00",
        "2026-11-25T01:00:06.000000+00:00",
    )
