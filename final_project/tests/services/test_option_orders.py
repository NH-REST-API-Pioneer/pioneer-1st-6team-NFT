"""옵션 주문: `_check`가 만드는 옵션 장부, 미리보기·승인 문구, 주문 직전 재검사, place_auto.

가짜 NH와 메모리 DB로 돈다. 종목 마스터에는 같은 날 받은 옵션 견본을 붙인다. 시계는 2026-12-01(화)
10:00 KST이고 2026-12물 만기(12-10)까지 9일이다. 주문 내역은 exec_div로 나눠 답한다: 2는 미체결
(계좌), 1은 오늘 체결(옵션 장부). 잔고·주문내역 행은 명세 필드로 만든 가짜다.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fakes import T0, FakeNH, World

from nh_mcp.domain import (
    OptionLimits,
    OrderRequest,
    RiskLimits,
    RunSnapshot,
    StrategyScope,
    UserId,
)
from nh_mcp.markets.domestic import (
    DEPOSIT,
    OPEN_INTEREST,
    ORDER,
    ORDER_HISTORY,
    ORDERABLE_QTY,
    PRICE,
    DomesticMarket,
)
from nh_mcp.markets.domestic_master import SERIES, UPPER_LIMIT, DomesticMaster
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL, DomesticProducts
from nh_mcp.nh.client import Credentials
from nh_mcp.services.orders import (
    ConfirmInvalid,
    LimitsNotSet,
    OptionTerms,
    OrderService,
    Preview,
    RiskBlocked,
)
from nh_mcp.store import confirm_tokens, features, limits, option_holdings, users
from nh_mcp.store.features import Feature
from nh_mcp.store.option_holdings import Holding
from nh_mcp.store.orders import StoredOrder

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDED = FIXTURES / "nh" / "domestic"
OPTION_MASTER = FIXTURES / "master" / "kospi200_options.mst"
CALL = "B016C725"  # 코스피200 C 202612 725.0, 승수 250000, 마스터 상한가 103.25
PUT = "C016C725"  # 코스피200 P 202612 725.0, 마스터 상한가 103.15
FUTURE = "A016C000"  # 코스피200 F 202612
NOT_IN_MASTER = "B016CZZZ"
# 하루 프리미엄 100만 원, 옵션 계약 합계 4, 매도 합계 2. 선물은 종목별 3, 1회 수량 2.
OPTION_LIMITS = OptionLimits(Decimal("1000000"), 4, 2)
LIMITS = RiskLimits(Decimal("500000"), 3, 2, OPTION_LIMITS)
BUY = OrderRequest("domestic", CALL, "buy", "limit", 1, Decimal("2.00"))  # 프리미엄 50만 원
SELL = OrderRequest("domestic", CALL, "sell", "limit", 1, Decimal("2.00"))
MARKET_BUY = OrderRequest("domestic", CALL, "buy", "market", 1, None)
FUTURE_BUY = OrderRequest("domestic", FUTURE, "buy", "limit", 1, Decimal("784.55"))
ACK = {"ord_no": "0000000101", "msg_cd": "00000", "msg_cntn": "정상 처리"}
# 시장가 매수 1계약(승수 250000)을 마스터 상한가 5.00(전날 파일)과 오늘 시세 상한가 9.00으로 센 것.
AT_MASTER = Decimal("5.00") * 250000
AT_TODAY = Decimal("9.00") * 250000


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def quote(up: str, symbol: str = CALL) -> dict[str, Any]:
    """그 종목의 오늘 시세. 녹화 시세에서 종목과 상한가(up)만 바꾼다. 빈 up은 상한가를 모름."""
    return recorded("price") | {"sym_exch": symbol, "up": up}


def premium_limit(daily: Decimal) -> RiskLimits:
    return replace(LIMITS, options=replace(OPTION_LIMITS, premium_daily=daily))


def occurs(*rows: dict[str, str]) -> dict[str, object]:
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": list(rows)}


def held(side: str, quantity: int, symbol: str = CALL) -> dict[str, str]:
    """잔고 한 행. side는 NH 코드(1 매수, 2 매도)."""
    return {
        "occurs1_sym": symbol,
        "occurs1_ko_sym_nm": symbol,
        "occurs1_trd_div": side,
        "occurs1_opint_qty": str(quantity),
        "occurs1_cls_psb_qty": str(quantity),
        "occurs1_pl_amt": "0",
    }


def row(side: str, symbol: str, **changes: str) -> dict[str, str]:
    """주문 내역 한 행. 기본은 1계약, 가격 1.00, 체결 없음."""
    base = {
        "occurs1_ord_no": "0000000201",
        "occurs1_sym": symbol,
        "occurs1_sym_nm": symbol,
        "occurs1_trd_div": side,
        "occurs1_ord_qty": "1",
        "occurs1_ord_pric": "1.00",
        "occurs1_exec_qty": "0",
        "occurs1_rem_ord_qty": "1",
        "occurs1_ord_div_cd": "1",
    }
    return base | changes


def resting(side: str, remaining: int, price: str = "1.00", symbol: str = CALL) -> dict[str, str]:
    qty = str(remaining)
    return row(side, symbol, occurs1_ord_qty=qty, occurs1_rem_ord_qty=qty, occurs1_ord_pric=price)


def filled(side: str, quantity: int, price: str, symbol: str = CALL) -> dict[str, str]:
    qty = str(quantity)
    return row(
        side,
        symbol,
        occurs1_ord_qty=qty,
        occurs1_exec_qty=qty,
        occurs1_exec_pric=price,
        occurs1_rem_ord_qty="0",
    )


class Setup:
    """옵션 묶음을 켠 사용자(옵션 매도는 꺼짐)와 옵션 한도. 잔고·미체결·오늘 체결은 비어 있다."""

    def __init__(self) -> None:
        self.nh = FakeNH()
        self.nh.reply(PRODUCT_DETAIL, recorded("product-detail"))
        self.nh.reply(DEPOSIT, recorded("deposit"))
        self.nh.reply(OPEN_INTEREST, recorded("open-interest"))
        self.nh.reply(ORDER_HISTORY, recorded("order-history"))
        self.nh.reply(ORDERABLE_QTY, recorded("orderable-quantity"))
        self.nh.reply(PRICE, recorded("price"))
        self.nh.reply(ORDER, ACK)
        self.world = World(nh=httpx2.MockTransport(self.nh))
        self.world.master.extra["fut_opt"] = OPTION_MASTER.read_text(encoding="utf-8")
        self.conn = self.world.conn
        self.clock = self.world.clock
        self.user: UserId = users.get_or_create(self.conn, "g-admin", "admin@example.com", T0)
        asyncio.run(
            self.world.store.register(self.user, "domestic", Credentials("fake-key-1", "x-sec"))
        )
        master = DomesticMaster(self.clock, httpx2.MockTransport(self.world.master))
        domestic = DomesticMarket(self.world.pool, master, self.clock)
        products = DomesticProducts(self.world.pool, self.clock)
        limits.put(self.conn, self.user, "domestic", LIMITS, T0)
        features.turn_on(self.conn, self.user, "options", T0)
        self.service = OrderService(self.conn, self.clock, domestic, products, True)

    def holding(self, *rows: dict[str, str]) -> None:
        self.nh.replies[OPEN_INTEREST] = [(200, occurs(*rows))]

    def waiting(self, *rows: dict[str, str]) -> None:
        self.nh.reply_where(ORDER_HISTORY, "exec_div", "2", occurs(*rows))

    def fills(self, *rows: dict[str, str]) -> None:
        self.nh.reply_where(ORDER_HISTORY, "exec_div", "1", occurs(*rows))

    def switch(self, feature: Feature, on: bool) -> None:
        if on:
            features.turn_on(self.conn, self.user, feature, T0)
        else:
            features.turn_off(self.conn, self.user, feature)

    def set_limits(self, new: RiskLimits) -> None:
        limits.put(self.conn, self.user, "domestic", new, T0)

    def master_upper(self, symbol: str, upper: str) -> None:
        """옵션 마스터 견본에서 그 종목의 상한가 칸을 바꾼다. 마스터를 받는 첫 점검 전에 부른다."""
        lines = self.world.master.extra["fut_opt"].splitlines()
        cells = [line.split(";") for line in lines]
        for row_cells in cells:
            if row_cells[SERIES] == symbol:
                row_cells[UPPER_LIMIT] = upper
        self.world.master.extra["fut_opt"] = "".join(";".join(c) + "\n" for c in cells)

    def preview(self, order: OrderRequest = BUY) -> Preview:
        return asyncio.run(self.service.preview(self.user, order))

    def blocked(self, order: OrderRequest = BUY) -> str:
        with pytest.raises(RiskBlocked) as caught:
            self.preview(order)
        return caught.value.rule

    def place(self, token: str) -> StoredOrder:
        return asyncio.run(self.service.place(self.user, token, "approved"))

    def message(self, token: str) -> str:
        return asyncio.run(self.service.pending(self.user, token)).message

    def order_calls(self) -> int:
        return self.nh.paths().count(ORDER)

    def fill_lookups(self) -> int:
        return sum(
            1 for p, body in self.nh.requests if p == ORDER_HISTORY and body["exec_div"] == "1"
        )


def run() -> RunSnapshot:
    """콜 하나를 받는 실행 중인 전략. 1회 2계약, 최대 3계약, 하루 5번, 내년 만료."""
    scope = StrategyScope(2, 3, 5, Decimal("300000"), T0.replace(year=2027), "hold", "auto")
    return RunSnapshot("run-1", scope, frozenset({CALL}), 0, 0, Decimal("0"))


# ---- 미리보기와 승인 문구 ------------------------------------------------------------


def test_option_buy_preview_shows_a_new_buy_with_its_premium() -> None:
    s = Setup()

    preview = s.preview()

    assert preview.option == OptionTerms("new_buy", Decimal("500000.00"))
    message = s.message(preview.confirm_token)
    assert "옵션 새 매수입니다. 프리미엄 500000.00원" in message
    assert s.fill_lookups() == 1


@pytest.mark.parametrize(
    ("live", "counted"), [("9.00", AT_TODAY), ("3.00", AT_MASTER), ("", AT_MASTER)]
)
def test_market_buy_premium_counts_at_the_larger_of_todays_and_the_master_upper_limit(
    live: str, counted: Decimal
) -> None:
    s = Setup()
    s.master_upper(CALL, "5.00")
    s.nh.reply_for(PRICE, CALL, quote(live))
    s.holding(held("2", 1))  # 보유 매도를 되사는 청산이라 증거금을 보지 않고 프리미엄을 보여 준다.

    assert s.preview(MARKET_BUY).option == OptionTerms("close", counted)


def test_market_buy_over_the_premium_limit_at_todays_upper_limit_is_blocked() -> None:
    # 한도 200만 원은 마스터 5.00(125만)으로는 안이지만 오늘 상한가 9.00(225만)으로는 넘는다.
    s = Setup()
    s.master_upper(CALL, "5.00")
    s.nh.reply_for(PRICE, CALL, quote("9.00"))
    s.set_limits(premium_limit(Decimal("2000000")))

    assert s.blocked(MARKET_BUY) == "premium_limit"
    s.set_limits(premium_limit(AT_TODAY))
    preview = s.preview(MARKET_BUY)
    assert preview.option == OptionTerms("new_buy", AT_TODAY)
    assert "많아야 2250000.00원" in s.message(preview.confirm_token)


def test_market_buy_without_an_upper_limit_in_the_quote_or_the_master_is_blocked() -> None:
    s = Setup()
    s.master_upper(CALL, "")
    s.nh.reply_for(PRICE, CALL, quote(""))

    assert s.blocked(MARKET_BUY) == "premium_limit"


def test_closing_sell_passes_and_is_shown_as_closing_with_options_and_selling_off() -> None:
    s = Setup()
    s.holding(held("1", 1))
    s.switch("options", False)
    s.set_limits(replace(LIMITS, options=None))

    preview = s.preview(SELL)

    assert preview.option == OptionTerms("close", None)
    assert "옵션 청산입니다." in s.message(preview.confirm_token)
    assert s.place(preview.confirm_token).status == "ACCEPTED"
    assert s.order_calls() == 1


def test_closing_buy_of_a_held_short_passes_with_options_off() -> None:
    s = Setup()
    s.holding(held("2", 1))
    s.switch("options", False)

    preview = s.preview(BUY)

    assert preview.option == OptionTerms("close", Decimal("500000.00"))


def test_new_sell_preview_warns_about_selling_without_holding() -> None:
    s = Setup()
    s.switch("option_sell", True)

    preview = s.preview(SELL)

    assert preview.option == OptionTerms("new_sell", None)
    assert "옵션 새 매도입니다. 보유하지 않은 옵션을" in s.message(preview.confirm_token)


def test_futures_preview_has_no_option_terms_and_reads_no_fills() -> None:
    s = Setup()

    preview = s.preview(FUTURE_BUY)

    assert preview.option is None
    assert s.fill_lookups() == 0
    assert "옵션" not in s.message(preview.confirm_token)


# ---- 규칙이 장부를 보는지 ------------------------------------------------------------


def test_options_bundle_off_blocks_increasing_option_orders() -> None:
    s = Setup()
    s.switch("options", False)

    assert s.blocked() == "options_off"


def test_missing_option_limits_block_option_orders_but_not_futures() -> None:
    s = Setup()
    s.set_limits(replace(LIMITS, options=None))

    with pytest.raises(LimitsNotSet):
        s.preview()
    assert s.preview(FUTURE_BUY).option is None


def test_naked_new_sell_is_blocked_while_selling_is_off() -> None:
    s = Setup()

    assert s.blocked(SELL) == "option_sell_off"
    s.holding(held("1", 1))
    assert s.blocked(replace(SELL, quantity=2)) == "option_sell_off"


def test_premium_counts_todays_option_buys_resting_buys_and_this_order() -> None:
    # 체결: 콜 매수 1 @ 0.60(15만), 풋 매수 1 @ 0.40(10만)(청산 매수도 센다).
    # 콜 매도 체결과 선물 매수 체결은 세지 않는다. 미체결: 풋 매수 1 @ 1.00(25만).
    # 15만 + 10만 + 25만 + 이번 2.00(50만) = 100만 원 = 한도.
    s = Setup()
    s.holding(held("2", 1, PUT))
    s.fills(
        filled("1", 1, "0.60"),
        filled("1", 1, "0.40", PUT),
        filled("2", 3, "5.00"),
        filled("1", 1, "784.00", FUTURE),
    )
    s.waiting(resting("1", 1, symbol=PUT))

    assert s.preview().option == OptionTerms("new_buy", Decimal("500000.00"))
    assert s.blocked(replace(BUY, price=Decimal("2.01"))) == "premium_limit"


def test_resting_market_buy_counts_at_the_larger_of_todays_and_the_master_upper_limit() -> None:
    # 풋 시장가 미체결 1 + 이번 콜 매수 50만, 한도 100만. 마스터 1.00(25만)으로 세면 75만으로
    # 안이고, 오늘 상한가 3.00(75만)으로 세면 125만으로 넘는다.
    s = Setup()
    s.master_upper(PUT, "1.00")
    s.waiting(resting("1", 1, price="0", symbol=PUT))
    s.nh.reply_for(PRICE, PUT, quote("", PUT))

    assert s.preview().option == OptionTerms("new_buy", Decimal("500000.00"))
    s.nh.reply_for(PRICE, PUT, quote("3.00", PUT))
    assert s.blocked() == "premium_limit"


def test_a_check_reads_each_market_priced_symbol_quote_once() -> None:
    # 시장가 콜 매수 + 시장가 미체결 콜 매수 2건 + 시장가 미체결 풋 매수 1건.
    s = Setup()
    s.waiting(
        resting("1", 1, price="0") | {"occurs1_ord_no": "0000000202"},
        resting("1", 1, price="0") | {"occurs1_ord_no": "0000000203"},
        resting("1", 1, price="0", symbol=PUT),
    )

    assert s.blocked(MARKET_BUY) == "premium_limit"
    quoted = sorted(str(body["sym"]) for path, body in s.nh.requests if path == PRICE)
    assert quoted == [CALL, PUT]


def test_option_position_limit_sums_every_option_but_not_futures() -> None:
    # 옵션: 콜 매수 2 + 풋 매도 1 + 이번 1 = 4(한도와 같음).
    # 선물 보유 10계약과 선물 미체결은 세지 않는다.
    s = Setup()
    s.holding(held("1", 2), held("2", 1, PUT), held("1", 10, FUTURE))
    s.waiting(resting("1", 3, price="784.55", symbol=FUTURE))

    assert s.preview().option == OptionTerms("new_buy", Decimal("500000.00"))
    s.waiting(resting("1", 1))  # 콜 매수 미체결 1이 더해지면 5
    assert s.blocked() == "option_position_limit"


def test_options_held_do_not_count_toward_the_futures_position_limit() -> None:
    s = Setup()
    s.holding(held("1", 5), held("1", 5, PUT))

    assert s.preview(FUTURE_BUY).option is None


def test_a_resting_sell_beyond_the_held_long_is_a_pending_new_sell() -> None:
    # 콜 매수 2 보유, 매도 4 미체결: 다 체결되면 2계약이 새 매도다. 2 + 이번 풋 1 > 매도 한도 2.
    s = Setup()
    s.switch("option_sell", True)
    s.holding(held("1", 2))
    s.waiting(resting("2", 4, price="2.00"))

    assert s.blocked(replace(SELL, symbol=PUT)) == "option_short_limit"
    # 미체결 매도 2가 보유 2만큼이면 새 매도가 아니다. 이번 1계약만 센다(2로 세면 3 > 2로 막힌다).
    s.waiting(resting("2", 2, price="2.00"))
    assert s.preview(replace(SELL, symbol=PUT)).option == OptionTerms("new_sell", None)


@pytest.mark.parametrize(
    "case",
    [
        "unknown_position",
        "unknown_resting",
        "resting_quote_failed",
        "resting_upper_unknown",
        "fills_failed",
        "fills_unreadable",
    ],
)
def test_an_unknown_option_book_blocks_increasing_but_lets_closing_through(case: str) -> None:
    s = Setup()
    rows = [held("1", 1)]
    if case == "unknown_position":
        rows.append(held("1", 1, NOT_IN_MASTER))
    elif case == "unknown_resting":
        s.waiting(resting("1", 1, symbol=NOT_IN_MASTER))
    elif case == "resting_quote_failed":
        s.waiting(resting("1", 1, price="0", symbol=PUT))
        s.nh.reply_for(PRICE, PUT, {})  # 빈 시세: NH가 모르는 종목에 주는 모양
    elif case == "resting_upper_unknown":
        s.waiting(resting("1", 1, price="0", symbol=PUT))
        s.master_upper(PUT, "")
        s.nh.reply_for(PRICE, PUT, quote("", PUT))
    elif case == "fills_failed":
        s.nh.reply_where(ORDER_HISTORY, "exec_div", "1", {"errorMessage": "서버 오류"}, 500)
    else:
        s.fills(filled("1", 1, "", PUT))
    s.holding(*rows)

    assert s.blocked() == "option_book_unknown"
    assert s.preview(SELL).option == OptionTerms("close", None)


# ---- 주문 직전 재검사와 토큰 ----------------------------------------------------------


def test_place_rechecks_the_premium_and_blocks_a_fill_after_the_preview() -> None:
    s = Setup()
    s.waiting(resting("1", 2, symbol=PUT))  # 50만
    token = s.preview().confirm_token  # 50만 + 50만 = 100만, 통과
    s.fills(filled("1", 1, "0.01", PUT))  # 미리보기 뒤 2,500원어치가 체결됐다

    with pytest.raises(RiskBlocked) as caught:
        s.place(token)

    assert caught.value.rule == "premium_limit"
    assert s.order_calls() == 0


def test_a_closing_sell_that_became_a_new_sell_after_the_preview_is_not_sent() -> None:
    s = Setup()
    s.switch("option_sell", True)
    s.holding(held("1", 1))
    token = s.preview(SELL).confirm_token
    s.holding()  # 미리보기 뒤 보유가 사라졌다

    with pytest.raises(RiskBlocked) as caught:
        s.place(token)

    assert caught.value.rule == "warning_changed"
    assert "청산 → 새 매도" in caught.value.message
    assert s.order_calls() == 0


def test_a_closing_sell_that_became_a_new_sell_is_blocked_by_the_sell_switch_first() -> None:
    s = Setup()
    s.holding(held("1", 1))
    token = s.preview(SELL).confirm_token
    s.holding()

    with pytest.raises(RiskBlocked) as caught:
        s.place(token)

    assert caught.value.rule == "option_sell_off"
    assert s.order_calls() == 0


# 매수 1계약 토큰의 옵션 칸을 주문과 맞지 않게 바꾼 것들.
TAMPERED: dict[str, dict[str, object]] = {
    "no_premium": {"option": {"action": "close"}},
    "new_sell_on_a_buy": {"option": {"action": "new_sell", "premium": None}},
    "unknown_action": {"option": {"action": "open", "premium": "1"}},
    "premium_on_a_sell": {"side": "sell", "option": {"action": "close", "premium": "500000"}},
}


@pytest.mark.parametrize("case", ["missing", *TAMPERED])
def test_a_token_whose_option_terms_do_not_fit_the_order_is_unusable(case: str) -> None:
    s = Setup()
    token = s.preview().confirm_token
    stored = confirm_tokens.find(s.conn, token)
    assert stored is not None
    values: dict[str, object] = json.loads(stored.payload)
    if case == "missing":
        del values["option"]
    else:
        values |= TAMPERED[case]
    payload = json.dumps(values, ensure_ascii=False)
    with s.conn:
        s.conn.execute("UPDATE confirm_tokens SET payload = ?", (payload,))

    with pytest.raises(ConfirmInvalid):
        s.message(token)
    with pytest.raises(ConfirmInvalid):
        s.place(token)
    assert s.order_calls() == 0


# ---- place_auto -----------------------------------------------------------------------


def test_place_auto_goes_through_the_same_option_rules() -> None:
    s = Setup()

    with pytest.raises(RiskBlocked) as caught:
        asyncio.run(s.service.place_auto(s.user, run(), SELL))
    assert caught.value.rule == "option_sell_off"
    s.fills(filled("1", 2, "1.00"))  # 오늘 50만 원
    with pytest.raises(RiskBlocked) as caught:
        asyncio.run(s.service.place_auto(s.user, run(), replace(BUY, price=Decimal("2.01"))))
    assert caught.value.rule == "premium_limit"
    assert s.order_calls() == 0
    assert asyncio.run(s.service.place_auto(s.user, run(), BUY)).status == "ACCEPTED"


# ---- 옵션 보유 기록 -------------------------------------------------------------------

AFTER_EXPIRY = datetime(2026, 12, 11, 1, 0, tzinfo=UTC)  # 만기 다음 날 10:00 KST


def recorded_holdings(s: Setup) -> list[Holding]:
    return option_holdings.load(s.conn, s.user, "domestic")


def test_order_checks_record_held_options_and_close_them_after_expiry() -> None:
    s = Setup()
    s.holding(held("1", 1))
    s.preview(SELL)  # 보유 콜을 되파는 청산

    [row] = recorded_holdings(s)
    assert (row.symbol, row.side, row.quantity, row.close_reason) == (CALL, "buy", 1, None)
    s.clock.now = AFTER_EXPIRY
    s.holding()
    s.preview(BUY)
    [row] = recorded_holdings(s)
    assert (row.closed_at, row.close_reason) == (AFTER_EXPIRY, "exercised_or_expired")


def test_an_order_check_on_an_unreadable_account_closes_nothing() -> None:
    s = Setup()
    s.holding(held("1", 1))
    s.preview(SELL)
    s.clock.now = AFTER_EXPIRY
    s.holding({"occurs1_sym": PUT, "occurs1_trd_div": "", "occurs1_opint_qty": "1"})

    assert s.blocked(SELL) == "account_unknown"
    [row] = recorded_holdings(s)
    assert (row.symbol, row.closed_at) == (CALL, None)
