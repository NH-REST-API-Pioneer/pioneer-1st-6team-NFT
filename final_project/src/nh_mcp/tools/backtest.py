"""백테스트 도구: backtest_strategy, 계약 리소스 strategy://contract, 예제 리소스
strategy://examples/mean-reversion·pair-spread·scale-in-limit·tick-momentum.

도구는 입력 검사와 응답 포장만 한다. 봉·체결·호가 모으기, 격리 실행, 성과 계산, 기록은
`services/backtest.py`가 한다. 주문을 내지 않으므로 `ORDERS_ENABLED`와 상관없이 등록한다.
속도 제한은 서비스보다 먼저 본다.
"""

import inspect
import re
from datetime import date
from decimal import Decimal
from typing import NotRequired, TypedDict, cast

from fastmcp import FastMCP
from pydantic import ConfigDict, with_config

from nh_mcp import envelope
from nh_mcp.domain import KST, JsonValue, ModelHash, Stream, Streams, model_hash
from nh_mcp.ops.rate_limit import BACKTEST_BUCKET, RateLimiter
from nh_mcp.services.backtest import (
    MAX_BARS,
    MAX_EVENTS,
    MAX_POSITION,
    MAX_TICK_EVENTS,
    TICK_EXAMPLE,
    BacktestRequest,
    BacktestService,
    check_strategy,
    check_streams,
    coverage_data,
    report_data,
    streams_data,
)
from nh_mcp.strategies.examples import MEAN_REVERSION, PAIR_SPREAD, SCALE_IN_LIMIT, TICK_MOMENTUM
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, Whole, whole
from nh_mcp.tools.contract import CONTRACT_URI, contract_text
from nh_mcp.tools.order import MAX_SYMBOL_LENGTH

EXAMPLE_URI = "strategy://examples/mean-reversion"
PAIR_EXAMPLE_URI = "strategy://examples/pair-spread"
SCALE_IN_EXAMPLE_URI = "strategy://examples/scale-in-limit"
# 흐름 이름. 봉 실행은 minute·day, 틱 실행은 tick·book·day(섞는 규칙은 서비스가 본다).
STREAM_NAMES: dict[str, Stream] = {"minute": "minute", "day": "day", "tick": "tick", "book": "book"}
# 날짜는 YYYY-MM-DD만. fromisoformat은 "20261101" 같은 다른 ISO 모양도 받아서 먼저 거른다.
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# 부호, 지수, NaN, Infinity, 전각 숫자, 밑줄, 쉼표를 모두 막으려고 ASCII 숫자만 받는다.
AMOUNT = re.compile(r"[0-9]{1,15}(\.[0-9]{1,6})?")
# 모르는 칸은 조용히 버리지 않고 거절한다(이름이 바뀐 옛 칸 settings.quantity 같은 것).
_EXACT = ConfigDict(extra="forbid")
_NO_FLOATS = (
    'strategy.params에는 실수를 넣을 수 없습니다. 소수는 "0.02"처럼 문자열로 넣고 '
    "코드에서 숫자로 바꿔 쓰세요."
)
BAD_MODEL = (
    "strategy.model은 모델 파일 화면에 보이는 SHA-256(소문자 16진수 64자)이어야 합니다. "
    "모델을 쓰지 않으면 이 칸을 빼 주세요."
)
# 도구 설명의 이 글자를 모델 파일 화면 주소로 바꾼다(주소는 설정의 공개 주소에서 온다).
MODELS_URL_SLOT = "{models_url}"


@with_config(_EXACT)
class StrategyCode(TypedDict):
    """전략 코드와 파라미터, 모델 파일(선택). 파라미터 값은 문자열·정수·참거짓·null·목록·객체."""

    code: str
    params: NotRequired[dict[str, object]]
    model: NotRequired[str]


@with_config(_EXACT)
class BacktestData(TypedDict):
    """흐름과 기간. streams는 종목코드 → 흐름 목록("minute" 1분봉, "day" 일봉, "tick" 체결,
    "book" 호가).

    날짜는 YYYY-MM-DD.
    """

    streams: dict[str, list[str]]
    start: str
    end: str


@with_config(_EXACT)
class BacktestSettings(TypedDict):
    """체결 조건. max_position은 목표 포지션 절댓값의 상한(계약 수), 수수료는 체결 1건·1계약당
    금액(숫자만 쓴 문자열)."""

    max_position: NotRequired[Whole]
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


def parse_strategy(
    raw: StrategyCode,
) -> tuple[str, dict[str, JsonValue], ModelHash | None] | str:
    """코드, 검사한 파라미터, 모델 해시(없으면 None), 또는 잘못됐다고 알려 줄 문장.

    코드는 고치지 않는다(해시가 바뀐다). 모델 해시도 대소문자를 바꾸지 않는다.
    """
    code = raw["code"]
    params: object = raw.get("params", {})
    problem = _params_problem(params)
    if problem is not None:
        return problem
    checked = cast(dict[str, JsonValue], params)
    problem = check_strategy(code, checked)
    if problem is not None:
        return problem
    given = raw.get("model")
    model = None if given is None else model_hash(given)
    if given is not None and model is None:
        return BAD_MODEL
    return code, checked, model


def parse_streams(raw: dict[str, list[str]]) -> Streams | str:
    """종목코드 → 흐름 집합, 또는 잘못됐다고 알려 줄 문장. 종목코드는 고치지 않는다(키가 바뀐다)."""
    streams: dict[str, frozenset[Stream]] = {}
    for symbol, names in raw.items():
        if not symbol or len(symbol) > MAX_SYMBOL_LENGTH:
            return "streams의 키는 search_symbol 결과의 종목코드여야 합니다."
        checked: list[Stream | None] = [STREAM_NAMES.get(name) for name in names]
        if None in checked:
            return (
                'streams의 흐름은 "minute"(1분봉), "day"(일봉), "tick"(체결), "book"(호가)입니다.'
            )
        streams[symbol] = frozenset(s for s in checked if s is not None)
    return check_streams(streams) or streams


def _day(raw: str) -> date | None:
    text = raw.strip()
    if _DATE.fullmatch(text) is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _period(raw: BacktestData, today: date) -> tuple[date, date] | str:
    start, end = _day(raw["start"]), _day(raw["end"])
    if start is None or end is None or start > end:
        return "data.start, end는 YYYY-MM-DD이고 start가 end보다 늦을 수 없습니다."
    if end > today:
        return f"data.end는 오늘({today.isoformat()})보다 늦을 수 없습니다."
    return start, end


def _settings(raw: BacktestSettings | None) -> tuple[int, str] | str:
    """(최대 포지션, 수수료 문자열), 또는 잘못됐다고 알려 줄 문장."""
    raw = raw or {}
    position = whole(raw.get("max_position", 1), 1, MAX_POSITION)
    if position is None:
        return f"settings.max_position은 1 이상 {MAX_POSITION} 이하의 정수(계약 수)여야 합니다."
    fee = raw.get("fee_per_fill", "0").strip()
    if AMOUNT.fullmatch(fee) is None:
        return 'settings.fee_per_fill은 0 이상의 금액을 숫자만으로 써 주세요. 예: "1500"'
    return position, fee


def _request(
    strategy: StrategyCode, data: BacktestData, settings: BacktestSettings | None, today: date
) -> BacktestRequest | str:
    """서비스에 넘길 요청, 또는 입력이 잘못됐다고 알려 줄 문장."""
    parsed = parse_strategy(strategy)
    if isinstance(parsed, str):
        return parsed
    streams = parse_streams(data["streams"])
    if isinstance(streams, str):
        return streams
    span = _period(data, today)
    if isinstance(span, str):
        return span
    terms = _settings(settings)
    if isinstance(terms, str):
        return terms
    code, params, model = parsed
    position, fee = terms
    return BacktestRequest(code, params, streams, span, position, Decimal(fee), model)


def register_backtest_tools(
    mcp: FastMCP,
    ctx: ToolContext,
    backtests: BacktestService,
    limiter: RateLimiter,
    models_url: str,
) -> None:
    contract = contract_text(models_url)

    @mcp.resource(CONTRACT_URI, name="strategy-contract", mime_type="text/markdown")
    def strategy_contract() -> str:
        """전략 계약 전부와 남는 제약(데이터, 자원, 라이브러리, 체결 가정), 범위 밖일 때 할 일.

        전략을 짜기 전에 읽는다. 여기에 없는 것은 할 수 없으니 비슷한 것으로 바꿔 짜지 말고
        사용자에게 먼저 말한다.
        """
        return contract

    @mcp.resource(EXAMPLE_URI, name="mean-reversion", mime_type="text/x-python")
    def mean_reversion_example() -> str:
        """평균회귀 예제 전략 코드(한 종목). backtest_strategy의 strategy.code로 쓰거나 고쳐 쓴다.

        params 예: {"window": 20, "band": "0.02"}. data.streams 예: {"A016C000": ["minute"]}.
        """
        return MEAN_REVERSION

    @mcp.resource(PAIR_EXAMPLE_URI, name="pair-spread", mime_type="text/x-python")
    def pair_spread_example() -> str:
        """두 종목 스프레드 예제 전략 코드. backtest_strategy의 strategy.code로 쓰거나 고쳐 쓴다.

        params 예: {"a": "A016C000", "b": "A0173000", "window": 30, "entry_z": "2",
        "exit_z": "0.5"}.
        a·b는 search_symbol로 찾은 종목코드로 바꾸고, data.streams에 두 종목을 ["minute"]으로
        넣는다. 목표가 params의 quantity(기본 1)이므로 settings.max_position을 그 이상으로 둔다.
        """
        return PAIR_SPREAD

    @mcp.resource(SCALE_IN_EXAMPLE_URI, name="scale-in-limit", mime_type="text/x-python")
    def scale_in_limit_example() -> str:
        """수량 조절·지정가 예제 전략 코드(한 종목, 1분봉): 평균보다 내려갈수록 한 계약씩 늘리고
        평균으로 돌아오면 정리한다. 주문은 종가에서 1틱 유리한 쪽 지정가다.

        params 예: {"window": 20, "step": "0.003", "max_steps": 3, "tick": "0.05"}. tick은
        search_symbol 결과의 그 종목 틱 단위로 바꾼다. settings.max_position은 max_steps 이상.
        data.streams 예: {"A016C000": ["minute"]}.
        """
        return SCALE_IN_LIMIT

    @mcp.resource(TICK_EXAMPLE, name="tick-momentum", mime_type="text/x-python")
    def tick_momentum_example() -> str:
        """틱 예제 전략 코드: 체결로 1분봉을 직접 만들고 호가 쏠림을 같이 본다(틱 실행).

        params 예: {"window": 5, "imbalance": "0.2"}. data.streams 예: {"A016C000": ["tick",
        "book"]}. 먼저 set_tick_recording으로 그 종목을 저장해 둔 기간이어야 한다.
        """
        return TICK_MOMENTUM

    async def backtest_strategy(
        strategy: StrategyCode,
        data: BacktestData,
        settings: BacktestSettings | None = None,
        market: str = "domestic",
    ) -> envelope.Response:
        """AI가 짠 전략 코드를 격리 환경에서 과거 봉·틱으로 돌려 성과를 잰다. 주문은 나가지 않는다.
        범위와 제약은 strategy://contract.

        쓸 때: 사용자의 매매 아이디어를 코드로 짠 뒤 과거에 어땠을지 볼 때. 전략을 켜기 전에
        반드시.
        쓰지 않을 때: 가격 흐름만 보려면 get_candles, 지금 주문하려면 preview_order.
        strategy: {"code": 코드 문자열, "params": JSON 객체(선택), "model": 모델 파일의
        SHA-256(선택)}. 코드에는 on_event(event, ctx)를 두고, 처음 한 번 할 일이 있으면
        init(ctx)를 둔다. 서버가 이벤트를 하나 넣고 출력을 받은 뒤에 다음 이벤트를 넣는다.
        실행은 둘 중 하나다. 봉 실행은 "minute"·"day"(NH에서 받는다). 틱 실행은 "tick"·
        "book"에 "day"를 더할 수 있다. "minute"과 "tick"·"book"은 같이 넣을 수 없다(실시간에서
        1분봉은 그 분이 끝나고 몇 초 뒤에 와서 순서를 맞출 수 없다). 틱 실행에서 1분봉이
        필요하면 코드에서 체결로 만든다(예제 tick-momentum).
        event: 봉 {"type": "bar", "symbol", "interval": "minute"|"day", "at", "open",
        "high", "low", "close", "volume"}(at은 봉이 시작한 KST 시각, 일봉은 그날 0시),
        체결 {"type": "tick", "symbol", "at", "price", "qty", "bid", "ask"}(bid·ask는 그때
        최우선 호가, 없으면 null), 호가 {"type": "book", "symbol", "at", "bids": [[가격,
        잔량], ...], "asks": [...]}(최우선부터 잔량이 있는 단계까지, 최대 10단계), 내 체결
        {"type": "fill", "symbol", "at", "side": "buy"|"sell", "qty", "price"}. 가격은
        문자열이다.
        ctx: params, positions(종목별 체결된 포지션, 롱 +·숏 -), history(symbol, interval)
        (그 종목·간격의 지난 봉, 이번 봉은 빼고 오래된 것부터. 체결·호가는 쌓지 않는다),
        model_path(모델 파일 경로, strategy.model이 없으면 None).
        모델 파일: 사용자가 {models_url} 화면에 올린 파일(50MB·5개까지)의 SHA-256을
        strategy.model에 넣으면 그 복사본이 ctx.model_path("/tmp/model")에 놓이고, init(ctx)에서
        joblib.load(ctx.model_path)나 torch.load(ctx.model_path)로 읽는다. 없는 해시면
        INVALID_INPUT이고, 메모리 512MB에는 이 파일도 든다.
        출력: None(그대로) 또는 {종목코드: {"position": 목표 계약 수(롱 +, 숏 -, 0은 정리),
        "price": "지정가"}}. price가 없으면 시장가다. 종목은 data.streams에 넣은 것만,
        |position|은 settings.max_position 이하, price는 그 종목 틱 단위. 어기면
        INVALID_INPUT.
        체결: 시장가는 판단 뒤 시작한 그 종목의 첫 봉 시가(틱 실행은 다음 체결가) ± 1틱(살 때
        +). 지정가는 그 뒤 봉의 저가(틱 실행은 체결가)가 지정가보다 낮을 때(팔 때는 고가가
        높을 때)만 지정가에 전량. 닿기만 해서는 체결되지 않고, 호가 이벤트로는 체결되지
        않는다. 목표를 새로 내면 그 종목의 걸린 주문은 취소된다. 체결은 그 이벤트보다 먼저
        fill 이벤트로 온다.
        미래는 볼 수 없다: 봉 실행은 봉이 끝난 시각 순서로 오고(같으면 일봉 먼저, 그다음
        종목코드 순) 그날 일봉은 그날 1분봉이 다 온 뒤에 온다. 틱 실행은 서버가 받은 순서
        그대로 오고(종목·흐름이 섞인다), at은 거래소 시각(초)에 받은 날짜를 붙인 것으로 뒤로
        가지 않는다. 일봉은 그날이 끝난 뒤 처음 오는 체결·호가 바로 앞에 온다(그날 안에는
        오지 않는다).
        params 값에 실수를 넣지 않는다(소수는 "0.02"처럼 문자열). 코드는 64KB, params는
        JSON 4KB까지. 라이브러리는 표준 라이브러리, numpy, pandas, scipy, scikit-learn,
        statsmodels, lightgbm, PyTorch(CPU). 코드가 현재 시각이나
        os.urandom을 읽으면 같은 입력에서도 결과가 달라질 수 있다.
        예제: 리소스 strategy://examples/mean-reversion(한 종목,
        params {"window": 20, "band": "0.02"}), strategy://examples/pair-spread(두 종목),
        strategy://examples/scale-in-limit(수량 조절과 지정가, settings.max_position은
        params.max_steps 이상), strategy://examples/tick-momentum(틱 실행, params {"window": 5,
        "imbalance": "0.2"}).
        data: {"streams": {종목코드: ["minute"(1분봉), "day"(일봉), "tick"(체결), "book"(호가)
        가운데 하나 이상]}, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}. 종목코드는
        search_symbol 결과의 국내 선물만, 5개까지. 봉 실행은 흐름마다 봉이 {max_bars}개를
        넘으면 가장 최근 {max_bars}개만 받고, 모두 합쳐 {max_events}개를 넘으면 가장 최근
        {max_events}개만 넣는다. 체결·호가는 그 사용자가 set_tick_recording으로 저장을 켠 뒤에
        받은 것만 있다(NH는 지난 체결·호가를 주지 않는다. 저장한 기간은 get_strategy_data).
        기간 안에 저장한 것이 없는 흐름이 있으면 INVALID_INPUT이다. 틱 실행은 일봉을 합쳐
        {max_tick_events}개까지 가장 최근 것만 넣는다. 잘리면 truncated와 warnings에 적힌다.
        저장이 끊긴 빈 구간은 채우지 않는다(coverage의 gaps와 warnings).
        settings: {"max_position": |position| 상한(1~100, 기본 1), "fee_per_fill": 체결
        1건·1계약당 수수료(숫자만 쓴 문자열, 기본 "0")}. market은 "domestic"만 된다.
        결과: totals(합계)와 symbols(종목별) 지표, trades(끝난 거래, 가장 최근 50건),
        open_positions(끝까지 남은 포지션, 마지막 가격으로 평가만 하고 net_pnl에 넣지
        않는다), pending_orders(끝까지 체결되지 않은 주문), coverage(종목·흐름마다 넣은
        범위: 봉은 first_bar_at·last_bar_at·bars, 체결·호가는 first_event_at·last_event_at·
        events·gaps(빈 구간의 total과 가장 최근 20개의 start·end·reason), 그리고 truncated),
        events(넣은 이벤트 수). 금액은 원 단위 문자열이고, fees에는 남은 포지션의 수수료도
        들어 있다.
        막히면 error.code가 INVALID_INPUT(입력·코드·출력 형식, 데이터 부족),
        STRATEGY_FAILED(코드의 오류나 자원 제한, error.rule에 사유),
        TEMPORARILY_UNAVAILABLE(실행기가 바쁨, 잠시 뒤 다시)다.
        다음: 결과가 좋으면 같은 code·params·streams·model로 preview_strategy.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, BACKTEST_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        request = _request(strategy, data, settings, ctx.clock().astimezone(KST).date())
        if isinstance(request, str):
            return ctx.invalid(request)

        async def work() -> dict[str, JsonValue]:
            outcome = await backtests.run(user_id, request)
            start, end = request.period
            answer: dict[str, JsonValue] = {
                "code_hash": outcome.code_hash,
                "model_hash": request.model,
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "streams": streams_data(request.streams),
                "settings": {
                    "max_position": request.max_position,
                    "fee_per_fill": str(request.fee_per_fill),
                },
                **report_data(outcome.report),
                "coverage": coverage_data(outcome.feed),
                "events": len(outcome.feed.events),
                "truncated": outcome.feed.truncated,
                "warnings": [*outcome.feed.warnings],
            }
            return answer

        return await ctx.answer(work())

    described = (
        (backtest_strategy.__doc__ or "")
        .replace(MODELS_URL_SLOT, models_url)
        .replace("{max_bars}", f"{MAX_BARS:,}")
        .replace("{max_events}", f"{MAX_EVENTS:,}")
        .replace("{max_tick_events}", f"{MAX_TICK_EVENTS:,}")
    )
    mcp.tool(annotations={"readOnlyHint": True}, description=inspect.cleandoc(described))(
        backtest_strategy
    )
