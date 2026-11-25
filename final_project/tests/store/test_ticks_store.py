import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from nh_mcp.domain import KST, UserId
from nh_mcp.realtime.protocol import Data, parse
from nh_mcp.store import db, recordings, ticks, users
from nh_mcp.store.ticks import BOOK_COLUMNS, LEVELS, TRADE_COLUMNS, Block, Row, Sub, TickStore

A, B = UserId(1), UserId(2)
SYM = "A016C000"
TICK, BOOK = Sub(SYM, "tick"), Sub(SYM, "book")
MINUTE = datetime(2026, 12, 1, 10, 0, tzinfo=KST)
HOUR = (MINUTE, MINUTE + timedelta(hours=1))


def store(tmp_path: Path) -> TickStore:
    return TickStore(ticks.connect(tmp_path / "ticks.db"))


def nh_book(tm: str, prices: list[tuple[str, str]]) -> str:
    """NH가 보내는 모양의 호가 메시지. prices는 단계마다 (매도, 매수) 가격 문자열."""
    body: dict[str, str] = {"exch_tm": tm, "ask_qty": "1926", "ask_cnt": "971", "bid_qty": "714"}
    body["bid_cnt"] = ""  # 빈 칸은 빈 값으로 남는다
    for level, (ask, bid) in enumerate(prices, start=1):
        body |= {f"ask_pric_{level}": ask, f"bid_pric_{level}": bid}
        body |= {f"ask_qty_{level}": str(level), f"bid_qty_{level}": str(10 + level)}
        body |= {f"ask_cnt_{level}": str(20 + level), f"bid_cnt_{level}": str(30 + level)}
    return json.dumps({"header": {"action_code": "KB", "action_sym": SYM}, "body": body})


def parsed(raw: str, seq: int) -> Row:
    message = parse(raw)
    assert isinstance(message, Data)
    return (seq, *message.values)


def test_columns_match_what_the_parser_produces() -> None:
    raw = nh_book("100000", [("726.15", "726.10")] * LEVELS)
    assert len(parsed(raw, 1)) == len(BOOK_COLUMNS)
    trade = {"tick_div": "1", "exch_tm": "100000", "last_pric": "1.00", "exec_qty": "1"}
    raw_trade = json.dumps({"header": {"action_code": "KA", "action_sym": SYM}, "body": trade})
    assert len(parsed(raw_trade, 1)) == len(TRADE_COLUMNS)


def test_block_round_trip_is_exact_in_order_with_all_levels(tmp_path: Path) -> None:
    # 단계마다 다른 가격(음수·0·빈 단계 포함)과 잔량·건수. 받은 순서는 seq 순이 아닌 그대로다.
    levels = [(f"{726 + n}.{n * 5:02d}", f"-{n + 1}.{n:02d}") for n in range(LEVELS - 1)]
    levels.append(("0.00", "0.00"))
    rows = [parsed(nh_book("100001", levels), 7), parsed(nh_book("100000", levels[::-1]), 9)]
    s = store(tmp_path)
    s.write(A, [Block(BOOK, MINUTE, rows)], [BOOK], MINUTE + timedelta(minutes=1))

    books = s.books(A, SYM, *HOUR)
    assert [b.seq for b in books] == [7, 9]
    assert [b.at for b in books] == [MINUTE + timedelta(seconds=1), MINUTE]
    first = books[0]
    assert [str(level.price) for level in first.asks] == [ask for ask, _ in levels]
    assert [str(level.price) for level in first.bids] == [bid for _, bid in levels]
    assert [(level.qty, level.count) for level in first.asks] == [
        (n, 20 + n) for n in range(1, LEVELS + 1)
    ]
    assert [(level.qty, level.count) for level in first.bids] == [
        (10 + n, 30 + n) for n in range(1, LEVELS + 1)
    ]
    assert (first.ask_total_qty, first.ask_total_cnt, first.bid_total_qty) == (1926, 971, 714)
    assert first.bid_total_cnt is None
    assert books[1].asks[0].price == Decimal("0.00")


def test_trades_come_back_with_exact_decimals_and_minute_boundaries(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.write(
        A,
        [
            Block(TICK, MINUTE, [(1, 36000, 72615, 2, 72610, None, 1, 68092)]),
            # 받은 분은 10:01이지만 거래소 시각은 10:00:59다(조금 늦게 받음).
            Block(TICK, MINUTE + timedelta(minutes=1), [(2, 36059, -5, 1, None, None, None, None)]),
        ],
        [TICK],
        MINUTE + timedelta(minutes=2),
    )
    trades = s.trades(A, SYM, MINUTE, MINUTE + timedelta(minutes=1))
    assert [(t.seq, t.at.isoformat(), str(t.price), t.bid, t.ask) for t in trades] == [
        (1, "2026-12-01T10:00:00+09:00", "726.15", Decimal("726.10"), None),
        (2, "2026-12-01T10:00:59+09:00", "-0.05", None, None),
    ]
    assert s.trades(A, SYM, MINUTE + timedelta(minutes=1), MINUTE + timedelta(minutes=2)) == []


def test_one_users_rows_and_gaps_are_invisible_to_another(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.write(A, [Block(TICK, MINUTE, [(1, 36000, 72615, 2, None, None, 1, 1)])], [TICK], MINUTE)
    s.open_gap(A, TICK, MINUTE, "disconnect")
    assert len(s.trades(A, SYM, *HOUR)) == 1
    assert s.trades(B, SYM, *HOUR) == []
    assert s.gaps(B) == []
    assert s.persisted_until(B, TICK) is None
    assert s.max_seq(B) == 0
    # B가 같은 종목을 저장해도 A의 순번·빈 구간과 섞이지 않는다.
    s.close_gap(B, TICK, MINUTE + timedelta(minutes=5))
    assert [g.ended_at for g in s.gaps(A)] == [None]


def test_gap_opens_once_and_closes(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.open_gap(A, TICK, MINUTE, "disconnect")
    s.open_gap(A, TICK, MINUTE + timedelta(seconds=5), "subscribe_failed")
    s.close_gap(A, TICK, MINUTE + timedelta(seconds=9))
    s.close_gap(A, TICK, MINUTE + timedelta(seconds=20))  # 열린 것이 없으면 그대로
    assert [(g.started_at, g.ended_at, g.reason) for g in s.gaps(A)] == [
        (MINUTE, MINUTE + timedelta(seconds=9), "disconnect")
    ]


def test_purge_removes_blocks_and_closed_gaps_older_than_thirty_days(tmp_path: Path) -> None:
    s = store(tmp_path)
    old, recent = MINUTE - timedelta(days=31), MINUTE - timedelta(days=29)
    s.write(
        A,
        [
            Block(TICK, old, [(1, 36000, 100, 1, None, None, None, None)]),
            Block(TICK, recent, [(2, 36000, 200, 1, None, None, None, None)]),
        ],
        [],
        MINUTE,
    )
    s.open_gap(A, TICK, old, "disconnect")
    s.close_gap(A, TICK, old + timedelta(minutes=1))
    s.open_gap(A, BOOK, old, "subscribe_failed")  # 아직 열린 빈 구간은 남긴다
    assert s.purge(MINUTE) == 1
    assert [t.price for t in s.trades(A, SYM, recent, MINUTE)] == [Decimal("2.00")]
    assert s.trades(A, SYM, old, old + timedelta(hours=1)) == []
    assert [g.sub for g in s.gaps(A)] == [BOOK]


def test_forget_closes_the_gap_and_drops_the_persisted_mark(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.write(A, [], [TICK], MINUTE)
    s.open_gap(A, TICK, MINUTE, "disconnect")
    s.forget(A, TICK, MINUTE + timedelta(minutes=3))
    assert s.persisted_until(A, TICK) is None
    assert [g.ended_at for g in s.gaps(A)] == [MINUTE + timedelta(minutes=3)]


def test_recording_settings_are_per_user_and_replacing_is_idempotent() -> None:
    conn = db.connect(":memory:")
    now = datetime(2026, 12, 1, tzinfo=UTC)
    a = users.get_or_create(conn, "sub-a", "a@example.com", now)
    b = users.get_or_create(conn, "sub-b", "b@example.com", now)
    recordings.replace(conn, a, frozenset({TICK, BOOK}), now)
    recordings.replace(conn, a, frozenset({TICK, BOOK}), now + timedelta(days=1))
    recordings.replace(conn, b, frozenset({TICK}), now)
    assert recordings.load(conn, a) == {TICK, BOOK}
    assert recordings.load_all(conn) == {a: {TICK, BOOK}, b: {TICK}}
    created = _created(conn, a)
    recordings.replace(conn, a, frozenset({TICK}), now + timedelta(days=2))
    assert recordings.load(conn, a) == {TICK}
    assert _created(conn, a) == {TICK: created[TICK]}
    assert created[TICK] == db.stamp(now)


def _created(conn: sqlite3.Connection, user_id: UserId) -> dict[Sub, str]:
    rows = conn.execute(
        "SELECT symbol, stream, created_at FROM tick_recordings WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {
        Sub(str(sym), "tick" if stream == "tick" else "book"): str(at) for sym, stream, at in rows
    }


def _trade_row(seq: int, second: int) -> Row:
    return (seq, second, 72600 + seq, 1, None, None, 1, None)


def _book_row(seq: int, second: int) -> Row:
    return (seq, second, *([0] * (len(BOOK_COLUMNS) - 2)))


def _interleaved(s: TickStore, user: UserId) -> None:
    """체결과 호가를 번갈아 받았다: 10:00에 1~6, 10:01에 7~10. 묶음은 흐름마다 따로다."""
    nxt = MINUTE + timedelta(minutes=1)
    s.write(
        user,
        [
            Block(TICK, MINUTE, [_trade_row(n, 36000 + n) for n in (1, 3, 5)]),
            Block(BOOK, MINUTE, [_book_row(n, 36000 + n) for n in (2, 4, 6)]),
            Block(TICK, nxt, [_trade_row(n, 36060 + n) for n in (7, 9)]),
            Block(BOOK, nxt, [_book_row(n, 36060 + n) for n in (8, 10)]),
        ],
        [],
        nxt,
    )


def test_latest_gives_the_most_recent_rows_across_streams_in_receive_order(
    tmp_path: Path,
) -> None:
    s = store(tmp_path)
    _interleaved(s, A)
    # 기간 밖(하루 전)의 묶음과 다른 사용자의 묶음은 읽지 않는다.
    s.write(A, [Block(TICK, MINUTE - timedelta(days=1), [_trade_row(0, 36000)])], [], MINUTE)
    s.write(B, [Block(TICK, MINUTE, [_trade_row(99, 36000)])], [], MINUTE)

    window = s.latest(A, [TICK, BOOK], *HOUR, limit=5)

    assert window.totals == {TICK: 5, BOOK: 5}
    assert window.count == 5
    rows = list(window.rows)
    assert [(sub.stream, row.seq) for sub, row in rows] == [
        ("book", 6),
        ("tick", 7),
        ("book", 8),
        ("tick", 9),
        ("book", 10),
    ]
    assert isinstance(rows[1][1], ticks.Trade) and rows[1][1].price == Decimal("726.07")
    assert isinstance(rows[0][1], ticks.Book)


def test_latest_under_the_limit_gives_everything_and_only_the_asked_streams(
    tmp_path: Path,
) -> None:
    s = store(tmp_path)
    _interleaved(s, A)

    window = s.latest(A, [TICK], *HOUR, limit=100)

    assert (window.totals, window.count) == ({TICK: 5}, 5)
    assert [row.seq for _, row in window.rows] == [1, 3, 5, 7, 9]
    assert s.latest(B, [TICK, BOOK], *HOUR, limit=100).count == 0


def test_latest_reads_older_blocks_that_still_hold_recent_rows(tmp_path: Path) -> None:
    """한 묶음의 순번 범위가 다른 흐름의 여러 묶음에 걸쳐도 최근 행을 빠뜨리지 않는다."""
    s = store(tmp_path)
    nxt = MINUTE + timedelta(minutes=1)
    s.write(
        A,
        [
            Block(TICK, MINUTE, [_trade_row(1, 36000), _trade_row(10, 36059)]),
            Block(BOOK, MINUTE, [_book_row(3, 36010), _book_row(5, 36020)]),
            Block(BOOK, nxt, [_book_row(8, 36060), _book_row(9, 36061)]),
        ],
        [],
        nxt,
    )

    window = s.latest(A, [TICK, BOOK], *HOUR, limit=4)

    assert [row.seq for _, row in window.rows] == [5, 8, 9, 10]
