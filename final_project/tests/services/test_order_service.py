"""주문 서비스: 미리보기 → (승인) → 주문, 취소, UNKNOWN 대조. 가짜 NH와 메모리 DB로 돈다.

주문·취소·주문내역 행은 녹화가 없어 명세 필드로 만든 가짜다(Task 9에서 녹화로 바꾼다).
"""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fakes import T0, FakeNH, World

from nh_mcp.domain import KST, OrderRequest, RiskLimits, UserId
from nh_mcp.markets.domestic import (
    CANCEL,
    DEPOSIT,
    OPEN_INTEREST,
    ORDER,
    ORDER_HISTORY,
    ORDERABLE_QTY,
    PRICE,
    Ack,
    DomesticMarket,
    to_nh_order,
)
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL, DomesticProducts
from nh_mcp.nh.client import Credentials
from nh_mcp.services.approval import Approval
from nh_mcp.services.orders import (
    ORDER_TTL,
    ConfirmInvalid,
    InvalidCancel,
    LimitsNotSet,
    MarketClosed,
    NotApproved,
    OrdersDisabled,
    OrderService,
    Preview,
    RiskBlocked,
    UnknownState,
    same_order_no,
)
from nh_mcp.services.queries import BrokerRejected, Unavailable, UnknownSymbol
from nh_mcp.store import confirm_tokens, limits, orders, users
from nh_mcp.store.orders import Outcome, StoredOrder

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"  # 코스피200 F 202612, 틱 0.05, 08:45~15:45
KTB = "A656C000"  # 3년국채 F 202612, 틱 0.01
STOCK_FUTURE = "A0A6C000"  # 주식선물: 틱·거래 시간 대응이 없다
LIMITS = RiskLimits(Decimal("500000"), 3, 2)
BUY = OrderRequest("domestic", SYMBOL, "buy", "limit", 1, Decimal("784.55"))
MARKET_BUY = OrderRequest("domestic", SYMBOL, "buy", "market", 1, None)
ACK = {"ord_no": "0000000101", "msg_cd": "00000", "msg_cntn": "정상 처리"}
# T0는 2026-12-01(화) 10:00 KST.
TODAY_10 = T0.astimezone(KST)


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def occurs(*rows: dict[str, str]) -> dict[str, object]:
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": list(rows)}


def held(side: str, quantity: str, closable: str, symbol: str = SYMBOL) -> dict[str, str]:
    return {
        "occurs1_sym": symbol,
        "occurs1_ko_sym_nm": "코스피200 F 202612",
        "occurs1_trd_div": side,
        "occurs1_opint_qty": quantity,
        "occurs1_cls_psb_qty": closable,
        "occurs1_pl_amt": "0",
    }


def history(**changes: str) -> dict[str, str]:
    row = {
        "occurs1_ord_no": "0000000201",
        "occurs1_org_ord_no": "",
        "occurs1_sym": SYMBOL,
        "occurs1_sym_nm": "코스피200 F 202612",
        "occurs1_trd_div": "1",
        "occurs1_ord_sts": "1",
        "occurs1_ord_sts_nm": "접수",
        "occurs1_ord_qty": "1",
        "occurs1_ord_pric": "784.55",
        "occurs1_exec_qty": "0",
        "occurs1_rem_ord_qty": "1",
        "occurs1_ord_div_cd": "1",
        "occurs1_exch_rcv_tm": "100001",
    }
    return row | changes


class Setup:
    def __init__(self, with_limits: bool = True, orders_enabled: bool = True) -> None:
        self.nh = FakeNH()
        self.nh.reply(PRODUCT_DETAIL, recorded("product-detail"))
        self.nh.reply(DEPOSIT, recorded("deposit"))
        self.nh.reply(OPEN_INTEREST, recorded("open-interest"))
        self.nh.reply(ORDER_HISTORY, recorded("order-history"))
        self.nh.reply(ORDERABLE_QTY, recorded("orderable-quantity"))
        self.nh.reply(PRICE, recorded("price"))
        self.nh.reply(ORDER, ACK)
        self.world = World(nh=httpx2.MockTransport(self.nh))
        self.conn = self.world.conn
        self.clock = self.world.clock
        self.user: UserId = users.get_or_create(self.conn, "g-admin", "admin@example.com", T0)
        self.other: UserId = users.get_or_create(self.conn, "g-other", "other@example.com", T0)
        asyncio.run(
            self.world.store.register(self.user, "domestic", Credentials("fake-key-1", "x-sec"))
        )
        master = DomesticMaster(self.clock, httpx2.MockTransport(self.world.master))
        domestic = DomesticMarket(self.world.pool, master, self.clock)
        products = DomesticProducts(self.world.pool, self.clock)
        if with_limits:
            limits.put(self.conn, self.user, "domestic", LIMITS, T0)
        self.service = OrderService(self.conn, self.clock, domestic, products, orders_enabled)

    def preview(self, order: OrderRequest = BUY) -> Preview:
        return asyncio.run(self.service.preview(self.user, order))

    def place(
        self, token: str, approval: Approval = "approved", user: UserId | None = None
    ) -> StoredOrder:
        return asyncio.run(self.service.place(user or self.user, token, approval))

    def order_calls(self) -> int:
        return self.nh.paths().count(ORDER)

    def tokens(self) -> int:
        row: tuple[int] = self.conn.execute("SELECT COUNT(*) FROM confirm_tokens").fetchone()
        return row[0]

    def audit(self, action: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT detail FROM audit_log WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def all_audit_text(self) -> str:
        rows = self.conn.execute("SELECT detail FROM audit_log").fetchall()
        return "\n".join(str(r[0]) for r in rows)

    def stuck(self, order: OrderRequest = BUY, at: datetime = T0) -> int:
        """응답을 못 받은 주문 행을 직접 만든다."""
        order_id = orders.insert_submitted(self.conn, self.user, order, at)
        orders.update(self.conn, self.user, order_id, Outcome("UNKNOWN"), at)
        return order_id

    def row(self, order_id: int) -> StoredOrder:
        found = orders.get(self.conn, self.user, order_id)
        assert found is not None
        return found


# ---- 미리보기 -------------------------------------------------------------


def test_preview_without_limits_is_blocked_after_reading_the_account() -> None:
    s = Setup(with_limits=False)

    with pytest.raises(LimitsNotSet):
        s.preview()

    assert DEPOSIT in s.nh.paths()
    assert s.order_calls() == 0
    assert s.tokens() == 0


def test_preview_issues_a_two_minute_token_and_audits_the_time_taken() -> None:
    s = Setup()

    preview = s.preview()

    assert preview.order == BUY
    assert preview.contract_name == "코스피200 F 202612"
    assert preview.warnings == []
    assert preview.expires_at == T0 + ORDER_TTL == T0 + timedelta(minutes=2)
    row = confirm_tokens.find(s.conn, preview.confirm_token)
    assert row is not None
    assert (row.user_id, row.purpose, row.used_at) == (s.user, "order", None)
    assert s.order_calls() == 0
    (detail,) = s.audit("order.preview")
    assert detail["outcome"] == "issued"
    assert isinstance(detail["elapsed_ms"], int)
    assert detail["order"]["price"] == "784.55"
    assert preview.confirm_token not in s.all_audit_text()


def test_preview_when_the_account_cannot_be_read_is_unavailable_and_issues_nothing() -> None:
    s = Setup()
    s.nh.down_paths.add(DEPOSIT)

    with pytest.raises(Unavailable):
        s.preview()

    assert s.tokens() == 0
    assert ORDERABLE_QTY not in s.nh.paths()
    (detail,) = s.audit("order.preview")
    assert detail["outcome"] == "error"


def test_preview_when_nh_is_down_is_unavailable() -> None:
    s = Setup()
    s.nh.down = True

    with pytest.raises(Unavailable):
        s.preview()

    assert s.tokens() == 0


def test_preview_of_a_code_missing_from_the_master_is_an_unknown_symbol() -> None:
    s = Setup()

    with pytest.raises(UnknownSymbol):
        s.preview(replace(BUY, symbol="A0000000"))

    assert s.tokens() == 0


def test_preview_of_a_future_without_tick_rules_is_blocked_as_order_format() -> None:
    s = Setup()

    with pytest.raises(RiskBlocked) as caught:
        s.preview(replace(BUY, symbol=STOCK_FUTURE, price=Decimal("12000")))

    assert caught.value.rule == "order_format"
    assert s.tokens() == 0
    (detail,) = s.audit("order.preview")
    assert (detail["outcome"], detail["rule"]) == ("blocked", "order_format")


def test_preview_off_tick_is_blocked_with_the_neighbour_ticks() -> None:
    s = Setup()

    with pytest.raises(RiskBlocked) as caught:
        s.preview(replace(BUY, price=Decimal("784.53")))

    assert caught.value.rule == "order_format"
    assert "784.50" in caught.value.message
    assert "784.55" in caught.value.message


def test_preview_outside_trading_hours_is_market_closed() -> None:
    s = Setup()
    s.clock.now = datetime(2026, 12, 1, 15, 45, tzinfo=KST)

    with pytest.raises(MarketClosed):
        s.preview()


def test_market_preview_asks_the_orderable_quantity_at_the_upper_limit() -> None:
    s = Setup()

    preview = s.preview(MARKET_BUY)

    asked = [body for path, body in s.nh.requests if path == ORDERABLE_QTY]
    assert asked[-1]["ord_pric"] == "835.60"
    assert any("시장가" in w for w in preview.warnings)


def test_limit_preview_does_not_read_the_quote() -> None:
    s = Setup()

    s.preview()

    assert PRICE not in s.nh.paths()


def test_increasing_order_beyond_the_orderable_quantity_is_blocked_by_margin() -> None:
    s = Setup()
    s.nh.replies[ORDERABLE_QTY] = [(200, {"ord_psb_qty": "0"})]

    with pytest.raises(RiskBlocked) as caught:
        s.preview()

    assert caught.value.rule == "margin"


def test_orderable_quantity_failure_blocks_increasing_orders_by_margin() -> None:
    s = Setup()
    s.nh.down_paths.add(ORDERABLE_QTY)

    with pytest.raises(RiskBlocked) as caught:
        s.preview()

    assert caught.value.rule == "margin"


# ---- 다 읽지 못한 계좌 (account_unknown) ---------------------------------------

ACCOUNT_UNKNOWN_MESSAGE = (
    "계좌 정보를 다 읽지 못해 주문을 막았습니다. "
    "잠시 뒤 다시 시도하거나 get_account로 확인해 주세요."
)


def incomplete_account(s: Setup, source: str) -> None:
    """계좌를 다 읽지 못하게 만든다. 보유는 늘 매도 1계약이라 BUY는 줄이는 주문이다."""
    closing = held("2", "1", "1")
    if source == "position":
        s.nh.replies[OPEN_INTEREST] = [(200, occurs(closing, held("", "1", "1", symbol=KTB)))]
        return
    s.nh.replies[OPEN_INTEREST] = [(200, occurs(closing))]
    if source == "open_order":
        s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(occurs1_rem_ord_qty="")))]
    elif source == "repeated_key":
        s.nh.replies[OPEN_INTEREST] = [(200, occurs(closing) | {"nxt_key": "K1"})]


@pytest.mark.parametrize("source", ["position", "open_order", "repeated_key"])
def test_preview_on_an_incompletely_read_account_blocks_even_a_reducing_order(
    source: str,
) -> None:
    s = Setup()
    incomplete_account(s, source)

    with pytest.raises(RiskBlocked) as caught:
        s.preview()

    assert (caught.value.rule, caught.value.message) == ("account_unknown", ACCOUNT_UNKNOWN_MESSAGE)
    assert s.tokens() == 0
    (detail,) = s.audit("order.preview")
    assert (detail["outcome"], detail["rule"]) == ("blocked", "account_unknown")


def test_an_account_read_incompletely_at_the_recheck_sends_nothing() -> None:
    s = Setup()
    s.nh.replies[OPEN_INTEREST] = [(200, occurs(held("2", "1", "1")))]
    token = s.preview().confirm_token
    incomplete_account(s, "position")

    with pytest.raises(RiskBlocked) as caught:
        s.place(token)

    assert caught.value.rule == "account_unknown"
    assert s.order_calls() == 0


# ---- 주문 ---------------------------------------------------------------


def test_place_sends_the_previewed_order_once_and_records_it() -> None:
    s = Setup()
    token = s.preview().confirm_token

    stored = s.place(token)

    assert s.order_calls() == 1
    sent = [body for path, body in s.nh.requests if path == ORDER]
    assert sent == [to_nh_order(BUY)]
    assert (stored.status, stored.nh_order_no) == ("ACCEPTED", "0000000101")
    assert (stored.symbol, stored.side, stored.quantity, stored.price) == (
        SYMBOL,
        "buy",
        1,
        Decimal("784.55"),
    )
    (detail,) = s.audit("order.place")
    assert detail["outcome"] == "accepted"
    assert detail["order_id"] == stored.id
    assert isinstance(detail["elapsed_ms"], int)
    assert token not in s.all_audit_text()


@pytest.mark.parametrize("case", ["missing", "expired", "other_user", "limits_token"])
def test_place_with_an_unusable_token_sends_nothing(case: str) -> None:
    s = Setup()
    token = s.preview().confirm_token
    if case == "missing":
        token = "not-a-token"
    elif case == "expired":
        s.clock.now = T0 + ORDER_TTL
    elif case == "limits_token":
        token, _ = confirm_tokens.issue(
            s.conn, confirm_tokens.NewToken(s.user, "limits", "{}"), T0, ORDER_TTL
        )
    user = s.other if case == "other_user" else s.user

    with pytest.raises(ConfirmInvalid):
        s.place(token, user=user)

    assert s.order_calls() == 0


def test_a_token_works_only_once() -> None:
    s = Setup()
    token = s.preview().confirm_token
    s.place(token)

    with pytest.raises(ConfirmInvalid):
        s.place(token)

    assert s.order_calls() == 1


def test_declined_consumes_the_token_and_sends_nothing() -> None:
    s = Setup()
    token = s.preview().confirm_token

    with pytest.raises(NotApproved):
        s.place(token, "declined")
    with pytest.raises(ConfirmInvalid):
        s.place(token, "approved")

    assert s.order_calls() == 0
    assert [d["outcome"] for d in s.audit("order.approve")] == ["declined"]


def test_unsupported_approval_goes_ahead_with_the_token() -> None:
    s = Setup()
    token = s.preview().confirm_token

    stored = s.place(token, "unsupported")

    assert stored.status == "ACCEPTED"
    assert s.order_calls() == 1


def test_pending_shows_the_stored_order_without_consuming_the_token() -> None:
    s = Setup()
    token = s.preview().confirm_token

    pending = asyncio.run(s.service.pending(s.user, token))

    assert pending.order == BUY
    assert pending.contract_name == "코스피200 F 202612"
    assert pending.state == hashlib.sha256(token.encode()).hexdigest()
    for part in ("코스피200 F 202612", SYMBOL, "매수", "지정가", "1계약", "784.55"):
        assert part in pending.message
    row = confirm_tokens.find(s.conn, token)
    assert row is not None
    assert row.used_at is None


def test_pending_message_carries_the_warnings() -> None:
    s = Setup()
    token = s.preview(MARKET_BUY).confirm_token

    pending = asyncio.run(s.service.pending(s.user, token))

    assert "시장가" in pending.message
    assert pending.warnings == s.preview(MARKET_BUY).warnings


def test_pending_with_an_unusable_token_is_invalid() -> None:
    s = Setup()
    token = s.preview().confirm_token

    with pytest.raises(ConfirmInvalid):
        asyncio.run(s.service.pending(s.other, token))
    with pytest.raises(ConfirmInvalid):
        asyncio.run(s.service.pending(s.user, "not-a-token"))


def test_approval_that_takes_longer_than_two_minutes_sends_nothing() -> None:
    # Review Focus 3: 승인 창이 뜬 채 2분이 지남.
    s = Setup()
    token = s.preview().confirm_token
    asyncio.run(s.service.pending(s.user, token))
    s.clock.now = T0 + ORDER_TTL + timedelta(seconds=1)

    with pytest.raises(ConfirmInvalid):
        s.place(token, "approved")

    assert s.order_calls() == 0


def test_market_closing_between_preview_and_place_blocks_and_consumes_the_token() -> None:
    # Review Focus 4: 15:44 미리보기, 15:45 주문.
    s = Setup()
    s.clock.now = datetime(2026, 12, 1, 15, 44, tzinfo=KST)
    preview = s.preview()
    assert any("종가 단일가" in w for w in preview.warnings)
    s.clock.now = datetime(2026, 12, 1, 15, 45, tzinfo=KST)

    with pytest.raises(MarketClosed):
        s.place(preview.confirm_token)

    assert s.order_calls() == 0
    row = confirm_tokens.find(s.conn, preview.confirm_token)
    assert row is not None
    assert row.used_at is not None
    (detail,) = s.audit("order.place")
    assert (detail["outcome"], detail["rule"]) == ("blocked", "trading_hours")


def test_a_warning_that_appears_after_the_preview_blocks_the_order() -> None:
    # 15:34:50에 확인한 주문이 15:35 뒤에 나가면 사람이 보지 못한 종가 단일가로 체결된다.
    s = Setup()
    s.clock.now = datetime(2026, 12, 1, 15, 34, 50, tzinfo=KST)
    preview = s.preview()
    assert preview.warnings == []
    s.clock.now = datetime(2026, 12, 1, 15, 35, 10, tzinfo=KST)

    with pytest.raises(RiskBlocked) as caught:
        s.place(preview.confirm_token)

    assert caught.value.rule == "warning_changed"
    assert caught.value.message.startswith("주문을 확인한 뒤 새 경고가 생겼습니다: ")
    assert "종가 단일가" in caught.value.message
    assert caught.value.message.endswith("다시 미리보기 해 주세요.")
    assert s.order_calls() == 0
    row = confirm_tokens.find(s.conn, preview.confirm_token)
    assert row is not None
    assert row.used_at is not None
    (detail,) = s.audit("order.place")
    assert (detail["outcome"], detail["rule"]) == ("blocked", "warning_changed")


def test_a_warning_already_shown_in_the_preview_does_not_block() -> None:
    s = Setup()
    s.clock.now = datetime(2026, 12, 1, 15, 35, 10, tzinfo=KST)
    preview = s.preview()
    assert any("종가 단일가" in w for w in preview.warnings)
    s.clock.now = datetime(2026, 12, 1, 15, 35, 30, tzinfo=KST)

    assert s.place(preview.confirm_token).status == "ACCEPTED"


def test_recheck_uses_the_limits_of_the_moment() -> None:
    s = Setup()
    token = s.preview().confirm_token
    limits.put(s.conn, s.user, "domestic", replace(LIMITS, max_position=0), T0)

    with pytest.raises(RiskBlocked) as caught:
        s.place(token)

    assert caught.value.rule == "max_position"
    assert s.order_calls() == 0


def test_the_same_token_placed_twice_at_once_sends_one_order() -> None:
    # Review Focus 1.
    s = Setup()
    token = s.preview().confirm_token

    async def both() -> list[object]:
        return list(
            await asyncio.gather(
                s.service.place(s.user, token, "approved"),
                s.service.place(s.user, token, "approved"),
                return_exceptions=True,
            )
        )

    results = asyncio.run(both())

    assert s.order_calls() == 1
    assert sum(isinstance(r, StoredOrder) for r in results) == 1
    assert sum(isinstance(r, ConfirmInvalid) for r in results) == 1


def test_no_response_to_an_order_is_unknown_and_never_resent() -> None:
    s = Setup()
    token = s.preview().confirm_token
    s.nh.down_paths.add(ORDER)

    with pytest.raises(UnknownState):
        s.place(token)

    assert s.order_calls() == 1
    (row,) = orders.list_for(s.conn, s.user)
    assert (row.status, row.nh_order_no) == ("UNKNOWN", None)
    (detail,) = s.audit("order.place")
    assert detail["outcome"] == "unknown"


def test_a_cancelled_send_is_recorded_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # 전송 중에 취소되면 나갔는지 모른다. SUBMITTED로 두면 대조되지 않고 막지도 않는다.
    s = Setup()
    token = s.preview().confirm_token
    started: list[OrderRequest] = []

    async def hang(self: DomesticMarket, user_id: UserId, order: OrderRequest) -> Ack:
        started.append(order)
        await asyncio.sleep(3600)
        raise AssertionError("여기까지 오지 않는다")

    monkeypatch.setattr(DomesticMarket, "send_order", hang)

    async def cancel_midway() -> None:
        task = asyncio.ensure_future(s.service.place(s.user, token, "approved"))
        while not started:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_midway())

    (row,) = orders.list_for(s.conn, s.user)
    assert row.status == "UNKNOWN"


def test_rejection_is_recorded_with_the_nh_message() -> None:
    s = Setup()
    token = s.preview().confirm_token
    s.nh.replies[ORDER] = [(200, {"ord_no": "", "msg_cd": "40250", "msg_cntn": "수량 부족"})]

    with pytest.raises(BrokerRejected) as caught:
        s.place(token)

    assert (caught.value.code, caught.value.message) == ("40250", "수량 부족")
    (row,) = orders.list_for(s.conn, s.user)
    assert (row.status, row.msg_cd, row.msg_cntn) == ("REJECTED", "40250", "수량 부족")
    (detail,) = s.audit("order.place")
    assert (detail["outcome"], detail["msg_cd"]) == ("rejected", "40250")


def failing(name: str) -> object:
    def broken(*_: object, **__: object) -> object:
        raise sqlite3.OperationalError(f"{name} 실패")

    return broken


def nothing(*_: object, **__: object) -> None:
    return None


@pytest.mark.parametrize("reply", ["accepted", "rejected", "no_answer"])
def test_a_store_failure_after_the_send_is_unknown_and_audited(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    s = Setup()
    token = s.preview().confirm_token
    if reply == "rejected":
        s.nh.replies[ORDER] = [(200, {"ord_no": "", "msg_cd": "40250", "msg_cntn": "수량 부족"})]
    elif reply == "no_answer":
        s.nh.down_paths.add(ORDER)
    monkeypatch.setattr(orders, "update", failing("update"))

    with pytest.raises(UnknownState) as caught:
        s.place(token)

    assert "get_orders" in caught.value.message
    assert s.order_calls() == 1
    (detail,) = s.audit("order.place")
    assert detail["outcome"] == "unknown"
    monkeypatch.undo()
    (row,) = orders.list_for(s.conn, s.user)
    assert row.status == "SUBMITTED"  # 나갔는지 모르는 행으로 남아 늘리는 주문을 막는다
    with pytest.raises(UnknownState):
        s.preview()


def test_a_row_that_cannot_be_read_back_after_the_send_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Setup()
    token = s.preview().confirm_token
    monkeypatch.setattr(orders, "get", nothing)

    with pytest.raises(UnknownState):
        s.place(token)

    assert s.order_calls() == 1
    (detail,) = s.audit("order.place")
    assert detail["outcome"] == "unknown"


def test_a_store_failure_before_the_send_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Setup()
    token = s.preview().confirm_token
    monkeypatch.setattr(orders, "insert_submitted", failing("insert_submitted"))

    with pytest.raises(sqlite3.OperationalError):
        s.place(token)

    assert s.order_calls() == 0


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {"errorCode": "EGW00500", "errorMessage": "오류"}),
        (504, b""),
        (408, b"timeout"),
    ],
)
def test_server_error_or_timeout_status_on_an_order_is_unknown_and_never_resent(
    status: int, body: dict[str, str] | bytes
) -> None:
    # 5xx·408은 NH 안쪽에서 처리됐는지 알 수 없다. 거부로 보면 AI가 다시 주문해 두 번 나갈 수 있다.
    s = Setup()
    token = s.preview().confirm_token
    s.nh.replies[ORDER] = [(status, body)]

    with pytest.raises(UnknownState):
        s.place(token)

    assert s.order_calls() == 1
    (row,) = orders.list_for(s.conn, s.user)
    assert (row.status, row.nh_order_no) == ("UNKNOWN", None)
    (detail,) = s.audit("order.place")
    assert detail["outcome"] == "unknown"
    with pytest.raises(UnknownState):
        s.preview()


@pytest.mark.parametrize("status", [200, 400])
def test_error_code_with_a_client_or_ok_status_is_a_rejection(status: int) -> None:
    s = Setup()
    token = s.preview().confirm_token
    s.nh.replies[ORDER] = [(status, {"errorCode": "40250", "errorMessage": "수량 부족"})]

    with pytest.raises(BrokerRejected):
        s.place(token)

    (row,) = orders.list_for(s.conn, s.user)
    assert (row.status, row.msg_cd) == ("REJECTED", "40250")


# ---- UNKNOWN이 남은 종목 --------------------------------------------------


def test_unknown_order_blocks_increasing_orders_on_that_symbol() -> None:
    s = Setup()
    s.stuck()

    with pytest.raises(UnknownState):
        s.preview()

    assert s.tokens() == 0


def test_unknown_order_lets_a_reducing_order_through() -> None:
    s = Setup()
    s.stuck()
    s.nh.replies[OPEN_INTEREST] = [(200, occurs(held("2", "1", "1")))]

    preview = s.preview()

    assert preview.order == BUY


def test_unknown_order_does_not_block_other_symbols() -> None:
    s = Setup()
    s.stuck()

    preview = s.preview(OrderRequest("domestic", KTB, "buy", "limit", 1, Decimal("104.01")))

    assert preview.contract_name.startswith("3년국채")


def test_unknown_order_also_blocks_place() -> None:
    s = Setup()
    token = s.preview().confirm_token
    s.stuck()

    with pytest.raises(UnknownState):
        s.place(token)

    assert s.order_calls() == 0


def test_yesterdays_unknown_order_does_not_block_and_is_not_reconciled() -> None:
    s = Setup()
    yesterday = s.stuck(at=T0 - timedelta(days=1))
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(occurs1_exch_rcv_tm="000001")))]

    s.preview()

    assert (s.row(yesterday).status, s.row(yesterday).nh_order_no) == ("UNKNOWN", None)


def test_todays_submitted_row_blocks_increasing_orders_on_that_symbol() -> None:
    # 잠금 안에서 보이는 SUBMITTED는 전송 중에 멈춘 행이다. 나갔는지 모른다.
    s = Setup()
    orders.insert_submitted(s.conn, s.user, BUY, T0)

    with pytest.raises(UnknownState):
        s.preview()

    preview = s.preview(OrderRequest("domestic", KTB, "buy", "limit", 1, Decimal("104.01")))
    assert preview.contract_name.startswith("3년국채")


def test_todays_submitted_row_lets_a_reducing_order_through() -> None:
    s = Setup()
    orders.insert_submitted(s.conn, s.user, BUY, T0)
    s.nh.replies[OPEN_INTEREST] = [(200, occurs(held("2", "1", "1")))]

    assert s.preview().order == BUY


def test_yesterdays_submitted_row_does_not_block() -> None:
    s = Setup()
    stale = orders.insert_submitted(s.conn, s.user, BUY, T0 - timedelta(days=1))

    s.preview()

    assert s.row(stale).status == "SUBMITTED"


def test_submitted_row_is_reconciled_like_an_unknown_one() -> None:
    s = Setup()
    stale = orders.insert_submitted(s.conn, s.user, BUY, T0)
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(occurs1_ord_sts="6")))]

    asyncio.run(s.service.reconcile(s.user))

    assert (s.row(stale).status, s.row(stale).nh_order_no) == ("FILLED", "0000000201")
    s.preview()  # 정리됐으니 막지 않는다


def test_a_failed_history_lookup_still_lets_a_reducing_order_through() -> None:
    s = Setup()
    stuck = s.stuck()
    s.nh.replies[OPEN_INTEREST] = [(200, occurs(held("2", "1", "1")))]
    # 첫 주문 내역 조회(대조)는 503, 다음(계좌의 미체결)은 정상.
    s.nh.replies[ORDER_HISTORY] = [(503, b""), (200, recorded("order-history"))]

    assert s.preview().order == BUY

    assert (s.row(stuck).status, s.row(stuck).nh_order_no) == ("UNKNOWN", None)
    (detail,) = s.audit("order.reconcile")
    assert detail["outcome"] == "failed"


def test_a_failed_history_lookup_still_blocks_increasing_orders_on_that_symbol() -> None:
    s = Setup()
    s.stuck()
    s.nh.replies[ORDER_HISTORY] = [(503, b""), (200, recorded("order-history"))]

    with pytest.raises(UnknownState):
        s.preview()

    assert s.tokens() == 0


def test_the_public_reconcile_still_reports_a_failed_history_lookup() -> None:
    s = Setup()
    s.stuck()
    s.nh.replies[ORDER_HISTORY] = [(503, b"")]

    with pytest.raises(BrokerRejected):
        asyncio.run(s.service.reconcile(s.user))


# ---- 대조 ---------------------------------------------------------------


def test_reconcile_attaches_the_single_matching_order() -> None:
    s = Setup()
    stuck = s.stuck(at=T0 + timedelta(milliseconds=500))
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(occurs1_exch_rcv_tm="100000")))]

    asyncio.run(s.service.reconcile(s.user))

    assert (s.row(stuck).status, s.row(stuck).nh_order_no) == ("ACCEPTED", "0000000201")
    (detail,) = s.audit("order.reconcile")
    assert (detail["outcome"], detail["order_id"]) == ("matched", stuck)


@pytest.mark.parametrize(
    ("code", "status"),
    [("1", "ACCEPTED"), ("4", "ACCEPTED"), ("6", "FILLED"), ("8", "CANCELLED"), ("9", "REJECTED")],
)
def test_reconcile_maps_the_nh_order_state(code: str, status: str) -> None:
    s = Setup()
    stuck = s.stuck()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(occurs1_ord_sts=code)))]

    asyncio.run(s.service.reconcile(s.user))

    assert s.row(stuck).status == status


def test_reconcile_ignores_price_for_market_orders() -> None:
    s = Setup()
    stuck = s.stuck(MARKET_BUY)
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(occurs1_ord_pric="0")))]

    asyncio.run(s.service.reconcile(s.user))

    assert s.row(stuck).nh_order_no == "0000000201"


@pytest.mark.parametrize(
    "different",
    [
        {"occurs1_sym": KTB},
        {"occurs1_trd_div": "2"},
        {"occurs1_ord_qty": "2"},
        {"occurs1_ord_pric": "784.60"},
        {"occurs1_ord_div_cd": "2"},
        {"occurs1_exch_rcv_tm": "095959"},
        {"occurs1_exch_rcv_tm": ""},
        {"occurs1_ord_no": ""},
    ],
)
def test_reconcile_leaves_unknown_when_nothing_matches(different: dict[str, str]) -> None:
    s = Setup()
    stuck = s.stuck()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(**different)))]

    asyncio.run(s.service.reconcile(s.user))

    assert (s.row(stuck).status, s.row(stuck).nh_order_no) == ("UNKNOWN", None)


def test_reconcile_leaves_unknown_when_two_orders_match() -> None:
    s = Setup()
    stuck = s.stuck()
    rows = occurs(history(), history(occurs1_ord_no="0000000202", occurs1_exch_rcv_tm="100005"))
    s.nh.replies[ORDER_HISTORY] = [(200, rows)]

    asyncio.run(s.service.reconcile(s.user))

    assert (s.row(stuck).status, s.row(stuck).nh_order_no) == ("UNKNOWN", None)
    (detail,) = s.audit("order.reconcile")
    assert (detail["outcome"], detail["candidates"]) == ("ambiguous", 2)


def test_reconcile_skips_order_numbers_already_bound_to_another_row() -> None:
    s = Setup()
    earlier = orders.insert_submitted(s.conn, s.user, BUY, T0)
    orders.update(s.conn, s.user, earlier, Outcome("ACCEPTED", nh_order_no="0000000201"), T0)
    stuck = s.stuck()
    rows = occurs(history(), history(occurs1_ord_no="0000000202", occurs1_exch_rcv_tm="100005"))
    s.nh.replies[ORDER_HISTORY] = [(200, rows)]

    asyncio.run(s.service.reconcile(s.user))

    assert s.row(stuck).nh_order_no == "0000000202"


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("201", "0000000201", True),
        (" 0000000201 ", "201", True),
        ("0", "000", True),
        ("201", "202", False),
        ("2010", "201", False),
        ("", "", False),
        ("", "0", False),
        ("  ", "", False),
    ],
)
def test_same_order_no_ignores_zero_padding_but_never_matches_empty(
    a: str, b: str, same: bool
) -> None:
    assert same_order_no(a, b) is same
    assert same_order_no(b, a) is same


@pytest.mark.parametrize(("bound", "listed"), [("201", "0000000201"), ("0000000201", "201")])
def test_reconcile_treats_a_differently_padded_number_as_already_bound(
    bound: str, listed: str
) -> None:
    s = Setup()
    earlier = orders.insert_submitted(s.conn, s.user, BUY, T0)
    orders.update(s.conn, s.user, earlier, Outcome("ACCEPTED", nh_order_no=bound), T0)
    stuck = s.stuck()
    rows = occurs(
        history(occurs1_ord_no=listed),
        history(occurs1_ord_no="0000000202", occurs1_exch_rcv_tm="100005"),
    )
    s.nh.replies[ORDER_HISTORY] = [(200, rows)]

    asyncio.run(s.service.reconcile(s.user))

    assert s.row(stuck).nh_order_no == "0000000202"


def test_one_nh_order_is_not_attached_to_two_unknown_rows() -> None:
    s = Setup()
    first, second = s.stuck(), s.stuck()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history()))]

    asyncio.run(s.service.reconcile(s.user))

    numbers = [s.row(first).nh_order_no, s.row(second).nh_order_no]
    assert numbers.count("0000000201") == 1
    assert numbers.count(None) == 1


def test_reconcile_without_unknown_rows_does_not_call_nh() -> None:
    s = Setup()

    asyncio.run(s.service.reconcile(s.user))

    assert s.nh.paths() == []


def test_preview_reconciles_first_and_then_lets_the_order_through() -> None:
    s = Setup()
    stuck = s.stuck()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(occurs1_ord_sts="6")))]

    s.preview()

    assert s.row(stuck).status == "FILLED"


def test_reconcile_touches_only_that_users_rows() -> None:
    s = Setup()
    theirs = orders.insert_submitted(s.conn, s.other, BUY, T0)
    orders.update(s.conn, s.other, theirs, Outcome("UNKNOWN"), T0)
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history()))]

    asyncio.run(s.service.reconcile(s.user))

    found = orders.get(s.conn, s.other, theirs)
    assert found is not None
    assert (found.status, found.nh_order_no) == ("UNKNOWN", None)


# ---- 손실 한도와 청산 --------------------------------------------------------


def test_closing_orders_pass_when_the_daily_loss_limit_is_hit() -> None:
    s = Setup()
    deposit = recorded("deposit") | {"fut_rez_pl_amt": "-1000000"}
    s.nh.replies[DEPOSIT] = [(200, deposit)]
    s.nh.replies[OPEN_INTEREST] = [(200, occurs(held("2", "1", "1")))]

    token = s.preview().confirm_token
    stored = s.place(token)

    assert stored.status == "ACCEPTED"
    with pytest.raises(RiskBlocked) as caught:
        s.preview(replace(BUY, quantity=2))
    assert caught.value.rule == "daily_loss"


# ---- 취소 ---------------------------------------------------------------


def open_order(**changes: str) -> dict[str, str]:
    base = {
        "occurs1_ord_no": "0000000301",
        "occurs1_trd_div": "2",
        "occurs1_ord_qty": "3",
        "occurs1_exec_qty": "1",
        "occurs1_rem_ord_qty": "2",
    }
    return history(**(base | changes))


def test_cancel_sends_the_remaining_quantity_of_that_order() -> None:
    s = Setup()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(history(), open_order()))]
    s.nh.reply(CANCEL, {"ord_no": "0000000302", "msg_cd": "", "msg_cntn": ""})

    ack = asyncio.run(s.service.cancel(s.user, "0000000301"))

    assert ack.order_no == "0000000302"
    sent = [body for path, body in s.nh.requests if path == CANCEL]
    assert sent == [{"sym": SYMBOL, "trd_div": "2", "ord_qty": "2", "org_ord_no": "0000000301"}]
    (detail,) = s.audit("order.cancel")
    assert detail["outcome"] == "accepted"
    assert isinstance(detail["elapsed_ms"], int)


def test_cancel_finds_the_order_whatever_the_zero_padding() -> None:
    s = Setup()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(open_order()))]
    s.nh.reply(CANCEL, {"ord_no": "0000000302", "msg_cd": "", "msg_cntn": ""})

    asyncio.run(s.service.cancel(s.user, "301"))

    sent = [body for path, body in s.nh.requests if path == CANCEL]
    # 원주문번호는 NH가 내역에 적은 그대로 보낸다.
    assert [body["org_ord_no"] for body in sent] == ["0000000301"]


@pytest.mark.parametrize(
    ("order_no", "rows"),
    [
        ("0000000999", [open_order()]),
        ("0000000301", [open_order(occurs1_rem_ord_qty="0")]),
        ("", [open_order(occurs1_ord_no="")]),
        ("0000000301", [open_order(occurs1_ord_div_cd="3")]),
    ],
)
def test_cancel_of_an_order_that_cannot_be_cancelled_is_invalid(
    order_no: str, rows: list[dict[str, str]]
) -> None:
    s = Setup()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(*rows))]

    with pytest.raises(InvalidCancel):
        asyncio.run(s.service.cancel(s.user, order_no))

    assert CANCEL not in s.nh.paths()


def test_no_response_to_a_cancel_is_unknown_and_never_resent() -> None:
    s = Setup()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(open_order()))]
    s.nh.down_paths.add(CANCEL)

    with pytest.raises(UnknownState):
        asyncio.run(s.service.cancel(s.user, "0000000301"))

    assert s.nh.paths().count(CANCEL) == 1


@pytest.mark.parametrize(("status", "body"), [(503, b""), (408, b""), (500, {"errorCode": "X"})])
def test_server_error_or_timeout_status_on_a_cancel_is_unknown(
    status: int, body: dict[str, str] | bytes
) -> None:
    s = Setup()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(open_order()))]
    s.nh.reply(CANCEL, body, status)

    with pytest.raises(UnknownState):
        asyncio.run(s.service.cancel(s.user, "0000000301"))

    assert s.nh.paths().count(CANCEL) == 1


def test_cancel_with_a_client_error_status_is_broker_rejected() -> None:
    s = Setup()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(open_order()))]
    s.nh.reply(CANCEL, {"errorCode": "40300", "errorMessage": "취소 불가"}, 400)

    with pytest.raises(BrokerRejected):
        asyncio.run(s.service.cancel(s.user, "0000000301"))


def test_cancel_rejection_is_broker_rejected() -> None:
    s = Setup()
    s.nh.replies[ORDER_HISTORY] = [(200, occurs(open_order()))]
    s.nh.reply(CANCEL, {"ord_no": "", "msg_cd": "40300", "msg_cntn": "취소 불가"})

    with pytest.raises(BrokerRejected):
        asyncio.run(s.service.cancel(s.user, "0000000301"))


def test_cancel_when_history_cannot_be_read_is_unavailable() -> None:
    s = Setup()
    s.nh.down_paths.add(ORDER_HISTORY)

    with pytest.raises(Unavailable):
        asyncio.run(s.service.cancel(s.user, "0000000301"))

    assert CANCEL not in s.nh.paths()


# ---- 주문 스위치 ------------------------------------------------------------


def test_orders_disabled_refuses_preview_place_and_cancel() -> None:
    s = Setup(orders_enabled=False)
    token, _ = confirm_tokens.issue(
        s.conn, confirm_tokens.NewToken(s.user, "order", "{}"), T0, ORDER_TTL
    )

    with pytest.raises(OrdersDisabled):
        s.preview()
    with pytest.raises(OrdersDisabled):
        s.place(token)
    with pytest.raises(OrdersDisabled):
        asyncio.run(s.service.cancel(s.user, "0000000301"))

    assert s.nh.paths() == []
