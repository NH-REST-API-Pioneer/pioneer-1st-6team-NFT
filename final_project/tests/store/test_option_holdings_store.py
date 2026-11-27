"""옵션 보유 기록: 잔고에 맞추기, 만기 전·후에 사라진 옵션, 같은 잔고로 다시 맞추기, 사용자 나누기.

2026-12물 옵션의 만기일은 2026-12-10(목)이다. 시각은 KST로 적는다.
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta

from nh_mcp.domain import KST, AccountState, Position, Side, UserId
from nh_mcp.store import db, option_holdings, users
from nh_mcp.store.option_holdings import Holding

CALL, PUT, FUTURE = "B016C725", "C016C725", "A016C000"
EXPIRY = date(2026, 12, 10)
EXPIRIES = {CALL: EXPIRY, PUT: EXPIRY}


def kst(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 12, day, hour, 0, tzinfo=KST)


SEEN = kst(1)


def position(symbol: str, side: Side = "buy", quantity: int = 1) -> Position:
    return Position(symbol, symbol, side, quantity, quantity, None, None, None)


def account(at: datetime, *positions: Position, complete: bool = True) -> AccountState:
    return AccountState(
        "domestic", "KRW", None, None, None, None, list(positions), [], at.astimezone(UTC), complete
    )


def setup() -> tuple[sqlite3.Connection, UserId, UserId]:
    conn = db.connect(":memory:")
    a = users.get_or_create(conn, "g-a", "a@example.com", SEEN)
    b = users.get_or_create(conn, "g-b", "b@example.com", SEEN)
    return conn, a, b


def rows(conn: sqlite3.Connection, user: UserId) -> list[Holding]:
    return option_holdings.load(conn, user, "domestic")


def test_held_options_are_recorded_and_futures_are_not() -> None:
    conn, a, _ = setup()

    seen = account(SEEN, position(CALL, quantity=2), position(PUT, "sell"), position(FUTURE))
    option_holdings.reconcile(conn, a, seen, EXPIRIES)

    assert rows(conn, a) == [
        Holding(CALL, "buy", 2, EXPIRY, SEEN, SEEN),
        Holding(PUT, "sell", 1, EXPIRY, SEEN, SEEN),
    ]


def test_balance_rows_of_the_same_option_and_side_are_recorded_as_their_sum() -> None:
    conn, a, _ = setup()

    seen = account(
        SEEN, position(CALL, quantity=2), position(CALL, quantity=3), position(CALL, "sell")
    )
    option_holdings.reconcile(conn, a, seen, EXPIRIES)

    assert rows(conn, a) == [
        Holding(CALL, "buy", 5, EXPIRY, SEEN, SEEN),
        Holding(CALL, "sell", 1, EXPIRY, SEEN, SEEN),
    ]


def test_a_quantity_change_updates_the_open_row() -> None:
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(2), position(CALL, quantity=3)), EXPIRIES)

    assert rows(conn, a) == [Holding(CALL, "buy", 3, EXPIRY, SEEN, kst(2))]


def test_an_option_gone_before_its_expiry_is_closed() -> None:
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(5)), EXPIRIES)

    assert rows(conn, a) == [Holding(CALL, "buy", 1, EXPIRY, SEEN, SEEN, kst(5), "closed")]


def test_an_option_gone_after_its_expiry_day_was_exercised_or_expired() -> None:
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(11, 9)), EXPIRIES)

    [row] = rows(conn, a)
    assert (row.closed_at, row.close_reason) == (kst(11, 9), "exercised_or_expired")


def test_an_option_gone_on_its_expiry_day_counts_as_closed_even_after_the_close() -> None:
    # 만기일 장 마감 시각과 NH가 만기 종목을 잔고에서 빼는 때를 확인하지 못해 날짜로만 가른다.
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(10, 20)), EXPIRIES)

    assert rows(conn, a)[0].close_reason == "closed"


def test_reconciling_again_with_the_same_account_changes_nothing() -> None:
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL), position(PUT)), EXPIRIES)
    after_expiry = account(kst(11), position(CALL))
    option_holdings.reconcile(conn, a, after_expiry, EXPIRIES)
    first = rows(conn, a)

    option_holdings.reconcile(conn, a, after_expiry, EXPIRIES)
    assert rows(conn, a) == first

    # 같은 잔고를 나중에 다시 받으면 마지막으로 본 시각만 바뀐다. 닫힌 행은 그대로다.
    option_holdings.reconcile(conn, a, account(kst(12), position(CALL)), EXPIRIES)
    call, put = rows(conn, a)
    assert call == Holding(CALL, "buy", 1, EXPIRY, SEEN, kst(12))
    assert put == first[1]
    assert (put.closed_at, put.close_reason) == (kst(11), "exercised_or_expired")


def test_an_incomplete_account_changes_nothing() -> None:
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(11), complete=False), EXPIRIES)
    option_holdings.reconcile(conn, a, account(kst(11), position(PUT), complete=False), EXPIRIES)

    assert rows(conn, a) == [Holding(CALL, "buy", 1, EXPIRY, SEEN, SEEN)]


def test_a_held_symbol_missing_from_the_master_keeps_its_open_row() -> None:
    # 만기 지난 종목은 마스터에서 빠진다. 잔고에 남아 있는 동안은 닫지 않는다.
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(11), position(CALL)), {})
    assert rows(conn, a) == [Holding(CALL, "buy", 1, EXPIRY, SEEN, SEEN)]

    option_holdings.reconcile(conn, a, account(kst(14)), {})
    assert rows(conn, a)[0].close_reason == "exercised_or_expired"


def test_a_side_flip_closes_one_side_and_opens_the_other() -> None:
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(2), position(CALL, "sell")), EXPIRIES)

    assert rows(conn, a) == [
        Holding(CALL, "buy", 1, EXPIRY, SEEN, SEEN, kst(2), "closed"),
        Holding(CALL, "sell", 1, EXPIRY, kst(2), kst(2)),
    ]


def test_a_symbol_held_again_after_closing_is_a_new_holding() -> None:
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)
    option_holdings.reconcile(conn, a, account(kst(2)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(3), position(CALL, quantity=4)), EXPIRIES)

    assert rows(conn, a) == [Holding(CALL, "buy", 4, EXPIRY, kst(3), kst(3))]


def test_an_older_account_applied_late_does_not_undo_a_newer_one() -> None:
    # 조회 도구와 주문 점검이 겹치면 먼저 받은 잔고를 나중에 맞출 수 있다.
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)
    option_holdings.reconcile(conn, a, account(kst(11, 11)), EXPIRIES)
    closed = rows(conn, a)

    option_holdings.reconcile(conn, a, account(kst(11, 10), position(CALL)), EXPIRIES)
    assert rows(conn, a) == closed

    option_holdings.reconcile(conn, a, account(kst(11, 12), position(CALL)), EXPIRIES)
    older = kst(11, 11) + timedelta(minutes=30)
    option_holdings.reconcile(conn, a, account(older), EXPIRIES)
    assert rows(conn, a)[0].closed_at is None


def test_another_users_rows_are_untouched_and_never_returned() -> None:
    conn, a, b = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL)), EXPIRIES)
    option_holdings.reconcile(conn, b, account(SEEN, position(CALL, "sell", 2)), EXPIRIES)

    option_holdings.reconcile(conn, a, account(kst(11)), EXPIRIES)

    assert rows(conn, b) == [Holding(CALL, "sell", 2, EXPIRY, SEEN, SEEN)]
    assert option_holdings.expired_since(conn, b, "domestic", SEEN) == []
    [mine] = option_holdings.expired_since(conn, a, "domestic", SEEN)
    assert (mine.side, mine.close_reason) == ("buy", "exercised_or_expired")
    # 다른 사용자의 빈 잔고는 내 기록을 닫지 않는다.
    option_holdings.reconcile(conn, a, account(kst(12), position(PUT)), EXPIRIES)
    option_holdings.reconcile(conn, b, account(kst(13)), EXPIRIES)
    assert rows(conn, a)[1] == Holding(PUT, "buy", 1, EXPIRY, kst(12), kst(12))


def test_expired_since_lists_only_options_closed_by_expiry_after_the_given_time() -> None:
    conn, a, _ = setup()
    option_holdings.reconcile(conn, a, account(SEEN, position(CALL), position(PUT)), EXPIRIES)
    option_holdings.reconcile(conn, a, account(kst(5), position(CALL)), EXPIRIES)  # PUT 청산
    option_holdings.reconcile(conn, a, account(kst(11)), EXPIRIES)  # CALL 행사 또는 소멸

    assert [h.symbol for h in option_holdings.expired_since(conn, a, "domestic", kst(4))] == [CALL]
    assert option_holdings.expired_since(conn, a, "domestic", kst(11, 11)) == []
