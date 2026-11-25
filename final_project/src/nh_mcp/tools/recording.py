"""틱 저장 도구: set_tick_recording, get_strategy_data.

도구는 입력 검사와 응답 포장만 한다. 종목 확인, 구독, 저장 상태는 `services/recording.py`가 한다.
주문을 내지 않으므로 기본 묶음이다(주문 스위치와 상관없이 등록).
"""

from fastmcp import FastMCP

from nh_mcp import envelope
from nh_mcp.ops.rate_limit import RECORDING_BUCKET, RateLimiter
from nh_mcp.services.recording import RecordingInvalid, RecordingService
from nh_mcp.store.ticks import RecordStream, Sub
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext
from nh_mcp.tools.order import MAX_SYMBOL_LENGTH

STREAM_NAMES: dict[str, RecordStream] = {"tick": "tick", "book": "book"}
BAD_STREAM = (
    '흐름은 "tick"(체결) 또는 "book"(호가)입니다. 1분봉·일봉은 저장하지 않아도 '
    "backtest_strategy에서 바로 씁니다."
)


def parse_symbols(raw: dict[str, list[str]]) -> frozenset[Sub] | str:
    """(종목, 흐름) 집합, 또는 잘못됐다고 알려 줄 문장. 종목코드는 고치지 않는다."""
    subs: set[Sub] = set()
    for symbol, names in raw.items():
        if not symbol or len(symbol) > MAX_SYMBOL_LENGTH:
            return "symbols의 키는 search_symbol 결과의 종목코드여야 합니다."
        if not names:
            return f"{symbol}: 흐름을 하나 이상 넣으세요. 이 종목의 저장을 멈추려면 종목을 빼세요."
        for name in names:
            stream = STREAM_NAMES.get(name)
            if stream is None:
                return BAD_STREAM
            subs.add(Sub(symbol, stream))
    return frozenset(subs)


def register_recording_tools(
    mcp: FastMCP, ctx: ToolContext, service: RecordingService, limiter: RateLimiter
) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
    async def set_tick_recording(
        symbols: dict[str, list[str]], market: str = "domestic"
    ) -> envelope.Response:
        """종목별 체결(tick)·호가(book)를 서버에 저장하는 것을 켜고 끈다. 주문은 나가지 않는다.
        범위와 제약은 strategy://contract.

        쓸 때: 사용자가 틱·호가로 전략을 시험하려고 데이터를 모으자고 할 때, 저장할 종목을
        바꾸거나 멈출 때.
        쓰지 않을 때: 1분봉·일봉 백테스트에는 필요 없다(NH가 과거 봉을 바로 준다). 무엇이
        저장됐는지 볼 때는 get_strategy_data.
        symbols: 저장할 전체 집합 {종목코드: ["tick", "book" 가운데 하나 이상]}. 보낸 값이 그대로
        새 설정이 되고, 빠진 종목·흐름은 멈춘다. {}이면 모두 멈춘다. 같은 값을 다시 보내도 바뀌는
        것이 없다. 종목코드는 search_symbol 결과의 국내 선물만 쓴다.
        구독: 종목 하나의 흐름 하나가 1건, APP Key당 20건까지. 넘으면 INVALID_INPUT. 자동 실행
        전략이 생기면 그 구독도 이 20건을 같이 쓴다.
        사용자 자기 NH 모의 키로 받고 그 사용자만 쓴다. 국내 키가 없으면 KEY_NOT_REGISTERED(등록
        화면 주소가 함께 온다). 멈추기({})는 키 없이도 된다. 키를 지우면 저장이 멈추고 이 설정도
        지워진다(새 키를 넣어도 다시 켜야 한다). 서버가 다시 뜨면 저장도 저절로 다시 켜진다.
        지금부터 받는 것만 저장한다(NH는 지난 틱을 주지 않는다). 평일 주간장(08:45~15:45)만
        저장하고 30일 뒤 지운다. 끊긴 동안은 빈 구간으로 남는다.
        결과: recording(지금 저장하는 집합), subscriptions(구독마다 state: subscribed 받는 중,
        pending NH 답을 기다리는 중, error NH가 거절함·message에 사유), used·limit(쓰는 구독
        건수와 상한), connection(NH 실시간 연결 여부, 끊겼으면 error에 사유 종류), note.
        market은 "domestic"만 된다. 1분에 10번까지 부를 수 있다(넘으면 TEMPORARILY_UNAVAILABLE).
        다음: 쌓인 기간과 빈 구간은 get_strategy_data로 본다. 쌓인 체결·호가로 백테스트하려면
        backtest_strategy의 data.streams에 {종목코드: ["tick", "book"]}을 넣는다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, RECORDING_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        subs = parse_symbols(symbols)
        if isinstance(subs, str):
            return ctx.invalid(subs)
        try:
            data = await service.set(user_id, subs)
        except RecordingInvalid as exc:
            return ctx.invalid(exc.message)
        except Exception as exc:
            return ctx.failed(exc)
        return envelope.ok(data, ctx.meta())

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_strategy_data(market: str = "domestic") -> envelope.Response:
        """내가 백테스트에 쓸 수 있는 데이터를 보여 준다: 저장한 체결·호가의 기간과 빈 구간, 올린
        모델 파일. 읽기만 한다. 범위와 제약은 strategy://contract.

        쓸 때: 틱·호가나 모델 파일을 쓰는 전략을 짜기 전에 무엇이 얼마나 있는지 볼 때,
        set_tick_recording 뒤에 잘 쌓이는지 볼 때.
        쓰지 않을 때: 1분봉·일봉은 저장하지 않아도 NH에서 바로 받으므로 볼 필요가 없다
        (get_candles, backtest_strategy).
        결과: recorded(종목·흐름마다 recording: subscribed·pending·error·off, start·end: 저장한
        첫 분의 시작과 마지막 분의 끝(KST, 받은 시각 기준), rows: 저장한 행 수, gaps: 빠진 구간의
        total(전체 수)과 recent(가장 최근 20개의 start·end·reason, end가 "진행 중"이면 아직
        이어지는 중, reason은 disconnect 끊김·subscribe_failed 구독 거절·server_stop 서버
        재시작·crash 서버 비정상 종료)). 지금 저장 중인 것이 먼저 오고 40개까지 싣는다
        (recorded_total이 전체 수). subscriptions(쓰는 구독 건수와 상한 20), connection,
        models(올린 모델 파일의 name·sha256·size(바이트)·uploaded_at), models_url(모델 파일을
        올리는 화면), bars.
        빈 구간의 틱은 다시 받을 수 없고 지어내지 않는다. 밤과 주말은 원래 저장하지 않는다.
        market은 "domestic"만 된다.
        다음: 모델 파일은 sha256을 backtest_strategy의 strategy.model에 넣는다. 저장을 켜거나
        바꾸려면 set_tick_recording. 체결·호가 백테스트는 backtest_strategy의 data.streams에
        {종목코드: ["tick", "book"]}을 넣고 data.start·end를 저장한 기간에 맞춘다.
        """
        user_id = ctx.user_id()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        return envelope.ok(service.data(user_id), ctx.meta())
