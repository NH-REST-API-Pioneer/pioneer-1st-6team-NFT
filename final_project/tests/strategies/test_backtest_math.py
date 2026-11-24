from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from nh_mcp.domain import KST, Candle, SignalName
from nh_mcp.strategies.backtest import FillTerms, simulate

TERMS = FillTerms(Decimal("0.05"), Decimal("250000"), 1, Decimal("1000"))
START = datetime(2026, 11, 2, tzinfo=KST)

type Row = tuple[str, str, SignalName]


def bars(rows: list[Row]) -> list[Candle]:
    """(시가, 종가, 신호) 행에서 봉을 만든다. 고가·저가는 시가·종가의 최대·최소."""
    out: list[Candle] = []
    for i, (open_, close, _) in enumerate(rows):
        o, c = Decimal(open_), Decimal(close)
        out.append(Candle(START + timedelta(days=i), o, max(o, c), min(o, c), c, 1))
    return out


def signals(rows: list[Row]) -> list[SignalName]:
    return [signal for _, _, signal in rows]


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
    r = simulate(bars(ROUND_TRIP), signals(ROUND_TRIP), TERMS)
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
    assert r.open_position is None
    assert r.last_signal_ignored is True


def test_trade_times_are_fill_bar_times() -> None:
    r = simulate(bars(ROUND_TRIP), signals(ROUND_TRIP), TERMS)
    assert [(t.entry_at, t.exit_at) for t in r.trades] == [
        (START + timedelta(days=1), START + timedelta(days=3)),
        (START + timedelta(days=3), START + timedelta(days=5)),
    ]


def test_position_left_open_is_marked_not_booked() -> None:
    rows: list[Row] = [("100", "100", "sell"), ("100", "99", "none")]
    r = simulate(bars(rows), signals(rows), TERMS)
    assert r.trades == [] and r.win_rate is None and r.net_pnl == Decimal("0")
    assert r.open_position is not None
    assert (r.open_position.side, r.open_position.entry_price) == ("sell", Decimal("99.95"))
    assert r.open_position.unrealized_pnl == Decimal("237500")
    assert r.open_position.entry_fee == Decimal("1000")
    assert r.open_position.mark_price == Decimal("99")
    assert r.open_position.quantity == 1
    assert r.open_position.entry_at == START + timedelta(days=1)
    assert r.last_signal_ignored is False


def test_same_direction_signal_does_nothing() -> None:
    rows: list[Row] = [
        ("100", "100", "buy"),
        ("100", "100", "buy"),
        ("100", "100", "buy"),
        ("100", "100", "none"),
    ]
    r = simulate(bars(rows), signals(rows), TERMS)
    assert r.fills == 1
    assert r.open_position is not None and r.open_position.side == "buy"
    assert r.trades == []


def test_none_on_last_bar_is_not_ignored_signal() -> None:
    rows: list[Row] = [("100", "100", "buy"), ("100", "101", "none")]
    r = simulate(bars(rows), signals(rows), TERMS)
    assert r.last_signal_ignored is False


def test_last_signal_matching_position_is_not_ignored() -> None:
    rows: list[Row] = [("100", "100", "buy"), ("100", "101", "buy")]
    r = simulate(bars(rows), signals(rows), TERMS)
    assert r.last_signal_ignored is False


def test_exit_when_flat_does_nothing() -> None:
    rows: list[Row] = [("100", "100", "exit"), ("90", "80", "exit"), ("70", "60", "none")]
    r = simulate(bars(rows), signals(rows), TERMS)
    assert r.fills == 0
    assert r.trades == [] and r.fees == Decimal("0") and r.net_pnl == Decimal("0")
    assert r.max_drawdown == Decimal("0")
    assert r.open_position is None
    assert r.last_signal_ignored is False


def test_quantity_multiplies_pnl_and_fees() -> None:
    one = simulate(bars(ROUND_TRIP), signals(ROUND_TRIP), TERMS)
    terms = FillTerms(TERMS.tick, TERMS.multiplier, 2, TERMS.fee_per_fill)
    two = simulate(bars(ROUND_TRIP), signals(ROUND_TRIP), terms)
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
    r = simulate(bars(rows), signals(rows), TERMS)
    assert len(r.trades) == 3
    assert r.win_rate == Decimal("0.3333")


def test_fewer_than_two_bars_or_length_mismatch_raises() -> None:
    rows: list[Row] = [("100", "100", "buy"), ("100", "100", "none")]
    with pytest.raises(ValueError):
        simulate(bars(rows[:1]), signals(rows[:1]), TERMS)
    with pytest.raises(ValueError):
        simulate([], [], TERMS)
    with pytest.raises(ValueError):
        simulate(bars(rows), signals(rows)[:1], TERMS)
