"""옵션 묶음의 구조 판정, 최대 손실, 다리 순서, 다음 다리 한계 가격(`docs/options.md` "묶음 주문").

최대 손실의 기대값은 `options.md` 표의 공식으로 손으로 계산했다. 승수는 250000원이다.
매수형 수직 = 낸 순프리미엄, 매도형 수직 = 행사가 차이 - 받은 순프리미엄,
스트래들·스트랭글 매수 = 낸 프리미엄 합계, 매도 = 한정되지 않음.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from builders import LIMITS_WITH_OPTIONS, OPTION_LIMITS, SESSION

from nh_mcp.domain import (
    ComboPlan,
    ComboRequest,
    Contract,
    ContractKind,
    InstrumentRules,
    Leg,
    MaxLoss,
    RiskLimits,
    Side,
    TickBand,
    Unbounded,
)
from nh_mcp.guard.combo import (
    COMBO_LOSS_LIMIT,
    COMBO_UNSUPPORTED,
    classify,
    evaluate_combo,
    leg_limit_price,
    max_loss,
    plan,
)
from nh_mcp.guard.risk import LIMITS_NOT_SET, OPTION_SELL_OFF, OPTIONS_OFF

EXPIRY = date(2026, 12, 10)
MULTIPLIER = Decimal("250000")
# 코스피200 옵션 호가 단위: 10 미만 0.01, 10 이상 0.05.
LADDER = (TickBand(Decimal(0), Decimal("0.01")), TickBand(Decimal(10), Decimal("0.05")))


def option(kind: ContractKind, strike: str | None, expiry: date = EXPIRY) -> Contract:
    """코스피200 월물 옵션 하나. 다른 칸은 `replace`로 바꾼다."""
    code = f"{kind[0].upper()}{strike}{expiry:%y%m}"
    return Contract(
        "domestic",
        code,
        code,
        "KOSPI",
        kind,
        expiry,
        MULTIPLIER,
        None if strike is None else Decimal(strike),
    )


C350, C355 = option("call", "350"), option("call", "355")
P345, P350, P355 = option("put", "345"), option("put", "350"), option("put", "355")

type LegSpec = tuple[Contract, Side, str]


def combo(
    first: LegSpec, second: LegSpec, quantity: int = 1, slippage: str = "0"
) -> tuple[ComboRequest, tuple[Contract, Contract]]:
    legs = (
        Leg(first[0].symbol, first[1], Decimal(first[2])),
        Leg(second[0].symbol, second[1], Decimal(second[2])),
    )
    return ComboRequest("domestic", legs, quantity, Decimal(slippage)), (first[0], second[0])


def rules(contract: Contract, ladder: tuple[TickBand, ...] = LADDER) -> InstrumentRules:
    return InstrumentRules(contract, ladder, SESSION)


# ---- 구조와 최대 손실 ------------------------------------------------------------

STRUCTURES: list[tuple[str, LegSpec, LegSpec, str, MaxLoss]] = [
    # 350 콜을 5.00에 사고 355 콜을 2.00에 판다. 낸 순프리미엄 3.00 * 250000 = 750000
    (
        "debit_call",
        (C350, "buy", "5.00"),
        (C355, "sell", "2.00"),
        "debit_vertical",
        Decimal(750000),
    ),
    # 355 풋을 5.00에 사고 350 풋을 2.00에 판다. 낸 순프리미엄 3.00 * 250000 = 750000
    ("debit_put", (P355, "buy", "5.00"), (P350, "sell", "2.00"), "debit_vertical", Decimal(750000)),
    # 350 콜을 5.00에 팔고 355 콜을 2.00에 산다. (5 - 3.00) * 250000 = 500000
    (
        "credit_call",
        (C350, "sell", "5.00"),
        (C355, "buy", "2.00"),
        "credit_vertical",
        Decimal(500000),
    ),
    # 355 풋을 5.00에 팔고 350 풋을 2.00에 산다. (5 - 3.00) * 250000 = 500000
    (
        "credit_put",
        (P355, "sell", "5.00"),
        (P350, "buy", "2.00"),
        "credit_vertical",
        Decimal(500000),
    ),
    # 350 콜 4.00 + 350 풋 3.00을 산다. 7.00 * 250000 = 1750000
    (
        "long_straddle",
        (C350, "buy", "4.00"),
        (P350, "buy", "3.00"),
        "long_straddle",
        Decimal(1750000),
    ),
    # 345 풋 2.00 + 355 콜 3.00을 산다. 5.00 * 250000 = 1250000
    (
        "long_strangle",
        (P345, "buy", "2.00"),
        (C355, "buy", "3.00"),
        "long_strangle",
        Decimal(1250000),
    ),
    (
        "short_straddle",
        (C350, "sell", "4.00"),
        (P350, "sell", "3.00"),
        "short_straddle",
        Unbounded(),
    ),
    (
        "short_strangle",
        (P345, "sell", "2.00"),
        (C355, "sell", "3.00"),
        "short_strangle",
        Unbounded(),
    ),
]


@pytest.mark.parametrize(
    ("first", "second", "structure", "loss"),
    [case[1:] for case in STRUCTURES],
    ids=[case[0] for case in STRUCTURES],
)
def test_each_structure_has_its_hand_computed_max_loss(
    first: LegSpec, second: LegSpec, structure: str, loss: MaxLoss
) -> None:
    request, contracts = combo(first, second)

    p = plan(request, contracts)

    assert p is not None
    assert (p.structure, p.max_loss) == (structure, loss)
    assert classify(request, contracts) == structure


def test_net_debit_is_buys_minus_sells_and_negative_for_credit() -> None:
    debit = plan(*combo((C350, "buy", "5.00"), (C355, "sell", "2.00"), slippage="0.10"))
    credit = plan(*combo((C350, "sell", "5.00"), (C355, "buy", "2.00"), slippage="0.10"))

    assert debit is not None and credit is not None
    assert (debit.net_debit, debit.worst_net_debit) == (Decimal("3.00"), Decimal("3.10"))
    assert (credit.net_debit, credit.worst_net_debit) == (Decimal("-3.00"), Decimal("-2.90"))


def test_vertical_is_named_by_strike_not_by_price() -> None:
    """낮은 행사가 콜을 사면 값이 더 싸도 매수형이다. 순가격을 받고 시작해 손실이 없다."""
    request, contracts = combo((C350, "buy", "1.00"), (C355, "sell", "2.00"))

    p = plan(request, contracts)

    assert p is not None
    assert (p.structure, p.net_debit, p.max_loss) == ("debit_vertical", Decimal("-1.00"), 0)


BOUNDED = [case for case in STRUCTURES if not isinstance(case[4], Unbounded)]


@pytest.mark.parametrize(
    ("first", "second"), [case[1:3] for case in BOUNDED], ids=[case[0] for case in BOUNDED]
)
def test_slippage_raises_max_loss_by_slippage_times_quantity_times_multiplier(
    first: LegSpec, second: LegSpec
) -> None:
    exact = plan(*combo(first, second, quantity=2))
    loose = plan(*combo(first, second, quantity=2, slippage="0.10"))

    assert exact is not None and loose is not None
    assert isinstance(exact.max_loss, Decimal) and isinstance(loose.max_loss, Decimal)
    # 0.10 * 2계약 * 250000 = 50000
    assert loose.max_loss - exact.max_loss == Decimal(50000)


def test_quantity_scales_max_loss() -> None:
    p = plan(*combo((C350, "buy", "5.00"), (C355, "sell", "2.00"), quantity=3))

    assert p is not None
    # 3.00 * 3계약 * 250000
    assert p.max_loss == Decimal(2250000)


def test_short_structure_stays_unbounded_with_slippage() -> None:
    p = plan(*combo((C350, "sell", "4.00"), (P350, "sell", "3.00"), slippage="0.50"))

    assert p is not None
    assert p.max_loss == Unbounded()


def test_max_loss_reads_the_payoff_not_the_structure_table() -> None:
    """표가 받지 않는 다리도 만기 손익으로 계산한다. 다리를 더할 때 표에 줄만 더하면 된다."""
    # 풋 사고 콜 팖: 오를수록 콜 매도 손실이 끝없이 커진다.
    rising = combo((P350, "buy", "3.00"), (C355, "sell", "2.00"))
    # 콜 사고 풋 팖, 순가격 1.00을 받음: S = 0에서 풋 매도 손실 350 - 1.00 = 349.00 * 250000
    falling = combo((C355, "buy", "2.00"), (P350, "sell", "3.00"))

    assert max_loss(*rising, Decimal("1.00")) == Unbounded()
    assert max_loss(*falling, Decimal("-1.00")) == Decimal(87250000)


# ---- 받지 않는 묶음 --------------------------------------------------------------

REJECTED: list[tuple[str, tuple[ComboRequest, tuple[Contract, Contract]]]] = [
    (
        "different_expiry",
        combo((C350, "buy", "5.00"), (option("call", "355", date(2027, 1, 14)), "sell", "2.00")),
    ),
    (
        "different_underlying",
        combo((C350, "buy", "5.00"), (replace(C355, underlying="KOSDAQ150"), "sell", "2.00")),
    ),
    (
        "different_multiplier",
        combo(
            (C350, "buy", "5.00"),
            (replace(C355, multiplier=Decimal(50000)), "sell", "2.00"),
        ),
    ),
    ("same_symbol_twice", combo((C350, "buy", "5.00"), (C350, "sell", "4.00"))),
    ("call_buy_put_sell", combo((C350, "buy", "4.00"), (P350, "sell", "3.00"))),
    ("put_buy_call_sell", combo((P350, "buy", "3.00"), (C350, "sell", "4.00"))),
    ("both_calls_bought", combo((C350, "buy", "5.00"), (C355, "buy", "2.00"))),
    ("both_puts_sold", combo((P350, "sell", "2.00"), (P355, "sell", "5.00"))),
    (
        "same_kind_same_strike",
        combo((C350, "buy", "5.00"), (replace(C350, symbol="C350X"), "sell", "4.00")),
    ),
    ("future_leg", combo((option("future", None), "buy", "350.00"), (C355, "sell", "2.00"))),
    ("missing_strike", combo((option("call", None), "buy", "5.00"), (C355, "sell", "2.00"))),
    ("zero_quantity", combo((C350, "buy", "5.00"), (C355, "sell", "2.00"), quantity=0)),
    ("zero_price", combo((C350, "buy", "5.00"), (C355, "sell", "0"))),
    ("negative_price", combo((C350, "buy", "-1.00"), (C355, "sell", "2.00"))),
    ("negative_slippage", combo((C350, "buy", "5.00"), (C355, "sell", "2.00"), slippage="-0.01")),
    ("slippage_off_grid", combo((C350, "buy", "5.00"), (C355, "sell", "2.00"), slippage="0.005")),
]


@pytest.mark.parametrize("case", [case[1] for case in REJECTED], ids=[case[0] for case in REJECTED])
def test_unsupported_combo_has_no_structure_and_no_plan(
    case: tuple[ComboRequest, tuple[Contract, Contract]],
) -> None:
    request, contracts = case

    assert classify(request, contracts) is None
    assert plan(request, contracts) is None


def test_contract_that_is_not_the_legs_symbol_is_rejected() -> None:
    request, _ = combo((C350, "buy", "5.00"), (C355, "sell", "2.00"))

    assert plan(request, (C350, option("call", "360"))) is None


# ---- 다리 순서 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "sequence"),
    [
        ((C350, "buy", "5.00"), (C355, "sell", "2.00"), (0, 1)),
        ((C355, "sell", "2.00"), (C350, "buy", "5.00"), (1, 0)),
        ((P350, "sell", "2.00"), (P355, "buy", "5.00"), (1, 0)),
        ((P350, "buy", "3.00"), (C350, "buy", "4.00"), (1, 0)),
        ((P345, "sell", "2.00"), (C355, "sell", "3.00"), (1, 0)),
        ((C350, "sell", "4.00"), (P350, "sell", "3.00"), (0, 1)),
    ],
    ids=[
        "buy_listed_first",
        "sell_listed_first",
        "put_vertical",
        "long_put_listed_first",
        "short_put_listed_first",
        "short_call_listed_first",
    ],
)
def test_sequence_sends_the_buy_leg_first_then_call_first(
    first: LegSpec, second: LegSpec, sequence: tuple[int, int]
) -> None:
    p = plan(*combo(first, second))

    assert p is not None
    assert p.sequence == sequence


# ---- 두 번째 다리 한계 가격 ------------------------------------------------------


def limit(
    first: LegSpec, second: LegSpec, slippage: str, fill: str, ladder: tuple[TickBand, ...] = LADDER
) -> Decimal | None:
    request, contracts = combo(first, second, slippage=slippage)
    p = plan(request, contracts)
    assert p is not None
    return leg_limit_price(request, p, Decimal(fill), rules(contracts[p.sequence[1]], ladder))


@pytest.mark.parametrize(
    ("fill", "expected"),
    [
        # 매수형 콜 수직: 5.00 매수 · 2.00 매도, 허용 폭 0.10 → worst 3.10. 하한 = 체결가 - 3.10
        ("5.00", Decimal("1.90")),  # 하한 1.90, 단위에 맞음
        ("4.995", Decimal("1.90")),  # 하한 1.895 → 0.01 단위로 올림
        ("4.90", Decimal("1.80")),  # 매수가 싸게 체결되면 매도 하한도 내려간다
    ],
)
def test_sell_leg_floor_follows_the_buy_fill(fill: str, expected: Decimal) -> None:
    assert limit((C350, "buy", "5.00"), (C355, "sell", "2.00"), "0.10", fill) == expected


@pytest.mark.parametrize(
    ("fill", "expected"),
    [
        # 340 콜 22.00 매수 · 350 콜 12.00 매도, 허용 폭 0 → worst 10.00. 하한 = 체결가 - 10.00
        ("20.01", Decimal("10.05")),  # 하한 10.01, 10 이상은 0.05 단위로 올림
        ("19.991", Decimal("10.00")),  # 하한 9.991 → 0.01 단위로 올리면 10.00
        ("19.95", Decimal("9.95")),  # 하한 9.95, 10 미만은 0.01 단위라 그대로
    ],
)
def test_sell_leg_floor_rounds_up_on_the_tick_ladder(fill: str, expected: Decimal) -> None:
    first: LegSpec = (option("call", "340"), "buy", "22.00")
    second: LegSpec = (option("call", "350"), "sell", "12.00")

    assert limit(first, second, "0", fill) == expected


@pytest.mark.parametrize(
    ("fill", "expected"),
    [
        # 스트래들 매수: 콜 10.00 + 풋 10.00, 허용 폭 0 → worst 20.00. 풋 상한 = 20.00 - 콜 체결가
        ("9.96", Decimal("10.00")),  # 상한 10.04 → 0.05 단위로 내림
        ("9.90", Decimal("10.10")),  # 상한 10.10, 단위에 맞음
        ("10.001", Decimal("9.99")),  # 상한 9.999 → 0.01 단위로 내림
    ],
)
def test_buy_leg_cap_rounds_down_on_the_tick_ladder(fill: str, expected: Decimal) -> None:
    assert limit((C350, "buy", "10.00"), (P350, "buy", "10.00"), "0", fill) == expected


def test_short_straddle_second_sell_floor_counts_the_first_sale_as_received() -> None:
    # 콜 4.00 · 풋 3.00 매도, 허용 폭 0.50 → worst -6.50. 콜을 4.10에 팔면 풋 하한 = 6.50 - 4.10
    assert limit((C350, "sell", "4.00"), (P350, "sell", "3.00"), "0.50", "4.10") == Decimal("2.40")


@pytest.mark.parametrize(
    "fill",
    ["7.00", "7.50", "6.995"],
    ids=["cap_zero", "cap_negative", "cap_below_one_tick"],
)
def test_buy_leg_with_no_positive_cap_cannot_be_sent(fill: str) -> None:
    # 스트래들 매수 콜 4.00 + 풋 3.00, worst 7.00. 풋 상한 = 7.00 - 콜 체결가 ≤ 0.005
    assert limit((C350, "buy", "4.00"), (P350, "buy", "3.00"), "0", fill) is None


def test_sell_leg_floor_at_or_below_zero_sells_at_the_lowest_tick() -> None:
    # 5.00 매수 · 0.05 매도, 허용 폭 0.10 → worst 5.05. 하한 5.00 - 5.05 = -0.05: 어떤 가격도 됨
    assert limit((C350, "buy", "5.00"), (C355, "sell", "0.05"), "0.10", "5.00") == Decimal("0.01")


def test_unknown_tick_ladder_gives_no_price() -> None:
    assert limit((C350, "buy", "5.00"), (C355, "sell", "2.00"), "0.10", "5.00", ladder=()) is None
    assert limit((C350, "buy", "5.00"), (C355, "sell", "0.05"), "0.10", "5.00", ladder=()) is None


# ---- 묶음 전체 규칙 1~5 ----------------------------------------------------------

# 350 콜 5.00 매수 · 355 콜 2.00 매도: 최대 손실 750000원.
DEBIT_CALL = plan(*combo((C350, "buy", "5.00"), (C355, "sell", "2.00")))
SHORT_STRADDLE = plan(*combo((C350, "sell", "4.00"), (P350, "sell", "3.00")))
# 콜을 사고 풋을 파는 묶음은 받지 않는다.
UNSUPPORTED = plan(*combo((C350, "buy", "5.00"), (P350, "sell", "2.00")))


def combo_limits(ceiling: str | None) -> RiskLimits:
    """옵션 한도에 묶음 최대 손실 한도(원)만 바꾼 것."""
    loss = None if ceiling is None else Decimal(ceiling)
    return replace(LIMITS_WITH_OPTIONS, options=replace(OPTION_LIMITS, max_combo_loss=loss))


def combo_rule(
    combo_plan: ComboPlan | None = DEBIT_CALL,
    limits: RiskLimits | None = None,
    options_enabled: bool = True,
    option_sell: bool = False,
) -> str | None:
    """기본은 옵션 묶음을 켜고 묶음 한도 100만 원을 정한, 매도를 켜지 않은 사용자."""
    chosen = combo_limits("1000000") if limits is None else limits
    block = evaluate_combo(combo_plan, chosen, options_enabled, option_sell)
    return None if block is None else block[0]


def test_a_bounded_combo_within_the_combo_loss_limit_passes() -> None:
    assert combo_rule() is None


def test_options_off_blocks_every_combo_first() -> None:
    assert combo_rule(UNSUPPORTED, options_enabled=False) == OPTIONS_OFF


def test_a_structure_outside_the_table_is_unsupported() -> None:
    assert UNSUPPORTED is None
    assert combo_rule(UNSUPPORTED, limits=combo_limits(None)) == COMBO_UNSUPPORTED


@pytest.mark.parametrize(
    "limits",
    [combo_limits(None), replace(LIMITS_WITH_OPTIONS, options=None)],
    ids=["no_combo_limit", "no_option_limits"],
)
def test_a_missing_combo_loss_limit_blocks_every_combo(limits: RiskLimits) -> None:
    assert combo_rule(limits=limits) == LIMITS_NOT_SET
    assert combo_rule(SHORT_STRADDLE, limits=limits, option_sell=True) == LIMITS_NOT_SET


def test_unbounded_combos_need_option_selling_and_skip_the_combo_loss_limit() -> None:
    assert combo_rule(SHORT_STRADDLE) == OPTION_SELL_OFF
    assert combo_rule(SHORT_STRADDLE, limits=combo_limits("1"), option_sell=True) is None


@pytest.mark.parametrize(("ceiling", "rule"), [("750000", None), ("749999.99", COMBO_LOSS_LIMIT)])
def test_combo_loss_limit_blocks_a_max_loss_above_it(ceiling: str, rule: str | None) -> None:
    assert combo_rule(limits=combo_limits(ceiling)) == rule
    # 한정되는 묶음은 매도를 켜도 같다.
    assert combo_rule(limits=combo_limits(ceiling), option_sell=True) == rule


def test_combo_loss_limit_message_shows_the_loss_and_the_limit() -> None:
    block = evaluate_combo(DEBIT_CALL, combo_limits("500000"), True, False)

    assert block is not None
    assert block[1] == "묶음 최대 손실 750000.00원이 묶음 최대 손실 한도 500000원을 넘습니다."
