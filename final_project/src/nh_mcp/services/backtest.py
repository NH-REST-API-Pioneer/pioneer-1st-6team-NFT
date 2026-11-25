"""백테스트 유스케이스: 흐름마다 데이터를 모아 넣는 순서로 섞고, 격리 실행기에 보내고, 출력으로
성과를 계산해 남긴다.

실행은 둘 중 하나다(흐름으로 가른다).
- 봉 실행: 1분봉·일봉. NH에서 받아 판단 시각(봉이 끝난 시각) 순서로 넣는다.
- 틱 실행: 체결(tick)·호가(book), 그리고 일봉. 체결·호가는 그 사용자가 저장한 것(`ticks.db`)을 받은
  순서(seq) 그대로 넣고, 일봉은 그날이 끝난 뒤 처음 오는 이벤트 바로 앞에 넣는다. 1분봉은 같이
  넣지 않는다: 실시간에서 1분봉은 그 분이 끝나고 몇 초 뒤에 오고 그사이 다음 분의 틱이 먼저 와서,
  섞으면 백테스트와 실시간의 순서가 같을 수 없다. 빈 구간(끊긴 동안)은 지어내지 않고 결과에 적는다.

전략 코드는 이 프로세스에서 실행하거나 파싱하지 않는다. 해시하고 저장하고 실행기로 넘기기만 한다.
모델 파일도 열어 보지 않는다. 그 사용자의 파일 바이트를 읽어 실행기로 넘기기만 한다.
순서: 입력 크기 → 종목(국내 선물만) → 사용자별 자리 → 모델 파일 → 데이터 → 데이터 양 → 틱 크기 →
실행기 → 성과 → 기록.
성과는 실행기가 돌려준 출력으로 서버가 체결 계산을 다시 돌려 낸다. 실행기의 계산을 믿지 않는다.
틱 크기를 데이터보다 나중에 본다. 틱 표는 키가 없을 때 None을 돌려주므로, 먼저 보면 키 없음이
"틱 모름"으로 보이기 때문이다(봉 조회와 틱 실행의 키 확인이 KeyMissing을 낸다).
"""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from nh_mcp.domain import (
    KST,
    BarEvent,
    BookEvent,
    Clock,
    Contract,
    JsonValue,
    MarketEvent,
    ModelHash,
    SandboxFailure,
    SandboxJob,
    SandboxResult,
    Stream,
    Streams,
    SymbolTerms,
    TickEvent,
    UserId,
)
from nh_mcp.markets.domestic import CandleSeries, Interval
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.models.store import ModelStore
from nh_mcp.sandbox.client import SandboxBusy, SandboxClient, SandboxDown
from nh_mcp.sandbox.wire import stream_of
from nh_mcp.services.queries import KeyMissing, QueryService
from nh_mcp.services.recording import MAX_LISTED_GAPS, OPEN_GAP
from nh_mcp.store import backtests, nh_keys, ticks
from nh_mcp.store.backtests import NewRun, RunKey
from nh_mcp.strategies.backtest import (
    EventReport,
    Holding,
    PendingOrder,
    Trade,
    decision_time,
    simulate_events,
)

# 흐름(종목·간격) 하나에서 받는 봉 수. 넘으면 가장 최근 것만 받는다. 모의 1분봉의 촘촘한
# 구간(2026-09-07부터 53거래일, 약 2.2만 봉, Q26)이 한 번에 들어가는 크기다.
MAX_BARS = 25_000
# 봉 실행에서 실행기에 넣는 시장 이벤트 수.
MAX_EVENTS = 25_000
# 틱 실행에서 실행기에 넣는 시장 이벤트 수(체결·호가·일봉 합). 넘으면 가장 최근 것만 넣는다.
# 이 Mac의 실제 컨테이너에서 `return None` 전략이 이벤트 하나에 약 0.33ms라 90,000개가 약 30초로,
# 벽시계 70초의 절반(35초) 안에 든다. 체결만, 실제 섞임, 10단계 호가만의 세 경우 모두 그렇다
# (2026-11-25, 경우마다 3번). 나머지 절반은 전략 계산 몫이다. 리눅스 서버에서는 아직 재지 않았다.
MAX_TICK_EVENTS = 90_000
# 출력을 다음 이벤트에서 체결하므로 이벤트가 하나뿐이면 체결할 수 없다.
MIN_EVENTS = 2
MAX_SYMBOLS = 5
MAX_CODE_BYTES = 65_536
MAX_PARAMS_BYTES = 4096
# 한 사용자가 동시에 돌릴 수 있는 백테스트 수. 실행기 전체 한도(3개)는 실행기가 본다.
PER_USER = 2
MAX_POSITION = 100
# 응답과 기록에 싣는 끝난 거래 수(가장 최근 것들). 전체 수는 totals.trades에 있다.
MAX_LISTED_TRADES = 50
# 데이터가 모자라 실행기를 부르지 않은 기록의 상태.
DATA_SHORT = "data_short"
# 첫 봉이 요청 시작일보다 이만큼 넘게 늦어야 경고한다. 주말·연휴로 늦는 것은 경고하지 않는다.
LATE_START_GRACE = timedelta(days=5)

NOT_FUTURE = (
    "백테스트는 국내 선물 종목만 됩니다. search_symbol 결과에서 kind가 future인 종목을 고르세요."
)
NO_MODEL = (
    "그런 모델 파일이 없습니다. 사용자가 모델 파일 화면(/models)에 올린 파일의 SHA-256을 "
    "strategy.model에 넣어 주세요."
)
TICK_EXAMPLE = "strategy://examples/tick-momentum"
MIXED = (
    '"tick"·"book"과 "minute"(1분봉)은 한 백테스트에 같이 넣을 수 없습니다. 실시간에서는 1분봉이 '
    "그 분이 끝나고 몇 초 뒤에 오고 그사이 다음 분의 체결이 먼저 와서, 섞으면 실시간과 같은 순서로 "
    f"넣을 수 없기 때문입니다. 1분봉이 필요하면 전략 안에서 체결로 만드세요(예제 {TICK_EXAMPLE}). "
    '"day"(일봉)는 같이 넣을 수 있습니다(그날이 끝난 뒤 다음 거래일 첫 이벤트 앞에 들어갑니다).'
)
NO_RECORDING = (
    "기간 안에 저장한 데이터가 없는 흐름이 있습니다: {names}. 체결·호가는 set_tick_recording으로 "
    "저장을 켠 뒤에 받은 것만 쌓입니다(NH는 지난 체결·호가를 주지 않습니다). get_strategy_data로 "
    "저장한 기간을 보고 기간을 맞추거나, 먼저 저장을 켜 주세요."
)

# 봉 실행에서 받는 흐름 → NH 차트 간격.
BAR_STREAMS: dict[Stream, Interval] = {"minute": "minute", "day": "day"}
# 틱 실행의 흐름. 그 사용자가 저장한 것에서 읽는다.
TICK_STREAMS: frozenset[Stream] = frozenset({"tick", "book"})
STREAM_NAMES: dict[Stream, str] = {
    "day": "일봉",
    "minute": "1분봉",
    "tick": "체결(tick)",
    "book": "호가(book)",
}
# 판단 시각이 같으면 일봉을 먼저 넣는다.
_INTERVAL_ORDER: dict[Interval, int] = {"day": 0, "minute": 1}

# 자원 제한 사유 → 사용자 말. 값은 docs/safety.md의 격리 제한과 같다.
_LIMIT_HIT: dict[SandboxFailure, str] = {
    "cpu": "전략 코드가 CPU 시간 제한(60초)을 넘었습니다.",
    "memory": "전략 코드가 메모리 제한(512MB)을 넘었습니다.",
    "processes": "전략 코드가 프로세스·스레드 수 제한을 넘었습니다.",
    "output": "전략 코드가 출력 제한(한 줄 64KB, 합계는 이벤트당 256바이트·최소 1MB)을 넘었습니다.",
    "timeout": "전략 실행이 시간 제한(70초)을 넘었습니다.",
}


class StrategyInvalid(Exception):
    """입력이나 코드·출력 형식이 맞지 않거나, 데이터·종목 정보가 모자라 돌릴 수 없다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StrategyFailed(Exception):
    """코드가 예외를 냈거나 자원 제한을 넘었다. 결과는 없다."""

    def __init__(self, reason: SandboxFailure, message: str) -> None:
        super().__init__(message)
        self.reason: SandboxFailure = reason
        self.message = message


class RunnerUnavailable(Exception):
    """실행기가 바쁘거나 답하지 않거나, 이 사용자가 이미 동시 한도만큼 돌리고 있다."""


@dataclass(frozen=True)
class BacktestRequest:
    code: str
    params: dict[str, JsonValue]
    streams: Streams
    period: tuple[date, date]
    max_position: int
    fee_per_fill: Decimal
    # 사용자가 올린 모델 파일의 해시. 없으면 모델 없이 돈다.
    model: ModelHash | None = None


# 흐름 하나: (종목코드, 흐름).
type StreamId = tuple[str, Stream]
type Recorded = TickEvent | BookEvent


@dataclass(frozen=True)
class Coverage:
    """흐름 하나에서 전략에 넣은 이벤트 수와 첫·끝 시각(봉은 시작 시각). truncated면 상한 때문에
    앞쪽을 버렸거나 NH에서 기간 앞쪽을 다 받지 못했다. gaps는 기간과 겹치는 빈 구간(체결·호가만,
    오래된 것부터)이다."""

    events: int
    first_at: datetime | None
    last_at: datetime | None
    truncated: bool
    gaps: tuple[ticks.Gap, ...] = ()


def event_at(event: MarketEvent) -> datetime:
    """이벤트의 시각. 봉은 시작한 시각이다."""
    match event:
        case BarEvent(bar=bar):
            return bar.at
        case TickEvent(at=at) | BookEvent(at=at):
            return at


def _names(ids: Iterable[StreamId]) -> str:
    return ", ".join(f"{symbol} {STREAM_NAMES[stream]}" for symbol, stream in ids)


@dataclass(frozen=True)
class Feed:
    """전략에 넣을 시장 이벤트(넣는 순서대로)와 흐름마다 넣은 범위."""

    events: list[MarketEvent]
    coverage: dict[StreamId, Coverage]
    warnings: list[str]

    @property
    def truncated(self) -> bool:
        return any(c.truncated for c in self.coverage.values())

    def span(self) -> tuple[datetime, datetime] | None:
        """넣은 이벤트의 가장 이른 시각과 가장 늦은 시각(봉은 시작 시각). 없으면 None."""
        if not self.events:
            return None
        starts = [event_at(e) for e in self.events]
        return min(starts), max(starts)

    def shortage(self) -> str | None:
        """돌릴 만큼 데이터가 있으면 None, 아니면 알려 줄 문장.

        상한에 잘려 넣지 못한 흐름은 모자란 것으로 보지 않는다(경고에 적혀 있다). 틱 실행의 일봉은
        그날 뒤에 이벤트가 있어야 들어가므로 비어도 막지 않는다(경고에 적혀 있다).
        """
        empty: list[StreamId] = [
            sid for sid, c in sorted(self.coverage.items()) if c.events == 0 and not c.truncated
        ]
        unrecorded = [sid for sid in empty if sid[1] in TICK_STREAMS]
        if unrecorded:
            return NO_RECORDING.format(names=_names(unrecorded))
        ticking = any(stream in TICK_STREAMS for _, stream in self.coverage)
        no_bars = [] if ticking else empty
        if no_bars:
            return (
                "기간 안에 봉이 하나도 없는 흐름이 있어 데이터가 모자랍니다: "
                + _names(no_bars)
                + ". 기간을 바꾸거나 그 흐름을 빼 주세요."
            )
        if len(self.events) < MIN_EVENTS:
            return (
                f"기간 안 이벤트가 {len(self.events)}개라 데이터가 모자랍니다. "
                f"{MIN_EVENTS}개 이상 있게 기간을 넓혀 주세요."
            )
        return None


@dataclass(frozen=True)
class BacktestOutcome:
    report: EventReport
    code_hash: str
    feed: Feed


def code_hash(code: str) -> str:
    """코드 UTF-8 바이트의 SHA-256. 공백 하나도 정규화하지 않는다."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _canonical(value: JsonValue) -> str:
    """키를 정렬하고 공백 없이 쓴 JSON. 파라미터·흐름·설정·결과를 같은 모양으로 저장한다."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def params_key(params: dict[str, JsonValue]) -> str:
    """파라미터를 비교·저장하는 문자열. `preview_strategy`도 같은 함수로 비교한다."""
    return _canonical(params)


def streams_data(streams: Streams) -> dict[str, JsonValue]:
    """흐름을 JSON 값으로. 종목코드 순, 종목마다 흐름 이름 순이다."""
    return {symbol: [*sorted(names)] for symbol, names in sorted(streams.items())}


def streams_key(streams: Streams) -> str:
    """흐름을 비교·저장하는 문자열. 종목이나 흐름을 적은 순서와 상관없이 같다."""
    return _canonical(streams_data(streams))


def run_key(
    code: str, params: dict[str, JsonValue], streams: Streams, model: ModelHash | None
) -> RunKey:
    """같은 백테스트인지 가르는 값. `preview_strategy`도 이 함수로 맞춘다. 모델이 없으면 None."""
    return RunKey("domestic", streams_key(streams), code_hash(code), params_key(params), model)


def check_strategy(code: str, params: dict[str, JsonValue]) -> str | None:
    """코드와 파라미터의 크기가 맞으면 None, 아니면 알려 줄 문장."""
    if not code.strip():
        return "strategy.code에 on_event(event, ctx) 함수를 담은 코드를 넣어 주세요."
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return f"strategy.code는 UTF-8로 {MAX_CODE_BYTES:,}바이트까지입니다."
    try:
        size = len(params_key(params).encode("utf-8"))
    except RecursionError:
        size = MAX_PARAMS_BYTES + 1  # 너무 깊게 중첩돼 직렬화할 수 없다.
    if size > MAX_PARAMS_BYTES:
        return f"strategy.params는 JSON으로 {MAX_PARAMS_BYTES:,}바이트까지입니다."
    return None


def check_streams(streams: Streams) -> str | None:
    """흐름이 이번에 돌릴 수 있는 모양이면 None, 아니면 알려 줄 문장. 종목이 있는지는 따로 본다."""
    if not streams:
        return 'streams에 종목코드와 흐름을 넣어 주세요. 예: {"A016C000": ["minute"]}'
    if len(streams) > MAX_SYMBOLS:
        return f"streams에는 종목을 {MAX_SYMBOLS}개까지 넣을 수 있습니다."
    if any(not names for names in streams.values()):
        return 'streams의 종목마다 흐름을 하나 이상 넣어 주세요. 예: ["minute"]'
    if tick_run(streams) and any("minute" in names for names in streams.values()):
        return MIXED
    return None


def tick_run(streams: Streams) -> bool:
    """체결·호가 흐름이 하나라도 있으면 틱 실행이다. 아니면 봉 실행이다."""
    return any(names & TICK_STREAMS for names in streams.values())


def check_settings(max_position: int, fee_per_fill: Decimal) -> str | None:
    """체결 조건이 맞으면 None. 체결 계산은 값을 검사하지 않으므로 여기서 막는다."""
    if type(max_position) is not int or not 1 <= max_position <= MAX_POSITION:
        return f"settings.max_position은 1 이상 {MAX_POSITION} 이하의 정수(계약 수)여야 합니다."
    if not fee_per_fill.is_finite() or fee_per_fill < 0:
        return 'settings.fee_per_fill은 0 이상의 금액을 숫자만으로 써 주세요. 예: "1500"'
    return None


async def futures(queries: QueryService, streams: Streams) -> list[Contract]:
    """흐름의 종목을 종목 마스터에서 찾는다(종목코드 순).

    없으면 UnknownSymbol(모르면 막는다), 국내 선물이 아니면 StrategyInvalid.
    """
    contracts: list[Contract] = []
    for symbol in sorted(streams):
        contract = await queries.contract(symbol)
        if contract.kind != "future":
            raise StrategyInvalid(f"{NOT_FUTURE} (선물이 아닌 종목: {symbol})")
        contracts.append(contract)
    return contracts


def feed_order(event: BarEvent) -> tuple[datetime, int, str]:
    """넣는 순서: 판단 시각(봉이 끝난 시각) → 일봉 먼저 → 종목코드.

    일봉은 다음 날 0시에 끝나므로 그날 1분봉이 다 들어간 뒤에 들어간다. 미래를 보지 않는다.
    """
    return decision_time(event), _INTERVAL_ORDER[event.interval], event.symbol


def _times(events: Iterable[MarketEvent]) -> dict[StreamId, list[datetime]]:
    """흐름마다 넣은 이벤트의 시각(넣은 순서)."""
    times: dict[StreamId, list[datetime]] = {}
    for event in events:
        times.setdefault((event.symbol, stream_of(event)), []).append(event_at(event))
    return times


def _covered(fed: list[datetime], truncated: bool, gaps: tuple[ticks.Gap, ...] = ()) -> Coverage:
    return Coverage(len(fed), fed[0] if fed else None, fed[-1] if fed else None, truncated, gaps)


def _fetch_note(name: str, series: CandleSeries) -> str | None:
    """NH에서 받은 봉이 상한에 닿았거나 기간 앞쪽을 끝까지 받지 못했으면 알릴 문장."""
    bars = series.candles
    if len(bars) >= MAX_BARS:
        return f"{name}: 기간 안 봉이 많아 가장 최근 {MAX_BARS:,}개만 받았습니다."
    if not series.complete:
        got = bars[0].at.astimezone(KST).date().isoformat() if bars else "없음"
        return (
            f"{name}: NH에서 기간 앞쪽을 끝까지 받지 못했습니다(받은 첫 봉 {got}). "
            "그 앞은 넣지 않았습니다."
        )
    return None


def build_feed(fetched: Mapping[tuple[str, Interval], CandleSeries], start: date) -> Feed:
    """봉 실행: 받은 봉을 넣는 순서로 섞고, 모두 합쳐 MAX_EVENTS개를 넘으면 가장 최근 것만 남긴다.

    NH에서 기간 앞쪽을 다 받지 못한 흐름은 truncated로 두고 경고한다. 봉을 지어내지 않는다.
    """
    every = sorted(
        (
            BarEvent(symbol, interval, c)
            for (symbol, interval), series in fetched.items()
            for c in series.candles
        ),
        key=feed_order,
    )
    events = list[MarketEvent](every[-MAX_EVENTS:])
    warnings: list[str] = []
    if len(every) > MAX_EVENTS:
        warnings.append(
            f"모든 흐름의 봉이 {len(every):,}개라 가장 최근 {MAX_EVENTS:,}개만 넣었습니다."
        )
    times = _times(events)
    coverage: dict[StreamId, Coverage] = {}
    for (symbol, interval), series in sorted(fetched.items()):
        fed = times.get((symbol, interval), [])
        name = f"{symbol} {STREAM_NAMES[interval]}"
        note = _fetch_note(name, series)
        truncated = note is not None or len(fed) < len(series.candles)
        if note is not None:
            warnings.append(note)
        elif not truncated and fed and fed[0].astimezone(KST).date() > start + LATE_START_GRACE:
            day = fed[0].astimezone(KST).date().isoformat()
            warnings.append(
                f"{name}: 요청한 시작일보다 늦은 날부터 데이터가 있습니다(첫 봉 {day})."
            )
        coverage[(symbol, interval)] = _covered(fed, truncated)
    return Feed(events, coverage, warnings)


def _levels(levels: tuple[ticks.Level, ...]) -> tuple[tuple[Decimal, int], ...]:
    """최우선부터 잔량이 있는 단계까지. NH는 빈 단계를 가격·잔량 0으로 준다(모의는 5단계만 찬다)."""
    out: list[tuple[Decimal, int]] = []
    for level in levels:
        if level.price is None or not level.qty:
            break
        out.append((level.price, level.qty))
    return tuple(out)


def recorded_events(rows: Iterable[tuple[ticks.Sub, ticks.Trade | ticks.Book]]) -> list[Recorded]:
    """저장한 행을 받은 순서 그대로 이벤트로 바꾼다.

    at은 거래소 시각(초)에 받은 날짜를 붙인 것이다. 앞 이벤트보다 이르면 앞 이벤트의 시각으로 둔다
    (종목·흐름마다 거래소 시각이 어긋나 받은 순서와 다를 수 있다). 그래서 at은 받은 순서대로 뒤로
    가지 않고, 판단한 뒤 처음 받은 그 종목의 체결에서 체결된다.
    """
    out: list[Recorded] = []
    latest: datetime | None = None
    for sub, row in rows:
        at = row.at if latest is None or row.at > latest else latest
        latest = at
        match row:
            case ticks.Trade(price=price, qty=qty, bid=bid, ask=ask):
                out.append(TickEvent(sub.symbol, at, price, qty, bid, ask))
            case ticks.Book(bids=bids, asks=asks):
                out.append(BookEvent(sub.symbol, at, _levels(bids), _levels(asks)))
    return out


def place_days(recorded: list[Recorded], days: Mapping[str, CandleSeries]) -> list[MarketEvent]:
    """일봉을 그날이 끝난 뒤 처음 오는 체결·호가 바로 앞에 넣는다(같은 날이면 종목코드 순).

    그날 안에는 넣지 않는다(그날 일봉은 그날이 끝나야 안다). 그 뒤에 이벤트가 없으면 넣지 않는다.
    """
    waiting = sorted(
        (BarEvent(symbol, "day", c) for symbol, series in days.items() for c in series.candles),
        key=lambda e: (e.bar.at, e.symbol),
    )
    out: list[MarketEvent] = []
    i = 0
    for event in recorded:
        day = event.at.astimezone(KST).date()
        while i < len(waiting) and waiting[i].bar.at.astimezone(KST).date() < day:
            out.append(waiting[i])
            i += 1
        out.append(event)
    return out


@dataclass(frozen=True)
class Recording:
    """틱 실행에 쓸, 그 사용자가 저장한 것. events는 받은 순서이고 가장 최근 MAX_TICK_EVENTS개까지,
    totals는 흐름마다 기간 안 전체 행 수, gaps는 흐름마다 기간과 겹치는 빈 구간이다."""

    events: list[Recorded]
    totals: Mapping[StreamId, int]
    gaps: Mapping[StreamId, tuple[ticks.Gap, ...]]


def build_tick_feed(recording: Recording, days: Mapping[str, CandleSeries]) -> Feed:
    """틱 실행: 저장한 체결·호가 사이에 일봉을 끼우고, 모두 합쳐 MAX_TICK_EVENTS개를 넘으면 가장
    최근 것만 남긴다. 빈 구간은 채우지 않고 흐름마다 적고 경고한다."""
    merged = place_days(recording.events, days)
    events = merged[-MAX_TICK_EVENTS:]
    times = _times(events)
    warnings: list[str] = []
    total = sum(recording.totals.values())
    fed_recorded = sum(len(times.get(sid, [])) for sid in recording.totals)
    if fed_recorded < total or len(events) < len(merged):
        warnings.append(
            f"기간 안에 저장한 체결·호가가 {total:,}개라 일봉을 합쳐 가장 최근 {len(events):,}개만 "
            f"넣었습니다(틱 실행은 한 번에 {MAX_TICK_EVENTS:,}개까지)."
        )
    coverage: dict[StreamId, Coverage] = {}
    for sid, count in sorted(recording.totals.items()):
        fed, gaps = times.get(sid, []), recording.gaps.get(sid, ())
        coverage[sid] = _covered(fed, len(fed) < count, gaps)
        if gaps:
            warnings.append(
                f"{_names([sid])}: 기간 안에 빈 구간이 {len(gaps)}개 있습니다. 끊긴 동안의 "
                "체결·호가는 없고 지어내지 않았습니다(coverage의 gaps)."
            )
    placed = _times(merged)
    for symbol, series in sorted(days.items()):
        name = f"{symbol} 일봉"
        fed, fits = times.get((symbol, "day"), []), placed.get((symbol, "day"), [])
        note = _fetch_note(name, series)
        if note is not None:
            warnings.append(note)
        if not fits:
            warnings.append(
                f"{name}: 넣은 일봉이 없습니다. 일봉은 그날이 끝난 뒤 처음 오는 체결·호가 앞에 "
                "들어가는데, 기간 안에 그런 이벤트가 없었습니다."
            )
        coverage[(symbol, "day")] = _covered(fed, note is not None or len(fed) < len(fits))
    return Feed(events, coverage, warnings)


def _stamp(at: datetime) -> str:
    return at.astimezone(KST).isoformat(timespec="seconds")


def _amount(value: Decimal) -> str:
    """가격·금액 문자열. 음의 0("-0.00")은 0으로 쓴다."""
    return str(abs(value) if value.is_zero() else value)


def _trade(t: Trade) -> dict[str, JsonValue]:
    return {
        "symbol": t.symbol,
        "side": t.side,
        "quantity": t.quantity,
        "entry_at": _stamp(t.entry_at),
        "entry_price": _amount(t.entry_price),
        "exit_at": _stamp(t.exit_at),
        "exit_price": _amount(t.exit_price),
        "pnl": _amount(t.pnl),
    }


def _holding(h: Holding) -> dict[str, JsonValue]:
    return {
        "symbol": h.symbol,
        "side": h.side,
        "quantity": h.quantity,
        "entry_at": _stamp(h.entry_at),
        "entry_price": _amount(h.entry_price),
        "mark_price": _amount(h.mark_price),
        "unrealized_pnl": _amount(h.unrealized_pnl),
        "realized_pnl": _amount(h.realized_pnl),
        "fees": _amount(h.fees),
    }


def _pending(p: PendingOrder) -> dict[str, JsonValue]:
    return {
        "symbol": p.symbol,
        "position": p.target,
        "price": None if p.price is None else _amount(p.price),
        "decided_at": _stamp(p.decided_at),
    }


def report_data(report: EventReport) -> dict[str, JsonValue]:
    """성과 지표. 금액은 문자열이다. 도구 응답과 저장하는 결과 JSON이 같이 쓴다.

    net_pnl은 끝난 거래만의 합(수수료를 뺀 값)이다. 끝까지 남은 포지션은 open_positions에 따로
    적고, 그 수수료는 fees에만 들어 있다. pending_orders는 마지막까지 체결되지 않은 주문이다.
    """
    totals: dict[str, JsonValue] = {
        "trades": len(report.trades),
        "fills": report.fills,
        "win_rate": None if report.win_rate is None else str(report.win_rate),
        "net_pnl": _amount(report.net_pnl),
        "max_drawdown": _amount(report.max_drawdown),
        "fees": _amount(report.fees),
    }
    symbols: dict[str, JsonValue] = {
        s.symbol: {
            "trades": s.trades,
            "fills": s.fills,
            "net_pnl": _amount(s.net_pnl),
            "fees": _amount(s.fees),
        }
        for s in report.symbols
    }
    return {
        "totals": totals,
        "symbols": symbols,
        "trades": [_trade(t) for t in report.trades[-MAX_LISTED_TRADES:]],
        "open_positions": [_holding(h) for h in report.open_positions],
        "pending_orders": [_pending(p) for p in report.pending],
    }


def _gaps(gaps: tuple[ticks.Gap, ...]) -> dict[str, JsonValue]:
    """빈 구간 수와 가장 최근 것들. 끝나지 않은 구간의 end는 get_strategy_data와 같은 말이다."""
    recent: list[JsonValue] = [
        {
            "start": _stamp(g.started_at),
            "end": OPEN_GAP if g.ended_at is None else _stamp(g.ended_at),
            "reason": g.reason,
        }
        for g in gaps[-MAX_LISTED_GAPS:]
    ]
    return {"total": len(gaps), "recent": recent}


def coverage_data(feed: Feed) -> dict[str, JsonValue]:
    """흐름마다 넣은 범위: {종목: {흐름: ...}}.

    봉은 {first_bar_at, last_bar_at, bars, truncated}(봉 시작 시각), 체결·호가는
    {first_event_at, last_event_at, events, truncated, gaps: {total, recent}}다.
    """
    out: dict[str, dict[str, JsonValue]] = {}
    for (symbol, stream), c in sorted(feed.coverage.items()):
        first = None if c.first_at is None else _stamp(c.first_at)
        last = None if c.last_at is None else _stamp(c.last_at)
        out.setdefault(symbol, {})[stream] = (
            {
                "first_event_at": first,
                "last_event_at": last,
                "events": c.events,
                "truncated": c.truncated,
                "gaps": _gaps(c.gaps),
            }
            if stream in TICK_STREAMS
            else {
                "first_bar_at": first,
                "last_bar_at": last,
                "bars": c.events,
                "truncated": c.truncated,
            }
        )
    return dict[str, JsonValue](out)


def _event_note(result: SandboxResult) -> str:
    return "" if result.index is None else f" ({result.index + 1}번째 이벤트에서)"


def _strategy_said(detail: str) -> str:
    """전략 코드가 낸 내용을 그렇다고 표시해 붙인다. 없으면 붙이지 않는다."""
    return f" 전략 코드가 낸 내용: {detail}" if detail else ""


def _failure(result: SandboxResult) -> Exception:
    """실패한 실행 결과를 서비스 예외로. detail은 전략 코드가 낸 것이라 그렇다고 적는다.

    invalid_code·invalid_output의 detail도 전략 코드가 만들 수 있다(코드 예외 문구, 돌려준 값,
    프로토콜 출구에 쓴 거짓 줄). 서버가 쓴 문장에는 표시를 붙이지 않는다.
    """
    note = _event_note(result)
    match result.status:
        case "ok":
            raise ValueError("성공한 결과는 실패로 바꿀 수 없다")
        case "invalid_code":
            return StrategyInvalid(
                "전략 코드를 불러오지 못했습니다. on_event(event, ctx) 함수가 있어야 하고, init을 "
                "두려면 init(ctx)여야 합니다. 문법 오류나 맨 위 코드·init에서 난 오류도 여기에 "
                "듭니다." + _strategy_said(result.detail)
            )
        case "invalid_output":
            return StrategyInvalid(
                f"전략 코드의 출력이 계약에 맞지 않습니다{note}. 출력은 None 또는 "
                '{종목코드: {"position": 정수, "price": "지정가"(생략하면 시장가)}}이고, 종목은 '
                "streams에 넣은 것만, |position|은 settings.max_position 이하, price는 그 종목 "
                "틱 단위의 소수 문자열입니다." + _strategy_said(result.detail)
            )
        case "exception":
            return StrategyFailed("exception", f"전략 코드가 낸 오류: {result.detail}{note}")
        case "cpu" | "memory" | "processes" | "output" | "timeout" as reason:
            return StrategyFailed(reason, _LIMIT_HIT[reason] + note)


@dataclass(frozen=True)
class _Run:
    """실행 한 건의 기록 재료."""

    user_id: UserId
    request: BacktestRequest
    key: RunKey
    feed: Feed


@dataclass(frozen=True)
class Sources:
    """실행기에 넣을 것을 가져오는 곳: NH 봉·종목, 종목 틱 표, 사용자가 올린 모델 파일, 저장한
    체결·호가(ticks.db는 수신기가 켜질 때 열리므로 쓸 때 꺼낸다)."""

    queries: QueryService
    products: DomesticProducts
    models: ModelStore
    tick_store: Callable[[], ticks.TickStore]


# 저장소의 흐름 이름(체결·호가만).
_RECORDED: dict[Stream, ticks.RecordStream] = {"tick": "tick", "book": "book"}


def kst_days(period: tuple[date, date]) -> tuple[datetime, datetime]:
    """기간(KST 날짜, 끝 날 포함)을 [시작 날 0시, 끝 다음 날 0시) 시각으로."""
    first, last = period
    after = last + timedelta(days=1)
    return datetime.combine(first, time(), KST), datetime.combine(after, time(), KST)


def read_recording(
    store: ticks.TickStore, user_id: UserId, streams: Streams, period: tuple[date, date]
) -> Recording:
    """이 사용자가 기간 안에 받은 체결·호가(가장 최근 MAX_TICK_EVENTS개)와 빈 구간. 다른 사용자의
    것은 저장소가 사용자 식별자로 가른다(안전 불변식 8)."""
    start, end = kst_days(period)
    subs = [
        ticks.Sub(symbol, _RECORDED[stream])
        for symbol, names in sorted(streams.items())
        for stream in sorted(names)
        if stream in _RECORDED
    ]
    window = store.latest(user_id, subs, start, end, MAX_TICK_EVENTS)
    events = recorded_events(window.rows)
    gaps: dict[StreamId, tuple[ticks.Gap, ...]] = {}
    for sub in subs:
        gaps[(sub.symbol, sub.stream)] = tuple(
            g
            for g in store.gaps(user_id, sub.symbol)
            if g.sub == sub and g.started_at < end and (g.ended_at is None or g.ended_at > start)
        )
    totals: dict[StreamId, int] = {(s.symbol, s.stream): n for s, n in window.totals.items()}
    return Recording(events, totals, gaps)


class BacktestService:
    def __init__(
        self, conn: sqlite3.Connection, clock: Clock, sources: Sources, sandbox: SandboxClient
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._queries = sources.queries
        self._products = sources.products
        self._models = sources.models
        self._tick_store = sources.tick_store
        self._sandbox = sandbox
        self._running: dict[UserId, int] = {}
        # 호출이 취소돼도 도는 실행. 끝날 때까지 참조를 붙들어 가비지 수집을 막는다.
        self._tasks: set[asyncio.Task[EventReport]] = set()

    def running(self, user_id: UserId) -> int:
        """이 사용자가 지금 돌리고 있는 백테스트 수."""
        return self._running.get(user_id, 0)

    async def run(self, user_id: UserId, request: BacktestRequest) -> BacktestOutcome:
        problem = (
            check_strategy(request.code, request.params)
            or check_streams(request.streams)
            or check_settings(request.max_position, request.fee_per_fill)
        )
        if problem is not None:
            raise StrategyInvalid(problem)
        contracts = await futures(self._queries, request.streams)
        self._take(user_id)
        handed_off = False
        try:
            model = await self._model(user_id, request.model)
            feed = await self._feed(user_id, request)
            key = run_key(request.code, request.params, request.streams, request.model)
            run = _Run(user_id, request, key, feed)
            short = feed.shortage()
            if short is not None:
                self._save(run, DATA_SHORT, None)
                raise StrategyInvalid(short)
            job = SandboxJob(
                code=request.code,
                params=request.params,
                streams=request.streams,
                max_position=request.max_position,
                terms=await self._terms(user_id, contracts, request.fee_per_fill),
                events=list[MarketEvent](feed.events),
                model=model,
            )
            # 실행기 호출부터 기록까지는 따로 띄운다. 도구 호출이 취소돼도 끝까지 돌고 남긴다.
            task = asyncio.create_task(self._execute(run, job))
            handed_off = True
        finally:
            if not handed_off:
                self._release(user_id)
        self._tasks.add(task)
        task.add_done_callback(self._settle)
        report = await asyncio.shield(task)
        return BacktestOutcome(report=report, code_hash=key.code_hash, feed=feed)

    async def _model(self, user_id: UserId, sha256: ModelHash | None) -> bytes | None:
        """이 사용자가 올린 모델 파일의 바이트. 없거나 남의 해시면 막는다(모르면 막는다)."""
        if sha256 is None:
            return None
        model = await self._models.read(user_id, sha256)
        if model is None:
            raise StrategyInvalid(NO_MODEL)
        return model

    async def _feed(self, user_id: UserId, request: BacktestRequest) -> Feed:
        """봉 실행은 NH 봉, 틱 실행은 저장한 체결·호가와 NH 일봉."""
        if not tick_run(request.streams):
            return build_feed(await self._bars(user_id, request), request.period[0])
        # 체결·호가는 키 없이도 읽히지만 틱 크기는 키로 받는다. 모르는 채로 돌리지 않는다.
        if "domestic" not in nh_keys.registered_markets(self._conn, user_id):
            raise KeyMissing("domestic")
        # 묶음을 풀고 이벤트로 바꾸는 일은 이벤트 루프 밖에서 한다(최대 MAX_TICK_EVENTS행).
        recording = await asyncio.to_thread(
            read_recording, self._tick_store(), user_id, request.streams, request.period
        )
        days = {
            symbol: series for (symbol, _), series in (await self._bars(user_id, request)).items()
        }
        return build_tick_feed(recording, days)

    async def _bars(
        self, user_id: UserId, request: BacktestRequest
    ) -> dict[tuple[str, Interval], CandleSeries]:
        """봉 흐름마다 봉을 받는다(종목코드 순, 간격 순). 흐름마다 가장 최근 MAX_BARS개까지."""
        fetched: dict[tuple[str, Interval], CandleSeries] = {}
        for symbol, names in sorted(request.streams.items()):
            for name in sorted(names):
                interval = BAR_STREAMS.get(name)
                if interval is None:
                    continue
                fetched[(symbol, interval)] = await self._queries.candle_series(
                    user_id, symbol, interval, request.period, MAX_BARS
                )
        return fetched

    async def _terms(
        self, user_id: UserId, contracts: list[Contract], fee_per_fill: Decimal
    ) -> dict[str, SymbolTerms]:
        """종목마다 체결 조건: 종목 정보의 틱과 승수, 요청한 수수료. 틱을 모르면 막는다."""
        terms: dict[str, SymbolTerms] = {}
        for contract in contracts:
            rules = await self._products.rules_for(user_id, contract)
            if rules is None:
                raise StrategyInvalid(
                    f"{contract.symbol} 종목의 틱 크기를 확인하지 못해 백테스트할 수 없습니다."
                )
            terms[contract.symbol] = SymbolTerms(rules.tick_size, contract.multiplier, fee_per_fill)
        return terms

    def _take(self, user_id: UserId) -> None:
        if self.running(user_id) >= PER_USER:
            raise RunnerUnavailable()
        self._running[user_id] = self.running(user_id) + 1

    def _release(self, user_id: UserId) -> None:
        left = self.running(user_id) - 1
        if left > 0:
            self._running[user_id] = left
        else:
            self._running.pop(user_id, None)

    def _settle(self, task: asyncio.Task[EventReport]) -> None:
        """끝난 실행을 놓아준다. 아무도 기다리지 않던 실행의 예외도 여기서 거둔다."""
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _execute(self, run: _Run, job: SandboxJob) -> EventReport:
        try:
            try:
                result = await self._sandbox.run(job)
            except (SandboxBusy, SandboxDown):
                # 코드가 돌지 않았으므로 기록하지 않는다.
                raise RunnerUnavailable() from None
            if result.status != "ok":
                self._save(run, result.status, None)
                raise _failure(result)
            try:
                report = simulate_events(job.events, result.outputs, job.terms)
            except ValueError:
                # 출력 줄 수가 넣은 이벤트와 맞지 않는다. 실행기의 답을 못 믿으니 기록하지 않는다.
                raise RunnerUnavailable() from None
            self._save(run, backtests.SUCCESS, _canonical(report_data(report)))
            return report
        finally:
            self._release(run.user_id)

    def _save(self, run: _Run, status: str, result: str | None) -> None:
        request = run.request
        settings: dict[str, JsonValue] = {
            "max_position": request.max_position,
            "fee_per_fill": str(request.fee_per_fill),
        }
        span = run.feed.span()
        backtests.record(
            self._conn,
            NewRun(
                user_id=run.user_id,
                key=run.key,
                code=request.code,
                settings=_canonical(settings),
                requested=request.period,
                first_bar_at=None if span is None else span[0],
                last_bar_at=None if span is None else span[1],
                bar_count=len(run.feed.events),
                status=status,
                result=result,
            ),
            self._clock(),
        )
