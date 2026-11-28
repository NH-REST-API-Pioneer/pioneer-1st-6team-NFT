"""옵션 묶음 기록: 넣은 그대로 읽히고, 쥐기는 한 실행만 이긴다. 사용자 나눔은 test_store_keys.py."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from nh_mcp.domain import OrderRequest, UserId
from nh_mcp.store import combos, db, orders, users
from nh_mcp.store.combos import Combo, ComboRow
from nh_mcp.store.orders import Origin

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
LEASE = timedelta(seconds=90)


def setup(conn: sqlite3.Connection) -> UserId:
    return users.get_or_create(conn, "g-a", "a@example.com", NOW)


def a_combo(user: UserId, combo_id: str = "c1") -> Combo:
    return Combo(
        combo_id, user, "domestic", '{"quantity":1}', '{"structure":"debit_vertical"}', NOW
    )


def lease(conn: sqlite3.Connection, user: UserId, combo_id: str = "c1") -> datetime | None:
    row = combos.get(conn, user, combo_id)
    assert row is not None
    return row.lease_until


def test_a_combo_reads_back_as_it_was_written_with_no_lease() -> None:
    conn = db.connect(":memory:")
    a = setup(conn)

    combos.insert(conn, a_combo(a))

    assert combos.get(conn, a, "c1") == ComboRow(a_combo(a), None)
    assert combos.get(conn, a, "nope") is None


def test_a_claimed_combo_cannot_be_claimed_again_until_released() -> None:
    conn = db.connect(":memory:")
    a = setup(conn)
    combos.insert(conn, a_combo(a))

    assert combos.claim(conn, a, "c1", NOW, NOW + LEASE) is True
    assert lease(conn, a) == NOW + LEASE
    assert combos.claim(conn, a, "c1", NOW + timedelta(seconds=30), NOW + 2 * LEASE) is False
    # 기한 끝 그 순간에도 아직 쥐고 있다.
    assert combos.claim(conn, a, "c1", NOW + LEASE, NOW + 2 * LEASE) is False
    assert lease(conn, a) == NOW + LEASE

    combos.release(conn, a, "c1", NOW + LEASE)

    assert lease(conn, a) is None
    assert combos.claim(conn, a, "c1", NOW + timedelta(seconds=31), NOW + LEASE) is True


def test_only_the_holder_renews_or_releases() -> None:
    """기한이 지나 다른 실행이 쥔 묶음을 먼저 실행이 늘리거나 놓지 못한다."""
    conn = db.connect(":memory:")
    a = setup(conn)
    combos.insert(conn, a_combo(a))
    combos.claim(conn, a, "c1", NOW, NOW + LEASE)

    assert combos.renew(conn, a, "c1", NOW + LEASE, NOW + 2 * LEASE) is True
    assert lease(conn, a) == NOW + 2 * LEASE
    later = NOW + 2 * LEASE + timedelta(microseconds=1)
    assert combos.claim(conn, a, "c1", later, later + LEASE) is True

    assert combos.renew(conn, a, "c1", NOW + 2 * LEASE, later + 2 * LEASE) is False
    combos.release(conn, a, "c1", NOW + 2 * LEASE)
    assert lease(conn, a) == later + LEASE
    combos.release(conn, a, "c1", later + LEASE)
    assert lease(conn, a) is None


def test_an_expired_lease_can_be_claimed() -> None:
    """실행 중에 서버가 죽어 놓지 못한 묶음은 기한이 지나면 다음 실행이 쥔다."""
    conn = db.connect(":memory:")
    a = setup(conn)
    combos.insert(conn, a_combo(a))
    combos.claim(conn, a, "c1", NOW, NOW + LEASE)

    later = NOW + LEASE + timedelta(microseconds=1)

    assert combos.claim(conn, a, "c1", later, later + LEASE) is True
    assert lease(conn, a) == later + LEASE


def test_holds_needs_the_same_lease_and_the_same_number_of_leg_orders() -> None:
    """다리를 보내기 직전의 확인: 쥔 기한 값과 그 묶음의 다리 주문 행 수가 둘 다 맞아야 한다."""
    conn = db.connect(":memory:")
    a = setup(conn)
    combos.insert(conn, a_combo(a))
    combos.claim(conn, a, "c1", NOW, NOW + LEASE)
    leg = OrderRequest("domestic", "B016C725", "buy", "limit", 1, Decimal("2.00"), "fak")

    assert combos.holds(conn, a, "c1", NOW + LEASE, 0) is True
    orders.insert_submitted(conn, a, leg, NOW, Origin(combo_id="c1"))
    orders.insert_submitted(conn, a, leg, NOW)  # 묶음 밖 주문은 세지 않는다

    assert combos.holds(conn, a, "c1", NOW + LEASE, 0) is False
    assert combos.holds(conn, a, "c1", NOW + LEASE, 1) is True
    assert combos.holds(conn, a, "c1", NOW + 2 * LEASE, 1) is False
    combos.release(conn, a, "c1", NOW + LEASE)
    assert combos.holds(conn, a, "c1", NOW + LEASE, 1) is False


def test_claim_of_a_missing_combo_is_refused() -> None:
    conn = db.connect(":memory:")
    a = setup(conn)

    assert combos.claim(conn, a, "nope", NOW, NOW + LEASE) is False


def race(conns: list[sqlite3.Connection], user: UserId, combo_id: str) -> list[bool]:
    """연결마다 스레드 하나가 같은 순간에 같은 묶음을 쥐려 한다."""
    start = threading.Barrier(len(conns))

    def claim(conn: sqlite3.Connection) -> bool:
        start.wait()
        return combos.claim(conn, user, combo_id, NOW, NOW + LEASE)

    with ThreadPoolExecutor(len(conns)) as pool:
        return list(pool.map(claim, conns))


def test_two_concurrent_claims_on_separate_connections_have_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    conns = [db.connect(path), db.connect(path)]
    a = setup(conns[0])
    rounds = 20
    for n in range(rounds):
        combos.insert(conns[0], a_combo(a, f"c{n}"))

    results = [race(conns, a, f"c{n}") for n in range(rounds)]

    assert all(sorted(r) == [False, True] for r in results)
