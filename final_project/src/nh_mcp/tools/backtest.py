"""백테스트 도구: backtest_strategy와 예제 리소스 strategy://examples/mean-reversion.

도구는 입력 검사와 응답 포장만 한다. 봉 모으기, 격리 실행, 성과 계산, 기록은
`services/backtest.py`가 한다. 주문을 내지 않으므로 `ORDERS_ENABLED`와 상관없이 등록한다.
속도 제한은 서비스보다 먼저 본다.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Literal, NotRequired, TypedDict, cast

from fastmcp import FastMCP

from nh_mcp import envelope
from nh_mcp.domain import KST, JsonValue
from nh_mcp.ops.rate_limit import BACKTEST_BUCKET, RateLimiter
from nh_mcp.services.backtest import (
    MAX_QUANTITY,
    BacktestRequest,
    BacktestService,
    check_strategy,
    report_data,
)
from nh_mcp.strategies.examples import MEAN_REVERSION
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, Whole, whole

EXAMPLE_URI = "strategy://examples/mean-reversion"
INTERVALS: dict[str, Literal["minute", "day"]] = {"minute": "minute", "day": "day"}
# 날짜는 YYYY-MM-DD만. fromisoformat은 "20261101" 같은 다른 ISO 모양도 받아서 먼저 거른다.
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# 부호, 지수, NaN, Infinity, 전각 숫자, 밑줄, 쉼표를 모두 막으려고 ASCII 숫자만 받는다.
AMOUNT = re.compile(r"[0-9]{1,15}(\.[0-9]{1,6})?")
_NO_FLOATS = (
    'strategy.params에는 실수를 넣을 수 없습니다. 소수는 "0.02"처럼 문자열로 넣고 '
    "코드에서 숫자로 바꿔 쓰세요."
)


class StrategyCode(TypedDict):
    """전략 코드와 파라미터. 파라미터 값은 문자열·정수·참거짓·null·목록·객체."""

    code: str
    params: NotRequired[dict[str, object]]


class BacktestPeriod(TypedDict):
    """봉 간격("minute" 1분봉 또는 "day" 일봉)과 기간. 날짜는 YYYY-MM-DD."""

    interval: str
    start: str
    end: str


class BacktestSettings(TypedDict):
    """체결 조건. 수량은 계약 수, 수수료는 체결 1건·1계약당 금액(숫자만 쓴 문자열)."""

    quantity: NotRequired[Whole]
    fee_per_fill: NotRequired[str]


def _params_problem(params: object) -> str | None:
    """JSON 값이 문자열·정수·참거짓·null·목록·문자열 키 객체로만 돼 있으면 None.

    재귀하지 않고 스택으로 훑는다(깊게 중첩된 입력에 막히지 않게).
    """
    if not isinstance(params, dict):
        return "strategy.params는 JSON 객체여야 합니다."
    stack: list[object] = [params]
    while stack:
        item = stack.pop()
        if item is None or type(item) in (str, int, bool):
            continue
        if isinstance(item, list):
            stack.extend(cast(list[object], item))
        elif isinstance(item, dict):
            for key, value in cast(dict[object, object], item).items():
                if not isinstance(key, str):
                    return "strategy.params의 키는 문자열이어야 합니다."
                stack.append(value)
        else:
            # JSON에서 오는 나머지 값은 실수뿐이다.
            return _NO_FLOATS
    return None


def parse_strategy(raw: StrategyCode) -> tuple[str, dict[str, JsonValue]] | str:
    """코드와 검사한 파라미터, 또는 잘못됐다고 알려 줄 문장. 코드는 고치지 않는다(해시가 바뀐다)."""
    code = raw["code"]
    params: object = raw.get("params", {})
    problem = _params_problem(params)
    if problem is not None:
        return problem
    checked = cast(dict[str, JsonValue], params)
    problem = check_strategy(code, checked)
    if problem is not None:
        return problem
    return code, checked


def _day(raw: str) -> date | None:
    text = raw.strip()
    if _DATE.fullmatch(text) is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _period(raw: BacktestPeriod, today: date) -> tuple[Literal["minute", "day"], date, date] | str:
    interval = INTERVALS.get(raw["interval"])
    if interval is None:
        return 'period.interval은 "minute"(1분봉) 또는 "day"(일봉)입니다.'
    start, end = _day(raw["start"]), _day(raw["end"])
    if start is None or end is None or start > end:
        return "period.start, end는 YYYY-MM-DD이고 start가 end보다 늦을 수 없습니다."
    if end > today:
        return f"period.end는 오늘({today.isoformat()})보다 늦을 수 없습니다."
    return interval, start, end


def _settings(raw: BacktestSettings | None) -> tuple[int, str] | str:
    """(수량, 수수료 문자열), 또는 잘못됐다고 알려 줄 문장."""
    raw = raw or {}
    quantity = whole(raw.get("quantity", 1), 1, MAX_QUANTITY)
    if quantity is None:
        return f"settings.quantity는 1 이상 {MAX_QUANTITY} 이하의 정수(계약 수)여야 합니다."
    fee = raw.get("fee_per_fill", "0").strip()
    if AMOUNT.fullmatch(fee) is None:
        return 'settings.fee_per_fill은 0 이상의 금액을 숫자만으로 써 주세요. 예: "1500"'
    return quantity, fee


def register_backtest_tools(
    mcp: FastMCP, ctx: ToolContext, backtests: BacktestService, limiter: RateLimiter
) -> None:
    @mcp.resource(EXAMPLE_URI, name="mean-reversion", mime_type="text/x-python")
    def mean_reversion_example() -> str:
        """평균회귀 예제 전략 코드. backtest_strategy의 strategy.code로 그대로 쓰거나 고쳐 쓴다.

        params 예: {"window": 20, "band": "0.02"}.
        """
        return MEAN_REVERSION

    @mcp.tool(annotations={"readOnlyHint": True})
    async def backtest_strategy(
        symbol: str,
        strategy: StrategyCode,
        period: BacktestPeriod,
        settings: BacktestSettings | None = None,
        market: str = "domestic",
    ) -> envelope.Response:
        """AI가 짠 전략 코드를 격리 환경에서 과거 봉으로 돌려 성과를 계산한다. 주문은 나가지 않는다.

        쓸 때: 사용자의 매매 아이디어를 코드로 짠 뒤 과거에 어땠을지 볼 때. 전략을 켜기 전에 반드시.
        쓰지 않을 때: 가격 흐름만 보려면 get_candles, 지금 주문하려면 preview_order.
        symbol: search_symbol 결과의 국내 선물 종목코드. market은 "domestic"만 된다.
        strategy: {"code": 코드 문자열, "params": JSON 객체}. 코드는 함수 하나다:
        on_bar(bar, history, params) -> "buy" | "sell" | "exit" | "none".
        bar는 {at, open, high, low, close, volume}이고 가격은 문자열이다.
        history는 이번 봉 이전의 봉들(오래된 것부터). 앞으로 올 봉은 볼 수 없다.
        신호는 목표 포지션이다: buy=매수 보유, sell=매도 보유, exit=정리, none=그대로.
        params 값에 실수를 넣지 않는다. 소수는 "0.02"처럼 문자열로 넣는다.
        코드는 64KB, params는 JSON 4KB까지. 라이브러리는 표준 라이브러리, numpy, pandas.
        예제: 리소스 strategy://examples/mean-reversion
        (평균회귀, params {"window": 20, "band": "0.02"}).
        period: {"interval": "minute"(1분봉) 또는 "day"(일봉), "start": "YYYY-MM-DD",
        "end": "YYYY-MM-DD"}. 봉이 2000개를 넘으면 가장 최근 2000개만 쓰고 truncated가 true다.
        settings: {"quantity": 계약 수(1~100, 기본 1), "fee_per_fill": 체결 1건·1계약당 수수료
        (숫자만 쓴 문자열, 기본 "0")}.
        체결은 신호 봉의 다음 봉 시가 ± 1틱(살 때 +, 팔 때 -). 마지막 봉의 신호는 버린다
        (last_signal_ignored). 끝까지 남은 포지션은 open_position에 따로 적고 net_pnl에 넣지 않는다.
        fees에는 그 진입 수수료(open_position.entry_fee)도 들어 있다. 금액은 원 단위 문자열.
        코드가 현재 시각이나 os.urandom을 직접 읽으면 같은 입력에서도 결과가 달라질 수 있다.
        막히면 error.code가 INVALID_INPUT(입력·코드 형식·데이터 부족), STRATEGY_FAILED(코드의 오류나
        자원 제한, error.rule에 사유), TEMPORARILY_UNAVAILABLE(실행기가 바쁨, 잠시 뒤 다시)다.
        다음: 결과가 좋으면 같은 코드·파라미터·간격으로 preview_strategy.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, BACKTEST_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        parsed = parse_strategy(strategy)
        if isinstance(parsed, str):
            return ctx.invalid(parsed)
        code, params = parsed
        span = _period(period, ctx.clock().astimezone(KST).date())
        if isinstance(span, str):
            return ctx.invalid(span)
        interval, start, end = span
        terms = _settings(settings)
        if isinstance(terms, str):
            return ctx.invalid(terms)
        quantity, fee = terms
        request = BacktestRequest(
            symbol=symbol.strip(),
            code=code,
            params=params,
            interval=interval,
            period=(start, end),
            quantity=quantity,
            fee_per_fill=Decimal(fee),
        )

        async def work() -> dict[str, JsonValue]:
            outcome = await backtests.run(user_id, request)
            warnings: list[JsonValue] = [*outcome.warnings]
            requested: dict[str, JsonValue] = {
                "interval": interval,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            data = report_data(outcome.report)
            data.update(
                code_hash=outcome.code_hash,
                period=requested,
                first_bar_at=outcome.first_bar_at.astimezone(KST).isoformat(timespec="seconds"),
                last_bar_at=outcome.last_bar_at.astimezone(KST).isoformat(timespec="seconds"),
                bars=outcome.bars,
                truncated=outcome.truncated,
                warnings=warnings,
            )
            return data

        return await ctx.answer(work())
