"""옵션 묶음의 구조 판정, 최대 손실, 다리 순서, 다음 다리 한계 가격, 묶음 전체 규칙. 순수 함수다.

다리의 가격이 호가 단위에 맞는지는 다리마다 지나는 한 종목 규칙(`guard.risk`)이 본다.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from nh_mcp.domain import (
    ComboPlan,
    ComboRequest,
    Contract,
    InstrumentRules,
    MaxLoss,
    RiskLimits,
    Side,
    Structure,
    Unbounded,
)
from nh_mcp.guard.risk import (
    LIMITS_NOT_SET,
    OPTION_KINDS,
    OPTION_SELL_OFF,
    OPTIONS_OFF,
    Block,
)

COMBO_UNSUPPORTED = "combo_unsupported"
COMBO_LOSS_LIMIT = "combo_loss_limit"

# 순가격 허용 폭의 단위(포인트).
SLIPPAGE_STEP = Decimal("0.01")

# 콜 하나·풋 하나를 같은 방향으로: (방향, 행사가가 같은가) -> 구조.
_CALL_AND_PUT: dict[tuple[Side, bool], Structure] = {
    ("buy", True): "long_straddle",
    ("buy", False): "long_strangle",
    ("sell", True): "short_straddle",
    ("sell", False): "short_strangle",
}


def _sign(side: Side) -> int:
    return 1 if side == "buy" else -1


def _well_formed(request: ComboRequest, contracts: tuple[Contract, Contract]) -> bool:
    """수량·가격·허용 폭이 맞고, 두 다리가 같은 품목·만기의 서로 다른 옵션이다."""
    first, second = contracts
    return (
        request.quantity >= 1
        and request.slippage >= 0
        and request.slippage % SLIPPAGE_STEP == 0
        and all(
            leg.price > 0
            and c.symbol == leg.symbol
            and c.market == request.market
            and c.kind in OPTION_KINDS
            for leg, c in zip(request.legs, contracts, strict=True)
        )
        and first.symbol != second.symbol
        and first.underlying == second.underlying
        and first.expiry == second.expiry
        and first.multiplier == second.multiplier
    )


def classify(request: ComboRequest, contracts: tuple[Contract, Contract]) -> Structure | None:
    """다리에서 구조를 읽는다. contracts는 legs와 같은 순서의 종목이다. 표에 없으면 None.

    수직 스프레드의 매수형·매도형은 가격이 아니라 행사가로 가른다. 콜은 낮은 행사가를, 풋은 높은
    행사가를 사면 매수형이다.
    """
    if not _well_formed(request, contracts):
        return None
    (a, b), (ca, cb) = request.legs, contracts
    ka, kb = ca.strike, cb.strike
    if ka is None or kb is None:
        return None
    if ca.kind == cb.kind:
        if a.side == b.side or ka == kb:
            return None
        bought, sold = (ka, kb) if a.side == "buy" else (kb, ka)
        debit = bought < sold if ca.kind == "call" else bought > sold
        return "debit_vertical" if debit else "credit_vertical"
    if a.side != b.side:
        return None
    return _CALL_AND_PUT[a.side, ka == kb]


def _option(contract: Contract) -> tuple[bool, Decimal]:
    """(콜인가, 행사가). classify를 지난 다리만 오므로 옵션이 아니면 호출한 쪽의 잘못이다."""
    if contract.kind not in OPTION_KINDS or contract.strike is None:
        raise ValueError(f"{contract.symbol}은(는) 행사가가 있는 옵션이 아닙니다.")
    return contract.kind == "call", contract.strike


def max_loss(
    request: ComboRequest, contracts: tuple[Contract, Contract], worst_net_debit: Decimal
) -> MaxLoss:
    """만기 손익 꺾은선 하나로 계산한 묶음 전체의 최대 손실(원). classify를 지난 요청만 넣는다.

    계약당 손익(S) = Σ 부호 * 내재가치(S) - worst_net_debit (사는 다리 +1, 파는 다리 -1).
    꺾이는 곳은 행사가뿐이라 최솟값은 S = 0, 각 행사가, S가 끝없이 커질 때 중 하나다. S가 커질 때
    기울기(콜 다리 부호의 합)가 음수면 Unbounded, 아니면 max(0, -최솟값) * 수량 * 승수다.
    """
    legs = [(_sign(leg.side), *_option(c)) for leg, c in zip(request.legs, contracts, strict=True)]
    if sum(sign for sign, is_call, _ in legs if is_call) < 0:
        return Unbounded()

    def payoff(spot: Decimal) -> Decimal:
        value = sum(
            (
                sign * max(Decimal(0), spot - strike if is_call else strike - spot)
                for sign, is_call, strike in legs
            ),
            Decimal(0),
        )
        return value - worst_net_debit

    lowest = min(payoff(s) for s in (Decimal(0), *(strike for _, _, strike in legs)))
    return max(Decimal(0), -lowest) * request.quantity * contracts[0].multiplier


def plan(request: ComboRequest, contracts: tuple[Contract, Contract]) -> ComboPlan | None:
    """묶음 계획. 구조가 표에 없으면 None(`combo_unsupported`)."""
    structure = classify(request, contracts)
    if structure is None:
        return None
    net = sum((_sign(leg.side) * leg.price for leg in request.legs), Decimal(0))
    worst = net + request.slippage
    first, second = sorted(
        range(2), key=lambda i: (request.legs[i].side != "buy", contracts[i].kind != "call")
    )
    return ComboPlan(structure, (first, second), net, worst, max_loss(request, contracts, worst))


def _to_tick(price: Decimal, rules: InstrumentRules, rounding: str) -> Decimal | None:
    tick = rules.tick_at(price)
    if price <= 0 or tick <= 0:
        return None
    rounded = (price / tick).to_integral_value(rounding=rounding) * tick
    return rounded if rounded > 0 else None


def leg_limit_price(
    request: ComboRequest, combo: ComboPlan, first_fill: Decimal, rules: InstrumentRules
) -> Decimal | None:
    """두 번째 다리(combo.sequence[1])의 지정가. first_fill은 첫 다리의 실제 평균 체결가, rules는
    두 번째 다리의 종목 규칙이다.

    두 다리의 순가격이 worst_net_debit을 넘지 않는 가장 불리한 가격이다. 사는 다리는 상한이라
    호가 단위로 내리고, 파는 다리는 하한이라 올린다. 사는 다리의 상한이 0 이하면 None(보낼 수
    없음)이다. 파는 다리의 하한이 0 이하면 어떤 가격도 허용 폭 안이라 가장 낮은 호가로 판다.
    호가 단위를 모르면 None이다.
    """
    first = request.legs[combo.sequence[0]]
    second = request.legs[combo.sequence[1]]
    paid = _sign(first.side) * first_fill
    if second.side == "buy":
        return _to_tick(combo.worst_net_debit - paid, rules, ROUND_FLOOR)
    floor = paid - combo.worst_net_debit
    return _to_tick(max(floor, rules.tick_at(Decimal(0))), rules, ROUND_CEILING)


def evaluate_combo(
    combo: ComboPlan | None, limits: RiskLimits | None, options_enabled: bool, option_sell: bool
) -> Block | None:
    """묶음 전체 규칙 1~5(`docs/options.md` "묶음 규칙"). 싼 것부터 보고 첫 차단에서 멈춘다.

    combo는 `plan`의 결과(표에 없는 구조면 None)다. 6번(다리별 한 종목 규칙을 앞 다리가 모두
    체결됐다고 본 계좌로 차례로)은 서비스가 다리마다 `evaluate`를 불러 지난다.
    """
    if not options_enabled:
        return (
            OPTIONS_OFF,
            "옵션 묶음이 꺼져 있어 묶음 주문은 할 수 없습니다. 사용자가 등록 화면의 '옵션' "
            "스위치를 켜야 합니다(AI는 켤 수 없습니다).",
        )
    if combo is None:
        return (
            COMBO_UNSUPPORTED,
            "받는 묶음 구조가 아닙니다. 같은 만기의 수직 스프레드(매수형·매도형)나 "
            "스트래들·스트랭글을 같은 수량의 지정가 두 다리로 넣어 주세요.",
        )
    options = None if limits is None else limits.options
    if options is None or options.max_combo_loss is None:
        return LIMITS_NOT_SET, "묶음 최대 손실 한도가 설정되지 않아 묶음 주문은 할 수 없습니다."
    return _check_loss(combo.max_loss, options.max_combo_loss, option_sell)


def _check_loss(loss: MaxLoss, ceiling: Decimal, option_sell: bool) -> Block | None:
    """규칙 4·5. 한정되지 않는 묶음은 손실 한도 대신 "옵션 매도" 스위치를 본다."""
    if isinstance(loss, Unbounded):
        if option_sell:
            return None
        return (
            OPTION_SELL_OFF,
            "최대 손실이 한정되지 않는 묶음(매도 스트래들·스트랭글)은 '옵션 매도'를 켠 사용자만 "
            "낼 수 있습니다. 사용자가 등록 화면의 '옵션 매도' 스위치를 켜야 합니다(AI는 켤 수 "
            "없습니다).",
        )
    if loss <= ceiling:
        return None
    return (
        COMBO_LOSS_LIMIT,
        f"묶음 최대 손실 {loss:f}원이 묶음 최대 손실 한도 {ceiling:f}원을 넘습니다.",
    )
