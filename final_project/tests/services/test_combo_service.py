"""옵션 묶음 서비스: 미리보기(가상 계좌), 이어 하기, 승인 문구, 실행(재생 데이터), 우회.

가짜 거래소(`Exchange`)가 다리 주문을 받아, 주문마다 정해 둔 주문내역 행을 조회 때마다 차례로
돌려준다(마지막 것은 되풀이). 잔고·미체결·오늘 체결은 지금까지 돌려준 행에서 만든다. 행은 명세
필드로 만든 가짜다. 시계는 2026-12-01(화) 10:00 KST이고, 쉬기는 시계만 넘긴다.

기본 묶음은 콜 수직 매수형: 725 콜 2.00 매수 · 730 콜 1.00 매도, 2계약, 허용 폭 0.05.
순가격 1.00, 끝 1.05, 최대 손실 1.05 * 250000 * 2 = 525000원. 매수가 2.00에 체결되면 매도 하한 0.95.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from fakes import T0, Clock, FakeNH, World
from option_exchange import NOTHING, WORKING, Exchange, Placed, Snap, filled, recorded

from nh_mcp.domain import (
    ComboContext,
    ComboRequest,
    Leg,
    OptionLimits,
    OrderRequest,
    RiskLimits,
    Unbounded,
    UserId,
)
from nh_mcp.guard.combo import plan
from nh_mcp.markets.domestic import (
    DEPOSIT,
    OPEN_INTEREST,
    ORDERABLE_QTY,
    PRICE_BID,
    DomesticMarket,
)
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL, DomesticProducts
from nh_mcp.nh.client import Credentials
from nh_mcp.services.combos import (
    LEASE,
    ComboDeps,
    ComboPreview,
    ComboResult,
    ComboService,
    ComboStop,
    NotResumable,
    UnknownCombo,
)
from nh_mcp.services.orders import (
    ConfirmInvalid,
    LeaseLost,
    LegClaim,
    LimitsNotSet,
    NotApproved,
    OrderService,
    RiskBlocked,
    UnknownState,
)
from nh_mcp.services.queries import UnknownSymbol
from nh_mcp.store import combos, features, limits, orders, users
from nh_mcp.store.combos import Combo
from nh_mcp.store.orders import Origin, Outcome

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OPTION_MASTER = FIXTURES / "master" / "kospi200_options.mst"
CALL = "B016C725"  # 코스피200 C 202612 725.0, 승수 250000
CALL_730 = "B016C730"
FAR = "B016C745"  # 같은 계열의 다른 콜
PUT = "C016C725"
PUT_720 = "C016C720"
OPTION_LIMITS = OptionLimits(Decimal("2000000"), 4, 2, Decimal("1000000"))
LIMITS = RiskLimits(Decimal("500000"), 3, 2, OPTION_LIMITS)
VERTICAL = ComboRequest(
    "domestic",
    (Leg(CALL, "buy", Decimal("2.00")), Leg(CALL_730, "sell", Decimal("1.00"))),
    2,
    Decimal("0.05"),
)
IN_RANGE = ("0.95", "1.00")  # 730 콜 매수 1호가 0.95 = 하한, 매도 1호가 1.00


class Setup:
    """옵션을 켠 사용자(옵션 매도는 꺼짐)와 묶음 한도. 잔고·미체결·오늘 체결은 비어 있다."""

    def __init__(self) -> None:
        self.nh = FakeNH()
        self.nh.reply(PRODUCT_DETAIL, recorded("product-detail"))
        self.nh.reply(DEPOSIT, recorded("deposit"))
        self.nh.reply(ORDERABLE_QTY, recorded("orderable-quantity"))
        self.exchange = Exchange(self.nh, Clock())
        self.world = World(nh=httpx2.MockTransport(self.exchange.handle))
        self.clock = self.exchange.clock = self.world.clock
        self.world.master.extra["fut_opt"] = OPTION_MASTER.read_text(encoding="utf-8")
        self.conn = self.world.conn
        self.user = self.add_user("g-admin", "admin@example.com", "fake-key-1")
        master = DomesticMaster(self.clock, httpx2.MockTransport(self.world.master))
        self.domestic = DomesticMarket(self.world.pool, master, self.clock)
        products = DomesticProducts(self.world.pool, self.clock)
        self.orders = OrderService(self.conn, self.clock, self.domestic, products, True)
        self.slept: list[timedelta] = []
        deps = ComboDeps(self.domestic, products, self.orders)
        self.service = ComboService(self.conn, self.clock, deps, self.sleep, True)
        self.exchange.book[CALL_730] = IN_RANGE

    def add_user(self, sub: str, email: str, key: str) -> UserId:
        user = users.get_or_create(self.conn, sub, email, T0)
        asyncio.run(self.world.store.register(user, "domestic", Credentials(key, "x-sec")))
        limits.put(self.conn, user, "domestic", LIMITS, T0)
        features.turn_on(self.conn, user, "options", T0)
        return user

    async def sleep(self, delta: timedelta) -> None:
        self.slept.append(delta)
        self.clock.now += delta
        await asyncio.sleep(0)

    def preview(self, request: ComboRequest = VERTICAL, user: UserId | None = None) -> ComboPreview:
        return asyncio.run(self.service.preview(user or self.user, request))

    def resume(self, combo_id: str, user: UserId | None = None) -> ComboPreview:
        return asyncio.run(self.service.preview_resume(user or self.user, combo_id))

    def place(self, token: str, user: UserId | None = None) -> ComboResult:
        return asyncio.run(self.service.place(user or self.user, token, "approved"))

    def message(self, token: str) -> str:
        return asyncio.run(self.service.pending(self.user, token)).message

    def blocked(self, request: ComboRequest = VERTICAL) -> str:
        with pytest.raises(RiskBlocked) as caught:
            self.preview(request)
        return caught.value.rule

    def set_limits(self, new: RiskLimits) -> None:
        limits.put(self.conn, self.user, "domestic", new, T0)

    def lease(self, combo_id: str) -> datetime | None:
        row = combos.get(self.conn, self.user, combo_id)
        assert row is not None
        return row.lease_until

    def combo_ids(self) -> list[str]:
        rows: list[tuple[str]] = self.conn.execute("SELECT id FROM combos").fetchall()
        return [row[0] for row in rows]

    def audited(self, action: str) -> list[dict[str, object]]:
        rows: list[tuple[str]] = self.conn.execute(
            "SELECT detail FROM audit_log WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close_by_hand(self, symbol: str, quantity: int) -> None:
        """사용자가 보유 매수를 한 종목 주문으로 되판다(1.90 지정가, 다 체결)."""
        close = OrderRequest("domestic", symbol, "sell", "limit", quantity, Decimal("1.90"))
        token = asyncio.run(self.orders.preview(self.user, close)).confirm_token
        self.exchange.script = [filled(quantity, "1.90")]
        asyncio.run(self.orders.place(self.user, token, "approved"))

    def cover_with(self, cover: str) -> None:
        """파는 다리를 덮을 것처럼 보이는 계좌: 매도를 켬, 또는 같은 계열의 745 콜 매수 2계약."""
        if cover == "selling_on":
            features.turn_on(self.conn, self.user, "option_sell", T0)
            return
        self.set_limits(replace(LIMITS, options=replace(OPTION_LIMITS, max_position=20)))
        self.exchange.holdings.append((FAR, "1", 2))

    def vertical(self) -> ComboContext:
        first = asyncio.run(self.domestic.contract(CALL))
        second = asyncio.run(self.domestic.contract(CALL_730))
        assert first is not None and second is not None
        found = plan(VERTICAL, (first, second))
        assert found is not None
        return ComboContext(VERTICAL, found)

    def leg_claim(self, combo_id: str = "c1") -> LegClaim:
        """place_leg를 바로 부르는 시험의 묶음 행을 넣고 지금 쥔다. 늘릴 기한도 같은 값이다."""
        row = Combo(combo_id, self.user, "domestic", "{}", "{}", self.clock.now)
        combos.insert(self.conn, row)
        held = self.clock.now + LEASE
        assert combos.claim(self.conn, self.user, combo_id, self.clock.now, held)
        return LegClaim(combo_id, held, 0, held)


def combo_with_options(max_combo_loss: Decimal | None) -> RiskLimits:
    return replace(LIMITS, options=replace(OPTION_LIMITS, max_combo_loss=max_combo_loss))


def sent_as(order: dict[str, object]) -> tuple[object, ...]:
    """받은 주문의 (종목, 방향 코드, 수량, 가격, 체결 조건)."""
    return (
        order["sym"],
        order["trd_div"],
        order["ord_qty"],
        order["ord_pric"],
        order["exec_cnd_cd"],
    )


# ---- 미리보기 ----------------------------------------------------------------------------


def test_a_sell_off_users_vertical_previews_with_the_sell_checked_after_the_buy() -> None:
    # 매도를 켜지 않은 사용자: 파는 다리는 사는 다리가 체결된 가상 계좌로 검사해야 지나간다.
    s = Setup()

    preview = s.preview()

    assert preview.structure == "debit_vertical"
    assert [(leg.symbol, leg.side, leg.filled) for leg in preview.legs] == [
        (CALL, "buy", 0),
        (CALL_730, "sell", 0),
    ]
    assert (preview.net_debit, preview.worst_net_debit) == (Decimal("1.00"), Decimal("1.05"))
    assert preview.max_loss == Decimal(525000)
    assert s.combo_ids() == [preview.combo_id]
    assert s.exchange.orders == []


def test_the_sell_leg_alone_is_still_blocked_for_a_sell_off_user() -> None:
    # 같은 매도를 한 종목 주문으로 내면 묶음 예외가 없다.
    s = Setup()
    s.exchange.holdings.append((CALL, "1", 2))
    sell = OrderRequest("domestic", CALL_730, "sell", "limit", 2, Decimal("1.00"))

    with pytest.raises(RiskBlocked) as caught:
        asyncio.run(s.orders.preview(s.user, sell))

    assert caught.value.rule == "option_sell_off"


def test_the_second_buy_of_a_straddle_counts_the_first_buys_premium() -> None:
    # 725 콜 2.00 + 725 풋 1.50 매수 1계약. 하루 프리미엄 한도 80만: 콜 50만만으로는 안이고, 풋은
    # 콜이 체결된 계좌에서 50만 + 풋 한계 가격(1.55) 38.75만 = 88.75만으로 넘는다.
    s = Setup()
    s.set_limits(replace(LIMITS, options=replace(OPTION_LIMITS, premium_daily=Decimal(800000))))
    straddle = ComboRequest(
        "domestic",
        (Leg(CALL, "buy", Decimal("2.00")), Leg(PUT, "buy", Decimal("1.50"))),
        1,
        Decimal("0.05"),
    )

    assert s.blocked(straddle) == "premium_limit"
    s.set_limits(replace(LIMITS, options=replace(OPTION_LIMITS, premium_daily=Decimal(887500))))
    assert s.preview(straddle).structure == "long_straddle"


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ("no_combo_limit", "limits_not_set"),
        ("combo_limit_below_the_loss", "combo_loss_limit"),
        ("options_off", "options_off"),
        ("call_bought_put_sold", "combo_unsupported"),
    ],
)
def test_combo_rules_block_the_preview(change: str, rule: str) -> None:
    s = Setup()
    request = VERTICAL
    if change == "no_combo_limit":
        s.set_limits(combo_with_options(None))
    elif change == "combo_limit_below_the_loss":
        s.set_limits(combo_with_options(Decimal("524999")))
    elif change == "options_off":
        features.turn_off(s.conn, s.user, "options")
    else:
        request = replace(VERTICAL, legs=(VERTICAL.legs[0], Leg(PUT, "sell", Decimal("1.00"))))

    assert s.blocked(request) == rule
    assert s.combo_ids() == []


def test_an_unknown_leg_symbol_blocks_the_preview() -> None:
    s = Setup()
    request = replace(VERTICAL, legs=(VERTICAL.legs[0], Leg("B016CZZZ", "sell", Decimal("1"))))

    with pytest.raises(UnknownSymbol):
        s.preview(request)


def test_the_approval_message_lists_legs_order_worst_net_and_max_loss() -> None:
    s = Setup()

    message = s.message(s.preview().confirm_token)

    assert "수직 스프레드 매수형, 다리마다 2계약" in message
    assert f"({CALL}) 매수 지정가 2.00" in message
    assert f"({CALL_730}) 매도 1번이 체결된 수량만큼" in message
    assert message.index(CALL) < message.index(CALL_730)
    assert "계약당 1.05를 넘지 않는" in message
    assert "최대 손실: 525000" in message


def test_an_unbounded_combo_is_shown_as_not_bounded() -> None:
    s = Setup()
    features.turn_on(s.conn, s.user, "option_sell", T0)
    short_strangle = ComboRequest(
        "domestic",
        (Leg(CALL_730, "sell", Decimal("1.00")), Leg(PUT_720, "sell", Decimal("1.00"))),
        1,
        Decimal("0"),
    )

    preview = s.preview(short_strangle)

    assert preview.max_loss == Unbounded()
    assert "최대 손실: 한정되지 않음" in s.message(preview.confirm_token)


# ---- 실행 (재생 데이터) --------------------------------------------------------------------


def test_a_filled_vertical_sends_the_buy_then_the_sell_at_the_floor() -> None:
    s = Setup()
    s.exchange.script = [filled(2, "2.00"), filled(2, "0.97")]

    result = s.place(s.preview().confirm_token)

    assert (result.status, result.stop) == ("filled", None)
    assert [sent_as(o) for o in s.exchange.orders] == [
        (CALL, "1", "2", "2.00", "3"),
        (CALL_730, "2", "2", "0.95", "3"),
    ]
    assert [(leg.filled, leg.avg_price) for leg in result.legs] == [
        (2, Decimal("2.00")),
        (2, Decimal("0.97")),
    ]
    assert s.lease(result.combo_id) is None
    [placed] = s.audited("combo.place")
    assert (placed["outcome"], placed["filled"]) == ("filled", [2, 2])


def test_no_buy_fill_sends_no_sell() -> None:
    s = Setup()
    s.exchange.script = [NOTHING]

    result = s.place(s.preview().confirm_token)

    assert (result.status, result.stop) == ("nothing_filled", None)
    assert [o["sym"] for o in s.exchange.orders] == [CALL]


def test_one_of_two_bought_sells_one_at_a_floor_from_the_buy_fill() -> None:
    # 매수 1계약이 1.98에 체결: 하한 1.98 - 1.05 = 0.93.
    s = Setup()
    s.exchange.script = [[WORKING, Snap(1, "1.98")], filled(1, "0.95")]

    result = s.place(s.preview().confirm_token)

    assert result.status == "partial"
    assert sent_as(s.exchange.orders[1]) == (CALL_730, "2", "1", "0.93", "3")
    assert [leg.filled for leg in result.legs] == [1, 1]


def test_a_sell_short_three_more_times_is_partial_after_exactly_three_resends() -> None:
    s = Setup()
    s.exchange.script = [filled(2, "2.00"), NOTHING, NOTHING, NOTHING, NOTHING]

    result = s.place(s.preview().confirm_token)

    assert (result.status, result.stop) == ("partial", ComboStop("retries_exhausted"))
    sells = s.exchange.legs(CALL_730)
    assert [sent_as(o) for o in sells] == [(CALL_730, "2", "2", "0.95", "3")] * 4
    assert s.slept == [timedelta(seconds=1)] * 3


def test_a_short_sell_is_completed_by_resending_the_rest_at_the_same_limit() -> None:
    s = Setup()
    s.exchange.script = [filled(2, "2.00"), [Snap(1, "1.10")], filled(1, "0.95")]

    result = s.place(s.preview().confirm_token)

    assert result.status == "filled"
    assert [sent_as(o)[2:4] for o in s.exchange.legs(CALL_730)] == [("2", "0.95"), ("1", "0.95")]
    assert result.legs[1].avg_price == Decimal("1.025")


@pytest.mark.parametrize("bid", ["0.94", ""])
def test_a_quote_outside_the_limit_sends_no_sell(bid: str) -> None:
    s = Setup()
    s.exchange.book[CALL_730] = (bid, "1.00")
    s.exchange.script = [filled(2, "2.00")]

    result = s.place(s.preview().confirm_token)

    assert (result.status, result.stop) == ("partial", ComboStop("price_out_of_range"))
    assert s.exchange.legs(CALL_730) == []


@pytest.mark.parametrize("unread", ["occurs1_rem_ord_qty", "occurs1_exec_qty"])
def test_a_leg_row_with_an_unreadable_quantity_never_counts_as_finished(unread: str) -> None:
    # 접수 중인 파는 다리 행의 잔량, 또는 자동취소 행의 체결 수량이 빈 값이다. 끝났는지 모르므로
    # 10초를 기다린 뒤 unknown으로 멈추고 다시 보내지 않는다.
    s = Setup()
    shown = Snap(done=unread == "occurs1_exec_qty", blank=unread)
    s.exchange.script = [filled(2, "2.00"), [shown], NOTHING, NOTHING, NOTHING]

    result = s.place(s.preview().confirm_token)

    assert (result.status, result.stop) == ("unknown", ComboStop("unknown"))
    assert len(s.exchange.legs(CALL_730)) == 1


@pytest.mark.parametrize("where", ["buy_no_answer", "sell_never_ends"])
def test_an_unknown_leg_stops_and_nothing_is_sent_again(where: str) -> None:
    s = Setup()
    if where == "buy_no_answer":
        s.exchange.script = [None]
    else:
        s.exchange.script = [filled(2, "2.00"), [WORKING]]
    preview = s.preview()

    result = s.place(preview.confirm_token)

    assert (result.status, result.stop) == ("unknown", ComboStop("unknown"))
    sent = len(s.exchange.orders)
    assert sent == (1 if where == "buy_no_answer" else 2)
    with pytest.raises(UnknownState):
        s.resume(preview.combo_id)
    assert len(s.exchange.orders) == sent


def test_a_blocked_leg_check_stops_with_its_rule() -> None:
    # 미리보기 뒤 하루 프리미엄 한도를 1원으로 낮췄다: 매수 다리가 주문 직전 재검사에서 막힌다.
    s = Setup()
    token = s.preview().confirm_token
    s.set_limits(replace(LIMITS, options=replace(OPTION_LIMITS, premium_daily=Decimal(1))))

    result = s.place(token)

    assert (result.status, result.stop) == ("previewed", ComboStop("risk_blocked", "premium_limit"))
    assert s.exchange.orders == []


# ---- 이어 하기 -------------------------------------------------------------------------


def crashed_after_the_buy(s: Setup) -> str:
    """미리보기 뒤 실행이 매수 2계약 체결까지 하고 죽었다: 묶음을 쥔 채, 파는 다리는 없다."""
    combo_id = s.preview().combo_id
    combos.claim(s.conn, s.user, combo_id, s.clock.now, s.clock.now + LEASE)
    buy = OrderRequest("domestic", CALL, "buy", "limit", 2, Decimal("2.00"), "fak")
    order_id = orders.insert_submitted(s.conn, s.user, buy, s.clock.now, Origin(combo_id=combo_id))
    orders.update(s.conn, s.user, order_id, Outcome("ACCEPTED", nh_order_no="777"), s.clock.now)
    s.exchange.earlier.append(Placed("0000000777", CALL, "1", 2, "2.00").row(Snap(2, "2.00")))
    s.exchange.holdings.append((CALL, "1", 2))
    return combo_id


def test_a_stale_combo_with_only_the_buy_filled_resumes_with_only_the_sell() -> None:
    s = Setup()
    combo_id = crashed_after_the_buy(s)
    with pytest.raises(NotResumable):
        s.resume(combo_id)  # 아직 쥐고 있다
    s.clock.now += LEASE + timedelta(seconds=1)
    s.exchange.script = [filled(2, "0.96")]

    preview = s.resume(combo_id)
    message = s.message(preview.confirm_token)
    result = s.place(preview.confirm_token)

    assert [leg.filled for leg in preview.legs] == [2, 0]
    assert "이미 체결 2/2계약" in message
    assert [sent_as(o) for o in s.exchange.orders] == [(CALL_730, "2", "2", "0.95", "3")]
    assert result.status == "filled"
    assert result.legs[0].order_nos == ("777",)


def bought_only(s: Setup) -> str:
    """사는 다리 2계약만 체결되고, 파는 다리는 호가가 한계선 밖이라 보내지 않은 묶음."""
    s.exchange.book[CALL_730] = ("0.90", "1.00")
    s.exchange.script = [filled(2, "2.00")]
    first = s.place(s.preview().confirm_token)
    assert (first.status, first.stop) == ("partial", ComboStop("price_out_of_range"))
    s.exchange.book[CALL_730] = IN_RANGE
    return first.combo_id


def test_a_resume_token_is_refused_once_the_fills_moved_on() -> None:
    s = Setup()
    combo_id = bought_only(s)
    stale = s.resume(combo_id).confirm_token
    fresh = s.resume(combo_id).confirm_token
    s.exchange.script = [filled(2, "0.95")]
    assert s.place(fresh).status == "filled"

    with pytest.raises(RiskBlocked) as caught:
        s.place(stale)

    assert caught.value.rule == "warning_changed"
    assert len(s.exchange.legs(CALL_730)) == 1


def test_a_resume_token_is_refused_once_more_leg_orders_were_sent() -> None:
    # 파는 다리가 하나도 체결되지 않아 체결 수량은 그대로지만, 다리 주문이 더 나갔다.
    s = Setup()
    combo_id = bought_only(s)
    stale = s.resume(combo_id).confirm_token
    fresh = s.resume(combo_id).confirm_token
    s.exchange.script = [NOTHING] * 8
    assert s.place(fresh).stop == ComboStop("retries_exhausted")
    sells = len(s.exchange.legs(CALL_730))

    with pytest.raises(RiskBlocked) as caught:
        s.place(stale)

    assert caught.value.rule == "warning_changed"
    assert len(s.exchange.legs(CALL_730)) == sells


@pytest.mark.parametrize("cover", ["selling_on", "far_long"])
def test_a_resume_after_the_buy_leg_was_closed_by_hand_is_blocked(cover: str) -> None:
    # 묶음 기록에는 사는 다리 2계약이 체결돼 있지만 사용자가 그 콜을 직접 되팔았다. 매도를 켰거나
    # 같은 계열의 다른 매수(745 콜)를 들었어도 파는 다리는 사는 다리 종목으로만 덮인다.
    s = Setup()
    s.cover_with(cover)
    combo_id = bought_only(s)
    s.close_by_hand(CALL, 2)
    sent = len(s.exchange.orders)

    with pytest.raises(RiskBlocked) as caught:
        s.resume(combo_id)

    assert caught.value.rule == "combo_uncovered"
    assert len(s.exchange.orders) == sent


@pytest.mark.parametrize("cover", ["selling_on", "far_long"])
def test_a_buy_leg_closed_between_the_legs_stops_the_sell(cover: str) -> None:
    s = Setup()
    s.cover_with(cover)
    preview = s.preview()
    s.exchange.script = [filled(2, "2.00"), filled(2, "0.95")]
    closed: list[bool] = []

    async def close_the_buy(request: httpx2.Request) -> None:
        # 파는 다리를 보내기 직전 호가를 볼 때, 사용자가 산 콜 2계약을 직접 되팔아 두었다.
        if request.url.path == PRICE_BID and not closed:
            closed.append(True)
            s.exchange.holdings.append((CALL, "2", 2))

    s.exchange.before = close_the_buy

    result = s.place(preview.confirm_token)

    assert result.stop == ComboStop("risk_blocked", "combo_uncovered")
    assert s.exchange.legs(CALL_730) == []


def test_a_resume_after_both_legs_part_filled_keeps_the_whole_combo_within_the_worst_net() -> None:
    # 첫 실행: 매수 1 @ 1.90, 매도 1 @ 0.85(하한 그대로). 이어 하기: 매수 1 @ 2.00.
    # 남은 매도 1계약의 하한은 전체 순가격이 2 * 1.05를 넘지 않는 3.90 - 0.85 - 2.10 = 0.95다.
    # 매수 평균(1.95)으로만 세면 0.90이 되어 전체 순가격이 1.075로 한계를 넘는다.
    s = Setup()
    s.exchange.script = [[Snap(1, "1.90")], [Snap(1, "0.85")]]
    first = s.place(s.preview().confirm_token)
    assert [leg.filled for leg in first.legs] == [1, 1]
    s.exchange.script = [filled(1, "2.00"), filled(1, "0.95")]

    result = s.place(s.resume(first.combo_id).confirm_token)

    assert [sent_as(o) for o in s.exchange.orders[2:]] == [
        (CALL, "1", "1", "2.00", "3"),
        (CALL_730, "2", "1", "0.95", "3"),
    ]
    assert result.status == "filled"
    bought, sold = result.legs
    assert bought.avg_price is not None and sold.avg_price is not None
    assert bought.avg_price - sold.avg_price == Decimal("1.05")


@pytest.mark.parametrize("state", ["previewed", "nothing_filled", "filled", "yesterday"])
def test_only_todays_partial_combos_resume(state: str) -> None:
    s = Setup()
    preview = s.preview()
    if state == "nothing_filled":
        s.exchange.script = [NOTHING]
        s.place(preview.confirm_token)
    elif state == "filled":
        s.exchange.script = [filled(2, "2.00"), filled(2, "0.95")]
        s.place(preview.confirm_token)
    elif state == "yesterday":
        s.exchange.book[CALL_730] = ("0.90", "1.00")
        s.exchange.script = [filled(2, "2.00")]
        s.place(preview.confirm_token)
        s.clock.now += timedelta(days=1)

    with pytest.raises(NotResumable):
        s.resume(preview.combo_id)


# ---- 우회 -----------------------------------------------------------------------------


def test_no_token_an_expired_token_and_a_used_token_are_refused() -> None:
    s = Setup()
    with pytest.raises(ConfirmInvalid):
        s.place("not-a-token")
    expired = s.preview().confirm_token
    s.clock.now += timedelta(minutes=2)
    with pytest.raises(ConfirmInvalid):
        s.place(expired)
    s.exchange.script = [NOTHING]
    used = s.preview().confirm_token
    s.place(used)

    with pytest.raises(ConfirmInvalid):
        s.place(used)

    assert len(s.exchange.orders) == 1


def test_the_same_token_twice_at_once_sends_once() -> None:
    s = Setup()
    s.exchange.script = [[WORKING, Snap()]]
    token = s.preview().confirm_token

    async def both() -> tuple[ComboResult | BaseException, ComboResult | BaseException]:
        return await asyncio.gather(
            s.service.place(s.user, token, "approved"),
            s.service.place(s.user, token, "approved"),
            return_exceptions=True,
        )

    outcomes = asyncio.run(both())

    assert sorted(type(o).__name__ for o in outcomes) == ["ComboResult", "ConfirmInvalid"]
    assert len(s.exchange.orders) == 1


def test_two_places_of_one_combo_at_once_run_one() -> None:
    s = Setup()
    s.exchange.book[CALL_730] = ("0.90", "1.00")
    s.exchange.script = [filled(2, "2.00")]
    combo_id = s.place(s.preview().confirm_token).combo_id
    s.exchange.book[CALL_730] = IN_RANGE
    first, second = s.resume(combo_id).confirm_token, s.resume(combo_id).confirm_token
    s.exchange.script = [[WORKING, Snap(2, "0.95")]]

    async def both() -> tuple[ComboResult | BaseException, ComboResult | BaseException]:
        return await asyncio.gather(
            s.service.place(s.user, first, "approved"),
            s.service.place(s.user, second, "approved"),
            return_exceptions=True,
        )

    done, refused = asyncio.run(both())

    assert isinstance(done, ComboResult) and done.status == "filled"
    assert isinstance(refused, RiskBlocked) and refused.rule == "combo_running"
    assert len(s.exchange.legs(CALL_730)) == 1


def test_a_run_that_lost_the_combo_while_checking_its_leg_sends_nothing() -> None:
    # 실행 A가 파는 다리를 검사하는 동안(사용자 잠금 안) NH가 느려 쥔 기한이 지났다. 그사이 실행 B가
    # 같은 묶음을 쥐고 잠금을 기다린다. A는 보내지 않고 멈추고, 파는 다리는 B가 한 번만 보낸다.
    # 매도를 켜고 한도를 넉넉히 두어 리스크 규칙이 두 번째 매도를 막는 데 기대지 않는다.
    s = Setup()
    features.turn_on(s.conn, s.user, "option_sell", T0)
    s.set_limits(replace(LIMITS, options=replace(OPTION_LIMITS, max_position=20, max_short=10)))
    combo_id = bought_only(s)
    first, second = s.resume(combo_id).confirm_token, s.resume(combo_id).confirm_token
    s.exchange.script = [filled(2, "0.95"), filled(2, "0.95")]
    stalled, taken = asyncio.Event(), asyncio.Event()

    async def slow_account(request: httpx2.Request) -> None:
        if stalled.is_set():
            taken.set()  # B가 NH를 부르기 시작했다. 이미 묶음을 쥐었다.
        elif request.url.path == OPEN_INTEREST:  # A의 다리 검사가 계좌를 읽는다
            stalled.set()
            s.clock.now += LEASE + timedelta(seconds=1)
            await taken.wait()

    s.exchange.before = slow_account

    async def both() -> tuple[ComboResult, ComboResult]:
        a = asyncio.create_task(s.service.place(s.user, first, "approved"))
        await stalled.wait()
        b = asyncio.create_task(s.service.place(s.user, second, "approved"))
        return await a, await b

    a, b = asyncio.run(both())

    assert (a.stop, b.status) == (ComboStop("lease_lost"), "filled")
    assert len(s.exchange.legs(CALL_730)) == 1


def test_another_users_token_and_combo_id_are_refused() -> None:
    s = Setup()
    other = s.add_user("g-other", "other@example.com", "fake-key-2")
    preview = s.preview()

    with pytest.raises(ConfirmInvalid):
        s.place(preview.confirm_token, user=other)
    with pytest.raises(ConfirmInvalid):
        asyncio.run(s.service.pending(other, preview.confirm_token))
    with pytest.raises(UnknownCombo):
        s.resume(preview.combo_id, user=other)
    with pytest.raises(UnknownCombo):
        s.resume("0123456789abcdef")
    assert s.exchange.orders == []


def test_a_declined_approval_spends_the_token_and_sends_nothing() -> None:
    s = Setup()
    token = s.preview().confirm_token

    with pytest.raises(NotApproved):
        asyncio.run(s.service.place(s.user, token, "declined"))
    with pytest.raises(ConfirmInvalid):
        s.place(token)

    assert s.exchange.orders == []


def test_a_token_of_another_purpose_is_refused_both_ways() -> None:
    s = Setup()
    single = OrderRequest("domestic", CALL, "buy", "limit", 1, Decimal("2.00"))
    order_token = asyncio.run(s.orders.preview(s.user, single)).confirm_token
    combo_token = s.preview().confirm_token

    with pytest.raises(ConfirmInvalid):
        s.place(order_token)
    with pytest.raises(ConfirmInvalid):
        asyncio.run(s.orders.place(s.user, combo_token, "approved"))

    assert s.exchange.orders == []


def test_the_lease_is_renewed_before_each_leg_on_a_slow_nh() -> None:
    # NH 요청마다 10초가 흘러 파는 다리를 보낼 때는 처음 쥔 뒤 90초가 훨씬 지났다.
    s = Setup()
    preview = s.preview()
    s.exchange.latency = timedelta(seconds=10)
    s.exchange.script = [filled(2, "2.00"), filled(2, "0.95")]
    started = s.clock.now
    held: list[bool] = []

    def still_held() -> None:
        lease = s.lease(preview.combo_id)
        held.append(lease is not None and lease > s.clock.now)

    s.exchange.on_order.append(still_held)

    result = s.place(preview.confirm_token)

    assert result.status == "filled"
    assert s.clock.now - started > LEASE
    assert held == [True, True]


# ---- place_leg: 묶음 파는 다리의 보호, 쥔 것 확인 ------------------------------------------

SELL_LEG = OrderRequest("domestic", CALL_730, "sell", "limit", 1, Decimal("1.00"), "fak")


@pytest.mark.parametrize(
    ("account", "rule"),
    [
        ("buy_leg_held", None),
        ("nothing_held", "combo_uncovered"),
        ("buy_leg_held_with_a_resting_new_sell", "combo_uncovered"),
        ("same_series_other_call_held", "combo_uncovered"),
        ("put_held", "combo_uncovered"),
    ],
)
def test_place_leg_lets_only_a_sell_covered_by_the_buy_leg_through(
    account: str, rule: str | None
) -> None:
    # 보호는 `_book`이 계좌에서 종목마다·계열마다 센 수로 본다. 미체결 새 매도도 센다.
    s = Setup()
    held = {"nothing_held": None, "same_series_other_call_held": FAR, "put_held": PUT}
    symbol = held.get(account, CALL)
    if symbol is not None:
        s.exchange.holdings.append((symbol, "1", 1))
    if account == "buy_leg_held_with_a_resting_new_sell":
        s.exchange.resting.append(Placed("555", CALL_730, "2", 1, "1.00").row(WORKING))
    s.exchange.script = [NOTHING]
    place = s.orders.place_leg(s.user, s.leg_claim(), s.vertical(), SELL_LEG, ())

    if rule is None:
        stored = asyncio.run(place)
        assert (stored.status, stored.combo_id) == ("ACCEPTED", "c1")
        assert [sent_as(o) for o in s.exchange.orders] == [(CALL_730, "2", "1", "1.00", "3")]
    else:
        with pytest.raises(RiskBlocked) as caught:
            asyncio.run(place)
        assert caught.value.rule == rule
        assert s.exchange.orders == []


@pytest.mark.parametrize("change", ["taken_by_another_run", "a_leg_the_run_did_not_see"])
def test_place_leg_sends_nothing_without_the_combo_as_the_run_saw_it(change: str) -> None:
    s = Setup()
    s.exchange.holdings.append((CALL, "1", 1))
    s.exchange.script = [NOTHING]
    claim = s.leg_claim()
    if change == "taken_by_another_run":
        later = claim.held + timedelta(seconds=1)
        assert combos.claim(s.conn, s.user, "c1", later, later + LEASE)
    else:  # 다른 실행이 이 묶음의 다리를 보내 접수됐다
        origin = Origin(combo_id="c1")
        other = orders.insert_submitted(s.conn, s.user, SELL_LEG, s.clock.now, origin)
        accepted = Outcome("ACCEPTED", nh_order_no="999")
        assert orders.update(s.conn, s.user, other, accepted, s.clock.now)

    with pytest.raises(LeaseLost):
        asyncio.run(s.orders.place_leg(s.user, claim, s.vertical(), SELL_LEG, ()))

    assert s.exchange.orders == []
    assert len(orders.list_for_combo(s.conn, s.user, "c1")) == (change != "taken_by_another_run")


def test_place_leg_refuses_a_warning_the_person_did_not_see() -> None:
    s = Setup()
    s.clock.now = datetime(2026, 12, 8, 1, 0, tzinfo=UTC)  # 만기 2일 전 10:00 KST
    s.exchange.holdings.append((CALL, "1", 1))
    s.exchange.script = [NOTHING]
    claim = s.leg_claim()

    with pytest.raises(RiskBlocked) as caught:
        asyncio.run(s.orders.place_leg(s.user, claim, s.vertical(), SELL_LEG, ()))
    assert caught.value.rule == "warning_changed"
    assert s.exchange.orders == []

    shown = ["만기까지 2일 남았습니다."]
    stored = asyncio.run(s.orders.place_leg(s.user, claim, s.vertical(), SELL_LEG, shown))
    assert stored.status == "ACCEPTED"


def test_place_leg_needs_the_same_limits_as_any_order() -> None:
    s = Setup()
    s.exchange.holdings.append((CALL, "1", 1))
    s.set_limits(replace(LIMITS, options=None))

    with pytest.raises(LimitsNotSet):
        asyncio.run(s.orders.place_leg(s.user, s.leg_claim(), s.vertical(), SELL_LEG, ()))
    assert s.exchange.orders == []
