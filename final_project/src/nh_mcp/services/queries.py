"""조회 유스케이스: 시세, 차트, 계좌, 주문 내역.

NH 쪽 예외를 서비스 예외로 바꾼다. 도구는 nh와 markets를 직접 부르지 않는다.
종목코드는 종목 마스터에 있는 것만 받는다. AI가 지어낸 코드로 NH를 부르지 않는다.
"""

from collections.abc import Awaitable
from datetime import date

from nh_mcp.domain import AccountState, Candle, Market, Order, OrderError, Quote, UserId
from nh_mcp.markets.domestic import DomesticMarket, Interval, NoSuchSymbol
from nh_mcp.markets.domestic_master import MasterUnavailable
from nh_mcp.nh.client import NoResponse, Rejected
from nh_mcp.nh.clients import KeyNotRegistered
from nh_mcp.nh.limits import UnknownApi


class KeyMissing(Exception):
    def __init__(self, market: Market) -> None:
        super().__init__(market)
        self.market: Market = market


class Unavailable(Exception):
    """NH가 답하지 않았다. 잠시 뒤 다시 시도할 수 있다."""


class BrokerRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code} {message}".strip())
        self.code = code
        self.message = message


class UnknownSymbol(Exception):
    """종목 마스터에 없거나 NH가 모르는 종목코드다."""


async def _call[T](work: Awaitable[T]) -> T:
    try:
        return await work
    except KeyNotRegistered:
        raise KeyMissing("domestic") from None
    except NoResponse:
        raise Unavailable() from None
    except Rejected as exc:
        raise BrokerRejected(exc.code, exc.message) from None
    except UnknownApi:
        raise Unavailable() from None
    except NoSuchSymbol as exc:
        raise UnknownSymbol(exc.symbol) from None


class QueryService:
    def __init__(self, domestic: DomesticMarket) -> None:
        self._domestic = domestic

    async def _check(self, symbol: str) -> None:
        try:
            contract = await self._domestic.contract(symbol)
        except MasterUnavailable:
            raise Unavailable() from None
        if contract is None:
            raise UnknownSymbol(symbol)

    async def quote(self, user_id: UserId, symbol: str) -> Quote:
        await self._check(symbol)
        return await _call(self._domestic.quote(user_id, symbol))

    async def candles(
        self,
        user_id: UserId,
        symbol: str,
        interval: Interval,
        period: tuple[date, date],
        limit: int,
    ) -> list[Candle]:
        await self._check(symbol)
        return await _call(self._domestic.candles(user_id, symbol, interval, period, limit))

    async def account(self, user_id: UserId) -> AccountState:
        return await _call(self._domestic.account(user_id))

    async def orders(self, user_id: UserId, day: date) -> tuple[list[Order], list[OrderError]]:
        return await _call(self._domestic.orders(user_id, day))
