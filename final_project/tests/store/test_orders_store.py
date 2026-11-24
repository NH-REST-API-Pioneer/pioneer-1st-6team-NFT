import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from nh_mcp.domain import OrderRequest, UserId
from nh_mcp.store import db, orders, users
from nh_mcp.store.orders import Outcome

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
LIMIT_BUY = OrderRequest("domestic", "A016C000", "buy", "limit", 1, Decimal("350.05"))
MARKET_SELL = OrderRequest("domestic", "A016C000", "sell", "market", 2, None)


def two_users(conn: sqlite3.Connection) -> tuple[UserId, UserId]:
    return (
        users.get_or_create(conn, "sub-a", "a@example.com", NOW),
        users.get_or_create(conn, "sub-b", "b@example.com", NOW),
    )


def test_insert_writes_a_submitted_row_with_the_request() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    order_id = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)
    market_id = orders.insert_submitted(conn, user_a, MARKET_SELL, NOW)

    stored = {row.id: row for row in orders.list_for(conn, user_a)}
    first = stored[order_id]
    assert first.status == "SUBMITTED"
    assert (first.market, first.symbol, first.side, first.type) == (
        "domestic",
        "A016C000",
        "buy",
        "limit",
    )
    assert (first.quantity, first.price) == (1, Decimal("350.05"))
    assert (first.nh_order_no, first.msg_cd, first.msg_cntn) == (None, None, None)
    assert first.submitted_at == NOW
    assert first.updated_at == NOW
    assert stored[market_id].price is None


def test_update_changes_status_and_keeps_fields_that_were_not_given() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    order_id = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)
    later = NOW + timedelta(seconds=3)

    orders.update(conn, user_a, order_id, Outcome("ACCEPTED", nh_order_no="0000123"), later)
    orders.update(conn, user_a, order_id, Outcome("FILLED"), later + timedelta(seconds=1))

    (row,) = orders.list_for(conn, user_a)
    assert row.status == "FILLED"
    assert row.nh_order_no == "0000123"
    assert row.submitted_at == NOW
    assert row.updated_at == later + timedelta(seconds=1)


def test_update_stores_the_rejection_message() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    order_id = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)

    orders.update(
        conn, user_a, order_id, Outcome("REJECTED", msg_cd="E1234", msg_cntn="거부됨"), NOW
    )

    (row,) = orders.list_for(conn, user_a)
    assert (row.status, row.msg_cd, row.msg_cntn) == ("REJECTED", "E1234", "거부됨")


def test_unknown_lists_only_unknown_rows_of_that_user_and_symbol() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    stuck = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)
    other_symbol = orders.insert_submitted(
        conn, user_a, OrderRequest("domestic", "A056C000", "buy", "market", 1, None), NOW
    )
    fine = orders.insert_submitted(conn, user_a, MARKET_SELL, NOW)
    theirs = orders.insert_submitted(conn, user_b, LIMIT_BUY, NOW)
    for order_id, user in ((stuck, user_a), (other_symbol, user_a), (theirs, user_b)):
        orders.update(conn, user, order_id, Outcome("UNKNOWN"), NOW)
    orders.update(conn, user_a, fine, Outcome("ACCEPTED"), NOW)

    assert [row.id for row in orders.unknown(conn, user_a, "A016C000")] == [stuck]
    assert {row.id for row in orders.unknown(conn, user_a)} == {stuck, other_symbol}
    assert [row.id for row in orders.unknown(conn, user_b, "A016C000")] == [theirs]


def test_unknown_is_empty_for_a_user_without_orders() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    stuck = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)
    orders.update(conn, user_a, stuck, Outcome("UNKNOWN"), NOW)

    assert orders.unknown(conn, user_b, "A016C000") == []


def test_update_cannot_change_another_users_order() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    order_id = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)

    orders.update(conn, user_b, order_id, Outcome("REJECTED"), NOW)

    (row,) = orders.list_for(conn, user_a)
    assert row.status == "SUBMITTED"


def test_bound_order_numbers_are_per_user() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    first = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)
    orders.insert_submitted(conn, user_a, MARKET_SELL, NOW)
    theirs = orders.insert_submitted(conn, user_b, LIMIT_BUY, NOW)
    orders.update(conn, user_a, first, Outcome("ACCEPTED", nh_order_no="0000123"), NOW)
    orders.update(conn, user_b, theirs, Outcome("ACCEPTED", nh_order_no="0000999"), NOW)

    assert orders.bound_order_numbers(conn, user_a) == {"0000123"}
    assert orders.bound_order_numbers(conn, user_b) == {"0000999"}


def test_update_says_whether_a_row_changed() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    order_id = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)

    assert orders.update(conn, user_a, order_id, Outcome("ACCEPTED"), NOW) is True
    assert orders.update(conn, user_b, order_id, Outcome("REJECTED"), NOW) is False
    assert orders.update(conn, user_a, order_id + 99, Outcome("REJECTED"), NOW) is False


def test_get_reads_one_row_of_that_user_only() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    order_id = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)

    row = orders.get(conn, user_a, order_id)
    assert row is not None
    assert (row.id, row.status, row.price) == (order_id, "SUBMITTED", Decimal("350.05"))
    assert orders.get(conn, user_b, order_id) is None
    assert orders.get(conn, user_a, order_id + 1) is None


def test_unsettled_lists_unknown_and_submitted_rows_of_that_user_and_symbol() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    submitted = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)
    stuck = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)
    orders.update(conn, user_a, stuck, Outcome("UNKNOWN"), NOW)
    fine = orders.insert_submitted(conn, user_a, LIMIT_BUY, NOW)
    orders.update(conn, user_a, fine, Outcome("ACCEPTED"), NOW)
    other_symbol = orders.insert_submitted(
        conn, user_a, OrderRequest("domestic", "A056C000", "buy", "market", 1, None), NOW
    )
    theirs = orders.insert_submitted(conn, user_b, LIMIT_BUY, NOW)

    assert [r.id for r in orders.unsettled(conn, user_a, "A016C000")] == [submitted, stuck]
    assert {r.id for r in orders.unsettled(conn, user_a)} == {submitted, stuck, other_symbol}
    assert [r.id for r in orders.unsettled(conn, user_b)] == [theirs]
