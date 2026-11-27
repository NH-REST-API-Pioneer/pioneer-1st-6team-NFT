"""옵션 도구: get_option_chain. "옵션" 묶음이다(`docs/tools.md`).

도구는 인자 검사와 응답 포장만 한다. 만기 고르기, 전광판 받기, 등가격 주변 자르기는
`services/options.py`가 한다.
"""

from datetime import date

from fastmcp import FastMCP

from nh_mcp import envelope
from nh_mcp.domain import ChainRow, JsonValue, OptionSide
from nh_mcp.services.options import ChainView, OptionService
from nh_mcp.tools.context import ToolContext, Whole, text, whole

# "옵션" 묶음의 도구에 붙이는 태그. 묶음을 켜지 않은 사용자에게서는 숨긴다(`tools/features.py`).
OPTIONS_TAG = "options"
DEFAULT_STRIKES_AROUND = 10
MAX_STRIKES_AROUND = 40


def _side(s: OptionSide | None) -> dict[str, JsonValue] | None:
    if s is None:
        return None
    return {
        "symbol": s.symbol,
        "last": text(s.last),
        "bid": text(s.bid),
        "ask": text(s.ask),
        "volume": s.volume,
        "open_interest": s.open_interest,
        "iv": text(s.iv),
        "delta": text(s.delta),
        "theoretical": text(s.theoretical),
    }


def _row(r: ChainRow) -> dict[str, JsonValue]:
    return {"strike": str(r.strike), "call": _side(r.call), "put": _side(r.put)}


def _view(v: ChainView) -> dict[str, JsonValue]:
    c = v.chain
    return {
        "underlying": c.underlying,
        "expiry": c.expiry.isoformat(),
        "expiries": [d.isoformat() for d in v.expiries],
        "spot": str(c.spot),
        "future_symbol": c.future_symbol,
        "future_price": text(c.future_price),
        "atm_strike": str(v.atm),
        "strikes_total": v.strikes,
        "dropped_codes": c.dropped,
        "rows": [_row(r) for r in c.rows],
    }


def register_option_tools(mcp: FastMCP, ctx: ToolContext, options: OptionService) -> None:
    @mcp.tool(tags={OPTIONS_TAG}, annotations={"readOnlyHint": True})
    async def get_option_chain(
        expiry: str | None = None, strikes_around: Whole = DEFAULT_STRIKES_AROUND
    ) -> envelope.Response:
        """코스피200 월물 옵션의 행사가별 콜 · 풋 종목코드와 시세를 돌려준다.

        쓸 때: 옵션 종목코드가 필요할 때, 행사가별 옵션 가격 · 내재변동성 · 델타를 볼 때.
        옵션 종목코드는 반드시 이 결과에서 고른다. 종목코드를 추측해서 만들지 않고, 종목코드에서
        행사가를 읽지 않는다(행사가는 strike).
        쓰지 않을 때: 선물 종목코드는 search_symbol. 옵션 한 종목의 호가 단계는 get_quote.
        다음: 고른 종목코드로 get_quote, 주문은 preview_order.
        expiry: 만기일 YYYY-MM-DD. 비우면 가장 가까운 만기. 응답의 expiries가 고를 수 있는 만기다.
        strikes_around: 등가격(기초자산 현재가 spot에 가장 가까운 행사가) 위아래로 돌려줄 행사가
        수(0~40, 기본 10). rows는 행사가 오름차순이고, 호가가 없으면 bid · ask가 null이다.
        종목 마스터에 없는 종목코드는 null로 비우고 dropped_codes에 센다.
        """
        around = whole(strikes_around, 0, MAX_STRIKES_AROUND)
        if around is None:
            return ctx.invalid(
                f"strikes_around는 0~{MAX_STRIKES_AROUND} 사이의 정수로 넣어 주세요."
            )
        chosen = None
        if expiry is not None and expiry.strip():
            try:
                chosen = date.fromisoformat(expiry.strip())
            except ValueError:
                return ctx.invalid(
                    "expiry는 YYYY-MM-DD로 넣어 주세요. 비우면 가장 가까운 만기입니다."
                )

        async def work() -> dict[str, JsonValue]:
            return _view(await options.chain(ctx.user_id(), chosen, around))

        return await ctx.answer(work())
