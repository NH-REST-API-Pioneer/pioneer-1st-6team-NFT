"""차트를 끝 날짜를 옮기며 받는다(Q26).

녹화: 2026-11-25 모의 서버, A016C000 1분봉, 기간 2026-11-10~11-24. 실제로 세 번 받았다
(끝 날짜 11-24 → 11-18 → 11-12, 2000·2000·1233행). 녹화 파일은 날마다 가장 늦은 2행과 가장 이른
2행만 남겨 줄였다(20·20·12행). 날짜와 순서는 그대로라 끝 날짜를 옮기는 순서가 실제와 같다.
그래서 한 번에 오는 최대 행 수를 20으로 바꿔 둔다(실제는 2000).
"""

import asyncio
import json
from datetime import date, datetime
from pathlib import Path

import httpx2
import pytest
from fakes import World

from nh_mcp.domain import KST, UserId
from nh_mcp.markets import domestic
from nh_mcp.markets.domestic import CandleSeries, DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.nh.client import Credentials
from nh_mcp.nh.recording import Fixture, replay_transport
from nh_mcp.store import users

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"
PERIOD = (date(2026, 11, 10), date(2026, 11, 24))
# 줄인 녹화에서 꽉 찬 응답의 행 수.
TRIMMED_PAGE = 20


class Logged(httpx2.AsyncBaseTransport):
    """보낸 요청 본문을 남기고 녹화 재생에 넘긴다."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self.inner = inner
        self.sent: list[dict[str, str]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if "chart" in request.url.path:
            self.sent.append(json.loads(request.content))
        return await self.inner.handle_async_request(request)


class Walk:
    def __init__(self, *pages: int) -> None:
        self.fixtures = [Fixture.load(RECORDED / f"chart-minute-walk-{n}.json") for n in pages]
        self.nh = Logged(replay_transport(self.fixtures))
        self.world = World(nh=self.nh)
        self.user: UserId = users.get_or_create(
            self.world.conn, "g-admin", "admin@example.com", self.world.clock()
        )
        credentials = Credentials("fake-key-1", "x-sec-1")
        asyncio.run(self.world.store.register(self.user, "domestic", credentials))
        master = DomesticMaster(self.world.clock, httpx2.MockTransport(self.world.master))
        self.market = DomesticMarket(self.world.pool, master, self.world.clock)

    def candles(self, period: tuple[date, date] = PERIOD, limit: int = 25_000) -> CandleSeries:
        return asyncio.run(self.market.candles(self.user, SYMBOL, "minute", period, limit))

    def ends(self) -> list[str]:
        return [payload["inq_ed_dt"] for payload in self.nh.sent]


def at(day: int, hhmm: str) -> datetime:
    return datetime(2026, 11, day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=KST)


@pytest.fixture
def trimmed_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(domestic.CHART_PAGE_ROWS, "minute", TRIMMED_PAGE)


@pytest.mark.usefixtures("trimmed_pages")
def test_walks_back_by_the_earliest_day_of_each_response() -> None:
    walk = Walk(1, 2, 3)

    series = walk.candles()

    # 셋째 응답이 한 번에 오는 최대보다 적어 기간을 다 받았다.
    assert series.complete is True
    assert walk.ends() == ["20261124", "20261118", "20261112"]
    recorded = [f.request for f in walk.fixtures]
    for sent, made in zip(walk.nh.sent, recorded, strict=True):
        # 실제로 답을 받은 요청과 같다(req_qty만 줄인 최대 행 수를 따른다).
        assert sent | {"req_qty": "2000"} == made
    stamps = [c.at for c in series.candles]
    assert stamps == sorted(set(stamps))
    # 20 + 18 + 10: 11-18과 11-12는 두 응답에 다 있던 마지막 두 봉(15:34·15:45)을 하나로 합쳤다.
    assert len(series.candles) == 48
    assert (stamps[0], stamps[-1]) == (at(10, "0845"), at(24, "1545"))
    assert stamps.count(at(18, "1545")) == 1 and at(18, "0845") in stamps


@pytest.mark.usefixtures("trimmed_pages")
def test_no_new_bars_stops_and_is_incomplete() -> None:
    # 끝 날짜를 옮겼는데 같은 행이 다시 온다(nxt_key를 따랐을 때처럼).
    walk = Walk(1, 1)

    series = walk.candles()

    assert series.complete is False
    assert walk.ends() == ["20261124", "20261118"]
    assert len(series.candles) == TRIMMED_PAGE


@pytest.mark.usefixtures("trimmed_pages")
def test_stopping_at_the_request_cap_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domestic, "CHART_MAX_PAGES", 2)
    walk = Walk(1, 2, 3)

    series = walk.candles()

    assert series.complete is False
    assert walk.ends() == ["20261124", "20261118"]
    assert series.candles[0].at == at(12, "0940")


@pytest.mark.usefixtures("trimmed_pages")
def test_bars_before_the_start_mean_the_start_is_covered() -> None:
    walk = Walk(1, 2, 3)

    series = walk.candles(period=(date(2026, 11, 16), PERIOD[1]))

    # 둘째 응답에 11-13·11-12 봉이 있어 시작일까지 받았다. 시작일 전 봉은 넣지 않는다.
    assert series.complete is True
    assert walk.ends() == ["20261124", "20261118"]
    assert series.candles[0].at == at(16, "0845")
    assert all(c.at.date() >= date(2026, 11, 16) for c in series.candles)


@pytest.mark.usefixtures("trimmed_pages")
def test_reaching_the_limit_stops_and_keeps_the_most_recent() -> None:
    walk = Walk(1, 2, 3)

    series = walk.candles(limit=30)

    assert series.complete is True
    assert walk.ends() == ["20261124", "20261118"]
    assert len(series.candles) == 30
    assert series.candles[-1].at == at(24, "1545")


def test_a_response_shorter_than_the_nh_maximum_is_the_whole_period() -> None:
    # 실제 최대(2000행)로는 줄인 첫 응답(20행)이 짧은 응답이다. 한 번만 묻는다.
    walk = Walk(1)

    series = walk.candles()

    assert series.complete is True
    assert walk.ends() == ["20261124"]
    assert walk.nh.sent[0]["req_qty"] == "2000"
    assert len(series.candles) == TRIMMED_PAGE
