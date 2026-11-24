import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx2
import pytest
from fakes import MASTER_SAMPLES, FakeMaster

from nh_mcp.markets.domestic_master import (
    FILES,
    MASTER_URL,
    DomesticMaster,
    MasterUnavailable,
    SearchResult,
    parse,
)

# 2026-12-01 10:00 KST
T0 = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def sample(name: str) -> str:
    return (MASTER_SAMPLES / f"{name}.mst").read_text(encoding="utf-8")


def search(master: DomesticMaster, query: str) -> SearchResult:
    return asyncio.run(master.search(query, "future", limit=20))


def test_parse_keeps_day_rows_and_reads_fields() -> None:
    contracts, skipped = parse(sample("fut_opt"))

    assert skipped == 0
    # 견본 9행 중 야간 1행을 뺀 8행.
    assert len(contracts) == 8
    near = next(c for c in contracts if c.symbol == "A016C000")
    assert (near.name, near.underlying, near.kind) == ("코스피200 F 202612", "KOSPI", "future")
    assert near.expiry == date(2026, 12, 10)
    assert near.multiplier == Decimal(250000)
    assert near.strike is None
    call = next(c for c in contracts if c.kind == "call")
    assert call.strike is not None and call.strike > 0
    assert {c.kind for c in contracts} == {"future", "spread", "call", "put"}


def test_broken_rows_are_skipped_and_counted() -> None:
    good = sample("fut_opt").split("\n")[0]
    broken = good.replace(";20261210;", ";2026-12-10;")
    contracts, skipped = parse("\n".join([good, broken, "too;few;cells"]))
    assert len(contracts) == 1
    assert skipped == 2


def test_search_by_name_underlying_and_code() -> None:
    master = DomesticMaster(Clock(), transport=httpx2.MockTransport(FakeMaster()))

    by_name = search(master, "코스피 200")
    assert [c.symbol for c in by_name.contracts] == ["A016C000", "A0173000", "A0176000", "A0179000"]
    assert search(master, "a016c000").contracts[0].symbol == "A016C000"
    assert [c.symbol for c in search(master, "3년국채").contracts] == ["A656C000", "A6573000"]
    assert search(master, "미니코스피").contracts[0].multiplier == Decimal(50000)
    assert search(master, "없는종목").contracts == []
    assert by_name.master_date == date(2026, 12, 1)


def test_search_marks_truncated_results() -> None:
    master = DomesticMaster(Clock(), transport=httpx2.MockTransport(FakeMaster()))
    result = asyncio.run(master.search("코스피", "future", limit=2))
    assert len(result.contracts) == 2 and result.truncated


def test_downloads_files_in_order_once_per_kst_day() -> None:
    fake, clock = FakeMaster(), Clock()
    master = DomesticMaster(clock, transport=httpx2.MockTransport(fake))
    expected = [MASTER_URL.format(name=name) for name in FILES]

    search(master, "코스피")
    search(master, "국채")
    assert fake.requested == expected

    # 한국 날짜가 바뀌면 다시 받는다(UTC 15시 = KST 자정).
    clock.now = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
    search(master, "코스피")
    assert fake.requested == expected * 2


def test_failed_download_keeps_yesterdays_list() -> None:
    fake, clock = FakeMaster(), Clock()
    master = DomesticMaster(clock, transport=httpx2.MockTransport(fake))
    search(master, "코스피")

    fake.down = True
    clock.now = T0 + timedelta(days=1)
    result = search(master, "코스피 200")
    assert len(result.contracts) == 4
    assert result.master_date == date(2026, 12, 1)


def test_no_list_at_all_is_unavailable() -> None:
    fake = FakeMaster()
    fake.down = True
    master = DomesticMaster(Clock(), transport=httpx2.MockTransport(fake))
    with pytest.raises(MasterUnavailable):
        search(master, "코스피")


def test_master_comes_from_the_developer_center_not_a_trading_server() -> None:
    assert MASTER_URL.startswith("https://apidev.futures.co.kr/guide/master/")
