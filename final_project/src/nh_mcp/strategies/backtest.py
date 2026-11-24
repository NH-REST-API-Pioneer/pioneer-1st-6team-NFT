"""신호로 체결을 흉내 내고 성과를 계산한다. 신호 봉의 다음 봉 시가 ± 1틱에 체결한다."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from nh_mcp.domain import Candle, Side, SignalName

# 승률은 소수 넷째 자리까지 쓴다.
_RATE_STEP = Decimal("0.0001")
# 신호를 다음 봉에 체결하므로 봉이 하나뿐이면 체결할 수 없다.
MIN_BARS = 2


@dataclass(frozen=True)
class FillTerms:
    """체결 조건. 수수료는 체결 1건·1계약당 금액이다."""

    tick: Decimal
    multiplier: Decimal
    quantity: int
    fee_per_fill: Decimal


@dataclass(frozen=True)
class Trade:
    """끝난 거래 하나. pnl은 두 번의 수수료를 뺀 값이다."""

    side: Side
    entry_at: datetime
    entry_price: Decimal
    exit_at: datetime
    exit_price: Decimal
    pnl: Decimal


@dataclass(frozen=True)
class OpenPosition:
    """끝까지 정리되지 않은 포지션. 마지막 종가로 평가만 하고 거래로 치지 않는다."""

    side: Side
    quantity: int
    entry_at: datetime
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    entry_fee: Decimal


@dataclass(frozen=True)
class Report:
    trades: list[Trade]
    fills: int
    win_rate: Decimal | None
    net_pnl: Decimal
    max_drawdown: Decimal
    fees: Decimal
    open_position: OpenPosition | None
    last_signal_ignored: bool


@dataclass(frozen=True)
class _Entry:
    direction: int
    at: datetime
    price: Decimal


def _target(signal: SignalName, current: int) -> int:
    match signal:
        case "buy":
            return 1
        case "sell":
            return -1
        case "exit":
            return 0
        case "none":
            return current


def _side(direction: int) -> Side:
    return "buy" if direction > 0 else "sell"


def simulate(bars: list[Candle], signals: list[SignalName], terms: FillTerms) -> Report:
    """봉 i의 신호를 봉 i+1 시가에 체결한다. 마지막 봉의 신호는 체결할 봉이 없어 버린다."""
    if len(bars) != len(signals) or len(bars) < MIN_BARS:
        raise ValueError("봉과 신호는 같은 길이의 2개 이상이어야 한다")

    fee = terms.fee_per_fill * terms.quantity
    scale = terms.multiplier * terms.quantity
    trades: list[Trade] = []
    entry: _Entry | None = None
    fills = 0
    closed_pnl = Decimal(0)
    peak = Decimal(0)  # 시작 자본 0도 최고점에 든다.
    max_drawdown = Decimal(0)
    target = 0
    last_signal_ignored = False

    for i, bar in enumerate(bars):
        current = 0 if entry is None else entry.direction
        if i > 0 and target != current:
            # 살 때 시가 + 틱, 팔 때 시가 - 틱. 뒤집기는 청산과 진입이 같은 가격이다.
            if entry is not None:
                price = bar.open - terms.tick * entry.direction
                pnl = (price - entry.price) * entry.direction * scale - fee * 2
                trades.append(
                    Trade(_side(entry.direction), entry.at, entry.price, bar.at, price, pnl)
                )
                closed_pnl += pnl
                fills += 1
                entry = None
            if target != 0:
                entry = _Entry(target, bar.at, bar.open + terms.tick * target)
                fills += 1

        # 봉마다 종가에서 자본을 재고 낙폭을 갱신한다. 미청산 진입 수수료는 미리 뺀다.
        equity = closed_pnl
        if entry is not None:
            equity += (bar.close - entry.price) * entry.direction * scale - fee
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

        current = 0 if entry is None else entry.direction
        target = _target(signals[i], current)
        if i == len(bars) - 1:
            last_signal_ignored = target != current

    open_position: OpenPosition | None = None
    if entry is not None:
        mark = bars[-1].close
        open_position = OpenPosition(
            side=_side(entry.direction),
            quantity=terms.quantity,
            entry_at=entry.at,
            entry_price=entry.price,
            mark_price=mark,
            unrealized_pnl=(mark - entry.price) * entry.direction * scale,
            entry_fee=fee,
        )

    wins = sum(1 for t in trades if t.pnl > 0)
    win_rate = (
        (Decimal(wins) / Decimal(len(trades))).quantize(_RATE_STEP, rounding=ROUND_HALF_EVEN)
        if trades
        else None
    )
    return Report(
        trades=trades,
        fills=fills,
        win_rate=win_rate,
        net_pnl=closed_pnl,
        max_drawdown=max_drawdown,
        fees=fee * fills,
        open_position=open_position,
        last_signal_ignored=last_signal_ignored,
    )
