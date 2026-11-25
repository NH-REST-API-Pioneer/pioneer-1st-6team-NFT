"""한 종목 일봉, 고정 수량의 손 계산(M3에서 옮김). 봉마다 신호대로 목표를 내는 전략을 돌린다."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fakes import scripted_outputs, signal_targets

from nh_mcp.domain import KST, BarEvent, Candle, MarketEvent, Output, SymbolTerms
from nh_mcp.strategies.backtest import EventReport, simulate_events

SYMBOL = "A"
TERMS = {SYMBOL: SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("1000"))}
START = datetime(2026, 11, 2, tzinfo=KST)

type Row = tuple[str, str, str]


def bars(rows: list[Row]) -> list[MarketEvent]:
    """(시가, 종가, 신호) 행에서 일봉을 만든다. 고가·저가는 시가·종가의 최대·최소."""
    out: list[MarketEvent] = []
    for i, (open_, close, _) in enumerate(rows):
        o, c = Decimal(open_), Decimal(close)
        candle = Candle(START + timedelta(days=i), o, max(o, c), min(o, c), c, 1)
        out.append(BarEvent(SYMBOL, "day", candle))
    return out


def outputs(rows: list[Row], quantity: int = 1) -> list[Output]:
    targets = signal_targets(SYMBOL, [signal for _, _, signal in rows], quantity)
    return scripted_outputs(bars(rows), targets, TERMS)


def simulate(rows: list[Row], quantity: int = 1) -> EventReport:
    return simulate_events(bars(rows), outputs(rows, quantity), TERMS)


# (open, close, signal)
ROUND_TRIP: list[Row] = [
    ("100", "100", "buy"),
    ("101", "102", "none"),  # 101.05에 롱 진입
    ("103", "99", "sell"),
    ("98", "97", "none"),  # 97.95에 롱 청산 + 숏 진입
    ("96", "95", "exit"),
    ("94", "94", "buy"),  # 94.05에 숏 청산. 마지막 봉의 buy는 버림
]


def test_hand_computed_round_trip() -> None:
    r = simulate(ROUND_TRIP)
    assert [t.pnl for t in r.trades] == [Decimal("-777000"), Decimal("973000")]
    assert [(t.side, t.entry_price, t.exit_price) for t in r.trades] == [
        ("buy", Decimal("101.05"), Decimal("97.95")),
        ("sell", Decimal("97.95"), Decimal("94.05")),
    ]
    assert r.fills == 4
    assert r.fees == Decimal("4000")
    assert r.net_pnl == Decimal("196000")
    assert r.win_rate == Decimal("0.5")
    assert r.max_drawdown == Decimal("777000")  # 최고 236500(봉1 종가) → 최저 -540500(봉3 종가)
    assert r.open_positions == []
    # 마지막 봉의 buy는 체결할 봉이 없어 걸린 채 남는다.
    assert [(o.symbol, o.target) for o in r.pending] == [(SYMBOL, 1)]


def test_trade_times_are_fill_bar_times() -> None:
    r = simulate(ROUND_TRIP)
    assert [(t.entry_at, t.exit_at) for t in r.trades] == [
        (START + timedelta(days=1), START + timedelta(days=3)),
        (START + timedelta(days=3), START + timedelta(days=5)),
    ]


def test_position_left_open_is_marked_not_booked() -> None:
    rows: list[Row] = [("100", "100", "sell"), ("100", "99", "none")]
    r = simulate(rows)
    assert r.trades == [] and r.win_rate is None and r.net_pnl == Decimal("0")
    [held] = r.open_positions
    assert (held.side, held.entry_price) == ("sell", Decimal("99.95"))
    assert held.unrealized_pnl == Decimal("237500")
    assert held.fees == Decimal("1000")
    assert held.mark_price == Decimal("99")
    assert held.quantity == 1
    assert held.entry_at == START + timedelta(days=1)
    assert r.pending == []


def test_same_direction_signal_does_nothing() -> None:
    rows: list[Row] = [
        ("100", "100", "buy"),
        ("100", "100", "buy"),
        ("100", "100", "buy"),
        ("100", "100", "none"),
    ]
    r = simulate(rows)
    assert r.fills == 1
    assert [h.side for h in r.open_positions] == ["buy"]
    assert r.trades == []


def test_none_on_last_bar_is_not_ignored_signal() -> None:
    rows: list[Row] = [("100", "100", "buy"), ("100", "101", "none")]
    r = simulate(rows)
    assert r.pending == []


def test_last_signal_matching_position_is_not_ignored() -> None:
    rows: list[Row] = [("100", "100", "buy"), ("100", "101", "buy")]
    r = simulate(rows)
    assert r.pending == []


def test_exit_when_flat_does_nothing() -> None:
    rows: list[Row] = [("100", "100", "exit"), ("90", "80", "exit"), ("70", "60", "none")]
    r = simulate(rows)
    assert r.fills == 0
    assert r.trades == [] and r.fees == Decimal("0") and r.net_pnl == Decimal("0")
    assert r.max_drawdown == Decimal("0")
    assert r.open_positions == []
    assert r.pending == []


def test_quantity_multiplies_pnl_and_fees() -> None:
    one = simulate(ROUND_TRIP)
    two = simulate(ROUND_TRIP, quantity=2)
    assert [t.pnl for t in two.trades] == [t.pnl * 2 for t in one.trades]
    assert two.fees == one.fees * 2
    assert two.net_pnl == one.net_pnl * 2
    assert two.max_drawdown == one.max_drawdown * 2
    assert two.fills == one.fills


def test_win_rate_is_quantized_to_four_places() -> None:
    # 세 거래 중 마지막 하나만 이긴다 → 1/3
    rows: list[Row] = [
        ("100", "100", "buy"),
        ("100", "100", "exit"),  # 100.05 → 89.95 손실
        ("90", "90", "buy"),
        ("90", "90", "exit"),  # 90.05 → 89.95 손실
        ("90", "90", "buy"),
        ("100", "100", "exit"),  # 100.05 → 109.95 이익
        ("110", "110", "none"),
    ]
    r = simulate(rows)
    assert len(r.trades) == 3
    assert r.win_rate == Decimal("0.3333")


def test_outputs_not_matching_fed_events_raise() -> None:
    # 봉0, 봉1에서 난 체결, 봉1 순으로 셋을 넣는다. 출력도 셋이어야 한다.
    rows: list[Row] = [("100", "100", "buy"), ("100", "100", "none")]
    fed = outputs(rows)
    assert len(fed) == 3
    without_fill = [fed[0], fed[2]]
    with pytest.raises(ValueError):
        simulate_events(bars(rows), without_fill, TERMS)
    with pytest.raises(ValueError):
        simulate_events(bars(rows), [*fed, None], TERMS)
    with pytest.raises(ValueError):
        simulate_events([], [None], TERMS)
