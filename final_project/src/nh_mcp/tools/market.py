"""시세 · 종목 도구: search_symbol, get_quote, get_candles."""

from datetime import date, timedelta
from typing import Literal, TypedDict

from fastmcp import FastMCP

from nh_mcp import envelope
from nh_mcp.domain import KST, Candle, Contract, JsonValue, Quote
from nh_mcp.services.queries import QueryService
from nh_mcp.services.symbols import SymbolService
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, text

SEARCH_LIMIT = 20
MAX_QUERY_LENGTH = 40
MAX_CANDLES = 2000
DEFAULT_CANDLES = 200
DEFAULT_DAILY_DAYS = 365


def _contract(c: Contract) -> dict[str, JsonValue]:
    return {
        "symbol": c.symbol,
        "name": c.name,
        "underlying": c.underlying,
        "kind": c.kind,
        "expiry": c.expiry.isoformat(),
        "multiplier": str(c.multiplier),
    }


def _quote(q: Quote) -> dict[str, JsonValue]:
    return {
        "symbol": q.symbol,
        "name": q.name,
        "last": text(q.last),
        "change": text(q.change),
        "change_rate": text(q.change_rate),
        "open": text(q.open),
        "high": text(q.high),
        "low": text(q.low),
        "volume": q.volume,
        "upper_limit": text(q.upper_limit),
        "lower_limit": text(q.lower_limit),
        "book": [
            {"bid": text(b.bid), "bid_qty": b.bid_qty, "ask": text(b.ask), "ask_qty": b.ask_qty}
            for b in q.book
        ],
    }


def _candle(c: Candle) -> dict[str, JsonValue]:
    return {
        "at": c.at.isoformat(),
        "open": str(c.open),
        "high": str(c.high),
        "low": str(c.low),
        "close": str(c.close),
        "volume": c.volume,
    }


class Period(TypedDict, total=False):
    """조회 기간. 날짜는 YYYY-MM-DD."""

    start: str
    end: str


def _date(raw: str | None, fallback: date) -> date | None:
    if raw is None or not raw.strip():
        return fallback
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def register_market_tools(
    mcp: FastMCP, ctx: ToolContext, symbols: SymbolService, queries: QueryService
) -> None:
    @mcp.tool(annotations={"readOnlyHint": True})
    async def search_symbol(query: str, market: str = "domestic") -> envelope.Response:
        """종목명, 품목명, 종목코드로 선물 종목을 찾아 종목코드 · 만기 · 승수를 돌려준다.

        쓸 때: 시세나 주문에 쓸 종목코드가 필요할 때. 종목코드는 반드시 이 결과에서 고른다.
        종목코드를 추측해서 만들지 않는다.
        쓰지 않을 때: 이 결과에서 받은 종목코드가 이미 있으면 바로 다음 도구를 쓴다.
        market: "domestic"(국내)만 된다. 해외는 아직 지원하지 않는다.
        예: query="코스피200", "미니코스피", "3년국채", "A016C000".
        """
        query = query.strip()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        if not query or len(query) > MAX_QUERY_LENGTH:
            return ctx.invalid(f"검색어는 1~{MAX_QUERY_LENGTH}자로 넣어 주세요.")

        async def work() -> dict[str, JsonValue]:
            result = await symbols.search_futures(query, SEARCH_LIMIT)
            return {
                "symbols": [_contract(c) for c in result.contracts],
                "truncated": result.truncated,
                "master_date": result.master_date.isoformat(),
            }

        return await ctx.answer(work())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_quote(symbol: str, market: str = "domestic") -> envelope.Response:
        """종목의 현재가, 등락, 시가 · 고가 · 저가, 거래량, 상하한가, 호가를 돌려준다.

        쓸 때: 사용자가 지금 가격이나 호가를 물을 때, 주문 가격을 정하기 전에.
        쓰지 않을 때: 지난 흐름은 get_candles. 종목코드를 모르면 먼저 search_symbol.
        symbol은 search_symbol 결과의 종목코드만 쓴다. 가격은 문자열로 온다.
        """
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)

        async def work() -> dict[str, JsonValue]:
            return _quote(await queries.quote(ctx.user_id(), symbol.strip()))

        return await ctx.answer(work())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_candles(
        symbol: str,
        interval: Literal["minute", "day"] = "day",
        period: Period | None = None,
        limit: int = DEFAULT_CANDLES,
        market: str = "domestic",
    ) -> envelope.Response:
        """분봉(1분) 또는 일봉을 오래된 것부터 돌려준다. 긴 기간은 서버가 나눠 받아 합친다.

        쓸 때: 가격 흐름을 보거나 전략을 따져 볼 때.
        쓰지 않을 때: 지금 가격 하나만 필요하면 get_quote.
        period: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}. 비우면 end는 오늘,
        start는 일봉이면 1년 전, 분봉이면 오늘.
        limit: 돌려줄 봉 수(1~2000). 기간 안에서 가장 최근 것부터 이만큼.
        """
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        today = ctx.clock().astimezone(KST).date()
        period = period or {}
        until = _date(period.get("end"), today)
        default_start = today - timedelta(days=DEFAULT_DAILY_DAYS) if interval == "day" else today
        since = _date(period.get("start"), default_start)
        if since is None or until is None or since > until:
            return ctx.invalid("start, end는 YYYY-MM-DD이고 start가 end보다 늦을 수 없습니다.")
        if not 1 <= limit <= MAX_CANDLES:
            return ctx.invalid(f"limit은 1~{MAX_CANDLES} 사이로 넣어 주세요.")

        async def work() -> dict[str, JsonValue]:
            candles = await queries.candles(
                ctx.user_id(), symbol.strip(), interval, (since, until), limit
            )
            return {
                "symbol": symbol.strip(),
                "interval": interval,
                "candles": [_candle(c) for c in candles],
                "count": len(candles),
            }

        return await ctx.answer(work())
