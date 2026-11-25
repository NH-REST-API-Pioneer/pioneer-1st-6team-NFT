from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from nh_mcp.domain import (
    KST,
    BarEvent,
    BookEvent,
    Candle,
    FillEvent,
    MarketEvent,
    Output,
    Side,
    StrategyEvent,
    SymbolTerms,
    Target,
    TickEvent,
)
from nh_mcp.strategies.backtest import (
    Holding,
    PendingOrder,
    Simulator,
    SymbolSummary,
    Trade,
    decision_time,
    simulate_events,
)

A, B = "A", "B"
TERMS = {
    A: SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("1000")),
    B: SymbolTerms(Decimal("1"), Decimal("10"), Decimal("2")),
}
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=KST)


def m(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def s(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def minute(symbol: str, at: int, ohlc: str) -> BarEvent:
    """ohlc는 "시가 고가 저가 종가"."""
    o, h, low, c = (Decimal(v) for v in ohlc.split())
    return BarEvent(symbol, "minute", Candle(m(at), o, h, low, c, 1))


def tick(symbol: str, at: int, price: str) -> TickEvent:
    return TickEvent(symbol, s(at), Decimal(price), 1, None, None)


def book(symbol: str, at: int, bid: str, ask: str) -> BookEvent:
    return BookEvent(symbol, s(at), ((Decimal(bid), 5),), ((Decimal(ask), 5),))


def fill(symbol: str, at: datetime, side: Side, qty: int, price: str) -> FillEvent:
    return FillEvent(symbol, at, side, qty, Decimal(price))


def limit(position: int, price: str) -> Target:
    return Target(position, Decimal(price))


# (이벤트, 그 이벤트에서 나와야 할 체결, 그 이벤트에 낸 출력)
type Step = tuple[MarketEvent, list[FillEvent], Output]


def run(steps: list[Step]) -> Simulator:
    sim = Simulator(TERMS)
    for i, (event, expected, output) in enumerate(steps):
        assert (i, sim.on_market(event)) == (i, expected)
        sim.on_output(output, decision_time(event))
    return sim


TWO_SYMBOLS: list[Step] = [
    (minute(A, 0, "100 100 100 100"), [], {B: Target(1)}),  # 09:01 판단
    (minute(B, 0, "50 50 50 50"), [], None),  # 09:00 시작이라 체결 자리가 아니다
    (minute(A, 1, "101 101 101 101"), [], None),
    (minute(B, 1, "52 56 52 55"), [fill(B, m(1), "buy", 1, "53")], None),
]


def test_decision_on_one_symbol_does_not_fill_on_another_symbols_bar_of_same_minute() -> None:
    sim = run(TWO_SYMBOLS)
    assert sim.positions == {A: 0, B: 1}
    r = sim.report()
    assert r.open_positions == [
        Holding(
            B, "buy", 1, m(1), Decimal("53"), Decimal("55"), Decimal("20"), Decimal(0), Decimal("2")
        )
    ]
    assert r.symbols == [
        SymbolSummary(A, 0, 0, Decimal(0), Decimal(0)),
        SymbolSummary(B, 0, 1, Decimal(0), Decimal("2")),
    ]


SCALE_IN: list[Step] = [
    (minute(A, 0, "100 100 100 100"), [], {A: Target(1)}),
    (minute(A, 1, "100 101 100 101"), [fill(A, m(1), "buy", 1, "100.05")], {A: Target(3)}),
    (
        minute(A, 2, "101.50 101.50 101 101"),
        [fill(A, m(2), "buy", 2, "101.55")],  # 평균 단가 303.15 / 3 = 101.05
        {A: Target(1)},
    ),
    (
        minute(A, 3, "103 103 103 103"),
        [fill(A, m(3), "sell", 2, "102.95")],
        {A: Target(0)},
    ),
    (
        minute(A, 4, "104.20 104.20 104.20 104.20"),
        [fill(A, m(4), "sell", 1, "104.15")],
        None,
    ),
]


def test_scale_in_and_partial_exit_use_weighted_average() -> None:
    # 2계약을 줄인 뒤: 확정 (102.95 - 101.05) x 2 x 250000, 남은 1계약은 평균 단가 101.05 그대로
    middle = run(SCALE_IN[:4]).report()
    assert middle.trades == []
    assert middle.open_positions == [
        Holding(
            A,
            "buy",
            1,
            m(1),
            entry_price=Decimal("101.05"),
            mark_price=Decimal("103"),
            unrealized_pnl=Decimal("487500"),
            realized_pnl=Decimal("950000"),
            fees=Decimal("5000"),
        )
    ]

    r = run(SCALE_IN).report()
    # 청산 평균 (2 x 102.95 + 104.15) / 3 = 103.35. (310.05 - 303.15) x 250000 - 수수료 6계약
    assert r.trades == [
        Trade(A, "buy", 3, m(1), Decimal("101.05"), m(4), Decimal("103.35"), Decimal("1719000"))
    ]
    assert (r.fills, r.fees, r.net_pnl) == (4, Decimal("6000"), Decimal("1719000"))
    # 봉 끝 자본: 0, 0, 236500, -40500, 1432500, 1719000
    assert r.max_drawdown == Decimal("277000")
    assert r.open_positions == [] and r.pending == []


FLIP: list[Step] = [
    (minute(A, 0, "100 100 100 100"), [], {A: Target(1)}),
    (
        minute(A, 1, "100 100 100 100"),
        [fill(A, m(1), "buy", 1, "100.05")],
        {A: Target(-2)},
    ),
    (
        minute(A, 2, "99 99 98 98"),
        [fill(A, m(2), "sell", 1, "98.95"), fill(A, m(2), "sell", 2, "98.95")],
        {A: Target(0)},
    ),
    (minute(A, 3, "97 97 97 97"), [fill(A, m(3), "buy", 2, "97.05")], None),
]


def test_flip_is_close_then_open_at_same_price() -> None:
    r = run(FLIP).report()
    assert r.trades == [
        Trade(A, "buy", 1, m(1), Decimal("100.05"), m(2), Decimal("98.95"), Decimal("-277000")),
        Trade(A, "sell", 2, m(2), Decimal("98.95"), m(3), Decimal("97.05"), Decimal("946000")),
    ]
    assert (r.fills, r.fees, r.net_pnl, r.win_rate) == (
        4,
        Decimal("6000"),
        Decimal("669000"),
        Decimal("0.5"),
    )
    assert r.symbols[0] == SymbolSummary(A, 2, 4, Decimal("669000"), Decimal("6000"))


LIMIT: list[Step] = [
    (minute(A, 0, "100 100 100 100"), [], {A: limit(1, "99")}),
    (minute(A, 1, "100 100.50 99 99.50"), [], None),  # 저가 = 지정가: 닿기만 함
    (
        minute(A, 2, "99.50 99.50 98.95 99.20"),
        [fill(A, m(2), "buy", 1, "99")],  # 저가 < 지정가: 틱 없이 지정가에
        {A: limit(0, "101")},
    ),
    (minute(A, 3, "99.50 100.50 99 100"), [], {A: limit(0, "102")}),  # 101을 102로 바꿈
    (minute(A, 4, "101 101.50 100.50 101"), [], None),  # 101이었다면 체결됐을 봉
]


def test_limit_fills_only_when_crossed_and_new_target_cancels_resting_order() -> None:
    sim = run(LIMIT)
    r = sim.report()
    assert r.fills == 1
    assert r.pending == [PendingOrder(A, 0, Decimal("102"), m(4))]
    assert r.open_positions == [
        Holding(
            A,
            "buy",
            1,
            m(2),
            Decimal("99"),
            Decimal("101"),
            Decimal("500000"),
            Decimal(0),
            Decimal("1000"),
        )
    ]
    # 지금 포지션과 같은 목표는 걸린 주문을 취소만 한다.
    sim.on_output({A: Target(1)}, m(5))
    assert sim.report().pending == []


TICKS: list[Step] = [
    (tick(A, 0, "100"), [], {A: Target(1)}),
    (book(A, 0, "100.20", "100.25"), [], None),  # 호가로는 체결하지 않는다
    (tick(A, 0, "100.10"), [fill(A, s(0), "buy", 1, "100.15")], {A: limit(0, "100.50")}),
    (tick(A, 1, "100.50"), [], None),  # 지정가에 닿기만 함
    (book(A, 1, "100.60", "100.65"), [], None),  # 호가가 지정가를 넘어도 체결하지 않는다
    (tick(A, 2, "100.55"), [fill(A, s(2), "sell", 1, "100.50")], None),
]


def test_tick_run_fills_at_next_tick_and_never_on_book() -> None:
    r = run(TICKS).report()
    assert r.trades == [
        Trade(A, "buy", 1, s(0), Decimal("100.15"), s(2), Decimal("100.50"), Decimal("85500"))
    ]


END: list[Step] = [
    (tick(A, 0, "100"), [], {A: Target(-1)}),
    (tick(A, 1, "100"), [fill(A, s(1), "sell", 1, "99.95")], None),
    (tick(B, 2, "50"), [], {B: limit(2, "49")}),
    (tick(A, 3, "101"), [], {A: limit(0, "95")}),
    (book(A, 4, "80", "80.05"), [], None),  # 호가는 평가 가격을 바꾸지 않는다
]


def test_orders_left_at_end_are_pending_and_positions_marked_at_last_trade() -> None:
    r = run(END).report()
    assert r.pending == [
        PendingOrder(A, 0, Decimal("95"), s(3)),
        PendingOrder(B, 2, Decimal("49"), s(2)),
    ]
    # 숏 1계약: (101 - 99.95) x -250000
    assert r.open_positions == [
        Holding(
            A,
            "sell",
            1,
            s(1),
            Decimal("99.95"),
            Decimal("101"),
            Decimal("-262500"),
            Decimal(0),
            Decimal("1000"),
        )
    ]
    assert (r.trades, r.net_pnl, r.fees, r.win_rate) == ([], Decimal(0), Decimal("1000"), None)
    assert r.max_drawdown == Decimal("263500")


REPLAY_EVENTS: list[MarketEvent] = [
    minute(A, 0, "100 100 100 100"),
    minute(A, 1, "100 100 100 100"),
    minute(B, 1, "50 50 50 50"),
    minute(B, 2, "60 60 60 60"),
]
REPLAY_OUTPUTS: list[Output] = [
    {A: Target(1)},  # A 09:00
    {B: Target(1)},  # A 09:01 시가 체결. 판단 시각은 체결 시각 09:01
    None,  # A 09:01
    None,  # B 09:01 시가 체결
    None,  # B 09:01
    None,  # B 09:02
]


def test_replay_feeds_fill_outputs_before_their_event() -> None:
    r = simulate_events(REPLAY_EVENTS, REPLAY_OUTPUTS, TERMS)
    # 체결에 낸 B 주문은 09:01 판단이라 B 09:01 봉(50 + 1틱)에 체결된다. 09:02 봉이면 61이었다.
    assert [(h.symbol, h.entry_price) for h in r.open_positions] == [
        (A, Decimal("100.05")),
        (B, Decimal("51")),
    ]
    assert r.fills == 2


@pytest.mark.parametrize("outputs", [REPLAY_OUTPUTS[:-1], [*REPLAY_OUTPUTS, None], []])
def test_replay_rejects_outputs_that_do_not_line_up(outputs: list[Output]) -> None:
    with pytest.raises(ValueError):
        simulate_events(REPLAY_EVENTS, outputs, TERMS)


def test_fills_come_back_with_positions_already_updated() -> None:
    sim = Simulator(TERMS)
    sim.on_output({A: Target(-3)}, s(0))
    assert sim.positions == {A: 0, B: 0}
    assert sim.on_market(tick(A, 0, "100")) == [fill(A, s(0), "sell", 3, "99.95")]
    assert sim.positions == {A: -3, B: 0}


DAY_START = datetime(2026, 11, 2, tzinfo=KST)
ONE = Decimal(1)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (minute(A, 0, "1 1 1 1"), m(1)),
        (
            BarEvent(A, "day", Candle(DAY_START, ONE, ONE, ONE, ONE, 1)),
            DAY_START + timedelta(days=1),
        ),
        (tick(A, 5, "1"), s(5)),
        (book(A, 5, "1", "1.05"), s(5)),
        (fill(A, s(7), "buy", 1, "1"), s(7)),
    ],
)
def test_decision_time(event: StrategyEvent, expected: datetime) -> None:
    assert decision_time(event) == expected


def test_symbol_without_terms_is_rejected() -> None:
    sim = Simulator(TERMS)
    with pytest.raises(ValueError):
        sim.on_market(tick("C", 0, "1"))
    with pytest.raises(ValueError):
        sim.on_output({"C": Target(1)}, s(0))
