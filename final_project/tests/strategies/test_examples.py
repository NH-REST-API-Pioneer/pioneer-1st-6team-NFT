"""예제 전략: 평범한 파이썬으로 ctx를 흉내 내 판단을 보고, 두 종목 예제는 실제 격리 컨테이너로도
돌린다(이미지 빌드 장치는 격리 테스트의 것을 같이 쓴다)."""

import ast
import asyncio
import runpy
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import cast

import pytest

from nh_mcp.domain import (
    KST,
    BarEvent,
    BookEvent,
    Candle,
    JsonValue,
    MarketEvent,
    SandboxJob,
    SymbolTerms,
    TickEvent,
)
from nh_mcp.sandbox.container import run_in_container
from nh_mcp.sandbox.wire import event_to_wire
from nh_mcp.strategies.backtest import Simulator, decision_time, simulate_events
from nh_mcp.strategies.examples import (
    MEAN_REVERSION,
    MEAN_REVERSION_PARAMS,
    PAIR_SPREAD,
    PAIR_SPREAD_PARAMS,
    SCALE_IN_LIMIT,
    SCALE_IN_LIMIT_PARAMS,
    TICK_MOMENTUM,
    TICK_MOMENTUM_PARAMS,
)

type Event = dict[str, object]
type OnEvent = Callable[[Event, object], object]
SYMBOL = "A016C000"
# 격리 테스트(tests/sandbox/conftest.py)의 세션 장치: 전략 이미지를 빌드하고 이름을 준다.
sandbox_image = import_module("sandbox.conftest").sandbox_image


class Ctx:
    """하네스의 ctx 흉내. history는 정해 둔 봉을 돌려준다."""

    def __init__(self, bars: list[Event], params: dict[str, object] | None = None) -> None:
        self.params = dict[str, object](MEAN_REVERSION_PARAMS) if params is None else params
        self.positions = {SYMBOL: 0}
        self.model_path = None
        self.bars = bars
        self.asked: list[tuple[str, str]] = []

    def history(self, symbol: str, interval: str) -> list[Event]:
        self.asked.append((symbol, interval))
        return list(self.bars)


def bar(close: str) -> Event:
    c = str(Decimal(close))
    return {
        "type": "bar",
        "symbol": SYMBOL,
        "interval": "minute",
        "at": "2026-11-02T09:00:00+09:00",
        "open": c,
        "high": c,
        "low": c,
        "close": c,
        "volume": 1,
    }


def load(tmp_path: Path, source: str = MEAN_REVERSION) -> dict[str, object]:
    path = tmp_path / "strategy.py"
    path.write_text(source, encoding="utf-8")
    return runpy.run_path(str(path))


def load_on_event(tmp_path: Path, source: str = MEAN_REVERSION) -> OnEvent:
    on_event = load(tmp_path, source)["on_event"]
    assert callable(on_event)
    return cast(OnEvent, on_event)


def test_example_code_has_on_event_with_two_parameters() -> None:
    tree = ast.parse(MEAN_REVERSION)
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "on_event"]
    assert len(found) == 1
    assert [a.arg for a in found[0].args.args] == ["event", "ctx"]


def test_example_params_are_json_safe() -> None:
    assert MEAN_REVERSION_PARAMS == {"window": 20, "band": "0.02"}


def test_example_runs_on_plain_python(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path)
    # 평균 100인 봉 20개 뒤에 종가 97 → 평균보다 2% 넘게 아래라 1계약 매수가 목표
    ctx = Ctx([bar("99"), bar("101")] * 10)
    assert on_event(bar("97"), ctx) == {SYMBOL: {"position": 1}}
    assert ctx.asked == [(SYMBOL, "minute")]
    # 평균 위로 돌아오면 정리
    assert on_event(bar("100"), ctx) == {SYMBOL: {"position": 0}}
    assert on_event(bar("103"), ctx) == {SYMBOL: {"position": 0}}
    # 평균 아래지만 밴드 안이면 그대로
    assert on_event(bar("99"), ctx) is None
    # 20개 미만이면 판단하지 않는다
    assert on_event(bar("97"), Ctx(ctx.bars[:19])) is None


def test_example_ignores_fills_and_takes_quantity_from_params(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path)
    ctx = Ctx([bar("99"), bar("101")] * 10, {**MEAN_REVERSION_PARAMS, "quantity": 3})
    fill: Event = {
        "type": "fill",
        "symbol": SYMBOL,
        "at": "2026-11-02T09:01:00+09:00",
        "side": "buy",
        "qty": 3,
        "price": "97.05",
    }
    assert on_event(fill, ctx) is None
    assert on_event(bar("97"), ctx) == {SYMBOL: {"position": 3}}


class PairCtx:
    """두 종목 예제용 ctx 흉내. history는 종목마다 정해 둔 1분봉을 돌려준다(복사본)."""

    def __init__(self, params: dict[str, object], bars: dict[str, list[Event]]) -> None:
        self.params = params
        self.positions = dict.fromkeys(bars, 0)
        self.model_path = None
        self.bars = bars

    def history(self, symbol: str, interval: str) -> list[Event]:
        assert interval == "minute"
        return list(self.bars.get(symbol, []))


A, B = "A016C000", "A0173000"
PAIR: dict[str, object] = {"a": A, "b": B, "window": 4, "entry_z": "1", "exit_z": "0.5"}


def minute_bar(symbol: str, minute: int, close: str) -> Event:
    return bar(close) | {"symbol": symbol, "at": f"2026-11-02T09:{minute:02d}:00+09:00"}


def pair_bars(a_closes: list[str], b_closes: list[str]) -> dict[str, list[Event]]:
    return {
        A: [minute_bar(A, i, c) for i, c in enumerate(a_closes)],
        B: [minute_bar(B, i, c) for i, c in enumerate(b_closes)],
    }


def test_pair_example_has_init_and_on_event() -> None:
    tree = ast.parse(PAIR_SPREAD)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert [a.arg for a in functions["on_event"].args.args] == ["event", "ctx"]
    assert [a.arg for a in functions["init"].args.args] == ["ctx"]
    assert {"a", "b"} <= set(PAIR_SPREAD_PARAMS)
    assert PAIR_SPREAD_PARAMS["a"] != PAIR_SPREAD_PARAMS["b"]


@pytest.mark.parametrize("params", [{"b": B}, {"a": A}, {"a": A, "b": A}, {"a": "", "b": B}])
def test_pair_init_refuses_missing_or_same_symbols(
    tmp_path: Path, params: dict[str, object]
) -> None:
    init = load(tmp_path, PAIR_SPREAD)["init"]
    assert callable(init)

    with pytest.raises(ValueError, match='"a", "b"'):
        init(PairCtx(params, {}))


@pytest.mark.parametrize(
    ("b_last", "expected"),
    [
        # 스프레드 1, 1, 1, 3: 평균 1.5, 표준편차 0.866, z = 1.73 > 1 → a를 팔고 b를 산다
        ("97", {A: {"position": -1}, B: {"position": 1}}),
        # 1, 1, 1, -1: z = -1.73 → a를 사고 b를 판다
        ("101", {A: {"position": 1}, B: {"position": -1}}),
    ],
)
def test_pair_enters_when_the_spread_stretches(
    tmp_path: Path, b_last: str, expected: dict[str, object]
) -> None:
    on_event = load_on_event(tmp_path, PAIR_SPREAD)
    # 같은 분이면 종목코드 순으로 온다. a의 3분 봉은 이미 왔고 지금 b의 3분 봉이 왔다.
    ctx = PairCtx(PAIR, pair_bars(["100"] * 4, ["99"] * 3))

    assert on_event(minute_bar(B, 3, b_last), ctx) == expected


def test_pair_flattens_near_the_mean(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path, PAIR_SPREAD)
    # 스프레드 0, 2, 0, 1: 평균 0.75, 표준편차 0.83, z = 0.30 < 0.5 → 둘 다 정리
    ctx = PairCtx(PAIR, pair_bars(["100"] * 4, ["100", "98", "100"]))

    assert on_event(minute_bar(B, 3, "99"), ctx) == {A: {"position": 0}, B: {"position": 0}}


def test_pair_waits_for_the_other_leg_of_the_same_minute(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path, PAIR_SPREAD)
    # a의 3분 봉이 왔지만 b의 3분 봉은 아직이다.
    ctx = PairCtx(PAIR, pair_bars(["100"] * 3, ["99"] * 3))

    assert on_event(minute_bar(A, 3, "103"), ctx) is None


def test_pair_pairs_only_minutes_both_legs_traded(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path, PAIR_SPREAD)
    bars = pair_bars(["100"] * 4, ["99"] * 3)
    del bars[B][1]  # b는 1분에 거래가 없었다: 짝지은 분이 0, 2, 3뿐이라 window 4에 모자란다
    ctx = PairCtx(PAIR, bars)

    assert on_event(minute_bar(B, 3, "97"), ctx) is None


def test_pair_ignores_fills_and_daily_bars(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path, PAIR_SPREAD)
    ctx = PairCtx(PAIR, pair_bars(["100"] * 4, ["99"] * 3))
    fill: Event = {
        "type": "fill",
        "symbol": A,
        "at": "2026-11-02T09:04:00+09:00",
        "side": "sell",
        "qty": 1,
        "price": "99.95",
    }
    daily = minute_bar(B, 3, "97") | {"interval": "day"}

    assert on_event(fill, ctx) is None
    assert on_event(daily, ctx) is None


T0 = datetime(2026, 11, 2, 9, 0, tzinfo=KST)


def flat_bars(symbol: str, closes: list[str]) -> list[MarketEvent]:
    out: list[MarketEvent] = []
    for i, text in enumerate(closes):
        p = Decimal(text)
        out.append(BarEvent(symbol, "minute", Candle(T0 + timedelta(minutes=i), p, p, p, p, 1)))
    return out


@pytest.mark.docker
def test_pair_example_runs_in_the_real_container(sandbox_image: str) -> None:
    # 1분마다 a 봉, b 봉 순서(종목코드 순). b의 3분 봉에서 스프레드가 벌어져 a 숏·b 롱,
    # 4분 봉에서 평균 가까이 돌아와(|z| 0.58 < 0.6) 둘 다 정리한다.
    a_bars = flat_bars("A", ["100"] * 6)
    b_bars = flat_bars("B", ["99", "99", "99", "97", "99", "99"])
    events = [e for pair in zip(a_bars, b_bars, strict=True) for e in pair]
    terms = {s: SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("0")) for s in "AB"}
    params: dict[str, JsonValue] = {
        "a": "A",
        "b": "B",
        "window": 4,
        "entry_z": "1",
        "exit_z": "0.6",
    }
    job = SandboxJob(
        code=PAIR_SPREAD,
        params=params,
        streams={"A": frozenset({"minute"}), "B": frozenset({"minute"})},
        max_position=1,
        terms=terms,
        events=events,
    )

    reply = asyncio.run(run_in_container(job, sandbox_image, wall_seconds=8.0))

    assert reply.status == "ok", reply
    report = simulate_events(events, reply.outputs, terms)
    # 4분: a 99.95 매도, b 99.05 매수. 5분: a 100.05 매수, b 98.95 매도.
    assert [(t.symbol, t.side, t.entry_price, t.exit_price) for t in report.trades] == [
        ("A", "sell", Decimal("99.95"), Decimal("100.05")),
        ("B", "buy", Decimal("99.05"), Decimal("98.95")),
    ]
    assert len(reply.outputs) == len(events) + report.fills


# --- 틱 예제: 체결로 1분봉을 만들고 호가 쏠림을 같이 본다 -----------------------------------------

D = datetime(2026, 11, 25, tzinfo=KST)
TICK_PARAMS: dict[str, JsonValue] = {"window": 2, "imbalance": "0.2"}


def tick(hhmmss: str, price: str) -> MarketEvent:
    h, m, sec = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:])
    return TickEvent("A", D.replace(hour=h, minute=m, second=sec), Decimal(price), 1, None, None)


def quotes(hhmmss: str, bid_qty: int, ask_qty: int) -> MarketEvent:
    h, m, sec = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:])
    bids = ((Decimal("99.00"), bid_qty),)
    asks = ((Decimal("99.05"), ask_qty),)
    return BookEvent("A", D.replace(hour=h, minute=m, second=sec), bids, asks)


# 받은 순서. 09:00봉(시가 100.00, 종가 100.50)은 09:01 호가가 와서 끝나고, 09:01봉(101.00)은
# 09:02 체결이 와서 끝난다. 그때 window 2분 동안 101.00 - 100.00 = +1.00이고 호가 쏠림
# (8 - 2) / 10 = 0.6 ≥ 0.2라 1계약 매수가 목표다. 09:02봉(101.50)이 끝날 때는 올랐지만 쏠림이
# (1 - 9) / 10 = -0.8이라 그대로, 09:03봉(100.00)이 끝날 때는 100.00 - 101.50 < 0이고 쏠림
# -0.8 ≤ -0.2라 1계약 숏이 목표다.
TICK_FEED: list[MarketEvent] = [
    tick("090010", "100.00"),
    tick("090040", "100.50"),
    quotes("090100", 8, 2),
    tick("090105", "101.00"),
    tick("090200", "101.50"),
    quotes("090230", 1, 9),
    tick("090300", "100.00"),
    tick("090400", "99.00"),
    tick("090430", "98.50"),
]
TICK_DECISIONS: list[object] = [
    None,
    None,
    None,
    None,
    {"A": {"position": 1}},
    None,
    None,
    {"A": {"position": -1}},
    None,
]


def wire(event: MarketEvent) -> Event:
    """하네스가 전략에 넘기는 모양(실행기가 보내는 JSON 그대로)."""
    return dict[str, object](event_to_wire(event))


class TickCtx:
    def __init__(self, params: dict[str, JsonValue]) -> None:
        self.params = dict[str, object](params)
        self.positions = {"A": 0}
        self.model_path = None


def test_tick_example_builds_minute_bars_from_ticks_on_plain_python(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path, TICK_MOMENTUM)
    ctx = TickCtx(TICK_PARAMS)
    answers: list[object] = []
    for event in TICK_FEED:
        answers.append(on_event(wire(event), ctx))
        # 하네스가 체결로 고치는 포지션: 매수 목표는 다음 체결(09:03:00)에서 체결된다.
        if answers[-1] == {"A": {"position": 1}}:
            ctx.positions = {"A": 1}
    assert answers == TICK_DECISIONS
    fill: Event = {
        "type": "fill",
        "symbol": "A",
        "at": "2026-11-25T09:04:30+09:00",
        "side": "sell",
        "qty": 1,
        "price": "98.45",
    }
    assert on_event(fill, ctx) is None


def test_tick_example_without_books_follows_the_bars_alone(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path, TICK_MOMENTUM)
    ctx = TickCtx(TICK_PARAMS)
    only_ticks = [e for e in TICK_FEED if isinstance(e, TickEvent)]
    answers = [on_event(wire(e), ctx) for e in only_ticks]
    # 호가 쏠림을 보지 않으므로 09:02 체결에서 사고(+1.00), 09:03 체결에서도 올라 있어(+0.50)
    # 그대로 사겠다고 하고(포지션이 아직 0), 09:04 체결에서 내려(-1.50) 숏이다.
    assert answers == [
        None,
        None,
        None,
        {"A": {"position": 1}},
        {"A": {"position": 1}},
        {"A": {"position": -1}},
        None,
    ]


def test_tick_example_params_are_json_safe() -> None:
    assert TICK_MOMENTUM_PARAMS == {"window": 5, "imbalance": "0.2"}
    tree = ast.parse(TICK_MOMENTUM)
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "on_event"]
    assert [a.arg for a in found[0].args.args] == ["event", "ctx"]


@pytest.mark.docker
def test_tick_example_runs_in_the_real_container(sandbox_image: str) -> None:
    terms = {"A": SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("0"))}
    job = SandboxJob(
        code=TICK_MOMENTUM,
        params=TICK_PARAMS,
        streams={"A": frozenset({"tick", "book"})},
        max_position=1,
        terms=terms,
        events=TICK_FEED,
    )

    reply = asyncio.run(run_in_container(job, sandbox_image, wall_seconds=8.0))

    assert reply.status == "ok", reply
    report = simulate_events(TICK_FEED, reply.outputs, terms)
    # 09:02:00에 산다고 정해 다음 체결(09:03:00 100.00)에서 100.05에 산다. 09:04:00에 숏으로 정해
    # 다음 체결(09:04:30 98.50)에서 98.45에 판다(청산 1 + 숏 진입 1).
    assert [(t.side, t.entry_price, t.exit_price, t.pnl) for t in report.trades] == [
        ("buy", Decimal("100.05"), Decimal("98.45"), Decimal("-400000.00"))
    ]
    [held] = report.open_positions
    assert (held.side, held.quantity, held.entry_price, held.mark_price) == (
        "sell",
        1,
        Decimal("98.45"),
        Decimal("98.50"),
    )
    assert len(reply.outputs) == len(TICK_FEED) + report.fills == len(TICK_FEED) + 3


# --- 수량 조절·지정가 예제: 내려갈수록 한 계약씩 늘리고 평균에서 정리한다 ------------------

SCALE: dict[str, object] = {"window": 3, "step": "0.01", "max_steps": 3, "tick": "0.05"}
type Target = Callable[[int, Decimal, Decimal, Decimal, int], int]


def scale_target(tmp_path: Path) -> Target:
    found = load(tmp_path, SCALE_IN_LIMIT)["target"]
    assert callable(found)
    return cast(Target, found)


@pytest.mark.parametrize(
    ("held", "close", "expected"),
    [
        ("0", "99.5", 0),  # 평균 100에서 0.5%: 한 단계(1%)에 못 미친다
        ("0", "98.9", 1),  # 1.1%: 한 단계
        ("0", "97.5", 2),  # 2.5%: 두 단계
        ("0", "90", 3),  # 10%: max_steps에서 멈춘다
        ("2", "98.9", 2),  # 덜 내려가도 이미 산 것을 줄이지 않는다
        ("3", "100", 0),  # 평균으로 돌아오면 모두 정리
        ("3", "101", 0),
    ],
)
def test_scale_in_sizes_by_steps_below_the_mean(
    tmp_path: Path, held: str, close: str, expected: int
) -> None:
    target = scale_target(tmp_path)
    assert target(int(held), Decimal(close), Decimal(100), Decimal("0.01"), 3) == expected


class ScaleCtx:
    def __init__(self, closes: list[str], held: int = 0) -> None:
        self.params = dict(SCALE)
        self.positions = {SYMBOL: held}
        self.model_path = None
        self.bars = [bar(c) for c in closes]

    def history(self, symbol: str, interval: str) -> list[Event]:
        assert (symbol, interval) == (SYMBOL, "minute")
        return list(self.bars)


def test_scale_in_buys_one_tick_below_and_sells_one_tick_above_the_close(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path, SCALE_IN_LIMIT)
    flat = ["100", "100", "100"]

    assert on_event(bar("97.5"), ScaleCtx(flat)) == {SYMBOL: {"position": 2, "price": "97.45"}}
    assert on_event(bar("100.2"), ScaleCtx(flat, held=2)) == {
        SYMBOL: {"position": 0, "price": "100.25"}
    }


def test_scale_in_leaves_a_resting_order_alone_when_the_target_holds(tmp_path: Path) -> None:
    on_event = load_on_event(tmp_path, SCALE_IN_LIMIT)
    # 목표가 지금 포지션과 같으면 내지 않는다. 내면 걸린 주문이 취소된다.
    assert on_event(bar("98.9"), ScaleCtx(["100"] * 3, held=1)) is None
    assert on_event(bar("100"), ScaleCtx(["100"] * 3)) is None
    # 평균을 낼 만큼 봉이 쌓이기 전에는 판단하지 않는다.
    assert on_event(bar("90"), ScaleCtx(["100"] * 2)) is None
    fill: Event = {
        "type": "fill",
        "symbol": SYMBOL,
        "at": "2026-11-02T09:04:00+09:00",
        "side": "buy",
        "qty": 1,
        "price": "98.85",
    }
    assert on_event(fill, ScaleCtx(["100"] * 3)) is None


@pytest.mark.parametrize(
    "params",
    [{"window": 3}, {"tick": "0"}, {"tick": "0.05", "step": "0"}, {"tick": "0.05", "max_steps": 0}],
)
def test_scale_in_init_refuses_missing_tick_or_bad_steps(
    tmp_path: Path, params: dict[str, object]
) -> None:
    init = load(tmp_path, SCALE_IN_LIMIT)["init"]
    assert callable(init)
    ctx = ScaleCtx([])
    ctx.params = params

    with pytest.raises(ValueError, match="params"):
        init(ctx)


def test_scale_in_params_are_json_safe() -> None:
    assert SCALE_IN_LIMIT_PARAMS == {"window": 20, "step": "0.003", "max_steps": 3, "tick": "0.05"}
    tree = ast.parse(SCALE_IN_LIMIT)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert [a.arg for a in functions["on_event"].args.args] == ["event", "ctx"]
    assert [a.arg for a in functions["init"].args.args] == ["ctx"]


def ohlc(minute: int, o: str, h: str, low: str, c: str) -> MarketEvent:
    prices = (Decimal(o), Decimal(h), Decimal(low), Decimal(c))
    return BarEvent("A", "minute", Candle(T0 + timedelta(minutes=minute), *prices, 1))


# window 3, 한 단계 1%. 3분 98.9(평균 100에서 1.1%)에 1계약을 98.85에 사겠다고 걸고, 4분 저가
# 98.80이 그보다 낮아 98.85에 산다. 4분 97.0(평균 99.63에서 2.6%)에 2계약으로 늘려 96.95에 걸고,
# 5분 저가 96.90에 산다. 6분 98.8이 평균 97.47 위라 0으로 98.85에 팔겠다고 걸고, 7분 고가 99.00에
# 두 계약을 판다. 8분은 걸린 주문이 없어 아무 일도 없다.
SCALE_FEED: list[MarketEvent] = [
    ohlc(0, "100", "100", "100", "100"),
    ohlc(1, "100", "100", "100", "100"),
    ohlc(2, "100", "100", "100", "100"),
    ohlc(3, "100", "100", "98.9", "98.9"),
    ohlc(4, "98.9", "99", "98.8", "97"),
    ohlc(5, "97", "97.1", "96.9", "96.5"),
    ohlc(6, "96.5", "98.9", "96.5", "98.8"),
    ohlc(7, "98.8", "99", "98.7", "99"),
    ohlc(8, "99", "99", "98.85", "99"),
]


@pytest.mark.docker
def test_scale_in_example_fills_limit_orders_in_the_real_container(sandbox_image: str) -> None:
    terms = {"A": SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("0"))}
    params: dict[str, JsonValue] = {"window": 3, "step": "0.01", "max_steps": 3, "tick": "0.05"}
    job = SandboxJob(
        code=SCALE_IN_LIMIT,
        params=params,
        streams={"A": frozenset({"minute"})},
        max_position=3,
        terms=terms,
        events=SCALE_FEED,
    )

    reply = asyncio.run(run_in_container(job, sandbox_image, wall_seconds=8.0))

    assert reply.status == "ok", reply
    # 체결 계산을 다시 돌리며 체결과 포지션을 모은다(simulate_events와 같은 순서).
    sim = Simulator(terms)
    fills: list[tuple[str, int, Decimal]] = []
    held: list[int] = []
    outputs = iter(reply.outputs)
    for event in SCALE_FEED:
        made = sim.on_market(event)
        fills += [(f.side, f.qty, f.price) for f in made]
        held.append(sim.positions["A"])
        for fed in [*made, event]:
            sim.on_output(next(outputs), decision_time(fed))
    assert next(outputs, "end") == "end"
    assert fills == [
        ("buy", 1, Decimal("98.85")),
        ("buy", 1, Decimal("96.95")),
        ("sell", 2, Decimal("98.85")),
    ]
    assert max(held) == 2
    [trade] = sim.report().trades
    assert (trade.quantity, trade.entry_price, trade.exit_price) == (
        2,
        Decimal("97.90"),
        Decimal("98.85"),
    )
