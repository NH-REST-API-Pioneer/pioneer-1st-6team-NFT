"""틱·호가 저장소(`ticks.db`). 쓰기가 잦아 주 DB와 파일을 나눈다.

모양:
- `blocks`: (사용자, 종목, 흐름)의 1분 묶음. 분은 받은 시각(KST 분, UTC 문자열로 저장)이다.
  `data`는 행 목록을 JSON 배열로 적어 zlib으로 누른 것이다. 행은 받은 순서이고 정수만 담는다.
  가격은 `price_scale`자리 정수(2면 1/100 단위, "726.15" → 72615)다. float는 쓰지 않는다.
  빈 값(NH가 빈 문자열로 보낸 칸)은 null이다.
- 행의 첫 칸 `seq`는 사용자마다 하나씩 늘어나는 받은 순번이다. 종목·흐름을 가로질러 받은 순서
  그대로라 되풀이할 때 순서가 같다.
- `gaps`: 빠졌을 수 있는 구간. `ended_at`이 NULL이면 아직 이어지는 중이다.
- `persisted_until`: 구독이 살아 있었고 그때까지 받은 것이 모두 저장됐다고 아는 마지막 시각.
  서버가 죽었다 켜지면 이 시각부터 다시 구독할 때까지가 빈 구간이다(열린 1분을 잃는다).

모든 표와 모든 조회에 사용자 식별자가 있다(안전 불변식 8).
"""

import heapq
import itertools
import json
import sqlite3
import threading
import zlib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from nh_mcp.domain import KST, UserId
from nh_mcp.store.db import parse_stamp, stamp

type RecordStream = Literal["tick", "book"]
type GapReason = Literal["disconnect", "subscribe_failed", "server_stop", "crash"]
# 행 한 칸. 정수 또는 빈 값.
type Cell = int | None
type Row = tuple[Cell, ...]

PRICE_SCALE = 2
RETENTION = timedelta(days=30)
LEVELS = 10
ZLIB_LEVEL = 6

# 행의 칸 순서. 이 순서가 저장 형식이다(바꾸면 옛 묶음을 못 읽는다).
TRADE_COLUMNS = ("seq", "exch_sec", "price", "qty", "bid", "ask", "side", "cum_qty")
BOOK_COLUMNS = (
    "seq",
    "exch_sec",
    "ask_total_qty",
    "ask_total_cnt",
    "bid_total_qty",
    "bid_total_cnt",
    *(
        f"{side}_{part}_{level}"
        for level in range(1, LEVELS + 1)
        for side in ("ask", "bid")
        for part in ("price", "qty", "cnt")
    ),
)
_BOOK_HEAD = 6
_LEVEL_WIDTH = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('tick', 'book')),
    minute TEXT NOT NULL,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    price_scale INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (user_id, symbol, stream, first_seq)
);
CREATE INDEX IF NOT EXISTS blocks_by_minute ON blocks (user_id, symbol, stream, minute);
CREATE INDEX IF NOT EXISTS blocks_for_purge ON blocks (minute);
CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('tick', 'book')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reason TEXT NOT NULL
        CHECK (reason IN ('disconnect', 'subscribe_failed', 'server_stop', 'crash'))
);
CREATE INDEX IF NOT EXISTS gaps_by_user ON gaps (user_id, symbol, stream);
CREATE TABLE IF NOT EXISTS persisted_until (
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('tick', 'book')),
    until TEXT NOT NULL,
    PRIMARY KEY (user_id, symbol, stream)
);
"""


@dataclass(frozen=True, order=True)
class Sub:
    """구독 하나: 종목 하나의 흐름 하나. NH 구독 한 건이다."""

    symbol: str
    stream: RecordStream


@dataclass(frozen=True)
class Block:
    """저장할 1분 묶음. rows는 받은 순서, 첫 칸이 seq."""

    sub: Sub
    minute: datetime
    rows: Sequence[Row]


@dataclass(frozen=True)
class Trade:
    seq: int
    at: datetime
    price: Decimal
    qty: int
    bid: Decimal | None
    ask: Decimal | None
    # NH `exec_div` 그대로(1·2). 모르면 None.
    side: int | None
    cum_qty: int | None


@dataclass(frozen=True)
class Level:
    price: Decimal | None
    qty: int | None
    count: int | None


@dataclass(frozen=True)
class Book:
    seq: int
    at: datetime
    asks: tuple[Level, ...]
    bids: tuple[Level, ...]
    ask_total_qty: int | None
    ask_total_cnt: int | None
    bid_total_qty: int | None
    bid_total_cnt: int | None


@dataclass(frozen=True)
class Gap:
    sub: Sub
    started_at: datetime
    ended_at: datetime | None
    reason: GapReason


@dataclass(frozen=True)
class Stored:
    """한 구독으로 저장해 둔 것: 첫 묶음과 마지막 묶음의 받은 분(KST 분의 시작), 행 수."""

    sub: Sub
    first_minute: datetime
    last_minute: datetime
    rows: int


@dataclass(frozen=True)
class Window:
    """기간 안에서 가장 최근에 받은 행들.

    totals는 구독마다 기간 안 전체 행 수, count는 rows가 낼 행 수(limit 이하)다. rows는 구독을
    가로질러 받은 순서(seq 오름차순)이고, 한 번만 훑을 수 있다(묶음을 훑는 대로 푼다).
    """

    totals: dict[Sub, int]
    count: int
    rows: Iterator[tuple[Sub, Trade | Book]]


def encode(rows: Sequence[Row]) -> bytes:
    return zlib.compress(json.dumps(rows, separators=(",", ":")).encode(), ZLIB_LEVEL)


def decode(data: bytes) -> list[Row]:
    loaded = cast(list[list[Cell]], json.loads(zlib.decompress(data)))
    return [tuple(row) for row in loaded]


def to_price(cell: Cell, scale: int) -> Decimal | None:
    return None if cell is None else Decimal(cell).scaleb(-scale)


def kst_minute(moment: datetime) -> datetime:
    """moment가 속한 분의 시작(KST)."""
    return moment.astimezone(KST).replace(second=0, microsecond=0)


def _at(day: date, exch_sec: Cell) -> datetime:
    seconds = exch_sec or 0
    return datetime.combine(day, time(seconds // 3600, seconds // 60 % 60, seconds % 60), KST)


def _int(cell: Cell) -> int:
    if cell is None:
        raise ValueError("빈 칸이 아니어야 하는 칸이 비었습니다.")
    return cell


def _trade(row: Row, day: date, scale: int) -> Trade:
    seq, exch_sec, price, qty, bid, ask, side, cum_qty = row
    return Trade(
        seq=_int(seq),
        at=_at(day, exch_sec),
        price=Decimal(_int(price)).scaleb(-scale),
        qty=_int(qty),
        bid=to_price(bid, scale),
        ask=to_price(ask, scale),
        side=side,
        cum_qty=cum_qty,
    )


def _book(row: Row, day: date, scale: int) -> Book:
    asks: list[Level] = []
    bids: list[Level] = []
    for level in range(LEVELS):
        at = _BOOK_HEAD + level * _LEVEL_WIDTH
        a_price, a_qty, a_cnt, b_price, b_qty, b_cnt = row[at : at + _LEVEL_WIDTH]
        asks.append(Level(to_price(a_price, scale), a_qty, a_cnt))
        bids.append(Level(to_price(b_price, scale), b_qty, b_cnt))
    return Book(
        seq=_int(row[0]),
        at=_at(day, row[1]),
        asks=tuple(asks),
        bids=tuple(bids),
        ask_total_qty=row[2],
        ask_total_cnt=row[3],
        bid_total_qty=row[4],
        bid_total_cnt=row[5],
    )


def _floor(heads: list[tuple[int, int, int]], limit: int) -> int:
    """마지막 순번이 이 값 이상인 묶음만 풀면 가장 최근 limit행이 모두 들어 있다.

    heads는 묶음마다 (마지막 순번, 첫 순번, 행 수)다. 마지막 순번이 큰 묶음부터 limit행을 모은
    묶음들의 가장 작은 첫 순번이다. 그보다 작은 순번만 가진 묶음에는 최근 limit행이 없다.
    행이 limit개 이하면 0(모두)이다.
    """
    count = 0
    floor: int | None = None
    for _, first, rows in sorted(heads, reverse=True):
        floor = first if floor is None else min(floor, first)
        count += rows
        if count >= limit:
            return floor
    return 0


def read_row(stream: RecordStream, row: Row, day: date, scale: int = PRICE_SCALE) -> Trade | Book:
    """저장 형식의 행 하나를 읽는다. day는 받은 분의 KST 날짜다. 수신기가 전략에 넘길 때도 쓴다."""
    return _trade(row, day, scale) if stream == "tick" else _book(row, day, scale)


def _unpacked(sub: Sub, blocks: list[tuple[str, int, bytes]]) -> Iterator[tuple[Sub, Trade | Book]]:
    """묶음을 받은 순서대로 하나씩 풀어 행을 낸다. 날짜는 받은 분의 KST 날짜다."""
    for minute, scale, data in blocks:
        day = parse_stamp(minute).astimezone(KST).date()
        for row in decode(data):
            yield sub, read_row(sub.stream, row, day, scale)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


class TickStore:
    """ticks.db 하나. 묶음 쓰기는 이벤트 루프 밖 스레드에서도 오므로 잠금으로 한 줄로 세운다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def write(
        self, user_id: UserId, blocks: Iterable[Block], live: Iterable[Sub], until: datetime
    ) -> None:
        """묶음을 쓰고, 살아 있는 구독의 저장 시각을 until로 올린다. 한 트랜잭션이다."""
        records = [
            (
                user_id,
                block.sub.symbol,
                block.sub.stream,
                stamp(block.minute),
                _int(block.rows[0][0]),
                _int(block.rows[-1][0]),
                len(block.rows),
                PRICE_SCALE,
                encode(block.rows),
            )
            for block in blocks
            if block.rows
        ]
        marks = [(user_id, sub.symbol, sub.stream, stamp(until)) for sub in live]
        with self._lock, self._conn:
            self._conn.executemany("INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
            self._conn.executemany(
                "INSERT INTO persisted_until VALUES (?, ?, ?, ?) "
                "ON CONFLICT (user_id, symbol, stream) DO UPDATE SET until = excluded.until",
                marks,
            )

    def max_seq(self, user_id: UserId) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(last_seq) FROM blocks WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row[0] or 0)

    def persisted_until(self, user_id: UserId, sub: Sub) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT until FROM persisted_until WHERE user_id = ? AND symbol = ? AND stream = ?",
                (user_id, sub.symbol, sub.stream),
            ).fetchone()
        return None if row is None else parse_stamp(str(row[0]))

    def forget(self, user_id: UserId, sub: Sub, at: datetime) -> None:
        """저장을 일부러 멈췄다. 열린 빈 구간을 닫고, 다시 켤 때 빈 구간이 생기지 않게 한다."""
        with self._lock, self._conn:
            self._close_gap(user_id, sub, at)
            self._conn.execute(
                "DELETE FROM persisted_until WHERE user_id = ? AND symbol = ? AND stream = ?",
                (user_id, sub.symbol, sub.stream),
            )

    def open_gap(self, user_id: UserId, sub: Sub, start: datetime, reason: GapReason) -> None:
        """빈 구간을 연다. 이미 열린 것이 있으면 그대로 둔다(먼저 시작한 쪽이 맞다)."""
        with self._lock, self._conn:
            if self._open_gap_id(user_id, sub) is None:
                self._conn.execute(
                    "INSERT INTO gaps (user_id, symbol, stream, started_at, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, sub.symbol, sub.stream, stamp(start), reason),
                )

    def close_gap(self, user_id: UserId, sub: Sub, end: datetime) -> None:
        with self._lock, self._conn:
            self._close_gap(user_id, sub, end)

    def gaps(self, user_id: UserId, symbol: str | None = None) -> list[Gap]:
        query = "SELECT symbol, stream, started_at, ended_at, reason FROM gaps WHERE user_id = ?"
        args: tuple[object, ...] = (user_id,)
        if symbol is not None:
            query += " AND symbol = ?"
            args = (user_id, symbol)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY started_at, id", args).fetchall()
        return [
            Gap(
                Sub(str(sym), cast(RecordStream, stream)),
                parse_stamp(str(started)),
                None if ended is None else parse_stamp(str(ended)),
                cast(GapReason, reason),
            )
            for sym, stream, started, ended, reason in rows
        ]

    def trades(self, user_id: UserId, symbol: str, start: datetime, end: datetime) -> list[Trade]:
        """start ≤ at < end인 체결, 받은 순서."""
        found = [
            _trade(row, day, scale)
            for day, scale, row in self._rows(user_id, Sub(symbol, "tick"), start, end)
        ]
        return [t for t in found if start <= t.at < end]

    def books(self, user_id: UserId, symbol: str, start: datetime, end: datetime) -> list[Book]:
        """start ≤ at < end인 호가, 받은 순서."""
        found = [
            _book(row, day, scale)
            for day, scale, row in self._rows(user_id, Sub(symbol, "book"), start, end)
        ]
        return [b for b in found if start <= b.at < end]

    def latest(
        self, user_id: UserId, subs: Iterable[Sub], start: datetime, end: datetime, limit: int
    ) -> Window:
        """받은 분이 start 이상 end 미만인 묶음의 행 가운데 받은 순번이 가장 큰 limit개.

        묶음 머리(순번 범위·행 수)만 먼저 읽어, 그 limit개가 다 들어 있는 묶음만 꺼낸다.
        """
        wanted = sorted(set(subs))
        where = "WHERE user_id = ? AND symbol = ? AND stream = ? AND minute >= ? AND minute < ?"
        heads: dict[Sub, list[tuple[int, int, int]]] = {}
        chosen: dict[Sub, list[tuple[str, int, bytes]]] = {}
        # 두 조회 사이에 수신기가 묶음을 쓰지 못하게 한 번에 잠근다(행 수가 꺼낸 묶음과 맞게).
        with self._lock:
            for sub in wanted:
                args = (user_id, sub.symbol, sub.stream, stamp(start), stamp(end))
                heads[sub] = [
                    (int(last), int(first), int(count))
                    for last, first, count in self._conn.execute(
                        f"SELECT last_seq, first_seq, row_count FROM blocks {where}", args
                    )
                ]
            floor = _floor([head for found in heads.values() for head in found], limit)
            for sub in wanted:
                args = (user_id, sub.symbol, sub.stream, stamp(start), stamp(end), floor)
                chosen[sub] = [
                    (str(minute), int(scale), bytes(data))
                    for minute, scale, data in self._conn.execute(
                        f"SELECT minute, price_scale, data FROM blocks {where} "
                        "AND last_seq >= ? ORDER BY first_seq",
                        args,
                    )
                ]
        rows = sum(count for found in heads.values() for last, _, count in found if last >= floor)
        merged = heapq.merge(
            *(_unpacked(sub, blocks) for sub, blocks in chosen.items()), key=lambda r: r[1].seq
        )
        return Window(
            totals={sub: sum(count for _, _, count in found) for sub, found in heads.items()},
            count=min(rows, limit),
            rows=itertools.islice(merged, max(rows - limit, 0), None),
        )

    def stored(self, user_id: UserId) -> list[Stored]:
        """이 사용자가 저장해 둔 구독마다 받은 분의 범위와 행 수(종목·흐름 순).

        묶음을 풀지 않는다. 분은 받은 시각의 KST 분 시작이고, 마지막 분의 끝은 last_minute + 1분.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, stream, MIN(minute), MAX(minute), SUM(row_count) FROM blocks "
                "WHERE user_id = ? GROUP BY symbol, stream ORDER BY symbol, stream",
                (user_id,),
            ).fetchall()
        return [
            Stored(
                Sub(str(symbol), cast(RecordStream, stream)),
                parse_stamp(str(first)).astimezone(KST),
                parse_stamp(str(last)).astimezone(KST),
                int(count),
            )
            for symbol, stream, first, last, count in rows
        ]

    def block_sizes(self, user_id: UserId, sub: Sub) -> list[tuple[int, int]]:
        """묶음마다 (행 수, 바이트). 용량 측정용."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT row_count, LENGTH(data) FROM blocks "
                "WHERE user_id = ? AND symbol = ? AND stream = ? ORDER BY first_seq",
                (user_id, sub.symbol, sub.stream),
            ).fetchall()
        return [(int(n), int(size)) for n, size in rows]

    def purge(self, now: datetime) -> int:
        """보관 기간(30일)이 지난 묶음과 닫힌 빈 구간을 지운다. 지운 묶음 수."""
        cutoff = stamp(now - RETENTION)
        with self._lock, self._conn:
            removed = self._conn.execute("DELETE FROM blocks WHERE minute < ?", (cutoff,)).rowcount
            self._conn.execute(
                "DELETE FROM gaps WHERE ended_at IS NOT NULL AND ended_at < ?", (cutoff,)
            )
        return removed

    def _rows(
        self, user_id: UserId, sub: Sub, start: datetime, end: datetime
    ) -> list[tuple[date, int, Row]]:
        # 묶음은 받은 분으로 찍히고 거래소 시각은 조금 앞설 수 있어 한 분 앞까지 읽는다.
        with self._lock:
            blocks = self._conn.execute(
                "SELECT minute, price_scale, data FROM blocks "
                "WHERE user_id = ? AND symbol = ? AND stream = ? AND minute >= ? AND minute < ? "
                "ORDER BY first_seq",
                (
                    user_id,
                    sub.symbol,
                    sub.stream,
                    stamp(kst_minute(start) - timedelta(minutes=1)),
                    stamp(end + timedelta(minutes=1)),
                ),
            ).fetchall()
        out: list[tuple[date, int, Row]] = []
        for minute, scale, data in blocks:
            day = parse_stamp(str(minute)).astimezone(KST).date()
            out.extend((day, int(scale), row) for row in decode(bytes(data)))
        return out

    def _open_gap_id(self, user_id: UserId, sub: Sub) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM gaps WHERE user_id = ? AND symbol = ? AND stream = ? "
            "AND ended_at IS NULL",
            (user_id, sub.symbol, sub.stream),
        ).fetchone()
        return None if row is None else int(row[0])

    def _close_gap(self, user_id: UserId, sub: Sub, end: datetime) -> None:
        self._conn.execute(
            "UPDATE gaps SET ended_at = ? WHERE user_id = ? AND symbol = ? AND stream = ? "
            "AND ended_at IS NULL",
            (stamp(end), user_id, sub.symbol, sub.stream),
        )
