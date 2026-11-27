"""코스피200 옵션 전광판(price-all-option)과 월물 만기 목록.

녹화: 2026-11-26 20:39 KST 모의 서버, 2026-12물(`exp_mon` 261210). 실제로 세 쪽을 받았다
(261·261·45행, 모두 567행, `nxt_stkr_pric` 945.00 → 292.50 → 빈 값). 녹화 파일은 쪽마다 처음 2행과
마지막 2행, 1쪽은 행사가 1000.00·1002.50, 2쪽은 등가격 727.50 위아래 40행만 남겨 줄였다(6·85·4행,
`nrec`도 맞춤). 머리 칸, 쪽 순서, 남긴 행의 값은 그대로다.
마스터 견본 `kospi200_options.mst`: 같은 날 받은 `fut_opt`에서 녹화에 나오는 옵션의 주간 행,
다른 월물 만기마다 콜 한 행, 위클리(WKI·WKM)·미니·코스닥150 옵션 한 행씩. 공용 견본 뒤에 붙인다.
"""

import asyncio
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from fakes import MASTER_SAMPLES, FakeNH, World

from nh_mcp.domain import KST, ChainRow, Contract, OptionChain, OptionSide, UserId
from nh_mcp.markets.domestic_master import DomesticMaster, parse
from nh_mcp.markets.options import BOARD, MAX_PAGES, BoardUnavailable, DomesticOptions, parse_chain
from nh_mcp.nh.client import Credentials
from nh_mcp.nh.recording import Fixture
from nh_mcp.store import users

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDED = FIXTURES / "nh" / "domestic"
OPTION_MASTER = FIXTURES / "master" / "kospi200_options.mst"
DECEMBER = date(2026, 12, 10)
MONTHLY = [
    date(2026, 12, 10),
    date(2027, 1, 14),
    date(2027, 2, 11),
    date(2027, 3, 11),
    date(2027, 4, 8),
    date(2027, 5, 12),
    date(2027, 6, 10),
    date(2027, 9, 9),
    date(2027, 12, 9),
    date(2028, 6, 8),
    date(2028, 12, 14),
]


def recorded_pages() -> list[Fixture]:
    return [Fixture.load(RECORDED / f"price-all-option-{n}.json") for n in (1, 2, 3)]


class Board:
    """가짜 NH가 정해 둔 전광판 쪽을 차례로 주고, 마스터에는 옵션 견본이 붙은 시장."""

    def __init__(self, bodies: list[Mapping[str, object]]) -> None:
        self.nh = FakeNH()
        for body in bodies:
            self.nh.reply(BOARD, body)
        self.world = World(nh=httpx2.MockTransport(self.nh))
        self.world.master.extra["fut_opt"] = OPTION_MASTER.read_text(encoding="utf-8")
        self.user: UserId = users.get_or_create(
            self.world.conn, "g-admin", "admin@example.com", self.world.clock()
        )
        credentials = Credentials("fake-key-1", "x-sec-1")
        asyncio.run(self.world.store.register(self.user, "domestic", credentials))
        master = DomesticMaster(self.world.clock, httpx2.MockTransport(self.world.master))
        self.options = DomesticOptions(self.world.pool, master, self.world.clock)

    def chain(self, expiry: date = DECEMBER) -> OptionChain:
        return asyncio.run(self.options.chain(self.user, expiry))

    def sent(self) -> list[object]:
        return [payload for path, payload in self.nh.requests if path == BOARD]


def recorded_board() -> Board:
    bodies = [f.body for f in recorded_pages()]
    return Board([b for b in bodies if isinstance(b, dict)])


def row(chain: OptionChain, strike: str) -> ChainRow:
    return next(r for r in chain.rows if r.strike == Decimal(strike))


def test_three_recorded_pages_become_rows_in_strike_order() -> None:
    board = recorded_board()
    chain = board.chain()

    page = {"prd": "01", "exp_mon": "261210"}
    assert board.sent() == [
        {**page, "nxt_stkr_pric": "99999999"},
        {**page, "nxt_stkr_pric": "945.00"},
        {**page, "nxt_stkr_pric": "292.50"},
    ]
    strikes = [r.strike for r in chain.rows]
    assert len(strikes) == 6 + 85 + 4
    assert strikes == sorted(set(strikes))
    assert (strikes[0], strikes[-1]) == (Decimal("180.00"), Decimal("1595.00"))
    assert (chain.underlying, chain.expiry, chain.spot) == ("KOSPI200", DECEMBER, Decimal("727.08"))
    assert (chain.future_symbol, chain.future_price) == ("A016C000", Decimal("726.75"))
    assert chain.dropped == 0


def test_strike_comes_from_the_board_not_from_the_code() -> None:
    chain = recorded_board().chain()

    # 행사가 1000 이상은 코드 끝이 일련번호다(A01 = 1000.00, A07 = 1002.50).
    thousand, next_up = row(chain, "1000.00"), row(chain, "1002.50")
    assert thousand.call is not None and thousand.call.symbol == "B016CA01"
    assert next_up.put is not None and next_up.put.symbol == "C016CA07"


def test_at_the_money_row_reads_both_sides_and_zero_quotes_become_none() -> None:
    at_money = row(recorded_board().chain(), "727.50")

    # 녹화의 콜 매도호가 "0.00"은 호가 없음이다.
    assert at_money.call == OptionSide(
        symbol="B016C727",
        last=Decimal("35.50"),
        bid=Decimal("2.03"),
        ask=None,
        volume=2,
        open_interest=15,
        iv=Decimal("58.80"),
        delta=Decimal("0.52563"),
        theoretical=Decimal("33.26"),
    )
    assert at_money.put == OptionSide(
        symbol="C016C727",
        last=Decimal("34.25"),
        bid=Decimal("32.40"),
        ask=Decimal("36.45"),
        volume=3,
        open_interest=17,
        iv=Decimal("62.54"),
        delta=Decimal("-0.47289"),
        theoretical=Decimal("38.19"),
    )


def master_by_symbol(lines: str) -> dict[str, Contract]:
    contracts, _ = parse(lines)
    return {c.symbol: c for c in contracts}


def test_codes_the_master_does_not_list_as_that_contract_are_left_out_and_counted() -> None:
    lines = [
        *(MASTER_SAMPLES / "fut_opt.mst").read_text(encoding="utf-8").splitlines(),
        *OPTION_MASTER.read_text(encoding="utf-8").splitlines(),
    ]
    kept: list[str] = []
    for line in lines:
        cells = line.split(";")
        if cells[8] in ("C016C727", "A016C000"):
            continue  # 마스터에 없는 풋, 마스터에 없는 대응 선물
        if cells[8] == "B016C725":
            cells[14] = "726.00"  # 마스터에는 있지만 행사가가 다른 콜
        kept.append(";".join(cells))
    bodies = [f.body for f in recorded_pages()]
    pages = [b for b in bodies if isinstance(b, dict)]

    chain = parse_chain(DECEMBER, pages, master_by_symbol("\n".join(kept)))

    assert row(chain, "727.50").put is None
    assert row(chain, "727.50").call is not None
    assert row(chain, "725.00").call is None
    assert row(chain, "725.00").put is not None
    assert chain.future_symbol is None
    assert chain.dropped == 3
    listed = {s.symbol for r in chain.rows for s in (r.call, r.put) if s is not None}
    assert len(listed) == 2 * 95 - 2


def test_a_board_that_never_ends_is_not_returned_in_part() -> None:
    head = {"spt_pric": "727.08", "fut_sym": "A016C000"}
    line = {"occurs1_strk_pric": "727.50", "occurs1_c_sym_exch": "B016C727"}
    bodies: list[Mapping[str, object]] = [
        {**head, "Occurs1": [line], "nxt_stkr_pric": f"{900 - n}.00"} for n in range(MAX_PAGES + 1)
    ]
    board = Board(bodies)

    with pytest.raises(BoardUnavailable):
        board.chain()
    assert len(board.sent()) == MAX_PAGES


def test_a_repeated_next_strike_stops_the_paging() -> None:
    first = recorded_pages()[0].body
    assert isinstance(first, dict)
    board = Board([first])  # 같은 쪽(nxt_stkr_pric 945.00)이 되풀이된다.

    with pytest.raises(BoardUnavailable):
        board.chain()
    assert len(board.sent()) == 2


def test_an_empty_page_that_still_names_a_next_strike_is_not_returned_in_part() -> None:
    first, second, _ = (f.body for f in recorded_pages())
    assert isinstance(first, dict) and isinstance(second, dict)
    # 2쪽이 행 없이 왔지만 다음 행사가(292.50)는 남아 있다. 1쪽만으로 사슬을 내지 않는다.
    board = Board([first, {**second, "nrec": "0", "Occurs1": []}])

    with pytest.raises(BoardUnavailable):
        board.chain()
    assert len(board.sent()) == 2


def test_an_empty_board_is_unavailable() -> None:
    # 위클리·만기 지난 월물에 온 실제 모양(2026-11-26 측정).
    empty = {"und_nm": "", "spt_pric": "0", "fut_sym": "A016C000", "nrec": "0", "nxt_stkr_pric": ""}
    board = Board([empty])

    with pytest.raises(BoardUnavailable):
        board.chain()
    assert len(board.sent()) == 1


def test_expiries_are_the_live_monthly_kospi200_expiries_nearest_first() -> None:
    board = Board([])

    # 위클리(WKI 2026-12-03, WKM), 미니, 코스닥150 옵션 만기는 들어가지 않는다.
    assert asyncio.run(board.options.expiries()) == MONTHLY
    board.world.clock.now = datetime(2026, 12, 11, 9, 0, tzinfo=KST)
    assert asyncio.run(board.options.expiries()) == MONTHLY[1:]
